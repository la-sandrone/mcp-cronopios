# /// script
# requires-python = ">=3.12"
# dependencies = ["mpmath>=1.3.0"]
# ///

"""
文件名: worker.py
摘要: mcp-calculator 的求值子进程。AST 白名单 + mpmath 任意精度数值求值。
      只允许数值计算与变量带入，不含任何求解类函数
      （findroot/odefun/solve 一律不在白名单——解方程走 mcp_eqsolve.py）。
      以 python -I 隔离模式运行；--selftest 可离线自测。
依赖: mpmath
"""

import ast
import json
import resource
import sys

import mpmath

# ═══════════════════════════════════════════════
#  资源红线（进程内自限；父进程超时/隔离为第二层）
# ═══════════════════════════════════════════════

MAX_EXPR_LEN = 2000        # 表达式最大字符数（与父进程一致）
MAX_DPS = 10000            # 有效数字位数上限
DEFAULT_DPS = 50           # 默认有效数字位数
GUARD_DIGITS = 10          # guard digits：内部计算精度 = 显示精度 + 10
MAX_POW_EXP = 1000         # 幂指数绝对值上限（防 10**1000000 打爆内存）
MAX_ARG_VALUE = 10000      # factorial/binomial 等参数上限
MAX_RESULT_CHARS = 20000   # 结果文本长度上限
MAX_AST_DEPTH = 200        # AST 嵌套深度上限
MAX_CALL_ARGS = 10         # 单次函数调用参数个数上限
MEM_LIMIT = 1 << 30        # RLIMIT_AS = 1 GiB

# ═══════════════════════════════════════════════
#  函数白名单：只允许"求值"类，无"求解"类
# ═══════════════════════════════════════════════
#  设计原则（庙算结论）：
#   · 求值类（三角/特殊函数/积分/求和/矩阵）→ 允许
#   · 求解类（findroot 数值求根 / odefun 微分方程 / solve）→ 一律排除，
#     划给 mcp_eqsolve.py（符号求解）与未来扩展。
#   · 白名单在启动时动态校验存在性：mpmath 版本差异导致的缺失函数名
#     会进入 MISSING_FUNCTIONS，再由下方 wrapper 兼容层补上（不崩溃）。

FUNCTION_NAMES = [
    # 基础
    # 对数：log(x) 默认自然对数（底 e）；log(x, b) 为底 b；ln(x) 是 log(x) 的同义别名
    "abs", "sqrt", "cbrt", "exp", "log", "ln", "log10", "log2",
    "degrees", "radians", "floor", "ceil", "trunc", "nint", "sign",
    # 三角
    "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
    "sec", "csc", "cot",
    # 双曲
    "sinh", "cosh", "tanh", "asinh", "acosh", "atanh",
    # 特殊函数
    "gamma", "lgamma", "factorial", "binomial",
    "erf", "erfc", "erfinv", "erfcinv", "zeta", "lambertw", "polylog",
    "expint", "si", "ci", "fresnels", "fresnelc", "agm", "harmonic",
    # Bessel
    "besselj", "bessely", "besseli", "besselk",
    "j0", "j1", "y0", "y1", "hankel1", "hankel2",
    # 复数
    "conj", "re", "im", "arg", "phase", "polar", "rect",
    # 积分 / 求和 / 微分（数值方法，非求解）
    "quad", "nsum", "nprod", "diff",
    # 矩阵
    "matrix", "det", "inv", "norm", "dot", "eye", "zeros", "ones",
    "diag", "lu_solve", "eig", "eigvals", "transpose",
]

# 常量白名单（变量名查找顺序: vars → 函数 → 常量）
CONSTANT_NAMES = {
    "pi": mpmath.pi,
    "e": mpmath.e,
    "inf": mpmath.inf,
    "nan": mpmath.nan,
    "j": mpmath.j,
}

# 动态校验：存在性过滤 + 拼写警告
FUNCTIONS: dict = {}
MISSING_FUNCTIONS: list = []
for _name in FUNCTION_NAMES:
    if _name == "abs":
        # mpmath 无模块级 abs，映射 Python 内置（对 mpf/mpc 有效）
        FUNCTIONS["abs"] = abs
    elif hasattr(mpmath, _name):
        FUNCTIONS[_name] = getattr(mpmath, _name)
    else:
        MISSING_FUNCTIONS.append(_name)

