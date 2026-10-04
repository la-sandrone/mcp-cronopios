# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp[cli]>=1.9.0", "mpmath>=1.3.0"]
# ///

"""
文件名: mcp_calculator.py
摘要: 数值计算 MCP 服务器（mpmath 任意精度，默认 50 位有效数字）。
      只做数值求值与变量带入，不解方程（方程求解在 mcp_eqsolve.py）。
      计算委托给 worker.py 子进程：python -I 隔离 + AST 白名单 + 资源红线。
依赖: mcp, mpmath（经 worker 子进程使用）
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional, Union

# ───── mcp 1.x / 2.x 兼容：FastMCP 改名（由 tools/mcp2_migrate.py 生成）─────
# mcp 2.x 把 FastMCP 改名为 MCPServer（构造参数与 .tool()/.run() 均兼容）。
try:
    from mcp.server.fastmcp import FastMCP  # mcp 1.x
except ModuleNotFoundError:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
# ───── 兼容层结束 ─────

# ═══════════════════════════════════════════════
#  常量与红线（与 worker.py 保持一致；改一处必须同步两处）
# ═══════════════════════════════════════════════

WORKER_PATH = Path(__file__).resolve().parent / "worker.py"

MAX_EXPR_LEN = 2000
MAX_DPS = 10000
DEFAULT_DPS = 50
TIMEOUT_SEC = 5.0

FUNCTION_TABLE = (
    "基础: abs sqrt cbrt exp log log10 log2 degrees radians floor ceil trunc nint sign\n"
    "三角: sin cos tan asin acos atan atan2 sec csc cot\n"
    "双曲: sinh cosh tanh asinh acosh atanh\n"
    "特殊: gamma lgamma factorial binomial erf erfc erfinv erfcinv zeta "
    "lambertw polylog expint si ci fresnels fresnelc agm harmonic\n"
    "Bessel: besselj bessely besseli besselk j0 j1 y0 y1 hankel1 hankel2\n"
    "复数: conj re im arg phase polar rect\n"
    "积分/求和/微分(数值): quad nsum nprod diff（区间用列表: quad(sin,[0,pi]) nsum(f,[a,b])）\n"
    "矩阵: matrix det inv norm dot eye zeros ones diag lu_solve eig eigvals transpose（求逆用 inv(A)，勿用 A**-1）"
)

mcp = FastMCP(
    "mcp-calculator",
    instructions="""数值计算 MCP 服务器（mpmath 任意精度）。

【定位】只做两件事：
  1. 数值求值 —— 表达式子集 + 白名单函数，任意精度（默认 50 位有效数字）
  2. 变量带入 —— vars 参数绑定变量，变量值也是表达式
  不做第三件事：不解方程（findroot/odefun/solve 均不可用；
  方程求解请用 mcp-eqsolve，本工具负责把解出的符号代入求数值）。

【expression 语法】Python 表达式子集：
  · 运算符: + - * / ** % //  一元正负号
  · 数字: 123  1.5  1_000_000（支持下划线）  复数 2+3j
  · 常量: pi e inf nan j（虚数单位）
  · 函数: 仅限下方白名单，例: sin(pi/2)  gamma(10)  besselj(0,1)
  · 列表/矩阵: [1,2,3] 或 matrix([[1,2],[3,4]])
  · 不支持: 变量赋值、语句、属性访问、import、比较、字符串

【白名单函数】
""" + FUNCTION_TABLE + """

【vars 参数】JSON 对象，变量名 → 表达式字符串，例如:
  {"x": "sqrt(2)", "y": "1.5"}
  变量值表达式内不允许引用其他变量（防幻觉：未绑定名字一律报错）。

【dps 参数】有效数字位数，1-10000，默认 50。内部计算用 dps+10 位 guard digits，尾噪声在显示层被截掉（二进制浮点：0.1 等十进制数非精确存储，dps 位内近似）。

【资源红线】表达式 ≤2000 字符；幂指数绝对值 ≤1000；
  factorial/binomial 参数 ≤10000；计算超时 5 秒。

【错误格式】结构化多行文本：错误类型 / 输入回显+箭头定位 / 提示。
  错误信息可直接用于修正表达式后重试。
