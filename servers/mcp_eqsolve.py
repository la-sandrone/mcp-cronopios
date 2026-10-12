# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "mcp[cli]>=1.9.0",
#     "sympy>=1.14.0",
# ]
# ///

"""
文件名: mcp_eqsolve.py
摘要: 方程求解 MCP 服务器。LLM 通过 declare_vars → add_equation → solve
      三步走，变量声明契约防止自创符号幻觉。输出 LaTeX 供人审计。
依赖: mcp, sympy
"""

import json

import sympy
from sympy import Eq, latex, linsolve, nonlinsolve, solveset
from sympy.parsing.sympy_parser import parse_expr
from typing import Optional

# ───── mcp 1.x / 2.x 兼容：FastMCP 改名（由 tools/mcp2_migrate.py 生成）─────
# mcp 2.x 把 FastMCP 改名为 MCPServer（构造参数与 .tool()/.run() 均兼容）。
try:
    from mcp.server.fastmcp import FastMCP  # mcp 1.x
except ModuleNotFoundError:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
# ───── 兼容层结束 ─────

# ═══════════════════════════════════════════════
#  状态
# ═══════════════════════════════════════════════

# 变量存储: name → sympy.Symbol
local_vars: dict[str, sympy.Symbol] = {}
# 变量元数据: name → { type, domain, is_parameter }
var_meta: dict[str, dict] = {}
# 方程存储: key → sympy.Expr (标准形式 = 0)
equations: dict[str, sympy.Expr] = {}
eq_counter: int = 0

# 域映射
DOMAIN_MAP: dict[str, sympy.Set] = {
    "R": sympy.S.Reals,
    "C": sympy.S.Complexes,
    "Z": sympy.S.Integers,
    "N": sympy.S.Naturals0,
}

# 类型 → SymPy assumptions
TYPE_ASSUMPTIONS: dict[str, dict] = {
    "real":    {"real": True},
    "integer": {"integer": True},
    "rational": {"rational": True},
    "complex": {},
}


# ═══════════════════════════════════════════════
#  MCP 服务器
# ═══════════════════════════════════════════════

mcp = FastMCP(
    "mcp-eqsolve",
    instructions="""方程求解 MCP 服务器。使用流程：

【第一步】declare_vars — 声明所有变量（变量契约）
  传入 JSON 数组，每项格式：
    name: str         — 变量名
    type: str         — "real" | "integer" | "complex" | "rational"（默认 "real"）
    domain: str       — "R" | "C" | "Z" | "N"（默认 "R"，求解域）
    is_parameter: bool — true=参数（不作为求解目标），默认 false

【第二步】add_equation — 添加方程
  传入 SymPy 语法字符串，如:
    Eq(2*x + 3*y, 5.2)
    Eq(x**2 + y**2, 1)
  方程中出现的所有变量必须已通过 declare_vars 声明，否则拒绝执行。

【第三步】solve — 求解
  可选指定求解变量列表，如 ["x", "y"]。
  不指定则自动求解所有非参数变量。
  返回 LaTeX 格式的解。

辅助工具:
  clear    — 清空当前会话
  list_vars — 查看已声明的变量
  list_eqs — 查看已添加的方程
""",
)


# ───────────────────────────────────────────────
#  工具: declare_vars
# ───────────────────────────────────────────────