# ── 缺失函数兼容层（mpmath 1.3.0 无这些名字 → 用已有功能组合）──
#  仅当原生缺失时包装；mpmath 升级到有原生实现后自动让位。


def _wrap_ln(x):
    """自然对数（底 e）——mpmath 无 'ln' 名，与 'log' 同义。"""
    return mpmath.log(x)


def _wrap_log2(x):
    return mpmath.log(x, 2)


def _wrap_trunc(x):
    return mpmath.mpf(int(x))


def _wrap_lgamma(x):
    # 对 x>0 正确；x=0/负整数 得 inf/nan（不崩溃）
    return mpmath.log(mpmath.gamma(x))


def _wrap_erfcinv(x):
    # erfc(x) = 1 - erf(x)，故 erfc 的反函数 = erfinv(1 - x)
    return mpmath.erfinv(1 - x)


def _wrap_y0(x):
    return mpmath.bessely(0, x)


def _wrap_y1(x):
    return mpmath.bessely(1, x)


def _wrap_inv(A):
    return A ** -1


def _wrap_dot(A, B):
    return A * B


def _wrap_eigvals(A):
    return mpmath.eig(A)[0]


def _wrap_transpose(A):
    return A.transpose()


_FUNCTION_WRAPPERS = {
    "ln": _wrap_ln,
    "log2": _wrap_log2,
    "trunc": _wrap_trunc,
    "lgamma": _wrap_lgamma,
    "erfcinv": _wrap_erfcinv,
    "y0": _wrap_y0,
    "y1": _wrap_y1,
    "inv": _wrap_inv,
    "dot": _wrap_dot,
    "eigvals": _wrap_eigvals,
    "transpose": _wrap_transpose,
}

for _wname, _wfn in _FUNCTION_WRAPPERS.items():
    if _wname not in FUNCTIONS:
        FUNCTIONS[_wname] = _wfn
        if _wname in MISSING_FUNCTIONS:
            MISSING_FUNCTIONS.remove(_wname)


# ═══════════════════════════════════════════════
#  错误模型（统一结构化，父进程格式化给 LLM 自愈）
# ═══════════════════════════════════════════════

class CalcError(Exception):
    def __init__(self, etype, message, expr="", line=0, col=0, hint=""):
        super().__init__(message)
        self.etype = etype
        self.message = message
        self.expr = expr
        self.line = line
        self.col = col
        self.hint = hint

    def to_dict(self):
        return {
            "type": self.etype,
            "message": self.message,
            "expr": self.expr,
            "line": self.line,
            "col": self.col,
            "hint": self.hint,
        }


# ═══════════════════════════════════════════════
#  数值常量转换
# ═══════════════════════════════════════════════
#  精度模型说明：
#  mpmath 内部是二进制尾数（man × 2^exp，位数由 mp.dps 决定），
#  与 IEEE double 同构但位数可调。因此：
#   · 0.1 这类十进制有限小数无法精确存储，误差 ~1e-dps；
#   · 显示层用 nstr(x, dps) 只显示 dps 位，配合 GUARD_DIGITS
#     （内部精度 dps+10）使尾噪声落在显示位数之外，输出干净。
#  float 常量经 repr() 还原十进制最短表示再转 mpf，避免
#  把 Python float 的 53 位 double 误差直接带入高精度计算。

def to_mpf(value):
    """数值常量 → mpf。"""
    if isinstance(value, bool):
        raise CalcError("TypeError", "不允许布尔常量")
    if isinstance(value, int):
        return mpmath.mpf(value)          # 任意精度整数无损转换
    if isinstance(value, float):
        return mpmath.mpf(repr(value))    # 0.1 → '0.1' → 避免 double 展开
    if isinstance(value, complex):
        return mpmath.mpc(
            mpmath.mpf(repr(value.real)),
            mpmath.mpf(repr(value.imag)),
        )
    raise CalcError("TypeError", f"不允许的常量类型: {type(value).__name__}")


# ═══════════════════════════════════════════════
#  AST 白名单求值器
# ═══════════════════════════════════════════════
#  允许节点: Constant(数值) / Name / BinOp(7种) /
#            UnaryOp(UAdd,USub) / Call / List / Tuple
#  顶层 ast.Expression 由 compute 在 parse_expr 后解包 .body。
#  其余节点（Attribute/Subscript/Import/Lambda/推导式/比较/布尔/字符串）
#  一律拒绝——攻击面只有数值运算与白名单函数调用。

