#!/usr/bin/env python3
"""
文件名: server.py
摘要: MCP Entropia — 随机数生成服务。提供 13 种概率分布的采样。
       ⚠️  NOT a CSPRNG（非密码学安全随机数生成器）。
       所有分布使用 Python stdlib `random` 模块（Mersenne Twister）
       或 NumPy 的 PCG-64 生成器。
       严禁用于密钥生成、密码盐值、安全令牌等密码学场景。
       如需 CSPRNG，请使用 Python `secrets` 模块或 OS 级熵源。
依赖: Python stdlib (random, json, asyncio)
      numpy (可选 — binomial / poisson / chi_squared 三个分布需要)
      mcp (MCP SDK)
"""

import random
import json
import asyncio
import sys

# ───── mcp 1.x / 2.x 兼容层（由 tools/mcp2_migrate.py 生成，勿手改）─────
# mcp 2.x 删除了 @server.list_tools() / @server.call_tool() 装饰器，改为
# Server(on_list_tools=..., on_call_tool=...) 构造参数 + add_request_handler()。
# 这里把 2.x 的接口适配回旧的装饰器写法，使本文件在 mcp<2 与 mcp>=2 上都能跑，
# 从而不必让"改代码"和"换 venv"两件事绑在一起。
from mcp.server import Server as _McpRawServer
import mcp.types as _mcp_types

if hasattr(_McpRawServer, "list_tools"):  # mcp 1.x：原样透传
    Server = _McpRawServer
else:  # mcp 2.x：装饰器 → on_* 构造参数的适配
    class Server:  # type: ignore[no-redef]
        """把 mcp 2.x 适配回 mcp 1.x 的注册写法。"""

        def __init__(self, name: str, **kw):
            self._raw = _McpRawServer(name, **kw)
            self._on_list_tools = None
            self._on_call_tool = None

        def list_tools(self):
            def _deco(fn):
                self._on_list_tools = fn
                return fn
            return _deco

        def call_tool(self):
            def _deco(fn):
                self._on_call_tool = fn
                return fn
            return _deco

        def create_initialization_options(self):
            return self._raw.create_initialization_options()

        def _bind(self) -> None:
            """旧装饰器攒下的 handler，在 run 之前一次性注册到真 Server。"""
            if self._on_list_tools is not None:
                async def _h_list(ctx, params):
                    return _mcp_types.ListToolsResult(
                        tools=list(await self._on_list_tools()))
                self._raw.add_request_handler(
                    "tools/list", _mcp_types.PaginatedRequestParams, _h_list)
            if self._on_call_tool is not None:
                async def _h_call(ctx, params):
                    res = await self._on_call_tool(
                        params.name, params.arguments or {})
                    # 旧代码返回 list[TextContent]；新版要求 CallToolResult，
                    # 但也容忍调用方已经自己包好。
                    if isinstance(res, _mcp_types.CallToolResult):
                        return res
                    return _mcp_types.CallToolResult(content=list(res))
                self._raw.add_request_handler(
                    "tools/call", _mcp_types.CallToolRequestParams, _h_call)

        async def run(self, read_stream, write_stream, options, *a, **kw):
            self._bind()
            return await self._raw.run(
                read_stream, write_stream, options, *a, **kw)
# ───── 兼容层结束 ─────
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ── NumPy 可选导入 ──────────────────────────────────────────────
try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False
    np = None  # type: ignore

# ── 常量 ────────────────────────────────────────────────────────
MAX_COUNT = 10000
_NUMPY_WARNING = (
    "NumPy 未安装。binomial / poisson / chi_squared 不可用。"
    "安装: uv pip install numpy  (或 pip install numpy)"
)

server = Server("mcp-entropia")


# ══════════════════════════════════════════════════════════════════
# 内部辅助
# ══════════════════════════════════════════════════════════════════

def _parse_count(arguments: dict) -> int:
    """解析并验证 `n` 参数（采样数量）。"""
    n = arguments.get("n", 1)
    if not isinstance(n, int) or n < 1:
        raise ValueError(f"n 必须为正整数，收到: {n}")
    if n > MAX_COUNT:
        raise ValueError(f"n 超过上限 {MAX_COUNT}，收到: {n}")
    return n


def _format(distribution: str, parameters: dict, values, n: int) -> str:
    """统一格式化返回值。"""
    result = {
        "distribution": distribution,
        "parameters": parameters,
    }
    if n == 1:
        result["value"] = values[0] if isinstance(values, list) else values
    else:
        result["values"] = values
    return json.dumps(result, ensure_ascii=False, default=str)