""",
)


# ───────────────────────────────────────────────
#  工具: calculate
# ───────────────────────────────────────────────

@mcp.tool()
def calculate(
    expression: str,
    vars: Optional[Union[str, dict]] = None,
    dps: Optional[int] = None,
) -> str:
    """数值计算（mpmath 任意精度，默认 50 位有效数字）。

    只做数值求值与变量带入，不解方程。

    对数约定：log(x) 是**自然对数**（底 e，不是 log10）；log(x, b) 是底 b 的对数；
              ln(x) 与 log(x) 等价。常用底数另有 log10 / log2。

    Args:
        expression: Python 表达式子集，如 "gamma(10)"、"(x**2 + 1)/2"、
                    "quad(sin, [0, pi])"、"det(matrix([[1,2],[3,4]]))"
        vars: 可选。JSON 对象或 JSON 字符串，绑定变量，如 {"x": "sqrt(2)"} 或 '{"x": "sqrt(2)"}'。
              变量值也是表达式（纯常量表达式，不允许引用其他变量）。
        dps: 可选。有效数字位数，1-10000，默认 50。

    Returns:
        计算结果（十进制），或结构化错误文本（类型/位置/提示）。
    """
    # ── 参数校验 ──
    if not expression or not expression.strip():
        return "✗ EmptyExpression: 表达式为空"
    if len(expression) > MAX_EXPR_LEN:
        return f"✗ LengthLimitError: 表达式长度超过 {MAX_EXPR_LEN} 字符"

    variables: dict = {}
    if vars:
        try:
            parsed = vars if isinstance(vars, dict) else json.loads(vars)
        except json.JSONDecodeError as e:
            return f"✗ VarError: vars JSON 解析失败 — {e}"
        if not isinstance(parsed, dict):
            return "✗ VarError: vars 必须是 JSON 对象，如 {\"x\": \"2\"}"
        for k, v in parsed.items():
            if not isinstance(k, str) or not k.isidentifier():
                return f"✗ VarError: 非法变量名 {k!r}"
            if not isinstance(v, str):
                return f"✗ VarError: 变量 '{k}' 的值必须是表达式字符串"
        variables = parsed

    if dps is None:
        dps = DEFAULT_DPS
    elif (not isinstance(dps, int) or isinstance(dps, bool)
          or not (1 <= dps <= MAX_DPS)):
        return f"✗ DpsError: dps 必须是 1-{MAX_DPS} 的整数"

    # ── 派发 worker 子进程（python -I 隔离模式）──
    payload = json.dumps(
        {"expr": expression, "vars": variables, "dps": dps},
        ensure_ascii=False,
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(WORKER_PATH), payload],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return (f"✗ TimeoutError: 计算超时（>{TIMEOUT_SEC}s）。"
                "可降低 dps 或拆分表达式")

    out = (proc.stdout or "").strip()
    if not out:
        detail = (proc.stderr or "").strip()[:500]
        return "✗ InternalError: 子进程无输出" + (
            f"\n  详情: {detail}" if detail else ""
        )

    try:
        resp = json.loads(out)
    except json.JSONDecodeError:
        detail = (proc.stderr or "").strip()[:500]
        return "✗ InternalError: 子进程输出无法解析" + (
            f"\n  详情: {detail}" if detail else ""
        )

    if resp.get("ok"):
        return f"✓ = {resp['result']}\n（{dps} 位有效数字，内部 {dps + 10} 位）"

    return format_error(resp.get("error", {}))


# ───────────────────────────────────────────────
#  错误格式化（LLM 自愈闭环的关键）
# ───────────────────────────────────────────────

def format_error(err: dict) -> str:
    etype = err.get("type", "Error")
    msg = err.get("message", "")
    lines = [f"✗ {etype}: {msg}"]

    expr = err.get("expr", "")
    col = err.get("col", 0)
    if expr:
        lines.append(f"  输入: {expr}")
        if col and col > 0:
            lines.append("        " + " " * (col - 1) + "^")

    hint = err.get("hint")
    if hint:
        lines.append(f"  提示: {hint}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run()