BINOP_MAP = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a ** b,
    ast.Mod: lambda a, b: a % b,
    # mpf 无 __floordiv__；floor(a/b) 等价 Python 的 // 语义
    ast.FloorDiv: lambda a, b: mpmath.floor(a / b),
}


class Evaluator:
    def __init__(self, variables, dps):
        self.vars = variables          # name -> 已求值的 mpf/mpc/matrix/list
        self.dps = dps

    # ── 入口 ──
    def evaluate(self, node, depth=0):
        if depth > MAX_AST_DEPTH:
            raise CalcError("DepthError",
                            f"表达式嵌套深度超过 {MAX_AST_DEPTH}")

        if isinstance(node, ast.Constant):
            return to_mpf(node.value)

        if isinstance(node, ast.Name):
            return self.lookup_name(node)

        if isinstance(node, ast.BinOp):
            return self.eval_binop(node, depth)

        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, (ast.UAdd, ast.USub)):
                operand = self.evaluate(node.operand, depth + 1)
                return operand if isinstance(node.op, ast.UAdd) else -operand
            raise CalcError("UnsupportedOp", "仅支持一元正负号")

        if isinstance(node, ast.Call):
            return self.eval_call(node, depth)

        if isinstance(node, (ast.List, ast.Tuple)):
            items = [self.evaluate(e, depth + 1) for e in node.elts]
            return items if isinstance(node, ast.List) else tuple(items)

        raise CalcError(
            "UnsupportedSyntax",
            f"不支持的语法节点: {type(node).__name__}",
            hint="仅支持: 数字 / 变量 / 四则 / 幂 / 取模 / 白名单函数调用 / 列表",
        )

    # ── 名字查找: vars → 函数 → 常量 ──
    def lookup_name(self, node):
        name = node.id
        if name in self.vars:
            return self.vars[name]
        if name in FUNCTIONS:
            return FUNCTIONS[name]
        if name in CONSTANT_NAMES:
            return CONSTANT_NAMES[name]
        raise CalcError(
            "NameError",
            f"名字 '{name}' 未定义",
            line=node.lineno,
            col=node.col_offset + 1,
            hint=('变量请通过 vars 参数绑定（如 {"x": "2"}）；'
                  "函数仅限白名单，见工具描述。"),
        )

    # ── 二元运算 ──
    def eval_binop(self, node, depth):
        if type(node.op) not in BINOP_MAP:
            raise CalcError("UnsupportedOp",
                            f"不支持的运算符: {type(node.op).__name__}")

        # 幂运算静态红线：指数为常量时直接拦截超大指数
        if (isinstance(node.op, ast.Pow)
                and isinstance(node.right, ast.Constant)
                and isinstance(node.right.value, (int, float))
                and not isinstance(node.right.value, bool)
                and abs(node.right.value) > MAX_POW_EXP):
            raise CalcError(
                "PowerLimitError",
                f"幂指数 |{node.right.value}| 超过上限 {MAX_POW_EXP}",
                line=node.right.lineno,
                col=node.right.col_offset + 1,
                hint=f"幂指数绝对值不得超过 {MAX_POW_EXP}",
            )

        left = self.evaluate(node.left, depth + 1)
        right = self.evaluate(node.right, depth + 1)
        try:
            return BINOP_MAP[type(node.op)](left, right)
        except CalcError:
            raise
        except ZeroDivisionError:
            raise CalcError("ZeroDivisionError", "除数为零",
                            line=node.lineno, col=node.col_offset + 1)
        except (ValueError, TypeError, OverflowError) as e:
            raise CalcError(type(e).__name__, f"运算失败: {e}",
                            line=node.lineno, col=node.col_offset + 1)

    # ── 函数调用 ──
    def eval_call(self, node, depth):
        if not isinstance(node.func, ast.Name):
            raise CalcError("UnsupportedSyntax", "函数必须是白名单内具名函数")

        fname = node.func.id
        if fname not in FUNCTIONS:
            raise CalcError(
                "NameError",
                f"函数 '{fname}' 不在白名单",
                line=node.func.lineno,
                col=node.func.col_offset + 1,
                hint=("可用函数见工具描述；本工具不提供求解类函数"
                      "（解方程请用 mcp-eqsolve）。"),
            )

        n_args = len(node.args) + len(node.keywords)
        if n_args > MAX_CALL_ARGS:
            raise CalcError("ArgLimitError",
                            f"函数调用参数过多（>{MAX_CALL_ARGS} 个）")

        args = [self.evaluate(a, depth + 1) for a in node.args]
        kwargs = {kw.arg: self.evaluate(kw.value, depth + 1)
                  for kw in node.keywords if kw.arg}

        self.check_arg_limits(fname, args)

        try:
            return FUNCTIONS[fname](*args, **kwargs)
        except CalcError:
            raise
        except ZeroDivisionError:
            raise CalcError("ZeroDivisionError", f"{fname}: 除数为零",
                            line=node.func.lineno, col=node.func.col_offset + 1)
        except (ValueError, TypeError, OverflowError) as e:
            raise CalcError(type(e).__name__, f"{fname}: {e}",
                            line=node.func.lineno, col=node.func.col_offset + 1)

    # ── 参数红线（借鉴 calc-mcp 的参数上限哲学）──
    def check_arg_limits(self, fname, args):
        if fname in ("factorial", "binomial") and args:
            try:
                n_int = int(args[0])
            except (TypeError, ValueError, OverflowError):
                return
            if abs(n_int) > MAX_ARG_VALUE:
                raise CalcError(
                    "ArgLimitError",
                    f"{fname}({n_int}) 参数超过上限 {MAX_ARG_VALUE}",
                    hint=f"{fname} 的参数绝对值不得超过 {MAX_ARG_VALUE}",
                )