# ══════════════════════════════════════════════════════════════════
# 分布采样函数
# ══════════════════════════════════════════════════════════════════

# ── stdlib 分布 ──────────────────────────────────────────────────

def _sample_uniform(n: int) -> list[float]:
    return [random.random() for _ in range(n)]


def _sample_std_normal(n: int) -> list[float]:
    return [random.gauss(0.0, 1.0) for _ in range(n)]


def _sample_uniform_range(a: float, b: float, n: int) -> list[float]:
    return [random.uniform(a, b) for _ in range(n)]


def _sample_normal(mu: float, sigma: float, n: int) -> list[float]:
    if sigma <= 0:
        raise ValueError(f"sigma 必须 > 0，收到: {sigma}")
    return [random.gauss(mu, sigma) for _ in range(n)]


def _sample_bernoulli(p: float, n: int) -> list[int]:
    if not (0 <= p <= 1):
        raise ValueError(f"p 必须在 [0,1] 区间内，收到: {p}")
    return [1 if random.random() < p else 0 for _ in range(n)]


def _sample_exponential(lambd: float, n: int) -> list[float]:
    if lambd <= 0:
        raise ValueError(f"lambd 必须 > 0，收到: {lambd}")
    return [random.expovariate(lambd) for _ in range(n)]


def _sample_gamma(alpha: float, beta: float, n: int) -> list[float]:
    if alpha <= 0:
        raise ValueError(f"alpha 必须 > 0，收到: {alpha}")
    if beta <= 0:
        raise ValueError(f"beta 必须 > 0，收到: {beta}")
    return [random.gammavariate(alpha, beta) for _ in range(n)]


def _sample_beta(alpha: float, beta: float, n: int) -> list[float]:
    if alpha <= 0:
        raise ValueError(f"alpha 必须 > 0，收到: {alpha}")
    if beta <= 0:
        raise ValueError(f"beta 必须 > 0，收到: {beta}")
    return [random.betavariate(alpha, beta) for _ in range(n)]


def _sample_lognormal(mu: float, sigma: float, n: int) -> list[float]:
    if sigma <= 0:
        raise ValueError(f"sigma 必须 > 0，收到: {sigma}")
    return [random.lognormvariate(mu, sigma) for _ in range(n)]


def _sample_randint(a: int, b: int, n: int) -> list[int]:
    if not isinstance(a, int) or not isinstance(b, int):
        raise ValueError(f"a 和 b 必须为整数，收到: a={a}, b={b}")
    if a > b:
        raise ValueError(f"a 必须 <= b，收到: a={a}, b={b}")
    return [random.randint(a, b) for _ in range(n)]


# ── NumPy 分布 ───────────────────────────────────────────────────

def _require_numpy():
    if not _NUMPY_AVAILABLE:
        raise RuntimeError(_NUMPY_WARNING)


def _sample_binomial(n_trials: int, p: float, n: int) -> list[int]:
    _require_numpy()
    if n_trials <= 0:
        raise ValueError(f"n_trials 必须 > 0，收到: {n_trials}")
    if not (0 <= p <= 1):
        raise ValueError(f"p 必须在 [0,1] 区间内，收到: {p}")
    return np.random.binomial(n_trials, p, size=n).tolist()  # type: ignore[union-attr]


def _sample_poisson(lam: float, n: int) -> list[int]:
    _require_numpy()
    if lam < 0:
        raise ValueError(f"lam 必须 >= 0，收到: {lam}")
    return np.random.poisson(lam, size=n).tolist()  # type: ignore[union-attr]


def _sample_chi_squared(k: int, n: int) -> list[float]:
    _require_numpy()
    if k <= 0:
        raise ValueError(f"k（自由度）必须 > 0，收到: {k}")
    return np.random.chisquare(k, size=n).tolist()  # type: ignore[union-attr]


# ══════════════════════════════════════════════════════════════════
# MCP 工具注册
# ══════════════════════════════════════════════════════════════════

NOT_CSPRNG = (
    "⚠️ 非密码学安全。使用 Mersenne Twister / PCG-64 伪随机数生成器，"
    "严禁用于密钥、密码盐值或安全令牌。"
)


