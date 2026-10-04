#!/usr/bin/env python3
"""
文件名: functional_test.py
摘要: mcp-cronopios 功能级测试 —— 真起 stdio MCP 服务器，发 JSON-RPC，验证工具真的能返回结果。
      与 smoke_test.py 分工：smoke 是零依赖的静态结构检查；本文件要真装依赖并真跑进程。
      fast_filesystem 的占位符守卫会拒启动，故测试时自动生成"占位符已填"的临时副本。
依赖: mcp（各 server 依赖）；按件另有 sympy / mpmath / numpy

用法:
    python3 tests/functional_test.py
（用装了上述依赖的解释器跑；sys.executable 会被用来拉起各 server 子进程。）
"""
from __future__ import annotations

import json
import select
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVERS = ROOT / "servers"
PY = sys.executable
TIMEOUT = 30


class Probe:
    """极简 MCP stdio 客户端：换行分隔的 JSON-RPC。"""

    def __init__(self, script: Path):
        self.p = subprocess.Popen(
            [PY, str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._id = 0

    def _write(self, obj: dict) -> None:
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def _read(self, timeout: int = TIMEOUT) -> dict:
        ready, _, _ = select.select([self.p.stdout], [], [], timeout)
        if not ready:
            err = ""
            try:
                err = self.p.stderr.read(600)
            except OSError:
                pass
            raise TimeoutError(f"服务器 {timeout}s 无响应；stderr={err!r}")
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("服务器进程已退出")
        return json.loads(line)

    def request(self, method: str, params: dict) -> dict:
        self._id += 1
        i = self._id
        self._write({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        while True:
            msg = self._read()
            if msg.get("id") == i:
                return msg

    def start(self) -> dict:
        r = self.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "cronopios-test", "version": "0"}})
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return r

    def tools(self) -> list[str]:
        res = self.request("tools/list", {}).get("result", {})
        return [t["name"] for t in res.get("tools", [])]

    def text(self, name: str, args: dict) -> str:
        r = self.request("tools/call", {"name": name, "arguments": args})
        if "error" in r:
            raise RuntimeError(f"{name} RPC 错误: {r['error']}")
        res = r["result"]
        if res.get("isError"):
            raise RuntimeError(f"{name} 返回 isError: {res}")
        return res["content"][0]["text"]

    def close(self) -> None:
        for fn in (self.p.stdin.close,
                   lambda: (self.p.terminate(), self.p.wait(timeout=5))):
            try:
                fn()
            except Exception:
                pass
        try:
            self.p.kill()
        except Exception:
            pass


# ── 各件测试 ──────────────────────────────────────────────────────

def test_calculator():
    p = Probe(SERVERS / "mcp_calculator.py")
    try:
        p.start()
        assert "calculate" in p.tools(), p.tools()
        out = p.text("calculate", {"expression": "sqrt(2)", "dps": 30})
        assert "1.4142135623730950488016887242" in out, out
        # ln 是 log 的别名；log(x) 默认自然对数（底 e）
        a = p.text("calculate", {"expression": "ln(10)", "dps": 25})
        b = p.text("calculate", {"expression": "log(10)", "dps": 25})
        assert a == b, (a, b)
        assert "2.302585092994045684017991" in a, a
        # 结构化错误（不是崩溃）
        out = p.text("calculate", {"expression": "1 +"})
        assert "error" in out.lower() or "错误" in out, out
    finally:
        p.close()


def test_entropia():
    p = Probe(SERVERS / "mcp_entropia.py")
    try:
        p.start()
        ts = p.tools()
        assert len(ts) == 13, f"应 13 个工具，实得 {len(ts)}: {ts}"
        d = json.loads(p.text("uniform", {"n": 3}))
        assert len(d["values"]) == 3, d
        d = json.loads(p.text("chi_squared", {"k": 4, "n": 2}))
        assert len(d["values"]) == 2, d
        d = json.loads(p.text("randint", {"a": 10, "b": 5}))   # 边界：应报错不静默
        assert "error" in d, d
    finally:
        p.close()


def test_eqsolve():
    p = Probe(SERVERS / "mcp_eqsolve.py")
    try:
        p.start()
        ts = p.tools()
        for t in ("declare_vars", "add_equation", "solve", "clear", "list_vars", "list_eqs"):
            assert t in ts, f"缺 {t}: {ts}"
        r = p.text("declare_vars", {"variables_json":
                   '[{"name":"x","type":"real","domain":"R","is_parameter":false}]'})
        assert "错误" not in r, r
        r = p.text("add_equation", {"expr_str": "Eq(2*x + 1, 5)"})
        assert "✓" in r or "已添加" in r, r
        sol = p.text("solve", {})
        assert "2" in sol, sol
    finally:
        p.close()


def test_pysyntaxcheck():
    p = Probe(SERVERS / "mcp_pysyntaxcheck.py")
    try:
        p.start()
        assert "check_syntax" in p.tools(), p.tools()
        with tempfile.TemporaryDirectory() as td:
            good = Path(td) / "good.py"
            good.write_text("x = 1\n", encoding="utf-8")
            bad = Path(td) / "bad.py"
            bad.write_text("def f(:\n", encoding="utf-8")
            r = json.loads(p.text("check_syntax", {"filepath": str(good)}))
            assert r["valid"] is True, r
            r = json.loads(p.text("check_syntax", {"filepath": str(bad)}))
            assert r["valid"] is False, r
    finally:
        p.close()


def test_fast_filesystem():
    """占位符守卫会拒启动 → 生成"占位符已填"的临时副本再测。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = (SERVERS / "fast_filesystem.py").read_text(encoding="utf-8")
        for ph in ("<root-1>", "<root-2>", "<root-3>", "<search-1>"):
            src = src.replace(f'"{ph}"', json.dumps(str(td)))
        script = td / "_fastfs_copy.py"
        script.write_text(src, encoding="utf-8")

        p = Probe(script)
        try:
            p.start()
            ts = p.tools()
            assert "cronopio_write" in ts and "cronopio_read" in ts, ts
            f = td / "hello.txt"
            p.text("cronopio_write", {"path": str(f), "content": "alpha\nbeta\n", "mode": "overwrite"})
            out = p.text("cronopio_read", {"path": str(f)})
            assert "alpha" in out and "beta" in out, out
            out = p.text("cronopio_ls", {"path": str(td)})
            assert "hello.txt" in out, out
            out = p.text("cronopio_roots", {})
            assert str(td) in out, out
        finally:
            p.close()


def _preflight() -> bool:
    try:
        __import__("mcp")
        return True
    except ImportError:
        print(f"⚠️  当前解释器 {PY} 没有 mcp —— 功能测试需要它。")
        print("   （静态检查请改用 tests/smoke_test.py，那个零依赖。）")
        return False


def main() -> int:
    if not _preflight():
        return 2
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:                       # noqa: BLE001 —— 测试运行器需要汇总所有失败
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