# ═══════════════════════════════════════════════
#  表达式解析与主计算
# ═══════════════════════════════════════════════

def parse_expr(text):
    try:
        return ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise CalcError(
            "SyntaxError",
            e.msg,
            expr=text,
            line=e.lineno or 0,
            col=e.offset or 0,
            hint=("语法错误——仅支持 Python 表达式子集："
                  "数字 / 变量 / 四则 / 幂 / 取模 / 白名单函数调用"),
        )
    except RecursionError:
        raise CalcError("DepthError", "表达式嵌套过深，无法解析")


def compute(expr, variables, dps):
    """核心计算：设置工作精度 → 变量求值（纯常量表达式）→ 主表达式求值。"""
    # guard digits：内部计算精度 = 显示精度 + 10，
    # 二进制存储的尾噪声因此落在显示位数之外（nstr 截掉）。
    mpmath.mp.dps = dps + GUARD_DIGITS

    if not expr.strip():
        raise CalcError("SyntaxError", "表达式为空")
    if len(expr) > MAX_EXPR_LEN:
        raise CalcError("LengthLimitError",
                        f"表达式长度超过 {MAX_EXPR_LEN} 字符")

    # 变量求值：变量值必须是"纯常量表达式"（不允许引用其他变量）
    # ——防幻觉机制继承 mcp_eqsolve 的变量契约哲学：未绑定即拒绝。
    var_values = {}
    for name, expr_str in (variables or {}).items():
        if not isinstance(name, str) or not name.isidentifier():
            raise CalcError("VarError", f"非法变量名: {name!r}")
        if not isinstance(expr_str, str):
            raise CalcError("VarError",
                            f"变量 '{name}' 的值必须是表达式字符串")
        var_node = parse_expr(expr_str).body
        var_values[name] = Evaluator({}, dps).evaluate(var_node)

    # 主表达式求值（解包顶层 ast.Expression）
    main_node = parse_expr(expr).body
    return Evaluator(var_values, dps).evaluate(main_node)


# ═══════════════════════════════════════════════
#  结果格式化
# ═══════════════════════════════════════════════

def format_mpf(x, dps):
    """mpf → 十进制字符串。整数结果不带小数点，大整数全位输出。"""
    if not mpmath.isfinite(x):
        return str(x)                      # inf / -inf / nan
    if x == mpmath.mpf(int(x)):
        return str(int(x))
    return mpmath.nstr(x, dps)


def format_complex(x, dps):
    re_part, im_part = x.real, x.imag
    if not mpmath.isfinite(re_part) or not mpmath.isfinite(im_part):
        return str(x)
    re_s = format_mpf(re_part, dps)
    im_s = format_mpf(abs(im_part), dps)
    sign = "+" if im_part >= 0 else "-"
    return f"{re_s} {sign} {im_s}j"


