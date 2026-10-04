# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp[cli]>=1.9.0",
# ]
# ///

"""
文件名: mcp_pysyntaxcheck.py
摘要: Python 语法检查 MCP 服务器。只检查语法（含编码声明、PEP 263），
      不检查 import 是否能找到、不检查运行时错误、不检查 API 是否过时。
依赖: mcp
"""

import ast
import os
import sys
import traceback
import json
from pathlib import Path

# ───── mcp 1.x / 2.x 兼容：FastMCP 改名（由 tools/mcp2_migrate.py 生成）─────
# mcp 2.x 把 FastMCP 改名为 MCPServer（构造参数与 .tool()/.run() 均兼容）。
try:
    from mcp.server.fastmcp import FastMCP  # mcp 1.x
except ModuleNotFoundError:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
# ───── 兼容层结束 ─────

server = FastMCP("mcp_pysyntaxcheck")


def _detect_encoding(path: str) -> tuple[str, str | None]:
    """检测文件编码和 PEP 263 声明。

    返回 (检测到的编码, PEP 263 声明行原文) 或 None。
    """
    # 先读前 2 行看 PEP 263 声明
    try:
        with open(path, "rb") as f:
            raw = f.read(256)
    except (FileNotFoundError, PermissionError, IsADirectoryError) as e:
        return ("utf-8", None, str(e))

    # 查找 PEP 263 编码声明
    import re
    encoding_decl = None
    head_text = raw.decode("latin-1")
    for line in head_text.splitlines()[:2]:
        m = re.search(r"coding[=:]\s*([-\w.]+)", line)
        if m:
            encoding_decl = m.group(1)
            break

    detected = encoding_decl or "utf-8"
    # 规范化编码名
    try:
        import codecs
        detected = codecs.lookup(detected).name
    except LookupError:
        detected = "utf-8"

    return (detected, encoding_decl, None)


def _check_syntax(path: str) -> dict:
    """核心：用 ast.parse 做纯语法检查。"""
    path_obj = Path(path)

    # --- 文件存在性检查 ---
    if not path_obj.exists():
        return {
            "valid": False,
            "error": {
                "message": f"文件不存在: {path}",
                "line": None,
                "col": None,
                "text": None,
            },
            "interpreter": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({sys.executable})",
        }

    if path_obj.is_dir():
        return {
            "valid": False,
            "error": {
                "message": f"路径是目录，不是文件: {path}",
                "line": None,
                "col": None,
                "text": None,
            },
            "interpreter": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({sys.executable})",
        }

    # --- 编码检测 ---
    encoding, decl_line, err = _detect_encoding(path)
    if err:
        return {
            "valid": False,
            "error": {
                "message": f"读取文件失败: {err}",
                "line": None,
                "col": None,
                "text": None,
            },
            "interpreter": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({sys.executable})",
        }

    # --- 读文件 ---
    try:
        with open(path, "r", encoding=encoding) as f:
            source = f.read()
    except UnicodeDecodeError:
        return {
            "valid": False,
            "error": {
                "message": f"编码检测为 {encoding} 但解码失败。文件可能是二进制或非 UTF-8 文本。",
                "line": None,
                "col": None,
                "text": None,
            },
            "interpreter": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({sys.executable})",
        }
    except Exception as e:
        return {
            "valid": False,
            "error": {
                "message": f"读取文件异常: {e}",
                "line": None,
                "col": None,
                "text": None,
            },
            "interpreter": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({sys.executable})",
        }

    # --- 空文件是合法的 Python ---
    if not source.strip():
        return {
            "valid": True,
            "error": None,
            "interpreter": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({sys.executable})",
            "encoding": encoding,
        }

    # --- 语法检查 ---
    try:
        ast.parse(source, filename=path)
    except SyntaxError as e:
        line = e.lineno
        col = e.offset
        text = e.text.rstrip("\n") if e.text else None
        msg = str(e.msg) if e.msg else "语法错误"
        return {
            "valid": False,
            "error": {
                "line": line,
                "col": col,
                "message": msg,
                "text": text,
            },
            "interpreter": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({sys.executable})",
        }
    except Exception as e:
        return {
            "valid": False,
            "error": {
                "line": None,
                "col": None,
                "message": f"ast.parse 异常: {traceback.format_exc()}",
                "text": None,
            },
            "interpreter": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({sys.executable})",
        }

    return {
        "valid": True,
        "error": None,
        "interpreter": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({sys.executable})",
        "encoding": encoding,
    }


@server.tool()
async def check_syntax(filepath: str) -> str:
    """检查指定 Python 文件的语法。

    ── 能力边界（本工具能做什么） ──
    ✅ 语法错误：缩进、配对标点、关键字拼写、f-string 格式等
    ✅ 编码声明：PEP 263（# -*- coding: xxx -*-），自动检测非 UTF-8 文件
    ✅ Python 版本差异：用当前解释器的规则检查
    ✅ 空文件 / 纯 shebang / 纯注释：均视为合法
    ✅ 大文件：不限大小，但一次读入内存

    ── 能力边界（本工具不做的事） ──
    ❌ 不检查 import 能否找到（模块不存在、拼写错误、版本不匹配）
    ❌ 不检查运行时错误（TypeError、AttributeError、ValueError 等）
    ❌ 不检查 API 是否过时或被删除
    ❌ 不执行任何用户代码（compile/code object 不运行）
    ❌ 不检查类型注解的类型正确性（那是类型检查器的事）

    一句话：只管「这个东西在 Python 语法上成不成立」——不管它跑不跑得通。

    ── 输出格式 ──
    成功：
        {"valid": true, "error": null, "interpreter": "3.12.x (/usr/bin/python3)", "encoding": "utf-8"}
    失败：
        {"valid": false, "error": {"line": 42, "col": 13, "message": "...", "text": "..."},
         "interpreter": "3.12.x (...)", "encoding": "utf-8"}
    """
    result = _check_syntax(filepath)
    return json.dumps(result, ensure_ascii=False)


def main():
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