@mcp.tool()
def declare_vars(variables_json: str) -> str:
    """声明变量（变量契约）。variables_json 是 JSON 数组字符串。

    每项格式:
    {
        "name": "x",
        "type": "real",       // "real" | "integer" | "complex" | "rational"
        "domain": "R",        // "R" | "C" | "Z" | "N"
        "is_parameter": false // true=参数（不作为求解目标）
    }

    示例:
    [{"name": "x", "type": "real", "domain": "R", "is_parameter": false},
     {"name": "y", "type": "real", "domain": "R", "is_parameter": false},
     {"name": "k", "type": "real", "domain": "R", "is_parameter": true}]
    """
    try:
        vars_list = json.loads(variables_json)
    except json.JSONDecodeError as e:
        return f"错误：JSON 解析失败 — {e}"

    if not isinstance(vars_list, list):
        return "错误：variables_json 必须是 JSON 数组"

    created: list[str] = []
    errors: list[str] = []

    for item in vars_list:
        name = item.get("name", "")
        if not name or not isinstance(name, str):
            errors.append(f"无效变量名: {name}")
            continue

        if name in local_vars:
            errors.append(f"变量 '{name}' 已声明，跳过")
            continue

        var_type = item.get("type", "real")
        domain_str = item.get("domain", "R")
        is_param = item.get("is_parameter", False)

        # 校验 type
        if var_type not in TYPE_ASSUMPTIONS:
            errors.append(
                f"变量 '{name}': 未知类型 '{var_type}'，"
                f"可选: real, integer, complex, rational"
            )
            continue

        # 校验 domain
        if domain_str not in DOMAIN_MAP:
            errors.append(
                f"变量 '{name}': 未知域 '{domain_str}'，可选: R, C, Z, N"
            )
            continue

        # 创建 SymPy symbol
        assumptions = TYPE_ASSUMPTIONS[var_type]
        try:
            symbol = sympy.symbols(name, **assumptions)
        except Exception as e:
            errors.append(f"变量 '{name}' 创建失败: {e}")
            continue

        local_vars[name] = symbol
        var_meta[name] = {
            "type": var_type,
            "domain": domain_str,
            "is_parameter": bool(is_param),
        }
        created.append(name)

    parts: list[str] = []
    if created:
        parts.append(f"✓ 已声明变量: {', '.join(created)}")
    if errors:
        parts.append(f"⚠ 错误: {'; '.join(errors)}")

    return "\n".join(parts) if parts else "无变量被声明"


# ───────────────────────────────────────────────
#  工具: add_equation
# ───────────────────────────────────────────────

@mcp.tool()
def add_equation(expr_str: str) -> str:
    """添加方程。expr_str 为 SymPy 语法字符串。

    示例:
      "Eq(2*x + 3*y, 5.2)"
      "Eq(x**2 + y**2, 1)"
      "x**2 + y**2 - 1"  （等价于 Eq(x**2 + y**2, 1)）

    方程中的所有符号必须已通过 declare_vars 声明，否则拒绝。
    """
    global eq_counter

    try:
        # 使用 local_dict 让解析出的符号引用已声明的 Symbol 对象
        expr = parse_expr(expr_str, local_dict=local_vars)
    except Exception as e:
        return f"错误：表达式解析失败 — {e}"

    # 转为 Eq 标准形式
    if isinstance(expr, Eq):
        eq_expr = expr.lhs - expr.rhs
    else:
        eq_expr = expr

    # 校验所有符号是否已声明
    free_syms = eq_expr.free_symbols
    undeclared: list[str] = []
    for s in free_syms:
        sname = str(s)
        if sname not in local_vars:
            undeclared.append(sname)

    if undeclared:
        return (
            f"错误：方程中包含未声明的变量: {', '.join(undeclared)}\n"
            f"请先通过 declare_vars 声明这些变量。"
        )

    # 存储
    key = f"eq_{eq_counter}"
    eq_counter += 1
    equations[key] = eq_expr

    return f"✓ 已添加方程 [{key}]: {latex(expr)}"


# ───────────────────────────────────────────────
#  工具: solve
# ───────────────────────────────────────────────