def format_matrix(m, dps):
    rows = []
    for i in range(m.rows):
        row = ", ".join(format_number(m[i, j], dps) for j in range(m.cols))
        rows.append(f"[{row}]")
    return "\n".join(rows)


def format_number(x, dps):
    """任意结果 → 可读字符串。"""
    if isinstance(x, mpmath.mpc):
        return format_complex(x, dps)
    if isinstance(x, mpmath.mpf):
        return format_mpf(x, dps)
    if isinstance(x, mpmath.matrix):
        return format_matrix(x, dps)
    if isinstance(x, (list, tuple)):
        inner = ", ".join(format_number(e, dps) for e in x)
        return f"[{inner}]" if isinstance(x, list) else f"({inner})"
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        return repr(x)
    return str(x)


def finalize_result(value, dps):
    text = format_number(value, dps)
    if len(text) > MAX_RESULT_CHARS:
        raise CalcError(
            "ResultLimitError",
            f"结果文本超过 {MAX_RESULT_CHARS} 字符（计算成功但输出被拒）",
            hint="可降低 dps 或拆分计算",
        )
    return text


# ═══════════════════════════════════════════════
#  主流程（单次调用，无状态）
# ═══════════════════════════════════════════════

def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def apply_resource_limits():
    """进程内内存自限（1 GiB）。POSIX 不可用时静默跳过（父进程超时兜底）。"""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MEM_LIMIT, MEM_LIMIT))
    except (ValueError, OSError):
        pass


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    argv = sys.argv[1:]
    if argv == ["--selftest"]:
        sys.exit(run_selftest())
    if len(argv) != 1:
        emit({"ok": False, "error": {
            "type": "InternalError",
            "message": "用法: worker.py <json-payload> 或 worker.py --selftest",
        }})
        sys.exit(2)

    apply_resource_limits()

    try:
        payload = json.loads(argv[0])
        expr = payload.get("expr", "")
        variables = payload.get("vars") or {}
        dps = payload.get("dps") or DEFAULT_DPS

        if not isinstance(variables, dict):
            raise CalcError("VarError", "vars 必须是 JSON 对象")
        if (not isinstance(dps, int) or isinstance(dps, bool)
                or not (1 <= dps <= MAX_DPS)):
            raise CalcError("DpsError",
                            f"dps 必须是 1-{MAX_DPS} 的整数")

        value = compute(expr, variables, dps)
        text = finalize_result(value, dps)
        emit({"ok": True, "result": text})
    except CalcError as e:
        emit({"ok": False, "error": e.to_dict()})
    except MemoryError:
        emit({"ok": False, "error": {
            "type": "MemoryError",
            "message": f"内存超限（{MEM_LIMIT >> 20} MiB 上限）",
        }})
    except Exception as e:
        # 兜底：任何意外异常都转结构化 JSON，绝不裸崩
        emit({"ok": False, "error": {
            "type": type(e).__name__,
            "message": str(e),
        }})


# ═══════════════════════════════════════════════
#  自测（--selftest，离线验证核心路径）
# ═══════════════════════════════════════════════

def expect_eq(text):
    return ("eq", text)


def expect_approx(text, tol="1e-20"):
    return ("approx", text, tol)


def expect_contains(sub):
    return ("contains", sub)


def expect_error(etype):
    return ("error", etype)