@server.list_tools()
async def list_tools() -> list[Tool]:
    tools = [
        # ── 无参数分布 ──
        Tool(
            name="uniform",
            description=f"Uniform(0,1) 均匀分布浮点数。{NOT_CSPRNG}",
            inputSchema={
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": f"采样数量，默认 1，上限 {MAX_COUNT}",
                        "default": 1
                    }
                }
            }
        ),
        Tool(
            name="std_normal",
            description=f"标准正态分布 N(0,1) 浮点数。{NOT_CSPRNG}",
            inputSchema={
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": f"采样数量，默认 1，上限 {MAX_COUNT}",
                        "default": 1
                    }
                }
            }
        ),
        # ── stdlib 参数分布 ──
        Tool(
            name="uniform_range",
            description=f"Uniform(a,b) 区间均匀分布浮点数。{NOT_CSPRNG}",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "下界"},
                    "b": {"type": "number", "description": "上界"},
                    "n": {
                        "type": "integer",
                        "description": f"采样数量，默认 1，上限 {MAX_COUNT}",
                        "default": 1
                    }
                },
                "required": ["a", "b"]
            }
        ),
        Tool(
            name="normal",
            description=f"正态分布 N(μ,σ²) 浮点数。{NOT_CSPRNG}",
            inputSchema={
                "type": "object",
                "properties": {
                    "mu": {"type": "number", "description": "均值 μ，默认 0", "default": 0},
                    "sigma": {"type": "number", "description": "标准差 σ，必须 > 0，默认 1", "default": 1},
                    "n": {
                        "type": "integer",
                        "description": f"采样数量，默认 1，上限 {MAX_COUNT}",
                        "default": 1
                    }
                }
            }
        ),
        Tool(
            name="bernoulli",
            description=f"Bernoulli(p) 二值分布（0 或 1）。{NOT_CSPRNG}",
            inputSchema={
                "type": "object",
                "properties": {
                    "p": {"type": "number", "description": "成功概率，[0,1]"},
                    "n": {
                        "type": "integer",
                        "description": f"采样数量，默认 1，上限 {MAX_COUNT}",
                        "default": 1
                    }
                },
                "required": ["p"]
            }
        ),
        Tool(
            name="exponential",
            description=f"指数分布 Exp(λ)。{NOT_CSPRNG}",
            inputSchema={
                "type": "object",
                "properties": {
                    "lambd": {"type": "number", "description": "速率参数 λ，必须 > 0"},
                    "n": {
                        "type": "integer",
                        "description": f"采样数量，默认 1，上限 {MAX_COUNT}",
                        "default": 1
                    }
                },
                "required": ["lambd"]
            }
        ),
        Tool(
            name="gamma",
            description=f"Gamma(α,β) 分布。{NOT_CSPRNG}",
            inputSchema={
                "type": "object",
                "properties": {
                    "alpha": {"type": "number", "description": "形状参数 α，必须 > 0"},
                    "beta": {"type": "number", "description": "尺度参数 β，必须 > 0"},
                    "n": {
                        "type": "integer",
                        "description": f"采样数量，默认 1，上限 {MAX_COUNT}",
                        "default": 1
                    }
                },
                "required": ["alpha", "beta"]
            }
        ),
        Tool(
            name="beta",
            description=f"Beta(α,β) 分布，值域 (0,1)。{NOT_CSPRNG}",
            inputSchema={
                "type": "object",
                "properties": {
                    "alpha": {"type": "number", "description": "形状参数 α，必须 > 0"},
                    "beta": {"type": "number", "description": "形状参数 β，必须 > 0"},
                    "n": {
                        "type": "integer",
                        "description": f"采样数量，默认 1，上限 {MAX_COUNT}",
                        "default": 1
                    }
                },
                "required": ["alpha", "beta"]
            }
        ),
        Tool(
            name="lognormal",
            description=f"对数正态分布 LogNormal(μ,σ)。{NOT_CSPRNG}",
            inputSchema={
                "type": "object",
                "properties": {
                    "mu": {"type": "number", "description": "对数均值 μ，默认 0", "default": 0},
                    "sigma": {"type": "number", "description": "对数标准差 σ，必须 > 0，默认 1", "default": 1},
                    "n": {
                        "type": "integer",
                        "description": f"采样数量，默认 1，上限 {MAX_COUNT}",
                        "default": 1
                    }
                }
            }
        ),
        Tool(
            name="randint",
            description=f"离散均匀分布整数，[a,b] 闭区间。{NOT_CSPRNG}",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "integer", "description": "下界（含）"},
                    "b": {"type": "integer", "description": "上界（含）"},
                    "n": {
                        "type": "integer",
                        "description": f"采样数量，默认 1，上限 {MAX_COUNT}",
                        "default": 1
                    }
                },
                "required": ["a", "b"]
            }
        ),
        # ── NumPy 分布 ──
        Tool(
            name="binomial",
            description=(
                f"二项分布 Binomial(n_trials,p)。"
                f"{'⚠️ 需要 NumPy。' if not _NUMPY_AVAILABLE else ''}"
                f"{NOT_CSPRNG}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "n_trials": {"type": "integer", "description": "试验次数，必须 > 0"},
                    "p": {"type": "number", "description": "每次试验成功概率，[0,1]"},
                    "n": {
                        "type": "integer",
                        "description": f"采样数量，默认 1，上限 {MAX_COUNT}",
                        "default": 1
                    }
                },
                "required": ["n_trials", "p"]
            }
        ),
        Tool(
            name="poisson",
            description=(
                f"泊松分布 Poisson(λ)。"
                f"{'⚠️ 需要 NumPy。' if not _NUMPY_AVAILABLE else ''}"
                f"{NOT_CSPRNG}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lam": {"type": "number", "description": "速率参数 λ，必须 >= 0"},
                    "n": {
                        "type": "integer",
                        "description": f"采样数量，默认 1，上限 {MAX_COUNT}",
                        "default": 1
                    }
                },
                "required": ["lam"]
            }
        ),
        Tool(
            name="chi_squared",
            description=(
                f"卡方分布 χ²(k)。"
                f"{'⚠️ 需要 NumPy。' if not _NUMPY_AVAILABLE else ''}"
                f"{NOT_CSPRNG}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "k": {"type": "integer", "description": "自由度，必须 > 0"},
                    "n": {
                        "type": "integer",
                        "description": f"采样数量，默认 1，上限 {MAX_COUNT}",
                        "default": 1
                    }
                },
                "required": ["k"]
            }
        ),
    ]
    return tools


