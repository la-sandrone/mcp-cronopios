# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp[cli]>=1.9.0", "mpmath>=1.3.0"]
# ///

"""
文件名: mcp_calculator.py
摘要: 数值计算 MCP 服务器（mpmath 任意精度，默认 50 位有效数字）。
      输入是一个「名字 → 表达式」映射（exprs），可以互相引用，按依赖序求值。
      只做数值求值与变量带入，不解方程（方程求解在 mcp_eqsolve.py）。
      计算委托给 worker.py 子进程：python -I 隔离 + AST 白名单 + 资源红线。
依赖: mcp, mpmath（经 worker 子进程使用）
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

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
MAX_EXPRS = 100
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

【表达式语法】exprs 里每个值都是一个 Python 表达式子集：
  · 运算符: + - * / ** % //  一元正负号
  · 数字: 123  1.5  1_000_000（支持下划线）  复数 2+3j
  · 常量: pi e inf nan j（虚数单位）
  · 函数: 仅限下方白名单，例: sin(pi/2)  gamma(10)  besselj(0,1)
  · 列表/矩阵: [1,2,3] 或 matrix([[1,2],[3,4]])
  · 不支持: 变量赋值、语句、属性访问、import、比较、字符串

【白名单函数】
""" + FUNCTION_TABLE + """

【exprs 参数】JSON 对象：名字 → 表达式字符串。名字自己起，值必须带引号。

  ✅ {"exprs": {"C": "3.74e28", "P": "140e15", "tau_s": "C/P/3.15576e7"}}
  ✅ {"exprs": {"T": "1.487*20/89.5"}}
  ❌ {"exprs": [1.487*20/89.5]}      ← 数组 + 裸算式：JSON 里不存在"裸算式"
  ❌ {"exprs": {"r": 1+1}}            ← 值没加引号

  exprs 内的名字可以互相引用，按依赖自动排序求值，与书写先后无关；
  互相引用成环会被拒绝并报出环的路径。
  名字必须是标识符（字母/下划线开头）；白名单函数名与常量名不可用作名字。

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
    exprs: dict,
    dps: Optional[int] = None,
) -> str:
    """数值计算（mpmath 任意精度，默认 50 位有效数字）。

    ⚠️ 非平凡数值计算（多位数乘除 / 乘方 / 开方 / 对数 / 三角函数 / 百分比 / 单位换算）
    请直接调用本工具，不要在思考中手算——LLM 心算靠查表而非推理，长链必错。

    只做数值求值与变量带入，不解方程。

    ══ exprs 的形状（铁律，照做即可）══
    一个「名字 → 表达式」的 JSON 对象。名字自己起；**值必须是带引号的字符串**。

      ✅ {"exprs": {"tau_s": "C/P/3.15576e7", "ratio": "tau_s/25"}}
      ✅ {"exprs": {"T": "1.487*20/89.5"}}
      ❌ {"exprs": [1.487*20/89.5]}     ← 数组 + 裸算式，不是合法 JSON
      ❌ {"exprs": {"r": 1+1}}           ← 值没加引号

    【为什么】JSON 的值只有 数字/字符串/布尔/null/对象/数组 六种，
    没有"裸算式"这种东西——**公式必须写在引号里**。
    把每个公式放在**名字后面**（键值对的值位），就永远不用纠结引号该加在哪。

    【依赖】exprs 里的名字可互相引用，按依赖关系自动排序求值，与书写先后无关。
    互相引用成环会被拒绝，并报出环的路径。

    对数约定：log(x) 是**自然对数**（底 e，不是 log10）；log(x, b) 是底 b 的对数；
              ln(x) 与 log(x) 等价。常用底数另有 log10 / log2。

    Args:
        exprs: 名字 → 表达式字符串，如
               {"C": "3.74e28", "P": "140e15", "tau_s": "C/P/3.15576e7"}。
               名字须是标识符；公式须以 JSON 字符串给出（带引号）。
        dps: 可选。有效数字位数，1-10000，默认 50。

    Returns:
        每行一个「名字 = 结果」（按名字排序），或结构化错误文本（类型/位置/提示）。
    """
    # ── 为什么 exprs 标注成裸 dict，而不是 dict[str, str] ──
    # dict[str, str] 在 pydantic 里生成 additionalProperties={"type": "string"}；
    # 而 DSH 的 JSON schema 子集只接受**布尔**形式的 additionalProperties
    #（packages/core/tools/src/json-schema.ts：「additionalProperties must be a boolean」），
    # 且 validateJsonSchemaValue 仅在 additionalProperties === false 时强制，
    # schema 形式会被静默忽略。裸 dict 生成 {"type": "object"}——与旧版 vars 同形
    #（那个形状已在线跑过数月、几百次调用），既不越界，也把"值必须是字符串"
    # 的判定留在本工具里，从而能给出带提示的错误而不是一句校验失败。
    #
    # ── 参数校验 ──
    # 判据与 worker.py 镜像（改一处必须同步两处）。这里做的是**预检**：
    # 让明显写错的调用不必起子进程；权威校验始终在 worker（它是隔离边界，
    # 且 exprs 的语义校验、依赖图、成环判定都在那边）。
    if not exprs:
        return "✗ EmptyExpression: exprs 为空——至少要有一个「名字: 表达式」"
    if len(exprs) > MAX_EXPRS:
        return f"✗ LengthLimitError: exprs 条目数 {len(exprs)} 超过上限 {MAX_EXPRS}"

    for name, value in exprs.items():
        if not isinstance(name, str) or not name.isidentifier():
            return ("✗ NameError: 非法名字 " + repr(name)
                    + "——键必须是标识符（字母/下划线开头），值才是表达式字符串\n"
                    '  正确: {"tau_s": "C/P/3.15576e7"}')
        if not isinstance(value, str):
            return (f"✗ TypeError: [{name}] 的值必须是表达式字符串（JSON 里要带引号）\n"
                    '  正确: {"r": "1+1"}    错误: {"r": 1+1}')
        if len(value) > MAX_EXPR_LEN:
            return f"✗ LengthLimitError: [{name}] 表达式长度超过 {MAX_EXPR_LEN} 字符"

    if dps is None:
        dps = DEFAULT_DPS
    elif (not isinstance(dps, int) or isinstance(dps, bool)
          or not (1 <= dps <= MAX_DPS)):
        return f"✗ DpsError: dps 必须是 1-{MAX_DPS} 的整数"

    # ── 派发 worker 子进程（python -I 隔离模式）──
    payload = json.dumps(
        {"exprs": exprs, "dps": dps},
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
                "可降低 dps 或拆分 exprs")

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
        rows = resp.get("results") or []
        lines = [f"✓ {name} = {text}" for name, text in rows]
        lines.append(f"（{dps} 位有效数字，内部 {dps + 10} 位）")
        return "\n".join(lines)

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