SELFTEST_CASES = [
    # (名称, 表达式, vars, dps, 期望)
    ("加法",            "1 + 2",                 None,   50, expect_eq("3")),
    ("浮点精度",        "0.1 + 0.2",             None,   50, expect_eq("0.3")),
    ("下划线数字",      "1_000_000 + 1",         None,   50, expect_eq("1000001")),
    ("取模",            "7 % 3",                 None,   50, expect_eq("1")),
    ("整除",            "7 // 3",                None,   50, expect_eq("2")),
    ("三角函数",        "sin(pi/2)",             None,   50, expect_eq("1")),
    ("gamma",           "gamma(10)",             None,   50, expect_eq("362880")),
    ("Bessel J0(1)",    "besselj(0, 1)",         None,   50,
     expect_approx("0.76519768655796655144971752610266322090927428975532")),
    ("zeta(2)",         "zeta(2)",               None,   50,
     expect_approx("1.6449340668482264364724151666460251892189499012068")),
    # quad 区间约定：mpmath 用 [a, b] 列表（quad(f, a, b) 会被当两个区间）
    ("积分",            "quad(sin, [0, pi])",    None,   50, expect_approx("2")),
    ("复数",            "sqrt(-1)",              None,   50, expect_contains("1j")),
    ("矩阵行列式",      "det(matrix([[1,2],[3,4]]))", None, 50, expect_eq("-2")),
    ("矩阵输出",        "matrix([[1,2],[3,4]])", None,   50, expect_contains("[1, 2]")),
    # 变量带入用精确值（sqrt(2) 的平方有浮点误差，另由 approx 用例覆盖）
    ("变量带入",        "x**2 + 1",              {"x": "2"}, 50, expect_eq("5")),
    ("变量值含函数",    "y * 2",                 {"y": "gamma(5)"}, 50,
     expect_eq("48")),
    # wrapper 兼容层（mpmath 1.3.0 缺失函数）
    ("log2包装",        "log2(8)",               None,   50, expect_eq("3")),
    ("trunc包装",       "trunc(-3.7)",           None,   50, expect_eq("-3")),
    ("lgamma包装",      "lgamma(5)",             None,   50,
     expect_approx("3.178053830347945619646941601297727408871")),
    ("erfcinv包装",     "erfcinv(0.5)",          None,   50,
     expect_approx("0.47693627620446987338141835364313055980896974905947")),
    ("y0包装",          "y0(1)",                 None,   50,
     expect_approx("0.088256964215676957982926766023514915890443728463031",
                   "1e-10")),
    # 对角矩阵求逆精确（0.5 = 2^-1 可精确表示），避免 LU 分解尾噪声
    ("inv包装",         "inv(matrix([[2,0],[0,3]]))", None, 50,
     expect_contains("0.5")),
    ("dot包装",         "dot(matrix([[1,2],[3,4]]), matrix([[0,1],[1,0]]))",
     None, 50, expect_contains("4")),
    ("transpose包装",   "transpose(matrix([[1,2],[3,4]]))", None, 50,
     expect_contains("3")),
    ("eigvals包装",     "eigvals(matrix([[1,2],[3,4]]))", None, 50,
     expect_contains("5.3722813232690143")),
    # 红线与安全
    ("幂指数超限",      "10**2000",              None,   50, expect_error("PowerLimitError")),
    ("factorial超限",   "factorial(20000)",      None,   50, expect_error("ArgLimitError")),
    ("未绑定名字",      "sqrt(1 + x)",           None,   50, expect_error("NameError")),
    ("除零",            "1/0",                   None,   50, expect_error("ZeroDivisionError")),
    ("不在白名单函数",  "besseljj(0, 1)",        None,   50, expect_error("NameError")),
    ("禁止属性访问",    "mpmath.sin(1)",         None,   50, expect_error("UnsupportedSyntax")),
    ("禁止import",      "import os",             None,   50, expect_error("SyntaxError")),
    ("无求解函数",      "findroot(sin, 3)",      None,   50, expect_error("NameError")),
]


def check_case(expr, variables, dps, expect):
    kind = expect[0]
    try:
        value = compute(expr, variables, dps)
        text = finalize_result(value, dps)
    except CalcError as e:
        if kind == "error":
            return e.etype == expect[1], e.etype
        return False, f"CalcError({e.etype}): {e.message}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    if kind == "eq":
        return text == expect[1], text
    if kind == "approx":
        try:
            return abs(float(text) - float(expect[1])) < float(expect[2]), text
        except (ValueError, TypeError):
            return False, text
    if kind == "contains":
        return expect[1] in text, text
    if kind == "error":
        return False, f"期望 {expect[1]} 错误，实际成功: {text}"
    return False, f"未知期望类型 {kind}"


def run_selftest():
    print(f"mpmath 版本: {mpmath.__version__}")
    if MISSING_FUNCTIONS:
        print(f"⚠ 白名单中不存在于 mpmath 且无兼容层的函数: {MISSING_FUNCTIONS}")
    print(f"白名单函数数: {len(FUNCTIONS)}")
    print("-" * 60)

    passed = failed = 0
    for name, expr, variables, dps, expect in SELFTEST_CASES:
        ok, detail = check_case(expr, variables, dps, expect)
        if ok:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}  →  {detail}")

    print("-" * 60)
    print(f"通过 {passed}/{len(SELFTEST_CASES)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    main()