# ══════════════════════════════════════════════════════════════════
# MCP 工具调用分发
# ══════════════════════════════════════════════════════════════════

_DISPATCH = {
    "uniform":       (lambda args: _sample_uniform(_parse_count(args)),           {},                     "uniform"),
    "std_normal":    (lambda args: _sample_std_normal(_parse_count(args)),        {},                     "std_normal"),
    "uniform_range": (lambda args: _sample_uniform_range(args["a"], args["b"], _parse_count(args)),
                      {"a": "a", "b": "b"},                                       "uniform_range"),
    "normal":        (lambda args: _sample_normal(
                          args.get("mu", 0), args.get("sigma", 1), _parse_count(args)),
                      {"mu": "mu", "sigma": "sigma"},                             "normal"),
    "bernoulli":     (lambda args: _sample_bernoulli(args["p"], _parse_count(args)),
                      {"p": "p"},                                                 "bernoulli"),
    "exponential":   (lambda args: _sample_exponential(args["lambd"], _parse_count(args)),
                      {"lambd": "lambd"},                                         "exponential"),
    "gamma":         (lambda args: _sample_gamma(args["alpha"], args["beta"], _parse_count(args)),
                      {"alpha": "alpha", "beta": "beta"},                         "gamma"),
    "beta":          (lambda args: _sample_beta(args["alpha"], args["beta"], _parse_count(args)),
                      {"alpha": "alpha", "beta": "beta"},                         "beta"),
    "lognormal":     (lambda args: _sample_lognormal(
                          args.get("mu", 0), args.get("sigma", 1), _parse_count(args)),
                      {"mu": "mu", "sigma": "sigma"},                             "lognormal"),
    "randint":       (lambda args: _sample_randint(args["a"], args["b"], _parse_count(args)),
                      {"a": "a", "b": "b"},                                       "randint"),
    "binomial":      (lambda args: _sample_binomial(args["n_trials"], args["p"], _parse_count(args)),
                      {"n_trials": "n_trials", "p": "p"},                         "binomial"),
    "poisson":       (lambda args: _sample_poisson(args["lam"], _parse_count(args)),
                      {"lam": "lam"},                                             "poisson"),
    "chi_squared":   (lambda args: _sample_chi_squared(args["k"], _parse_count(args)),
                      {"k": "k"},                                                 "chi_squared"),
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name not in _DISPATCH:
        raise ValueError(f"未知工具: {name}")

    sampler, param_keys, dist_name = _DISPATCH[name]

    try:
        values = sampler(arguments)
    except (ValueError, RuntimeError) as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "distribution": dist_name},
            ensure_ascii=False
        ))]

    n = arguments.get("n", 1)
    params = {label: arguments[key] for label, key in param_keys.items()}
    return [TextContent(type="text", text=_format(dist_name, params, values, n))]


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
