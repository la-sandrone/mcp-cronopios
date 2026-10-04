#!/usr/bin/env python3
"""
文件名: smoke_test.py
摘要: mcp-cronopios 发布件冒烟测试。零第三方依赖（不 import mcp/sympy/mpmath），
      只做静态结构检查，因此在未安装任何 MCP 依赖的环境里也能跑。
      覆盖：语法可编译 / 必备文件 / 工具名前缀 / fast_filesystem 配置占位符与守卫。
依赖: 标准库

用法:
    python3 tests/smoke_test.py     # 直接跑
    pytest tests/                    # 或交给 pytest
"""
from __future__ import annotations

import ast
import py_compile
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVERS = ROOT / "servers"
FASTFS = SERVERS / "fast_filesystem.py"

EXPECTED_SERVERS = [
    "fast_filesystem.py", "mcp_calculator.py", "worker.py",
    "mcp_entropia.py", "mcp_eqsolve.py", "mcp_pysyntaxcheck.py",
]

# fast_filesystem 的 13 个工具名（发布后一律 cronopio_ 前缀）
EXPECTED_TOOLS = [
    "cronopio_read", "cronopio_write", "cronopio_grep", "cronopio_grepdir",
    "cronopio_find", "cronopio_context", "cronopio_ls", "cronopio_stat",
    "cronopio_gc", "cronopio_copy", "cronopio_scan", "cronopio_convert",
    "cronopio_roots",
]


def test_servers_present():
    """六个 server 文件都在。"""
    missing = [f for f in EXPECTED_SERVERS if not (SERVERS / f).is_file()]
    assert not missing, f"缺 server 文件: {missing}"


def test_servers_compile():
    """所有 server 文件语法可编译。"""
    with tempfile.TemporaryDirectory() as td:
        for f in EXPECTED_SERVERS:
            p = SERVERS / f
            py_compile.compile(str(p), cfile=str(Path(td) / (f + "c")), doraise=True)


def test_servers_have_docstring():
    """每个 server 文件都有模块级 docstring（保持自文档纪律）。"""
    for f in EXPECTED_SERVERS:
        tree = ast.parse((SERVERS / f).read_text(encoding="utf-8"))
        assert (ast.get_docstring(tree) or "").strip(), f"{f} 缺模块 docstring"


def test_fastfs_tool_prefix():
    """fast_filesystem 的 13 个工具名都带 cronopio_ 前缀，且无旧前缀残留。"""
    text = FASTFS.read_text(encoding="utf-8")
    assert ("my" + "filesystem") not in text, "残留旧前缀（改名没跑干净）"
    for t in EXPECTED_TOOLS:
        assert f'"{t}"' in text, f"缺工具名 {t}"


def test_fastfs_config_placeholder():
    """fast_filesystem 的安全边界是占位符 + 有启动守卫。"""
    text = FASTFS.read_text(encoding="utf-8")
    assert '"<root-1>"' in text, "ALLOWED_ROOTS 缺占位符"
    assert '"<search-1>"' in text, "SEARCHABLE_DIRS 缺占位符"
    assert "_UNFILLED" in text and "SystemExit" in text, "缺占位符启动守卫"


def test_license_present():
    """LICENSE 是 AGPL-3.0。"""
    lic = ROOT / "LICENSE"
    assert lic.is_file(), "缺 LICENSE"
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in lic.read_text(encoding="utf-8")


def _run_all():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as e:
            print(f"  FAIL {name}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