@mcp.tool()
def solve(target_vars: Optional[list[str]] = None) -> str:
    """求解当前方程组。

    ⚠️ 含符号变量的推导（展开 / 化简 / 因式分解 / 求导 / 积分 / 解方程 / 恒等式验证）
    请走本服务器，不要在思考里手推——手推会累积误差且无法自检。
    需要精确解时用它，不要手算。

    自动检测:
      · 线性方程组 → linsolve（精确解）
      · 非线性方程组 → nonlinsolve
      · 单个方程 → solveset

    Args:
        target_vars: 可选，指定求解变量列表，如 ["x", "y"]
                     不指定则自动求解所有非参数变量

    Returns:
        LaTeX 格式的解
    """
    if not equations:
        return "错误：没有方程可解。请先用 add_equation 添加方程。"

    if not local_vars:
        return "错误：没有声明变量。请先用 declare_vars 声明变量。"

    # ── 确定求解变量 ──
    if target_vars:
        target_names = list(target_vars)
    else:
        # 自动：所有非参数变量
        target_names = [
            name
            for name, meta in var_meta.items()
            if not meta["is_parameter"]
        ]
        if not target_names:
            return (
                "错误：没有非参数变量可供求解。"
                "请检查变量声明或指定 target_vars"
            )

    # 校验变量存在
    symbols: list[sympy.Symbol] = []
    for name in target_names:
        if name not in local_vars:
            return f"错误：变量 '{name}' 未声明"
        symbols.append(local_vars[name])

    # 构建方程组（标准形式 = 0）
    system: list[sympy.Expr] = list(equations.values())

    # 确定域
    primary_domain_str = var_meta.get(target_names[0], {}).get("domain", "R")
    primary_domain = DOMAIN_MAP.get(primary_domain_str, sympy.S.Reals)

    try:
        # ── 情况 1: 单个方程，单个变量 → solveset ──
        if len(system) == 1 and len(symbols) == 1:
            sol = solveset(system[0], symbols[0], domain=primary_domain)
            return f"解: {latex(sol)}"

        # ── 情况 2: 多元方程组 ──
        # 先试 linsolve
        try:
            sol = linsolve(system, symbols)
            # linsolve 返回空集时是 EmptySet，不是 falsy
            # 我们需要检查是否真的求解成功
            if sol is not sympy.EmptySet:
                return f"线性方程组解: {latex(sol)}"
        except Exception:
            pass

        # 再试 nonlinsolve
        try:
            sol = nonlinsolve(system, symbols)
            if sol is not sympy.EmptySet:
                return f"方程组解: {latex(sol)}"
        except Exception as e:
            return f"错误：非线性求解失败 — {e}"

        # 兜底：逐变量 solveset
        results: dict[str, str] = {}
        for i, sym in enumerate(symbols):
            try:
                sol = solveset(system[i] if i < len(system) else system[-1],
                               sym, domain=primary_domain)
                results[str(sym)] = latex(sol)
            except Exception as e:
                results[str(sym)] = f"<错误: {e}>"

        parts = [f"{var}: {sol}" for var, sol in results.items()]
        return "逐变量求解:\n" + "\n".join(parts)

    except Exception as e:
        return f"错误：求解过程异常 — {type(e).__name__}: {e}"


# ───────────────────────────────────────────────
#  工具: clear
# ───────────────────────────────────────────────

@mcp.tool()
def clear() -> str:
    """清空当前会话的所有变量和方程。"""
    global eq_counter
    local_vars.clear()
    var_meta.clear()
    equations.clear()
    eq_counter = 0
    return "✓ 已清空所有变量和方程"


# ───────────────────────────────────────────────
#  工具: list_vars
# ───────────────────────────────────────────────

@mcp.tool()
def list_vars() -> str:
    """列出当前会话中已声明的变量及其属性。"""
    if not local_vars:
        return "当前未声明任何变量。"

    lines = ["已声明变量:"]
    for name in local_vars:
        meta = var_meta.get(name, {})
        param_tag = " [参数]" if meta.get("is_parameter") else ""
        lines.append(
            f"  · {name}  type={meta.get('type', '?')}  "
            f"domain={meta.get('domain', '?')}{param_tag}"
        )
    return "\n".join(lines)


# ───────────────────────────────────────────────
#  工具: list_eqs
# ───────────────────────────────────────────────

@mcp.tool()
def list_eqs() -> str:
    """列出当前会话中已添加的方程。"""
    if not equations:
        return "当前未添加任何方程。"

    lines = ["已添加方程:"]
    for key, expr in equations.items():
        lines.append(f"  [{key}] {latex(expr)}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run()
