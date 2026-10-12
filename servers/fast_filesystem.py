#!/usr/bin/env python3
"""
MCP filesystem server — precise, fast, safe.  v4.8

v4.0 墓碑删除体系 (FAT32‑inspired tombstone + GC)
─────────────────────────────────────────────────
  核心原则：行号是坐标系，换行符是坐标网格。不许偷渡换行符。
  唯一导致行号漂移的是换行符进入 at_line——字节长度变化不影响行号。

  铁律（不容商量）：
    1. 单行替换：at_line + replace=true → content 不含任何换行符。字节任意变。
    2. 墓碑删除：mode='delete_lines' → 编码层非法字节等长填充，行数不变。
    3. 尾续追加：mode='append' → 唯一允许新增行的非全量操作。
    4. 全量重建：mode='overwrite' → 不接受 at_line，永远替换整个文件。
    5. 垃圾回收：cronopio_gc → 唯一物理移除墓碑行的操作。
    6. 墓碑透明：读取时墓碑行显示为 [DELETED]。at_line 替换即复活，不限等长。
    7. 行号失效：任何导致行号改变的操作后，旧行号全部作废，请重新计算。

Design principles (inherited from v3.x):
  - WSL-adapted (this project was born in WSL): slow-search guard for /mnt/*
    (drvfs is slow across filesystems) + Windows <-> WSL path normalization.
    Inert on native Linux / macOS. See docs/DEPLOY.md.
  - ALLOWED_ROOTS:   security boundary (read/write/copy) — broad
  - SEARCHABLE_DIRS: search scope (cronopio_find) — narrow, fast
  - Default non-recursive (flat scandir), recursive = explicit opt-in
  - Unified offset semantics: 0-based, negative = from end
  - Two-phase confirmation for destructive writes
  - Newline preservation: 萧规曹随 (follow original file style, empty → LF)
  - ALL tool names carry 'cronopio_' prefix — prevents confusion with AI client built-ins

行号口径 (v4.8) — 按工具名对齐上游，不强行统一
──────────────────────────────────────────────────────────────
  原则：**工具名就是接口契约**。叫 grep 的就按 grep 的口径，叫 read/write 的
  按数组索引的口径。模型对同名工具有极强的条件反射式先验，扭曲它 =
  制造"名字承诺 A、行为交付 B"的陷阱。

  1) 报告行号（给人看、给 grep 生态比对）→ **1-based，对齐上游 grep**
       cronopio_grep / cronopio_grepdir / cronopio_scan
       · 实测口径依据：GNU grep 3.12 对第 3 行输出 `3:charlie`；rg 15.2 同为 `3:`；
         sed 行地址 / awk NR / perl $. / gcc 诊断行号，一律 1-based。
       · scan 的 `detail_lines` 输入参数**同为 1-based**——输出里看到的行号
         可原样填回，零换算。

  2) 行索引参数（当索引用、进数组）→ **0-based，对齐 C/C++ 数组**
       read 的 offset_lines / write 的 at_line：第一行 = 0，负数从尾倒数。
       这些值经 _resolve_line_offset 直接成为 `lines[i]` 的 i。
       给它们套 1-based 会破坏真实索引语义，故**不动**。

  ⚠️ 混合口径的使用注意：
    · grep/scan 报的行号 → 直接喂 cronopio_grep / scan 的 detail_lines：**零换算**
    · grep/scan 报的行号 → 喂 read 的 offset_lines / write 的 at_line：**需要 -1**
    · 内部实现全部 0-based（Python 列表下标），1-based 只在**对外输出/输入**处 ±1。
      兑换点集中在少数几行，有回归网逐条守着。

Tools (v4.0):
  cronopio_find     — glob search with dir/recursive/max_depth control
  cronopio_context  — set session-level CWD for subsequent searches
  cronopio_grep     — grep on LOCAL/WSL filesystem files
  cronopio_grepdir  — directory grep (rg engine, 4 file types)
                      ⚠️ NOT the AI client's attachment-file search!
                      NFC‑normalised matching, encoding‑aware
  cronopio_read     — read with offset_lines/offset_bytes (mutually exclusive)
                      encoding‑aware, tombstone lines → [DELETED]
  cronopio_write    — v4.0 铁律写入（见上文）
  cronopio_gc       — 垃圾回收：物理移除墓碑行
  cronopio_copy     — cp with overwrite flag
  cronopio_roots    — show security + search whitelists
  cronopio_stat     — stat() metadata (size, mtime, ctime, encoding, newline)
  cronopio_ls       — ls/dir with mtime + size + type
  cronopio_scan     — scan .md files for # ░ 对话索引 ░ / # 对话索引 blocks

v4.6 新增 ZIP 容器支持 (DSH Session Log 导出归档)
─────────────────────────────────────────────────
  DSH 上多模态后，/export 与「下载 Session 日志」不再给裸 jsonl，而给 ZIP：
    dsh-session-<id>.zip > session[.vN].jsonl  +  media/sha256:<hex>.<ext>
  官方规范见 packages/session-query/session-log-export（archive.ts + README.zh.md）。

  实现路线（不求巧，只求复用）：解压到私有 /tmp 临时目录 → 走既有的真实文件代码
  路径（scan / read / grep 全部不变）→ finally 无条件清理。不新增任何并行解析器。

  分类不靠解压，靠 ZIP 中央目录（零解压，~3ms）：
    有条目匹配 ^session(?:\\.vN)?\\.jsonl$  → 再读该成员首行确认 type=="session"
  任一级失败都显式报（generic-zip 只列清单不装懂；corrupt 直接 fail-loud）。

  安全：临时文件名恒用 mNNNN 自铸名，绝不采信成员名 ⇒ zip-slip 结构性不可能；
       且 DSH 的 media 成员名自带冒号（sha256:），采信它会咬坏 rg 输出解析。
  守卫：中央目录声明尺寸预筛（免费）+ 逐成员实际读取计数（权威，防 zip bomb）。

v4.7 编码层：BOM 显式建模 + 无 BOM 判定策略 + 墓碑字节表修复
─────────────────────────────────────────────────────────────
  修三处：墓碑字节表查表全 miss（退化成空格 ⇒ 真实全空格行被误判 + gc 误删真实
  内容，数据丢失级）；BOM 未显式建模（overwrite 丢 BOM）；写内容不守文件换行约定 +
  换行探测只读 8KB（首行超长即翻车）。

  口径（跟随 Windows 习惯）：
    · BOM 优先；无 BOM 则 **UTF-8 严格校验** → 通过即 utf-8，否则 **gb18030**
    · 统计检测器（chardet / charset-normalizer）**只作旁证**，分歧写进「说明」不翻案
    · **绝不自动采信无 BOM 的 UTF-16/32**（经典「联通」问题即此：GBK 字节
      b'\xc1\xaa\xcd\xa8' 是两个已分配 CJK 码点的 UTF-16LE 编码，Notepad 据此误采）；
      改用 NUL 字节判据主动报警
    · UTF-16/32 **写强制补 BOM**（Windows 消费端没 BOM 必乱码）
    · 目标编码不可表示时 **fail-loud**，绝不静默 replace 成 '?'
  · _detect_style() 给完整样式报告（BOM / 换行计数与混用 / 检测旁证与分歧 / 告警）
"""

import os
import json
import codecs
import subprocess
import fnmatch
import shutil
import zipfile
import tempfile
import contextlib
import asyncio; import re
import sys
import unicodedata
from datetime import datetime, timezone

import chardet

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

# ═══════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════

ALLOWED_ROOTS: list[str] = [
    "<root-1>",   # 例：/home/yourname     (Linux / WSL)
    "<root-2>",   # 例：C:/Projects        (Windows)
    "<root-3>",   # 例：/tmp               (不需要可删)
]

SEARCHABLE_DIRS: list[str] = [
    "<search-1>",   # 只读搜索范围，通常比 ALLOWED_ROOTS 窄
]

# ── 占位符守卫：未填写则拒绝启动 ──────────────────────────────
# 宁可 fail-loud，也不留一个"静默允许任意目录"的默认值。
_UNFILLED = [r for r in (*ALLOWED_ROOTS, *SEARCHABLE_DIRS) if r.startswith("<")]
if _UNFILLED:
    raise SystemExit(
        "mcp-cronopios: 请先填写本文件顶部的 ALLOWED_ROOTS / SEARCHABLE_DIRS，"
        "当前仍为占位符：" + ", ".join(_UNFILLED)
    )

SEARCH_TIMEOUT: int = 30
CURRENT_SEARCH_DIR: str | None = None

server = Server("fast-filesystem")

# ═══════════════════════════════════════════════════════════
#  Encoding property layer — 单一真相源
#
#  v4.7 修正三处同层缺陷：
#   1) _TOMBSTONE_BYTE 查表**全 miss**：查询键做了归一化（去短横线），表键却留着
#      短横线 → 除 gb18030/gbk/big5 外全部落到 fallback。后果：UTF-8 的墓碑字节
#      退化成**空格**（合法字符）⇒ 真实的全空格行被读成 [DELETED]，gc 会
#      把真实内容当墓碑物理删除（静默数据丢失）。改为 _norm_enc 统一两侧。
#   2) BOM 显式建模：读要剥（否则行首留幽灵 U+FEFF）、写要补（overwrite 会丢 BOM）。
#      此前 BOM 语义靠 codec 隐式承载（utf-8-sig / utf-16），一旦换成显式字节序
#      codec（utf-16-le）就全丢。现改为「codec 一律无 BOM 语义 + BOM 单独承载」。
#   3) 无 BOM 的编码判定改为**确定性策略**（跟随 Windows 习惯）：UTF-8 严格校验
#      通过 → utf-8；否则 → gb18030。统计检测器（chardet / charset-normalizer）
#      退居旁证，只写进「说明」，不翻案。
#
#  为什么不用统计检测器主判：经典「联通」问题 —— GBK 的 b'\xc1\xaa\xcd\xa8'
#  不是合法 UTF-8（0xC1 永远非法），但它是两个**已分配的 CJK 码点**
#  （U+AAC1 / U+A8CD）的 UTF-16LE 编码，Notepad 的启发式就据此采信了 UTF-16
#  ⇒ 显示乱码。根因是「无 BOM 的 UTF-16 被启发式误采」，故本层规定：
#  **绝不自动采信无 BOM 的 UTF-16/32，只报警**。
# ═══════════════════════════════════════════════════════════

def _norm_enc(name: str) -> str:
    """编码名归一化：小写 + 去分隔符。**表键与查询键必须都过它**，否则查表必 miss。"""
    return name.lower().replace("-", "").replace("_", "").replace(" ", "")


# 墓碑字节 = 该编码下**永远不可能出现在正文里**的字节序列。
# 归一化键 → (墓碑字节, 规范 codec 名, 是否 UTF-8 家族, 是否强制要求 BOM)
_ENCODING_TABLE: dict[str, tuple[bytes, str, bool, bool]] = {
    _norm_enc('utf-8'):     (b'\xFF',            'utf-8',     True,  False),
    _norm_enc('utf-8-sig'): (b'\xFF',            'utf-8',     True,  False),  # BOM 单独承载
    _norm_enc('utf-16-le'): (b'\xFF\xFF',        'utf-16-le', False, True),   # U+FFFF noncharacter
    _norm_enc('utf-16-be'): (b'\xFF\xFF',        'utf-16-be', False, True),
    _norm_enc('utf-16'):    (b'\xFF\xFF',        'utf-16-le', False, True),   # 裸名按 LE 规范
    _norm_enc('utf-32-le'): (b'\xFF\xFF\xFF\xFF', 'utf-32-le', False, True),
    _norm_enc('utf-32-be'): (b'\xFF\xFF\xFF\xFF', 'utf-32-be', False, True),
    _norm_enc('utf-32'):    (b'\xFF\xFF\xFF\xFF', 'utf-32-le', False, True),
    _norm_enc('gb18030'):   (b'\xFF',            'gb18030',   False, False),  # GB18030 不用 0xFF
    _norm_enc('gbk'):       (b'\xFF',            'gb18030',   False, False),
    _norm_enc('gb2312'):    (b'\xFF',            'gb18030',   False, False),
    _norm_enc('big5'):      (b'\xFF',            'big5',      False, False),
    _norm_enc('shift_jis'): (b'\xFF',            'shift_jis', False, False),
    _norm_enc('euc-kr'):    (b'\xFF',            'euc-kr',    False, False),
    _norm_enc('latin-1'):   (b'\x7F',            'latin-1',   False, False),  # DEL
    _norm_enc('iso-8859-1'):(b'\x7F',            'latin-1',   False, False),
    _norm_enc('windows-1252'): (b'\x7F',         'latin-1',   False, False),
}

_TOMBSTONE_FALLBACK: bytes = b''    # 编码未知时**不给墓碑**（空格会与正文碰撞，绝不能再兜底成空格）

# BOM 表：顺序敏感——UTF-32LE(FF FE 00 00) 必须先于 UTF-16LE(FF FE) 判定
_BOM_TABLE: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_BE, 'utf-32-be'),   # 00 00 FE FF
    (codecs.BOM_UTF32_LE, 'utf-32-le'),   # FF FE 00 00
    (codecs.BOM_UTF8,     'utf-8'),       # EF BB BF
    (codecs.BOM_UTF16_BE, 'utf-16-be'),   # FE FF
    (codecs.BOM_UTF16_LE, 'utf-16-le'),   # FF FE
)

# 无 BOM 时的兜底编码：跟随 Windows 习惯（简体中文 ANSI = CP936）。
# 文档口径：无 BOM 且不是合法 UTF-8 ⇒ 一律按 GB18030 处理（GBK 的超集，更安全）。
_NO_BOM_FALLBACK = 'gb18030'

# UTF-8 严格校验的窗口上限（超出部分不校验，报告里会注明）
_UTF8_VALIDATE_CAP = 4 * 1024 * 1024
# 换行探测窗口（v4.7 从 8KB 提上来：首行 >8KB 的 CRLF 文件此前会被误判为 LF）
_NL_PROBE_MAX = 1024 * 1024

# ── zstd 读取（v4.8）────────────────────────────────────────────
# 后端优先级：zstd/zstdcat CLI（自文档、不引入 import 副作用）
#            → zstandard 模块（**实测本机 3.12 venv 已装**；标准库 compression.zstd 需 3.14+）
#            → 都没有则 fail-loud（绝不当成未压缩文件静默读出二进制垃圾）
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"          # zstd 帧魔数（小端 0xFD2FB528）
_ZSTD_PY_CHUNK = 1 << 16                    # 回退路径的分块粒度，避免一次性物化整个解压结果
# zstandard 无此属性名的老版本会在调用「返回整块」的 API 时只读首帧——
# 那是多帧文件（DSH 会话日志就是多帧）最典型的坑，详见 _zstd_read_python 的注释。


def _get_tombstone_byte(encoding: str) -> bytes:
    """该编码的墓碑字节；未知编码返回 b''（**不再兜底成空格**）。"""
    entry = _ENCODING_TABLE.get(_norm_enc(encoding))
    return entry[0] if entry else _TOMBSTONE_FALLBACK


def _canonical_enc(name: str) -> str:
    """规范 codec 名（无 BOM 语义）。未收录的编码原样小写返回。"""
    entry = _ENCODING_TABLE.get(_norm_enc(name))
    return entry[1] if entry else name.lower()


def _is_utf8_family(name: str) -> bool:
    entry = _ENCODING_TABLE.get(_norm_enc(name))
    if entry:
        return entry[2]
    return _norm_enc(name) in ("utf8", "utf8sig", "ascii")


def _requires_bom(name: str) -> bool:
    """该编码是否**强制要求** BOM（UTF-16/32：Windows 消费端没 BOM 必乱码）。"""
    entry = _ENCODING_TABLE.get(_norm_enc(name))
    return bool(entry and entry[3])


def _detect_bom(head: bytes) -> bytes:
    """返回文件开头的 BOM 字节（无则 b''）。

    codecs 的坑：BOM / BOM_LE / BOM_UTF16_LE / BOM32_LE 是**同一个对象** b'\\xff\\xfe'
    （三个不同概念共用一个值），所以只能靠长度 + 后继字节消歧，故本表顺序敏感。
    """
    for bom, _enc in _BOM_TABLE:
        if head.startswith(bom):
            return bom
    return b""


def _bom_encoding(bom: bytes) -> str | None:
    for b, enc in _BOM_TABLE:
        if bom == b:
            return enc
    return None


def _native_bom(enc: str) -> bytes:
    """某编码的标准 BOM（用于「强制补 BOM」）。"""
    canon = _canonical_enc(enc)
    for bom, name in _BOM_TABLE:
        if name == canon:
            return bom
    return b""


def _encode_report(text: str, enc: str) -> tuple[bytes | None, str]:
    """按 enc 编码；不可表示时**拒绝**并报出具体字符（绝不静默 replace 成 '?'）。

    Returns (bytes, "") 或 (None, 错误说明)。
    """
    try:
        return text.encode(enc), ""
    except LookupError:
        return None, f"❌ 未知编码: {enc}"
    except UnicodeEncodeError as exc:
        bad = text[exc.start:exc.end]
        return None, (
            f"❌ 目标编码 {enc} 无法表示这些字符（位置 {exc.start}）: {bad!r} "
            f"= {[hex(ord(c)) for c in bad]}\n"
            f"→ 已拒绝写入（绝不静默替换成 '?'）。请改用能表示它们的编码"
            f"（如 utf-8 / utf-16），或先改写内容。"
        )


def _write_bytes_with_bom(path: str, payload: bytes, bom: bytes) -> None:
    """原子性写：BOM（若有）在前，正文在后。"""
    with open(path, "wb") as fh:
        if bom:
            fh.write(bom)
        fh.write(payload)


# ═══════════════════════════════════════════════════════════
#  Security & helpers
# ═══════════════════════════════════════════════════════════

def _is_allowed(path: str) -> bool:
    for root in ALLOWED_ROOTS:
        if path.startswith(root):
            return True
    return False


def _newline_stats(text: str) -> dict:
    """数换行：CRLF / 裸 LF / 裸 CR。**不能用 str.splitlines()**——它按 Unicode
    行边界切，\\v \\f \\x85 \\u2028 \\u2029 都会被算成换行，数字会虚高。"""
    crlf = text.count("\r\n")
    rest = text.replace("\r\n", "")
    return {"crlf": crlf, "lf": rest.count("\n"), "cr": rest.count("\r")}


def _dominant_newline(stats: dict) -> str:
    """按出现次数取主换行（空文件/无换行 → LF）。"""
    if stats["crlf"] == 0 and stats["lf"] == 0 and stats["cr"] == 0:
        return "\n"
    best = max(("crlf", "lf", "cr"), key=lambda k: stats[k])
    return {"crlf": "\r\n", "lf": "\n", "cr": "\r"}[best]


def _is_mixed_newline(stats: dict) -> bool:
    return sum(1 for k in ("crlf", "lf", "cr") if stats[k] > 0) > 1


def _probe_text(path: str, encoding: str, cap: int = _NL_PROBE_MAX) -> str | None:
    """读最多 cap 字节并解码（用于换行探测/旁证），失败返回 None。"""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(cap)
    except (FileNotFoundError, OSError):
        return None
    try:
        return chunk.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return None


def _detect_newline(path: str, encoding: str | None = None) -> str:
    """检测换行约定，返回主换行符。

    v4.7：探测窗口 8KB → 1MB。此前首行超 8KB 的 CRLF 文件会被误判为 LF，
    再经 at_line 替换写回就把裸 LF 混进 CRLF 文件（实测复现）。
    """
    if encoding is None:
        encoding, _ = _detect_encoding(path)
    text = _probe_text(path, _canonical_enc(encoding))
    if text is None:
        return "\n"
    return _dominant_newline(_newline_stats(text))


def _strict_utf8_ok(path: str, cap: int = _UTF8_VALIDATE_CAP) -> tuple[bool, bool]:
    """严格 UTF-8 校验（增量，遇首个非法字节立即否定）。

    Returns (是否合法, 是否受 cap 截断)。
    绝不用「逐个 codec 试 decode、看有没有 U+FFFD」的梯子法——实测 GBK 字节会被
    错误 codec「成功」解码，错误编码可能解码成功。
    """
    dec = codecs.getincrementaldecoder("utf-8")()
    read = 0
    try:
        with open(path, "rb") as fh:
            while read < cap:
                chunk = fh.read(1 << 20)
                if not chunk:
                    return (True, False)
                read += len(chunk)
                try:
                    dec.decode(chunk)
                except UnicodeDecodeError:
                    return (False, False)
    except OSError:
        return (True, False)
    return (True, True)


def _nul_hint(head: bytes) -> str:
    """无 BOM 但含 NUL 字节 ⇒ 疑似 BOM-less UTF-16/32（确定性信号）。

    UTF-16/32 文本几乎必有 0x00 字节；GB18030 / UTF-8 文本不用 NUL。
    按策略不会被自动采信（联通问题就是这么来的），但必须**主动报警**。

    ⚠️ **有 BOM 时本判据无意义**——BOM 已把编码定死，此时还报「疑似无 BOM 的
    UTF-16」是自相矛盾的假警报（真机输出里就出现过）。故判据自带前提检查，
    调用方无需记得先判 BOM，也就无法误用。
    """
    if _detect_bom(head):
        return ""
    n = head.count(0)
    if n == 0:
        return ""
    return (f"⚠️ 检出 {n} 个 NUL 字节但无 BOM：疑似**无 BOM 的 UTF-16/UTF-32**。"
            f"按策略以 {_NO_BOM_FALLBACK} 处理（Windows 消费端上无 BOM 的 UTF-16 必乱码，"
            f"故本工具不自动采信）。若确为 UTF-16 请显式指定 encoding。")


def _detector_note(head: bytes, chosen: str) -> str:
    """旁证：chardet + charset-normalizer。**只写说明，不翻案。**

    短样本上两者都不可靠（这是本质困难）：实测 GBK 被 charset-normalizer 判成
    big5、latin-1 判成 cp1006；chardet 则给低置信度的 GB18030 / Windows-1253。
    所以它们只用来提示分歧，绝不用来定案。
    """
    found: list[tuple[str, str]] = []
    try:
        det = chardet.detect(head) or {}
        if det.get("encoding"):
            found.append(("chardet", f"{det['encoding']}({det.get('confidence') or 0:.2f})"))
    except Exception:
        pass
    try:
        from charset_normalizer import from_bytes as _cn
        best = _cn(head).best()
        if best is not None:
            found.append(("charset-normalizer",
                          best.encoding + ("+BOM" if getattr(best, "byte_order_mark", False) else "")))
    except Exception:
        pass
    if not found:
        return ""

    chosen_n = _norm_enc(chosen)

    def agrees(name: str) -> bool:
        n = _norm_enc(name)
        if n.startswith("utf8"):
            return chosen_n.startswith("utf8")
        if n.startswith("utf16"):
            return chosen_n.startswith("utf16")
        if n.startswith("utf32"):
            return chosen_n.startswith("utf32")
        if n in ("gb18030", "gbk", "gb2312", "cp936"):
            return chosen_n in ("gb18030", "gbk", "gb2312", "cp936")
        if n in ("cp1252", "windows1252", "latin1", "iso88591"):
            return chosen_n in ("latin1", "cp1252")
        return n == chosen_n

    joined = "，".join(f"{src}={name}" for src, name in found)
    bad = [f"{src}={name}" for src, name in found if not agrees(name.split("(")[0])]
    if bad:
        return (f"检测旁证: {joined}  ⚠️ 分歧（{', '.join(bad)}）"
                f"——策略结果 {chosen} 为准，可用 encoding= 强制")
    return f"检测旁证: {joined}（与策略一致）"


def _detect_encoding(path: str) -> tuple[str, bool]:
    """检测编码 → (规范 codec 名, 是否 UTF-8 家族)。codec 名**无 BOM 语义**，
    BOM 请用 _detect_bom() 单独取（要回写就得有字节）。

    顺序（确定性，跟随 Windows 习惯）：
      1. 有 BOM → 按 BOM 定
      2. 无 BOM → UTF-8 严格校验：通过 → utf-8
      3. 仍不通过 → gb18030（文档口径：无 BOM 且非 UTF-8 ⇒ 按 GBK 处理）
    统计检测器（chardet / charset-normalizer）只作旁证（见 _detector_note）。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except (FileNotFoundError, OSError):
        return ("utf-8", True)

    bom = _detect_bom(head)
    if bom:
        enc = _bom_encoding(bom)
        if enc:
            return (enc, _is_utf8_family(enc))

    ok, _truncated = _strict_utf8_ok(path)
    if ok:
        return ("utf-8", True)
    return (_NO_BOM_FALLBACK, False)


def _detect_style(path: str) -> dict:
    """完整样式报告（stat / convert 共用）。字段：

        exists / size / encoding / is_utf8 / bom / bom_name / requires_bom
        newline / nl_stats / nl_mixed / lines / notes[]
    """
    st: dict = {
        "exists": False, "size": 0, "encoding": "utf-8", "is_utf8": True,
        "bom": b"", "bom_name": None, "requires_bom": False,
        "newline": "\n", "nl_stats": {"crlf": 0, "lf": 0, "cr": 0},
        "nl_mixed": False, "lines": 0, "notes": [],
    }
    if not os.path.isfile(path):
        return st
    st["exists"] = True
    st["size"] = os.path.getsize(path)

    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError as exc:
        st["notes"].append(f"读取失败: {exc}")
        return st

    bom = _detect_bom(head)
    enc, is_utf8 = _detect_encoding(path)
    st.update({
        "encoding": enc, "is_utf8": is_utf8, "bom": bom,
        "bom_name": _bom_encoding(bom), "requires_bom": _requires_bom(enc),
    })

    if bom:
        st["notes"].append(f"BOM 已检出: {bom!r} → {st['bom_name']}")
    elif _requires_bom(enc):
        st["notes"].append("⚠️ 编码要求 BOM 但文件无 BOM——写入时将强制补上")

    nul = _nul_hint(head)
    if nul:
        st["notes"].append(nul)

    note = _detector_note(head, enc)
    if note:
        st["notes"].append(note)

    text = _probe_text(path, enc)
    if text is not None:
        stats = _newline_stats(text)
        st["nl_stats"] = stats
        st["newline"] = _dominant_newline(stats)
        st["nl_mixed"] = _is_mixed_newline(stats)
        if st["nl_mixed"]:
            st["notes"].append(
                f"⚠️ 换行符**混用**: CRLF×{stats['crlf']} 裸LF×{stats['lf']} 裸CR×{stats['cr']}"
                f"（主换行 {st['newline']!r}）——建议用 convert 归一")
        st["lines"] = text.count("\n") + text.count("\r") - text.count("\r\n") + (1 if text and not text.endswith(("\n", "\r")) else 0)
    return st


def _style_report(path: str) -> str:
    """_detect_style 的文本渲染（stat / convert 共用）。"""
    st = _detect_style(path)
    if not st["exists"]:
        return ""
    ns = st["nl_stats"]
    bom_txt = f"{st['bom']!r}" + (f" → {st['bom_name']}" if st["bom_name"] else "")
    if not st["bom"]:
        bom_txt += "（无）"
    nl_txt = f"CRLF×{ns['crlf']}  裸LF×{ns['lf']}  裸CR×{ns['cr']}"
    nl_txt += (f"  ⚠️ 混用（主 {st['newline']!r}）" if st["nl_mixed"]
               else f"  ← 主 {st['newline']!r}")
    lines = [
        f"\n── 样式报告 ──",
        f"编码: {st['encoding']}"
        + ("（UTF-8 家族）" if st["is_utf8"] else "")
        + ("  ⚠️ 该编码强制要求 BOM" if st["requires_bom"] else ""),
        f"BOM: {bom_txt}",
        f"换行: {nl_txt}",
        f"行结束符总数: {ns['crlf'] + ns['lf'] + ns['cr']}（探测窗口 {_NL_PROBE_MAX // 1024}KB 内）",
    ]
    for n in st["notes"]:
        lines.append(f"  {n}")
    return "\n".join(lines)


# ── 转码 / 转换行（convert 工具）────────────────────────────────

def _nl_literal(name: str) -> str | None:
    """'lf' / 'crlf' / 'cr' / 'keep' → 字面换行符（None = keep）。"""
    key = name.strip().lower()
    return {"lf": "\n", "\n": "\n", "crlf": "\r\n", "cr": "\r"}.get(key)


def _convert_newlines(text: str, target: str) -> str:
    """换行归一化到 target。先全部收成 LF，再展开——顺序不可颠倒。"""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    return t if target == "\n" else t.replace("\n", target)


def _convert_v1(arguments: dict) -> list[TextContent]:
    """cronopio_convert：编码 / 换行 / BOM 转换，兼检测报告。

    不传任何目标（或全为 keep）→ 只回检测报告，不动文件。
    传了目标 → 两阶段：confirm=false 预览，confirm=true 执行。

    三条硬约束（fail-loud，绝不静默降级）：
      1. 目标编码不可表示正文 → 拒绝（绝不 replace 成 '?'）
      2. 文件含墓碑行 → 拒绝（墓碑字节是编码层的非法字节，转码会破坏这个不变式；
         请先 cronopio_gc）
      3. 行结束符总数**必须不变**（转换只改形态，不改行数——行号是坐标系）
    """
    path: str = _normalize_path(arguments["path"])
    if not _is_allowed(path):
        return [TextContent(type="text", text=f"路径不在安全白名单内: {path}")]
    if not os.path.isfile(path):
        return [TextContent(type="text", text=f"不是文件: {path}")]

    to_enc_arg = (arguments.get("to_encoding") or "").strip()
    to_nl_arg = (arguments.get("to_newline") or "keep").strip().lower()
    to_bom_arg = (arguments.get("to_bom") or "keep").strip().lower()
    from_enc_arg = (arguments.get("from_encoding") or "").strip()
    confirmed = bool(arguments.get("confirm", False))

    if to_nl_arg not in ("keep", "lf", "crlf", "cr"):
        return [TextContent(type="text", text=f"❌ to_newline 只能是 keep/lf/crlf/cr，收到 {to_nl_arg!r}")]
    if to_bom_arg not in ("keep", "add", "strip"):
        return [TextContent(type="text", text=f"❌ to_bom 只能是 keep/add/strip，收到 {to_bom_arg!r}")]

    # ── 源编码：默认按策略检测，可用 from_encoding 强制 ──
    if from_enc_arg:
        src_enc = _canonical_enc(from_enc_arg)
        src_src = f"强制指定 {from_enc_arg!r}"
    else:
        src_enc, _ = _detect_encoding(path)
        src_src = "按策略检测（BOM → UTF-8 严格校验 → gb18030）"
    src_bom = b""

    with open(path, "rb") as fh:
        raw = fh.read()
    head0 = raw[:4096]
    src_bom = _detect_bom(raw[:8])
    if src_bom and raw.startswith(src_bom):
        raw = raw[len(src_bom):]

    # ── 源编码不确定守卫（**只在真要转换时触发**；仅检测模式照常出报告）──
    #   无 BOM 但含 NUL ⇒ 极可能是无 BOM 的 UTF-16/32，而策略按 Windows 习惯把它
    #   当 GB18030。危险点：**UTF-16 的字节流往往能被 GB18030 完整解码**
    #   （GBK 两字节覆盖极广，这正是「联通」式歧义的反向形态）——于是转码会
    #   **悄无声息地把乱码固化下来**。故要求先显式声明源编码。
    no_target_yet = (not to_enc_arg and to_nl_arg == "keep" and to_bom_arg == "keep")
    # _nul_hint 自带「有 BOM 则返回空」的前提检查，故此处无需再判 src_bom
    if (not from_enc_arg) and _nul_hint(head0) and not no_target_yet:
        return [TextContent(type="text", text=(
            f"❌ 拒绝转换：源编码不确定，拒绝固化可能的乱码。\n"
            f"{_nul_hint(head0)}\n"
            f"原因：该字节流虽含 NUL，但**也能被 {src_enc} 完整解码**（GBK 两字节覆盖极广），"
            f"所以「解码成功」在这里不代表猜对了——转换会把错位文本永久固化。\n"
            f"→ 若确为 UTF-16/32，请用 from_encoding=\"utf-16-le\"（或 be / utf-32）重试；"
            f"若确实是 {src_enc}，也请用 from_encoding=\"{src_enc}\" 显式确认。"
            f"{_style_report(path)}"))]

    # ── 墓碑守卫（必须**先于解码**：墓碑字节本身就是非法字节，解码必炸，
    #     若放在解码之后就会被解码错误抢先，用户看到的是误导性的「检测错了」）──
    _, _, _, ts_count, _ = _read_file_with_tombstones(path)
    if ts_count > 0:
        return [TextContent(type="text", text=(
            f"❌ 拒绝转换：文件含 {ts_count} 行墓碑（[DELETED]）。\n"
            f"墓碑字节是该编码下的**非法字节序列**，转码会把它们当正文重新编码，"
            f"破坏墓碑不变式（此后 read 不再认得它们、gc 也回收不了）。\n"
            f"→ 请先 cronopio_gc 物理清除墓碑，再转换。"
            f"{_style_report(path)}"))]

    # ── 目标 ──
    tgt_enc = _canonical_enc(to_enc_arg) if to_enc_arg else src_enc
    tgt_nl = _nl_literal(to_nl_arg) or _detect_newline(path, src_enc)
    if _requires_bom(tgt_enc):
        # UTF-16/32：写强制 BOM（Windows 消费端没 BOM 必乱码）
        if to_bom_arg == "strip":
            return [TextContent(type="text", text=(
                f"❌ 拒绝：{tgt_enc} 强制要求 BOM（Windows 消费端无 BOM 必乱码），"
                f"不能用 to_bom='strip'。\n"
                f"若确实要无 BOM 的 UTF-16，请改用能表达该意图的编码名"
                f"（如 utf-16-le 也无济于事——本工具对 UTF-16/32 一律补 BOM）。"))]
        tgt_bom = _native_bom(tgt_enc)
    else:
        if to_bom_arg == "add":
            tgt_bom = _native_bom(tgt_enc) or src_bom
            if not tgt_bom:
                return [TextContent(type="text", text=(
                    f"❌ {tgt_enc} 没有标准 BOM，to_bom='add' 无意义。"))]
        elif to_bom_arg == "strip":
            tgt_bom = b""
        else:                                    # keep
            tgt_bom = src_bom if _norm_enc(src_enc) == _norm_enc(tgt_enc) else b""

    # ── 解码（源编码严格；解不开说明检测错了，fail-loud 让人用 from_encoding=）──
    try:
        text = raw.decode(src_enc)
    except UnicodeDecodeError as exc:
        return [TextContent(type="text", text=(
            f"❌ 按 {src_enc}（{src_src}）无法解码该文件: {exc}\n"
            f"→ 检测可能错了。请用 from_encoding= 显式指定源编码重试。"))]
    except LookupError:
        return [TextContent(type="text", text=f"❌ 未知源编码: {src_enc}")]

    src_stats = _newline_stats(text)
    src_terminators = src_stats["crlf"] + src_stats["lf"] + src_stats["cr"]
    new_text = _convert_newlines(text, tgt_nl)
    tgt_stats = _newline_stats(new_text)
    tgt_terminators = tgt_stats["crlf"] + tgt_stats["lf"] + tgt_stats["cr"]

    # ── 硬约束 3：行数不变（先算后写，双保险）──
    if src_terminators != tgt_terminators:
        return [TextContent(type="text", text=(
            f"❌ 内部错误：换行转换改变了行结束符总数 "
            f"({src_terminators} → {tgt_terminators})。已中止，文件未动。"))]

    payload, enc_err = _encode_report(new_text, tgt_enc)
    if payload is None:
        return [TextContent(type="text", text=enc_err + _style_report(path))]

    no_target = (not to_enc_arg and to_nl_arg == "keep" and to_bom_arg == "keep")

    # ── 检测模式：只报告 ──
    if no_target:
        return [TextContent(type="text", text=(
            f"文件: {path}\n"
            f"（未指定任何转换目标 → 仅检测，未改动文件）"
            f"{_style_report(path)}\n"
            f"\n→ 如需转换，传 to_encoding / to_newline / to_bom 之一。"))]

    # ── 两阶段 ──
    if not confirmed:
        return [TextContent(type="text", text=(
            f"⚠️ 转换预览（尚未执行）\n"
            f"文件: {path}\n"
            f"编码: {src_enc} → {tgt_enc}"
            + ("（未变）" if _norm_enc(src_enc) == _norm_enc(tgt_enc) else "") + "\n"
            f"BOM: {src_bom!r} → {tgt_bom!r}\n"
            f"换行: {_dominant_newline(src_stats)!r} → {tgt_nl!r}（主换行）\n"
            f"      CRLF×{src_stats['crlf']} 裸LF×{src_stats['lf']} 裸CR×{src_stats['cr']}"
            f"  →  CRLF×{tgt_stats['crlf']} 裸LF×{tgt_stats['lf']} 裸CR×{tgt_stats['cr']}\n"
            f"行结束符总数: {src_terminators} → {tgt_terminators}（行号坐标系不变）\n"
            f"字节数: {os.path.getsize(path):,} → {len(tgt_bom) + len(payload):,}\n"
            f"源编码依据: {src_src}\n"
            f"{_style_report(path)}\n"
            f"\n如确认转换，请追加 confirm=true 后重新调用。"))]

    before_size = os.path.getsize(path)
    _write_bytes_with_bom(path, payload, tgt_bom)

    # ── 回读校验（断言，不是祈祷）──
    with open(path, "rb") as fh:
        rb = fh.read()
    rb_bom = _detect_bom(rb[:8])
    body = rb[len(rb_bom):] if rb_bom else rb
    try:
        rtext = body.decode(tgt_enc)
    except UnicodeDecodeError as exc:
        return [TextContent(type="text", text=(
            f"❌ 回读失败：写入后按 {tgt_enc} 解不开（{exc}）。\n"
            f"文件可能已损坏，大小 {len(rb):,} 字节。请人工检查: {path}"))]
    rstats = _newline_stats(rtext)
    rterm = rstats["crlf"] + rstats["lf"] + rstats["cr"]

    checks = [
        ("BOM", rb_bom == tgt_bom),
        ("行结束符总数", rterm == src_terminators),
        ("主换行", _dominant_newline(rstats) == tgt_nl),
        ("文本还原", rtext == _convert_newlines(text, tgt_nl)),
    ]
    bad = [n for n, okk in checks if not okk]
    head = f"转换完成: {path}\n" if not bad else f"⚠️ 转换已完成但回读校验有异常: {path}\n"
    return [TextContent(type="text", text=(
        head
        + f"编码: {src_enc} → {tgt_enc}\n"
        + f"BOM: {src_bom!r} → {rb_bom!r}\n"
        + f"换行: CRLF×{rstats['crlf']} 裸LF×{rstats['lf']} 裸CR×{rstats['cr']}"
          f"（主 {_dominant_newline(rstats)!r}）\n"
        + f"行结束符总数: {src_terminators} → {rterm}（不变 ✅）\n"
        + f"字节数: {before_size:,} → {len(rb):,}\n"
        + f"回读校验: " + (" ".join(f"{n}✅" for n, _ in checks) if not bad
                            else "❌ " + " ".join(f"{n}❌" for n in bad))))]


# ── File I/O with tombstone awareness ──

def _split_lines_exact(raw: bytes, nl_bytes: bytes) -> list[bytes]:
    """按换行字节切行，**丢弃「尾换行切出的空尾元素」**（幽灵行）。

    raw.split(nl) 对「以换行符结尾」的文件会多切出一个 b""：
    10 行的文件 -> 11 个元素。那是坐标网格线的产物，不是内容行
    ——readlines() / splitlines() 都不计它，行数会凭空多 1。

    对坐标系的两个危害（实测）：
      · read/grep 自报「共 11 行」，与 write 路径（readlines()=10）**口径打架**
      · 幽灵行能被搜到：query='^$' 会命中它，还带 at_line=10 的合法写入目标

    保留语义边界：
      · b""      -> []            （空文件 = 0 行）
      · b"a\\n"   -> [b"a"]        （1 行）
      · b"\\n"    -> [b""]         （1 个空行）
      · b"a"     -> [b"a"]        （无尾换行，原样）
    仅剥一个空尾元素；文件真的以「空行」结尾时（b"a\\n\\n" -> [b"a", b""]）不受影响。
    """
    out = raw.split(nl_bytes)
    if len(out) > 1 and out[-1] == b"":
        out.pop()
    return out


def _read_file_with_tombstones(path: str) -> tuple[list[str], str, bool, int, int]:
    """Read *path*, rendering tombstone lines as '[DELETED]' + newline.

    Detection works at the **byte level** — each raw line (split by the file's
    newline bytes) is checked: if *all* bytes equal the encoding's tombstone
    byte, the line is a tombstone.

    v4.7：行首 BOM **从显示里剥掉**（否则第一行会带一个隐形 U+FEFF，read/grep/scan
    全被它污染）。只影响显示——剥 BOM 不改变换行数量，行号坐标系不变；
    写路径（delete_lines / gc）走 _read_raw_lines，BOM 原样保留。

    v4.8：行切分改走 _split_lines_exact，**不再切出尾换行导致的幽灵空行**
    （10 行文件过去报 11 行，且 '^$' 能搜到那个幽灵行）。total_lines 现与
    readlines()/splitlines() 一致，也与 write 路径（readlines()）同一坐标系。

    Returns:
        display_lines   — list of decoded lines (tombstones → '[DELETED]\n')
        encoding        — detected encoding name
        is_utf8         — True if UTF-8 family
        tombstone_count — number of tombstone lines detected
        total_lines     — total physical line count
    """
    enc, is_utf8 = _detect_encoding(path)
    tombstone = _get_tombstone_byte(enc)
    nl_text = _detect_newline(path, enc)

    with open(path, "rb") as fh:
        raw = fh.read()

    bom = _detect_bom(raw[:8])
    if bom and raw.startswith(bom):
        raw = raw[len(bom):]          # 见 docstring：剥掉行首 BOM，只影响显示

    nl_bytes = nl_text.encode(enc)
    raw_lines = _split_lines_exact(raw, nl_bytes)

    display_lines: list[str] = []
    tombstone_count = 0

    for raw_line in raw_lines:
        if _is_raw_tombstone_line(raw_line, tombstone):
            display_lines.append("[DELETED]" + nl_text)
            tombstone_count += 1
        else:
            try:
                decoded = raw_line.decode(enc, errors="replace")
            except (LookupError, UnicodeDecodeError):
                decoded = raw_line.decode("utf-8", errors="replace")
            display_lines.append(decoded + nl_text)

    return display_lines, enc, is_utf8, tombstone_count, len(raw_lines)


def _is_raw_tombstone_line(raw: bytes, tombstone: bytes) -> bool:
    """Check whether *raw* (without newline) is a pure tombstone line.

    v4.7：tombstone 为空（编码未知）时**恒返回 False**。此前未知编码兜底成空格，
    导致真实的全空格行被当成墓碑（read 显示 [DELETED]、gc 直接删真实内容）。
    """
    if not raw or not tombstone:
        return False
    if len(tombstone) == 1:
        return all(b == tombstone[0] for b in raw)
    ts_len = len(tombstone)
    if len(raw) % ts_len != 0:
        return False
    for i in range(0, len(raw), ts_len):
        if raw[i:i + ts_len] != tombstone:
            return False
    return True


def _read_raw_lines(path: str, enc: str, nl_text: str) -> list[bytes]:
    """Read *path* as raw bytes, split by newline bytes (no decoding).

    Returns list of raw line bytes (newline stripped from each).

    v4.8：走 _split_lines_exact，**不再多出尾换行切出的空尾元素**——否则
    delete_lines 的 at_line 索引空间会比 read/grep 报出的行号多一行，
    两个坐标系错位（反向 off-by-one）。

    调用方若要把结果 join 回文件，**必须自己补回尾换行**（见 delete_lines / gc）：
    本函数只保证"行内容"清单，尾换行的形状由调用方显式重建。
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    nl_bytes = nl_text.encode(enc)
    return _split_lines_exact(raw, nl_bytes)


def _resolve_line_offset(total_lines: int, offset_lines: int, *, clamp_to: str = "start") -> int:
    if offset_lines >= 0:
        idx = offset_lines
    else:
        idx = total_lines + offset_lines

    if clamp_to == "start":
        if idx < 0:
            idx = 0
    elif clamp_to == "end":
        if idx < 0:
            idx = 0
        if idx > total_lines:
            idx = total_lines
    return idx


def _resolve_search_dir(dir_arg: str) -> str:
    if dir_arg in ("", "."):
        if CURRENT_SEARCH_DIR is None:
            existing = [d for d in SEARCHABLE_DIRS if os.path.isdir(d)]
            raise ValueError(
                "未指定搜索目录且未设置当前工作目录。\n"
                "请指定 dir 参数，或先调用 cronopio_context 设置工作目录。\n"
                "可搜索目录:\n" + "\n".join(f"  {d}" for d in existing)
            )
        return CURRENT_SEARCH_DIR

    if dir_arg.startswith("/"):
        if not _is_allowed(dir_arg):
            raise ValueError(
                f"路径不在安全白名单内: {dir_arg}\n"
                f"允许的目录: {', '.join(ALLOWED_ROOTS)}"
            )
        if not os.path.isdir(dir_arg):
            raise ValueError(f"目录不存在: {dir_arg}")
        return dir_arg

    for base in SEARCHABLE_DIRS:
        candidate = os.path.join(base, dir_arg)
        if os.path.isdir(candidate):
            return candidate

    existing = [d for d in SEARCHABLE_DIRS if os.path.isdir(d)]
    raise ValueError(
        f"在可搜索目录中未找到 '{dir_arg}'。\n"
        f"可搜索目录:\n" + "\n".join(f"  {d}" for d in existing)
    )


# ── Search helpers ──

def _search_flat(dirpath: str, pattern: str, max_results: int) -> list[str]:
    results: list[str] = []
    try:
        with os.scandir(dirpath) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False) and fnmatch.fnmatch(entry.name, pattern):
                    results.append(entry.path)
                    if len(results) >= max_results:
                        break
    except (PermissionError, OSError):
        pass
    return results


def _search_recursive(dirpath: str, pattern: str, max_results: int, max_depth: int) -> list[str]:
    results: list[str] = []
    base_depth = dirpath.rstrip("/").count("/")
    for root, dirs, files in os.walk(dirpath):
        current_depth = root.rstrip("/").count("/") - base_depth
        if max_depth >= 0 and current_depth > max_depth:
            dirs.clear()
            continue
        for f in files:
            if fnmatch.fnmatch(f, pattern):
                results.append(os.path.join(root, f))
                if len(results) >= max_results:
                    return results
    return results


def _search_collect(dirpath: str, pattern: str, recursive: bool,
                    max_depth: int, max_results: int) -> list[str]:
    if recursive:
        return _search_recursive(dirpath, pattern, max_results, max_depth)
    return _search_flat(dirpath, pattern, max_results)


# ═══════════════════════════════════════════════════════════
#  Tool definitions
# ═══════════════════════════════════════════════════════════

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # ── 1. cronopio_find ───────────────────────────
        Tool(
            name="cronopio_find",
            description=(
                "⚠️ 此工具操作你的 WSL/Windows 本地文件系统，不要与 AI 客户端内置功能混淆！\n"
                "\n"
                "快速搜索文件（glob 模式）。\n"
                "默认只搜一层（flat scandir），比 os.walk 快数百倍。\n"
                "需指定 dir（搜索起始目录），AI 应先 cronopio_context 或每次显式传 dir。\n"
                "递归搜索请显式设置 recursive=true。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "glob 模式，如 *.tex、**/*.bib、*毕业论文*"
                    },
                    "dir": {
                        "type": "string",
                        "description": (
                            "搜索起始目录。\n"
                            "  相对路径 → 在 SEARCHABLE_DIRS 下查找，如 '虚拟机/WSL工具'\n"
                            "  绝对路径 → 必须在 ALLOWED_ROOTS 内\n"
                            "  空字符串或 '.' → 使用 CURRENT_SEARCH_DIR\n"
                            "未设置 CWD 且不填 dir 时返回错误 + 可用目录列表。"
                        ),
                        "default": ""
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "是否递归搜索子目录。默认 false（仅当前目录一层）。",
                        "default": False
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "递归最大深度（仅 recursive=true 时生效）。0=当前目录，-1=无限。默认 2。",
                        "default": 2
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大返回条数，默认 50",
                        "default": 50
                    }
                },
                "required": ["pattern"]
            }
        ),

        # ── 2. cronopio_context ─────────────────────
        Tool(
            name="cronopio_context",
            description=(
                "⚠️ 此工具操作你的 WSL/Windows 本地文件系统，不要与 AI 客户端内置功能混淆！\n"
                "\n"
                "设置会话级当前工作目录（CWD）。后续 cronopio_find 不填 dir 时默认使用此目录。\n"
                "类似 shell 的 cd 命令，一次设置，后续搜索都在此目录下进行。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dir": {
                        "type": "string",
                        "description": (
                            "目录路径（相对或绝对）。\n"
                            "  相对路径 → 在 SEARCHABLE_DIRS 下查找\n"
                            "  绝对路径 → 必须在 ALLOWED_ROOTS 内"
                        )
                    }
                },
                "required": ["dir"]
            }
        ),

        # ── 3. cronopio_grep ────────────────────
        Tool(
            name="cronopio_grep",
            description=(
                "⚠️ 此工具操作 WSL 本地文件系统，不要与 AI 客户端内置的附件文件搜索混淆！\n"
                "内置搜索 = 搜对话附件（fileKey），本工具 = 搜你的磁盘文件（path）。\n"
                "\n"
                "在磁盘文件中搜索关键词，返回匹配行的行号和前后 N 行上下文。\n"
                "支持从尾部向前搜索（reverse=true），特别适合查找文件末尾的对话索引！\n"
                "可通过 offset_lines + max_lines 限定搜索范围（如只搜最后 200 行）。\n"
                "行号 1-based：第 1 行 = 1（与 GNU grep -n / rg -n 同口径，"
                "直接把输出喂给 sed/awk/grep 比对无需换算）。\n"
                "⚠️ 但 read/write 的 offset_lines / at_line 是 0-based 索引，"
                "把这里报的行号喂给它们需要 -1。\n"
                "\n"
                "编码：自动检测文件编码（UTF-8 / UTF-16LE / GB18030）。\n"
                "非 UTF-8 文件透明转码搜索，输出标称原编码。\n"
                "\n"
                "搜索时自动进行 NFC 归一化（Unicode 等价字符匹配）。\n"
                "例如：搜 é 可匹配 NFD 形式的 e + 组合变音符，搜 ǖ 匹配 ü + 长音符。\n"
                "不影响输出——原文仍以文件原始字节形态呈现。\n"
                "\n"
                "墓碑行：grep 在墓碑行上不会命中——墓碑行内容为 [DELETED]，不含业务关键词。\n"
                "\n"
                "ZIP（DSH Session Log 导出归档，session[.vN].jsonl + media/…）：\n"
                "path 指向 .zip 时，按中央目录分类（零解压），只解压会话日志成员后照常搜索。\n"
                "member 不指定 → 默认根日志；指定 → 搜该成员（子会话为 subagents/<id>/session…）。\n"
                "媒体图片成员是二进制，不作文本搜索（会显式拒绝，不给乱码）。\n"
                "\n"
                "输出闸门（约 48K 计量单位；0.1.5=字节 / 0.1.7=UTF-16 码元）：\n"
                "· 上下文单行超 500 字符即截断。**命中行把窗口居中到命中处**：\n"
                "  行首锚 120 字符 + `…[中略 N B]` + 命中窗口 + `…[+N B]`（N 均为真实字节数）；\n"
                "  非命中的上下文行纯头部截断。切点一律吸附到**字素簇**边界（PyICU / UAX #29），\n"
                "  不会切断组合序列、ZWJ emoji、肤色修饰、区域指示符或天城文连字。\n"
                "· 整段超预算时，超出部分折叠为行号清单，请减小 max_results / before_context /\n"
                "  after_context 分批取。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径（必须在 ALLOWED_ROOTS 内）；也可指向 DSH 导出的 .zip"
                    },
                    "member": {
                        "type": "string",
                        "description": "可选。仅当 path 是 .zip 时生效：ZIP 成员名（如 'session.v3.jsonl' 或 'subagents/<id>/session.v3.jsonl'）。不填 = 根日志。"
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索正则（rg 语法，默认大小写不敏感；纯字符串请用 fixed_string=true）"
                    },
                    "offset_lines": {
                        "type": "integer",
                        "description": "从哪行开始搜（0-based，负数 = 从尾部倒数）。默认 0。",
                        "default": 0
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "最多搜多少行。-1 = 到末尾。默认 -1。",
                        "default": -1
                    },
                    "before_context": {
                        "type": "integer",
                        "description": "匹配行之前展示的行数，默认 0",
                        "default": 0
                    },
                    "after_context": {
                        "type": "integer",
                        "description": "匹配行之后展示的行数，默认 0",
                        "default": 0
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大返回条数，默认 10。设置 0 = 不限（仍受约 48K 输出预算闸门约束，超限部分折叠为行号清单）。",
                        "default": 10
                    },
                    "reverse": {
                        "type": "boolean",
                        "description": "从尾部向前扫描。特别适合查找文件末尾的对话索引！默认 false。",
                        "default": False
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "大小写敏感。默认 false（-i）。",
                        "default": False
                    },
                    "fixed_string": {
                        "type": "boolean",
                        "description": "把 query 当纯字符串而非正则（rg -F）。默认 false。",
                        "default": False
                    },
                    "enable_pcre2": {
                        "type": "boolean",
                        "description": "启用 PCRE2 引擎（rg --pcre2），支持 lookaround/backreference。默认 false（线性时间引擎，无回溯风险）。",
                        "default": False
                    }
                },
                "required": ["path", "query"]
            }
        ),


        # ── 3b. cronopio_grepdir（目录版）────────────────
        Tool(
            name="cronopio_grepdir",
            description=(
                "⚠️ 此工具操作 WSL 本地文件系统，不要与 AI 客户端内置的附件文件搜索混淆！\n"
                "\n"
                "在目录内按关键词/正则搜索文件内容，返回命中文件列表（含命中数、首命中行号）。\n"
                "搜索语法 = rg 正则（成熟语法），支持复杂正则与多文件检索。\n"
                "默认扫描 md/txt/json/jsonl/zip 五类文件；可用 pattern（glob）过滤。\n"
                "编码：自动检测，统一转换为 UTF-8 NFC + LF 后搜索（GBK/CRLF/NFD 文件同样可命中）。\n"
                "行号 1-based：第 1 行 = 1（与 GNU grep -n 同口径）。\n"
                "使用场景：找旧文件、恢复上下文、'哪些文件提到 XX'。\n"
                "\n"
                "ZIP：目录里的 DSH 导出归档会被自动钻取——先按中央目录分类（零解压），\n"
                "只解压会话日志成员进同一个临时目录，复用同一次 rg 调用；\n"
                "命中文件名标注为「zip名::成员名」。非 DSH 导出的普通 zip 跳过，不硬啃二进制。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dir": {
                        "type": "string",
                        "description": (
                            "搜索目录。\n"
                            "  相对路径 → 在 SEARCHABLE_DIRS 下查找\n"
                            "  绝对路径 → 必须在 ALLOWED_ROOTS 内\n"
                            "  空字符串或 '.' → 使用 CURRENT_SEARCH_DIR"
                        ),
                        "default": ""
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索正则（rg 语法，默认大小写不敏感；纯字符串请用 fixed_string=true）"
                    },
                    "pattern": {
                        "type": "string",
                        "description": "可选 glob 过滤，如 *.md、落实（*）*。默认空 = 五类扩展名（md/txt/json/jsonl/zip）。",
                        "default": ""
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回多少个命中文件。默认 10。设置 0 = 不限。",
                        "default": 10
                    },
                    "max_per_file": {
                        "type": "integer",
                        "description": "每个文件最多展示几条命中。默认 3。设置 0 = 不限。",
                        "default": 3
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "大小写敏感。默认 false。",
                        "default": False
                    },
                    "fixed_string": {
                        "type": "boolean",
                        "description": "把 query 当纯字符串而非正则。默认 false。",
                        "default": False
                    },
                    "enable_pcre2": {
                        "type": "boolean",
                        "description": "启用 PCRE2 引擎（支持 lookaround/backreference）。默认 false。",
                        "default": False
                    }
                },
                "required": ["dir", "query"]
            }
        ),

        # ── 4. cronopio_read ─────────────────────────
        Tool(
            name="cronopio_read",
            description=(
                "⚠️ 此工具操作你的 WSL/Windows 本地文件系统，不要与 AI 客户端内置功能混淆！\n"
                "\n"
                "读取文件内容。\n"
                "行模式：offset_lines + max_lines（默认：从头读 200 行）\n"
                "字节模式：offset_bytes + max_bytes\n"
                "两模式互斥，不可同时指定。\n"
                "偏移量 0-based，负数 = 从尾部倒数。\n"
                "换行符原样保留，不做转换。\n"
                "\n"
                "编码：自动检测文件编码（UTF-8 / UTF-16LE / GB18030）。\n"
                "非 UTF-8 文件透明转码为 UTF-8 后返回，在输出头标称原编码。\n"
                "\n"
                "━━━━━━ 墓碑行呈 ━━━━━━\n"
                "文件中全部字节均为墓碑字节（UTF-8: 0xFF, UTF-16: 0xFFFF, Latin-1: 0x7F）的行，\n"
                "在读取时显示为 [DELETED] 标记。\n"
                "行号与物理文件严格一致——[DELETED] 也占一行。\n"
                "你可以用 at_line 替换 [DELETED] 行以复活该行——不限字节长度，只需不含换行符。\n"
                "墓碑行不能通过 at_line 缩短或删除——要真正移除，请用 cronopio_gc。\n"
                "\n"
                "ZIP（DSH Session Log 导出归档）：\n"
                "path 指向 .zip 时——不填 member → 回条目清单（含声明尺寸与角色）供你挑；\n"
                "填 member → 解压该成员后按上面同样的行/字节模式读取。\n"
                "媒体图片成员是二进制，会显式拒绝（不给乱码）。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径（必须在 ALLOWED_ROOTS 内）；也可指向 DSH 导出的 .zip"
                    },
                    "member": {
                        "type": "string",
                        "description": "可选。仅当 path 是 .zip 时生效：ZIP 成员名（如 'session.v3.jsonl'）。不填 = 只回条目清单。"
                    },
                    "offset_lines": {
                        "type": "integer",
                        "description": "起始行（0-based，负数 = 从尾部倒数）。默认 0。与 offset_bytes 互斥。"
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "最多读取行数。默认 200；-1 = 到末尾。与 max_bytes 互斥。"
                    },
                    "offset_bytes": {
                        "type": "integer",
                        "description": "起始字节偏移（负数 = 从尾部倒数）。与 offset_lines 互斥。"
                    },
                    "max_bytes": {
                        "type": "integer",
                        "description": "最多读取字节数。默认 4096；-1 = 到末尾。与 max_bytes 互斥。"
                    }
                },
                "required": ["path"]
            }
        ),

        # ── 5. cronopio_write ────────────────────────
        Tool(
            name="cronopio_write",
            description=(
                "⚠️ 此工具操作你的 WSL/Windows 本地文件系统，不要与 AI 客户端内置功能混淆！\n"
                "\n"
                "═══════════════════════════════════════════════════════\n"
                "              铁 律 · 不 容 商 量\n"
                "═══════════════════════════════════════════════════════\n"
                "\n"
                "本工具实行严格的文件坐标系不变原则：\n"
                "\n"
                "  行号是坐标系，换行符是坐标网格。不许偷渡换行符。\n"
                "  唯一导致行号漂移的是换行符进入 at_line——字节长度变化不影响行号。\n"
                "\n"
                "1. 【单行替换铁律】\n"
                "   at_line + replace=true → content 不得包含任何换行符（\\n / \\r\\n / \\r）。\n"
                "   行的字节长度可以任意变化（变长、变短、不变）——只要不改变行数，坐标就稳定。\n"
                "   违者工具将直接拒绝，不做任何写入。\n"
                "   多行替换 → 请用 overwrite。\n"
                "\n"
                "2. 【删除铁律】\n"
                "   mode='delete_lines' → 删除的行用编码层非法字节（UTF-8: 0xFF、\n"
                "   UTF-16: 0xFFFF、Latin-1: 0x7F DEL）等长填充，绝不缩短行或行数。\n"
                "   被删除的行在 read 时显示为 [DELETED]，但物理行号永久保留。\n"
                "   仅 cronopio_gc 可物理移除墓碑行。\n"
                "\n"
                "3. 【尾续铁律】\n"
                "   append 是唯一允许新增行的非全量操作。\n"
                "   所有新行追加到文件末尾，不影响已有行号。\n"
                "\n"
                "4. 【全量铁律】\n"
                "   overwrite 不接受 at_line 参数。\n"
                "   overwrite 永远替换整个文件——要么重建一行，要么重建全部。\n"
                "   不存在\"只重写文件的一部分\"。违者责任自负。\n"
                "\n"
                "5. 【墓碑透明铁律】\n"
                "   墓碑行（全部字节为墓碑字节的行）在读取时显示为 [DELETED]，\n"
                "   保持行号与物理文件一致。\n"
                "   at_line 替换墓碑行 → 新行不再是全墓碑字节 → 自动复活。\n"
                "   复活不要求等长——新行可以是任意字节长度，只需不含换行符。\n"
                "   墓碑行变短或变长都不会影响行号——只有换行符能改变行号。\n"
                "\n"
                "6. 【行号失效警告】\n"
                "   任何导致行号改变的操作（overwrite / gc / append）后，\n"
                "   旧行号全部作废，请重新计算新的行号。\n"
                "\n"
                "═══════════════════════════════════════════════════════\n"
                "\n"
                "操作矩阵（唯一合法路径）：\n"
                "\n"
                "   单行修改   → at_line + replace=true  content=1行，无换行符\n"
                "   删除多行   → mode='delete_lines'    墓碑化，行数不变\n"
                "   新增内容   → mode='append'          唯一合法新增路径\n"
                "   全量重建   → mode='overwrite'       永远替换整个文件\n"
                "   清理墓碑   → cronopio_gc   独立工具\n"
                "\n"
                "───────────────────────────────────────────────\n"
                "\n"
                "编码策略（萧规曹随）：\n"
                "  · 不指定 encoding/newline → 自动检测并沿用（无需确认，非 UTF-8 静默写入）\n"
                "  · 指定 encoding 或 newline → 强制覆盖，需 encoding_confirmed=true 确认\n"
                "\n"
                "覆盖确认流程：\n"
                "  阶段 1 — 调用 replace=true（不填 confirmed），返回预览\n"
                "  阶段 2 — 确认无误后，追加 confirmed=true 执行写入\n"
                "\n"
                "换行符：遵循原文件风格（萧规曹随）。空白/新建文件默认 LF。\n"
                "单行替换时 content 末尾如缺换行符，自动补原文件风格的换行符，防止行粘连。\n"
                "\n"
                "───────────────────────────────────────────────\n"
                "\n"
                "常见违规（工具将直接拒绝，请勿尝试）：\n"
                "  ✗ at_line + content 含换行符          → 拒绝\n"
                "  ✗ at_line + replace=false（插入模式）  → 已移除，请用 append 或 overwrite\n"
                "  ✗ overwrite + at_line                 → 拒绝（overwrite 不接受 at_line）"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目标文件路径（必须在 ALLOWED_ROOTS 内）"
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "要写入的文本内容。\n"
                            "  at_line 模式：必须为单行，不含任何换行符。字节长度任意。末尾缺换行符时自动补。\n"
                            "  append / overwrite 模式：可包含任意行数和换行符。\n"
                            "  delete_lines 模式：忽略此参数（工具自行生成墓碑内容）。"
                        )
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["append", "overwrite", "delete_lines"],
                        "description": (
                            "写入模式（仅在 at_line 未指定时生效）。\n"
                            "  append=追加到末尾（唯一允许新增行的非全量操作）\n"
                            "  overwrite=覆盖全文件（不接受 at_line，永远替换整个文件）\n"
                            "  delete_lines=墓碑化指定行（需 at_line + count）\n"
                            "默认 append。"
                        ),
                        "default": "append"
                    },
                    "at_line": {
                        "type": "integer",
                        "description": (
                            "目标行号（0-based，负数 = 从尾部倒数）。\n"
                            "  仅用于单行替换（replace=true）或墓碑删除（mode='delete_lines'）。\n"
                            "  例：at_line=0 → 第一行；at_line=-1 → 最后一行。\n"
                            "  overwrite / append 不接受 at_line。"
                        )
                    },
                    "count": {
                        "type": "integer",
                        "description": (
                            "删除行数。仅 mode='delete_lines' 时有效。\n"
                            "从 at_line 开始，连续 count 行被墓碑化。默认 1。"
                        ),
                        "default": 1
                    },
                    "replace": {
                        "type": "boolean",
                        "description": "仅 at_line 指定时生效。true=单行替换（需两阶段确认），false=已禁用。默认 false。",
                        "default": False
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "覆盖确认。仅在 replace=true 时生效。首次调用不填（预览），确认后填 true。",
                        "default": False
                    },
                    "encoding": {
                        "type": "string",
                        "description": (
                            "强制指定写入编码（如 'gbk'、'utf-16-le'）。\n"
                            "  不填 → 检测原文件编码，沿用（萧规曹随）。\n"
                            "  填写 → 强制覆盖，需 encoding_confirmed=true。"
                        )
                    },
                    "newline": {
                        "type": "string",
                        "enum": ["\n", "\r\n", "\r"],
                        "description": (
                            "强制指定换行符。\n"
                            "  不填 → 检测原文件换行符，沿用（萧规曹随）。\n"
                            "  填写 → 强制覆盖，需 encoding_confirmed=true。"
                        )
                    },
                    "encoding_confirmed": {
                        "type": "boolean",
                        "description": "编码/换行符覆盖确认。指定了 encoding 或 newline 时首次调用不填（预览），确认后填 true。",
                        "default": False
                    }
                },
                "required": ["path", "content"]
            }
        ),

        # ── 6. cronopio_gc ────────────────────────────────
        Tool(
            name="cronopio_gc",
            description=(
                "⚠️ 此工具操作你的 WSL/Windows 本地文件系统。\n"
                "\n"
                "═══════════════════════════════════════════════════════\n"
                "\n"
                "垃圾回收：物理移除文件中的所有墓碑行（[DELETED]）。\n"
                "\n"
                "本工具是墓碑删除体系的最终环节：\n"
                "  标记删除（delete_lines） → 墓碑行保留，行号不变\n"
                "       ↓\n"
                "  垃圾回收（本工具）       → 物理移除，文件压缩\n"
                "\n"
                "执行后：\n"
                "  - 所有墓碑行被移除\n"
                "  - ⚠️ 文件行号重新排列（这是唯一合法的行号变更场景之一）\n"
                "  - 旧行号全部作废，请重新计算新的行号\n"
                "  - 返回：移除 N 行，M 行 → M-N 行，墓碑率 X%\n"
                "\n"
                "建议使用时机：每次对话结束时。\n"
                "如果文件中无墓碑行，工具直接返回\"无需 GC\"，不做写入。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目标文件路径（必须在 ALLOWED_ROOTS 内）"
                    }
                },
                "required": ["path"]
            }
        ),

        # ── 7. cronopio_copy ──────────────────────────────
        Tool(
            name="cronopio_copy",
            description=(
                "⚠️ 此工具操作你的 WSL/Windows 本地文件系统，不要与 AI 客户端内置功能混淆！\n"
                "\n"
                "复制文件从源路径到目标路径（两端都必须在 ALLOWED_ROOTS 内）。不可用于移动或删除。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "源文件路径"
                    },
                    "destination": {
                        "type": "string",
                        "description": "目标文件路径"
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "如果目标已存在是否覆盖，默认 false",
                        "default": False
                    }
                },
                "required": ["source", "destination"]
            }
        ),

        # ── 8. cronopio_roots ───────────────
        Tool(
            name="cronopio_roots",
            description=(
                "⚠️ 此工具操作你的 WSL/Windows 本地文件系统，不要与 AI 客户端内置功能混淆！\n"
                "\n"
                "列出安全白名单 (ALLOWED_ROOTS) 和搜索白名单 (SEARCHABLE_DIRS) 及当前搜索目录。"
            ),
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),

        # ── 9. cronopio_stat ──────────────────────────────
        Tool(
            name="cronopio_stat",
            description=(
                "⚠️ 此工具操作你的 WSL/Windows 本地文件系统，不要与 AI 客户端内置功能混淆！\n"
                "\n"
                "返回文件/目录的元数据：大小、修改时间 (mtime)、创建时间 (ctime)。\n"
                "不读取文件内容，仅返回 os.stat() 的关键字段。\n"
                "对于文件，附加检测编码、换行符和墓碑行计数。\n"
                "用于获取文件的保存时间、判断文件是否存在、比较文件新旧。\n"
                "关键词：stat、文件时间、mtime、文件大小、元数据。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件或目录路径（必须在 ALLOWED_ROOTS 内）"
                    }
                },
                "required": ["path"]
            }
        ),

        # ── 10. cronopio_ls ────────────────────────
        Tool(
            name="cronopio_ls",
            description=(
                "⚠️ 此工具操作你的 WSL/Windows 本地文件系统，不要与 AI 客户端内置功能混淆！\n"
                "\n"
                "列出目录内容，每项含：修改时间 (ISO 8601)、大小、类型、文件名。\n"
                "默认显示隐藏文件（. 开头条目），设置 show_hidden=false 隐藏。\n"
                "符号链接自动解析目标路径，断链标为\"疑似断链\"。\n"
                "类似 Windows 'dir' 或 Linux 'ls -la'，但输出为结构化文本。\n"
                "用于查看文件时间戳、目录结构概览、批量获取 mtime。\n"
                "关键词：目录列表、dir、ls、文件时间、mtime、目录浏览。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目录路径（必须在 ALLOWED_ROOTS 内）"
                    },
                    "pattern": {
                        "type": "string",
                        "description": "可选 glob 过滤，如 '*.md'。不填 = 全部。",
                        "default": "*"
                    },
                    "show_hidden": {
                        "type": "boolean",
                        "description": "是否显示隐藏文件（. 开头的文件/目录）。默认 true。",
                        "default": True
                    },
                },
                "required": ["path"]
            }
        ),

        # ── 11. cronopio_scan ──────────────────────────
        Tool(
            name="cronopio_scan",
            description=(
                "⚠️ 此工具操作你的 WSL/Windows 本地文件系统，不要与 AI 客户端内置功能混淆！\n"
                "\n"
                "扫描对话文件中的对话索引块（支持 md/txt/json/jsonl/zip）。\n自动按格式分流：普通文本/单文档 JSON 扫对话索引；dsh JSONL 扫 U+2591 索引区 + compaction 压缩记录。\n输出中 U+2591 已转义为 [U+2591]（防污染）。行号 1-based（对齐 grep 生态）。\n"
                "\n"
                "普通文本/单文档 JSON：\n"
                "优先匹配新版格式 `# ░ 对话索引 ░`（含 U+2591 标记），无新版命中时回退旧版宽松格式。\n"
                "从文件尾部反向扫描，默认只返回最后 2 处匹配（可设置 max_results，0=全部）。\n"
                "每个匹配附带元数据行 + 上下文行（前 3 + 后 40），足以区分索引块 vs 指令提及。\n"
                "\n"
                "dsh JSONL（DeepSeek Harness 会话日志）：\n"
                "· U+2591 索引区：候选限定 assistant/message 消息（避开 request/header 全文、tool/result 回显等污染面），\n"
                "  精确匹配 ░ 标记，倒序取最后 max_results 条（默认 2），洗掉 jsonl 结构只输出文本全文 + 行号。\n"
                "· compaction 压缩记录：type 白名单全量提取（无污染风险），含完整摘要全文。\n"
                "两区共享约 48K 的输出预算（计量单位随 DSH 版本，见输出闸门说明）：索引区优先保，\n"
                "压缩记录用剩余预算；装不下时压缩记录降级为位置+摘要开头，可用 detail_lines 按行号提取全文。\n"
                "\n"
                "detail_lines：逗号分隔 1-based 行号（可直接填输出里看到的行号），只洗这些 jsonl 行的文本输出（绕过索引/压缩扫描，\n"
                "仅对 dsh-jsonl 与 ZIP 生效：dsh 会话日志原生为 .jsonl.zstd（zstd 压缩），浏览器下载得到的是 ZIP 打包版，两者都支持；\n"
                "裸 .jsonl 亦支持。传给普通文本/单文档 JSON 会**显式提示已忽略**（不静默吞参）。\n"
                "供降级后二次提取完整摘要），如 detail_lines=\"3766,4621\"。\n"
                "\n"
                "ZIP（DSH Session Log 导出归档，session[.vN].jsonl + media/…，无 manifest）：\n"
                "先按 ZIP 中央目录分类（零解压）——有根日志 session[.vN].jsonl 才算 DSH 导出；\n"
                "只解压目标成员到私有临时目录（结束即删，媒体不落盘），再走上面的 dsh JSONL 双区扫描。\n"
                "容器头会列出条目清单 + 媒体引用校验（日志引用的附件是否都在归档里，缺则显式报警），\n"
                "并与索引区共享同一个输出预算。\n"
                "member 不指定 → 默认根日志（= 完整对话）；子会话用 subagents/<id>/session…。\n"
                "detail_lines 的行号作用域 = 本次扫描的那个成员。\n"
                "\n"
                "关键词：对话索引、索引块、压缩记录、compaction、恢复上下文、scan、zip、session 导出。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径（必须在 ALLOWED_ROOTS 内）；也可指向 DSH 导出的 .zip"
                    },
                    "member": {
                        "type": "string",
                        "description": "可选。仅当 path 是 .zip 时生效：扫描哪个 ZIP 成员（如 'session.v3.jsonl' 或 'subagents/<id>/session.v3.jsonl'）。不填 = 根日志。detail_lines 的行号作用域 = 该成员。"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "索引区返回条数（普通文本/单文档 JSON 同样适用）。默认 2（倒序取最后 N 处）。设置 0 返回全部（受约 48K 预算闸门约束）。",
                        "default": 2
                    },
                    "detail_lines": {
                        "type": "string",
                        "description": "可选。逗号分隔的 1-based 行号（如 \"3766,4621\"，与 scan 输出同口径，可直接复制）。仅对 dsh-jsonl 与 ZIP 生效：dsh 会话日志原生为 .jsonl.zstd（zstd 压缩），浏览器界面下载得到的是 ZIP 打包版，两者都支持；裸 .jsonl 亦支持。传给普通文本/单文档 JSON 时会显式提示已忽略该参数。",
                    },
                    "debug": {
                        "type": "string",
                        "enum": ["stats", "keywords"],
                        "description": "【测试期仪表，非日常使用】stats=统计事件分布、compaction 计数、被压区/当前表层字节量、以及 stream 排除的收益核算；keywords=挖出「只在被压区出现、当前表层已看不到」的候选词，供挑选回查测试的关键词。只在 dsh JSONL（含 .jsonl.zstd）上生效。",
                    }
                },
                "required": ["path"]
            }
        ),

        # ── 12. cronopio_convert ─────────────────────
        Tool(
            name="cronopio_convert",
            description=(
                "⚠️ 此工具操作你的 WSL/Windows 本地文件系统，不要与 AI 客户端内置功能混淆！\n"
                "\n"
                "检测文件样式（编码 / BOM / 换行），并可**转码 / 转换行 / 增删 BOM**。\n"
                "不传任何目标（to_encoding/to_newline/to_bom 全缺省或 keep）→ 只回检测报告，不改文件。\n"
                "传了目标 → 两阶段：先预览，确认后追加 confirm=true 执行，执行后**回读校验**。\n"
                "\n"
                "检测口径（确定性，跟随 Windows 习惯）：\n"
                "  1. 有 BOM → 按 BOM 定\n"
                "  2. 无 BOM → UTF-8 严格校验：通过则 utf-8\n"
                "  3. 仍不通过 → gb18030（无 BOM 且非 UTF-8 ⇒ 按 GBK 处理）\n"
                "  chardet / charset-normalizer 只作**旁证**，分歧写进报告但不翻案。\n"
                "  ⚠️ 绝不自动采信无 BOM 的 UTF-16/32（经典「联通」问题即此）——\n"
                "  改用 NUL 字节判据主动报警；若确为 UTF-16 请用 from_encoding= 显式指定。\n"
                "\n"
                "三条硬约束（fail-loud，绝不静默降级）：\n"
                "  · 目标编码不可表示正文 → 拒绝，并报出具体字符（绝不替换成 '?'）\n"
                "  · 文件含墓碑行 → 拒绝（墓碑是编码层非法字节，转码会破坏不变式；请先 gc）\n"
                "  · 行结束符总数必须不变（转换只改形态，不改行数——行号是坐标系）\n"
                "  · **源编码不确定时拒绝转换**：无 BOM 但含 NUL ⇒ 疑似无 BOM 的 UTF-16/32，\n"
                "    而该字节流往往**也能被 gb18030 完整解码**（GBK 两字节覆盖极广）——\n"
                "    「解码成功」不代表猜对了，转换会把乱码永久固化。此时须用 from_encoding= 显式声明。\n"
                "    （纯检测模式不受此限，照常出报告。）\n"
                "\n"
                "UTF-16 / UTF-32 **写时强制补 BOM**（Windows 消费端无 BOM 必乱码），\n"
                "故 to_bom='strip' 对它们会被拒绝。\n"
                "\n"
                "关键词：转码、换行符、CRLF、LF、BOM、编码检测、GBK、UTF-16、样式。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径（必须在 ALLOWED_ROOTS 内）"
                    },
                    "to_encoding": {
                        "type": "string",
                        "description": "可选。目标编码，如 utf-8 / utf-16 / utf-16-le / utf-16-be / gb18030 / big5 / latin-1。缺省 = 不改编码。"
                    },
                    "to_newline": {
                        "type": "string",
                        "enum": ["keep", "lf", "crlf", "cr"],
                        "description": "可选。目标换行符。keep（默认）= 不改；lf / crlf / cr = 归一化成该约定（顺带消除混用）。",
                        "default": "keep"
                    },
                    "to_bom": {
                        "type": "string",
                        "enum": ["keep", "add", "strip"],
                        "description": "可选。keep（默认）= 编码不变则沿用原 BOM；add = 加标准 BOM；strip = 去 BOM（UTF-16/32 会被拒绝）。",
                        "default": "keep"
                    },
                    "from_encoding": {
                        "type": "string",
                        "description": "可选。强制源编码（覆盖检测）。用于检测策略的已知盲区（如短 GBK 文本字节恰好构成合法 UTF-8）。"
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "两阶段确认。首次调用不填（回预览），确认后填 true 执行。默认 false。",
                        "default": False
                    }
                },
                "required": ["path"]
            }
        ),
    ]


# ═══════════════════════════════════════════════════════════
#  Tool implementations
# ═══════════════════════════════════════════════════════════

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:

    # ═══════════════════════════════════════════════════════
    #  1. cronopio_find
    # ═══════════════════════════════════════════════════════
    if name == "cronopio_find":
        pattern: str = arguments["pattern"]
        dir_arg: str = _normalize_path(arguments.get("dir", ""))
        recursive: bool = arguments.get("recursive", False)
        max_depth: int = arguments.get("max_depth", 2)
        max_results: int = arguments.get("max_results", 50)

        try:
            search_dir = _resolve_search_dir(dir_arg)
        except ValueError as exc:
            return [TextContent(type="text", text=str(exc))]

        is_slow = search_dir.startswith("/mnt/")
        slow_warning = (
            "\n⚠️ 此目录位于 Windows 文件系统（/mnt/...），drvfs 跨 FS 搜索可能较慢。\n"
            "   建议指定子目录缩小范围，例如 dir='项目名/子目录'。\n"
        ) if is_slow else ""

        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(
                    _search_collect, search_dir, pattern, recursive, max_depth, max_results
                ),
                timeout=SEARCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return [TextContent(
                type="text",
                text=(
                    f"⚠️ 搜索超时（{SEARCH_TIMEOUT}s）。{slow_warning}\n"
                    "建议：\n"
                    "  · 用 dir 参数缩小搜索范围到具体子目录\n"
                    "  · 减小 max_results\n"
                    "  · 只在必要时开启 recursive=true"
                )
            )]

        if not results:
            mode_str = f"递归 (max_depth={max_depth})" if recursive else "单层"
            return [TextContent(
                type="text",
                text=f"(无匹配文件)  [{mode_str} 搜索于 {search_dir}]{slow_warning}"
            )]

        return [TextContent(
            type="text",
            text=f"找到 {len(results)} 个文件:\n" + "\n".join(results) + slow_warning
        )]

    # ═══════════════════════════════════════════════════════
    #  2. cronopio_context
    # ═══════════════════════════════════════════════════════
    elif name == "cronopio_context":
        global CURRENT_SEARCH_DIR
        dir_arg: str = _normalize_path(arguments["dir"])
        try:
            resolved = _resolve_search_dir(dir_arg)
        except ValueError as exc:
            return [TextContent(type="text", text=str(exc))]
        CURRENT_SEARCH_DIR = resolved
        return [TextContent(
            type="text",
            text=f"当前搜索目录已设置为: {resolved}"
        )]

    # ═══════════════════════════════════════════════════════
    #  3. cronopio_grep
    # ═══════════════════════════════════════════════════════
    elif name == "cronopio_grep":
        return _grep_file_v2(arguments)
    elif name == "cronopio_grepdir":
        return _grep_files_v2(arguments)
    elif name == "cronopio_read":
        path: str = _normalize_path(arguments["path"])
        # 模式互斥检查先于一切（保持 v4.0 原有的报错优先级，也避免为冲突参数白解压 ZIP）
        _line_mode = (arguments.get("offset_lines") is not None) or (arguments.get("max_lines") is not None)
        _byte_mode = (arguments.get("offset_bytes") is not None) or (arguments.get("max_bytes") is not None)
        if _line_mode and _byte_mode:
            return [TextContent(
                type="text",
                text="offset_lines/max_lines 与 offset_bytes/max_bytes 不能同时指定。\n"
                     "行操作请用 offset_lines + max_lines；字节操作请用 offset_bytes + max_bytes。"
            )]
        if not _is_allowed(path):
            return [TextContent(type="text", text=f"路径不在安全白名单内: {path}")]
        if not os.path.isfile(path):
            return [TextContent(type="text", text=f"不是文件: {path}")]
        # ZIP 容器：解压指定成员后复用 _read_v2（不给 member 则回条目清单）
        if _is_zip_archive(path):
            return _read_zip(path, arguments)
        return _read_v2(path, arguments)

    # ═══════════════════════════════════════════════════════
    #  5. cronopio_write  (v4.0 铁律)
    # ═══════════════════════════════════════════════════════
    elif name == "cronopio_write":
        path: str = _normalize_path(arguments["path"])
        content: str = arguments.get("content", "")
        mode: str = arguments.get("mode", "append")
        at_line: int | None = arguments.get("at_line", None)
        count: int = arguments.get("count", 1)
        replace: bool = arguments.get("replace", False)
        confirmed: bool = arguments.get("confirmed", False)
        enc_override: str | None = arguments.get("encoding", None)
        nl_override: str | None = arguments.get("newline", None)
        encoding_confirmed: bool = arguments.get("encoding_confirmed", False)

        if not _is_allowed(path):
            return [TextContent(type="text", text=f"路径不在安全白名单内: {path}")]

        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

        file_exists = os.path.isfile(path)
        if not file_exists: _b=os.path.basename(path); assert _b and _b not in (".",".."), f"文件名无效: {_b}"; _n=_b.rsplit(".",1)[0].upper(); assert _n not in {"CON","PRN","AUX","NUL","COM1","COM2","COM3","COM4","COM5","COM6","COM7","COM8","COM9","LPT1","LPT2","LPT3","LPT4","LPT5","LPT6","LPT7","LPT8","LPT9"}, f"❌ 文件名是 Windows 保留名称: {_b}"; assert not any(c in _b for c in '\\/:*?"<>|'), f"❌ 文件名含 Windows 保留字符: {_b}"; assert not _b.endswith(" ") and not _b.endswith("."), f"❌ 文件名不能以空格或句点结尾: {_b}"; assert len(_b) <= 255, f"❌ 文件名过长 ({len(_b)}): {_b}"
        # ── 铁律 4: overwrite 不接受 at_line ──
        if mode == "overwrite" and at_line is not None:
            return [TextContent(
                type="text",
                text="❌ 铁律违反：overwrite 不接受 at_line 参数。\n"
                     "overwrite 永远替换整个文件——不存在'只重写文件的一部分'。\n"
                     "如需修改部分行，请用 at_line + replace=true（单行替换）。\n"
                     "如需删除行，请用 mode='delete_lines'。"
            )]

        # ── Determine encoding & newline ──
        has_override = (enc_override is not None) or (nl_override is not None)

        orig_enc = "utf-8"
        orig_bom = b""
        if file_exists:
            orig_enc, _ = _detect_encoding(path)
            with open(path, "rb") as fh:
                orig_bom = _detect_bom(fh.read(8))

        if enc_override is not None:
            enc = _canonical_enc(enc_override)
            is_utf8 = _is_utf8_family(enc_override)
        elif file_exists:
            enc, is_utf8 = _detect_encoding(path)
        else:
            enc, is_utf8 = "utf-8", True

        if nl_override is not None:
            nl = nl_override
        elif file_exists:
            nl = _detect_newline(path)
        else:
            nl = "\n"

        # ── BOM 策略（v4.7）──
        #   · UTF-16/32：**强制**写 BOM（Windows 消费端没 BOM 必乱码）
        #   · 其它编码：萧规曹随——仅在「编码不变」时沿用原 BOM；
        #     若本次改换了编码族，丢掉旧 BOM（否则会把 UTF-16 的 BOM 塞进 utf-8 文件）
        same_enc_family = _canonical_enc(orig_enc) == _canonical_enc(enc)
        if _requires_bom(enc):
            bom_out = _native_bom(enc)
        elif same_enc_family:
            bom_out = orig_bom
        else:
            bom_out = b""

        # ── Encoding/newline override confirmation gate ──
        needs_confirm = has_override

        if needs_confirm and not encoding_confirmed:
            lines_warn = ["⚠️ 强制写入模式 — 已指定编码/换行符，将无视文件现有格式\n"]
            lines_warn.append(f"文件: {path}")
            if enc_override is not None:
                det_enc = orig_enc if file_exists else "新文件"
                lines_warn.append(f"编码: 原 {det_enc} → 强制 {enc_override}"
                                  f"（规范 {enc}）")
                if _requires_bom(enc):
                    lines_warn.append(f"  ⚠️ {enc} 强制要求 BOM：将写入 {bom_out!r}")
            if nl_override is not None:
                det_nl = repr(_detect_newline(path)) if file_exists else "新文件 LF"
                lines_warn.append(f"换行符: 原 {det_nl} → 强制 {repr(nl_override)}")
            lines_warn.append("\n请核实后追加 encoding_confirmed=true 重新调用。")
            return [TextContent(type="text", text="\n".join(lines_warn))]

        # ═══════════════════════════════════════════════════
        #  MODE: delete_lines  (墓碑化)
        # ═══════════════════════════════════════════════════
        if mode == "delete_lines":
            if not file_exists:
                return [TextContent(type="text", text=f"文件不存在: {path}")]

            if at_line is None:
                return [TextContent(
                    type="text",
                    text="❌ mode='delete_lines' 需要指定 at_line（从哪行开始墓碑化）。"
                )]

            raw_lines = _read_raw_lines(path, enc, nl)
            total = len(raw_lines)

            start_idx = _resolve_line_offset(total, at_line, clamp_to="end")
            if start_idx >= total:
                return [TextContent(type="text", text=f"at_line 超出文件范围（文件共 {total} 行）。")]

            count = max(1, min(count, total - start_idx))
            tombstone = _get_tombstone_byte(enc)
            if not tombstone:
                return [TextContent(type="text", text=(
                    f"❌ 编码 {enc} 没有可用的墓碑字节（未知编码不许兜底成空格——\n"
                    f"空格是合法字符，会与真实内容碰撞，导致 read 误判 + gc 误删真实内容）。\n"
                    f"请显式指定 encoding=。"))]
            nl_bytes = nl.encode(enc)

            # Build tombstone lines: each has same byte-length as the original
            new_raw_lines = list(raw_lines)
            ts_len = len(tombstone)
            for i in range(start_idx, start_idx + count):
                old_len = len(raw_lines[i])
                if old_len == 0:
                    continue
                repeat = (old_len // ts_len) * ts_len
                remainder = old_len - repeat
                if remainder:
                    # 墓碑单元长度除不尽原行 → 无法产出「纯墓碑行」（残留字节会混进去，
                    # 该行就不会被识别为墓碑）。fail-loud，不静默写半截。
                    return [TextContent(type="text", text=(
                        f"❌ 行 {i} 长度 {old_len} 字节不能被墓碑单元 {ts_len} 字节整除"
                        f"（余 {remainder}），无法等长墓碑化。\n"
                        f"该编码（{enc}）的墓碑字节是多字节序列；请改用单字节墓碑的编码"
                        f"（如 utf-8 / gb18030），或先转码。"))]
                new_raw_lines[i] = tombstone * (repeat // ts_len)

            # Reassemble（raw 路径天然保留行首 BOM）
            # v4.8：_read_raw_lines 已剥掉尾换行切出的空尾元素，故此处**显式补回**
            # 文件结尾换行——否则首次墓碑化就会静默吞掉文件末尾的换行符。
            with open(path, "rb") as fh:
                _had_trailing_nl = fh.read().endswith(nl_bytes)
            new_raw = nl_bytes.join(new_raw_lines)
            if _had_trailing_nl:
                new_raw += nl_bytes
            with open(path, "wb") as fh:
                fh.write(new_raw)

            new_size = os.path.getsize(path)
            enc_note = "" if is_utf8 else f", 编码: {enc}"
            ts_desc = repr(tombstone)
            return [TextContent(
                type="text",
                text=(
                    f"墓碑化完成: {path}\n"
                    f"已墓碑化 {count} 行（行 {start_idx}-{start_idx + count - 1}）\n"
                    f"墓碑字节: {ts_desc}\n"
                    f"⚠️ 行号不变——墓碑行在 read 时显示为 [DELETED]。\n"
                    f"文件大小: {new_size} 字节{enc_note}"
                )
            )]

        # ═══════════════════════════════════════════════════
        #  MODE: append / overwrite  (whole-file, no at_line)
        # ═══════════════════════════════════════════════════
        if at_line is None:
            # content 换行**默认归一化**到文件约定（v4.7）：
            # 此前只在 caller 显式传 newline= 时才归一，默认路径把 LF 原样写进 CRLF 文件
            # ⇒ 文件内混用（实测复现）。newline= 仍可强制成任意约定。
            content = content.replace("\r\n", "\n").replace("\r", "\n")
            if nl != "\n":
                content = content.replace("\n", nl)

            payload, enc_err = _encode_report(content, enc)
            if payload is None:
                return [TextContent(type="text", text=enc_err)]

            if mode == "append":
                # 已有内容：绝不重复写 BOM（BOM 只在文件开头）；
                # 空文件 / 新文件：按 bom_out 写。
                existing = os.path.getsize(path) if os.path.isfile(path) else 0
                if existing == 0:
                    _write_bytes_with_bom(path, payload, bom_out)
                else:
                    with open(path, "ab") as fh:
                        fh.write(payload)
            elif mode == "overwrite":
                _write_bytes_with_bom(path, payload, bom_out)
            else:
                return [TextContent(
                    type="text",
                    text=f"❌ 未知模式: {mode}。支持的模式: append, overwrite, delete_lines。"
                )]

            new_size = os.path.getsize(path)
            enc_note = "" if is_utf8 else f", 编码: {enc}"
            nl_note = f", 换行符: {repr(nl)}"
            bom_note = f", BOM: {bom_out!r}" if bom_out else ""
            warn_note = "\n⚠️ 行号已改变，旧行号全部作废，请重新计算。" if mode == "overwrite" else ""
            return [TextContent(
                type="text",
                text=(f"写入成功: {path} ({new_size} 字节, 模式: {mode}"
                      f"{enc_note}{nl_note}{bom_note}){warn_note}")
            )]

        # ═══════════════════════════════════════════════════
        #  MODE: at_line + replace=true  (单行替换铁律)
        # ═══════════════════════════════════════════════════
        # at_line is not None at this point

        # ── 铁律: replace=false (插入模式) 已禁用 ──
        if not replace:
            return [TextContent(
                type="text",
                text="❌ 铁律违反：at_line + replace=false（插入模式）已移除。\n"
                     "插入新行会改变所有后续行号，破坏文件坐标系。\n"
                     "如需新增内容，请用 mode='append'（尾续）。\n"
                     "如需重构文件，请用 mode='overwrite'（全量重建）。"
            )]

        # ── 铁律 1: content 不得包含换行符（行号漂移的唯一根源） ──
        if "\n" in content or "\r" in content:
            return [TextContent(
                type="text",
                text="❌ 铁律违反：at_line + replace=true 的 content 包含换行符。\n"
                     "换行符是文件坐标网格，偷渡换行符会改变行数→行号漂移。\n"
                     "字节长度可以任意变化——只有换行符会影响行号。\n"
                     "如需多行替换，请用 mode='overwrite'（全量重建整个文件）。\n"
                     "如需插入新行，请用 mode='append'（尾续）。"
            )]

        # ── Sanitise content to strip any stray newlines ──
        content = content.replace("\n", "").replace("\r", "")

        if not file_exists:
            payload, enc_err = _encode_report(content + nl, enc)
            if payload is None:
                return [TextContent(type="text", text=enc_err)]
            _write_bytes_with_bom(path, payload, bom_out)
            new_size = os.path.getsize(path)
            enc_note = "" if is_utf8 else f", 编码: {enc}"
            bom_note = f", BOM: {bom_out!r}" if bom_out else ""
            return [TextContent(
                type="text",
                text=f"文件不存在，已创建并写入单行: {path} ({new_size} 字节{enc_note}{bom_note})"
            )]

        # Read original lines（编码已是无 BOM 语义的规范名，故行首会留一个 U+FEFF；
        # 读入后剥掉、写回时用 bom_out 显式补——不隐式依赖 codec 的 BOM 行为）
        with open(path, "r", encoding=enc, newline="", errors="replace") as fh:
            original_lines = fh.readlines()
        if original_lines and original_lines[0].startswith("\ufeff"):
            original_lines[0] = original_lines[0][1:]

        total = len(original_lines)
        target_idx = _resolve_line_offset(total, at_line, clamp_to="end")

        if target_idx >= total:
            return [TextContent(type="text", text=f"at_line 超出文件范围（文件共 {total} 行）。")]

        old_line = original_lines[target_idx]

        # ── Phase 1: preview ──
        if not confirmed:
            preview_start = max(0, target_idx - 3)
            preview_end = min(total, target_idx + 4)

            preview_lines: list[str] = []
            for j in range(preview_start, preview_end):
                marker = ">>>" if j == target_idx else "   "
                # Use tombstone-aware display for preview
                enc_for_display, _ = _detect_encoding(path)
                ts_for_display = _get_tombstone_byte(enc_for_display)
                raw_line_bytes = original_lines[j].rstrip("\n").rstrip("\r").encode(enc_for_display)
                if _is_raw_tombstone_line(raw_line_bytes, ts_for_display):
                    display = "[DELETED]" + nl
                else:
                    display = original_lines[j].rstrip()
                preview_lines.append(f"{marker} {j:6d}: {display}")

            return [TextContent(
                type="text",
                text=(
                    f"⚠️ 单行替换预览（尚未执行）\n"
                    f"文件: {path}\n"
                    f"位置: at_line={at_line}（解析为行 {target_idx}）\n"
                    f"换行符: {repr(nl)}, 编码: {enc}\n"
                    f"\n上下文（>>> 标记将被替换的行）:\n"
                    + "\n".join(preview_lines)
                    + f"\n\n旧内容:\n{old_line.rstrip()}\n"
                    + f"\n新内容:\n{content}\n"
                    + f"\n如确认替换，请追加 confirmed=true 后重新调用。"
                )
            )]

        # ── Phase 2: execute ──
        # Byte length can change freely — only newlines affect the coordinate system.
        if not content.endswith(("\n", "\r\n", "\r")):
            content += nl

        new_lines = list(original_lines)
        new_lines[target_idx] = content

        payload, enc_err = _encode_report("".join(new_lines), enc)
        if payload is None:
            return [TextContent(type="text", text=enc_err)]
        _write_bytes_with_bom(path, payload, bom_out)

        new_size = os.path.getsize(path)
        enc_note = "" if is_utf8 else f", 编码: {enc}"
        bom_note = f", BOM: {bom_out!r}" if bom_out else ""
        return [TextContent(
            type="text",
            text=(
                f"单行替换完成: {path}\n"
                f"行 {target_idx} 已替换（文件共 {total} 行，行数不变）\n"
                f"文件大小: {new_size} 字节{enc_note}{bom_note}"
            )
        )]

    # ═══════════════════════════════════════════════════════
    #  6. cronopio_gc  (垃圾回收)
    # ═══════════════════════════════════════════════════════
    elif name == "cronopio_gc":
        path: str = _normalize_path(arguments["path"])

        if not _is_allowed(path):
            return [TextContent(type="text", text=f"路径不在安全白名单内: {path}")]
        if not os.path.isfile(path):
            return [TextContent(type="text", text=f"不是文件: {path}")]

        enc, is_utf8 = _detect_encoding(path)
        nl_text = _detect_newline(path, enc)
        tombstone = _get_tombstone_byte(enc)
        nl_bytes = nl_text.encode(enc)

        with open(path, "rb") as fh:
            raw = fh.read()

        raw_lines = _split_lines_exact(raw, nl_bytes)
        total = len(raw_lines)
        kept: list[bytes] = []
        removed = 0

        for raw_line in raw_lines:
            if _is_raw_tombstone_line(raw_line, tombstone):
                removed += 1
            else:
                kept.append(raw_line)

        if removed == 0:
            return [TextContent(
                type="text",
                text=f"无需 GC: {path}\n文件中无墓碑行（共 {total} 行）。"
            )]

        # Reassemble and write
        # v4.8：同上，空尾元素已剥掉，尾换行需显式补回，否则 GC 会吞掉文件末尾换行。
        new_raw = nl_bytes.join(kept)
        if raw.endswith(nl_bytes):
            new_raw += nl_bytes
        with open(path, "wb") as fh:
            fh.write(new_raw)

        new_size = os.path.getsize(path)
        enc_note = "" if is_utf8 else f", 编码: {enc}"
        ratio = f"{removed / total * 100:.1f}%" if total > 0 else "0%"
        return [TextContent(
            type="text",
            text=(
                f"GC 完成: {path}\n"
                f"移除 {removed} 行墓碑，{total} 行 → {total - removed} 行（墓碑率 {ratio}）\n"
                f"文件大小: {new_size} 字节{enc_note}\n"
                f"⚠️ 行号已改变，旧行号全部作废，请重新计算新行号。"
            )
        )]

    # ═══════════════════════════════════════════════════════
    #  7. cronopio_copy
    # ═══════════════════════════════════════════════════════
    elif name == "cronopio_copy":
        source: str = _normalize_path(arguments["source"])
        destination: str = _normalize_path(arguments["destination"])
        overwrite: bool = arguments.get("overwrite", False)

        if not _is_allowed(source):
            return [TextContent(type="text", text=f"源路径不在安全白名单内: {source}")]
        if not _is_allowed(destination):
            return [TextContent(type="text", text=f"目标路径不在安全白名单内: {destination}")]
        if not os.path.isfile(source):
            return [TextContent(type="text", text=f"源文件不存在: {source}")]

        dest_parent = os.path.dirname(destination)
        if dest_parent and not os.path.isdir(dest_parent):
            os.makedirs(dest_parent, exist_ok=True)

        if os.path.exists(destination) and not overwrite:
            return [TextContent(
                type="text",
                text=f"目标已存在，设置 overwrite=true 覆盖: {destination}"
            )]
        _b=os.path.basename(destination); assert _b and _b not in (".",".."), f"文件名无效: {_b}"; _n=_b.rsplit(".",1)[0].upper(); assert _n not in {"CON","PRN","AUX","NUL","COM1","COM2","COM3","COM4","COM5","COM6","COM7","COM8","COM9","LPT1","LPT2","LPT3","LPT4","LPT5","LPT6","LPT7","LPT8","LPT9"}, f"❌ 文件名是 Windows 保留名称: {_b}"; assert not any(c in _b for c in '\\/:*?"<>|'), f"❌ 文件名含 Windows 保留字符: {_b}"; assert not _b.endswith(" ") and not _b.endswith("."), f"❌ 文件名不能以空格或句点结尾: {_b}"; assert len(_b) <= 255, f"❌ 文件名过长 ({len(_b)}): {_b}"
        shutil.copy2(source, destination)
        new_size = os.path.getsize(destination)
        return [TextContent(
            type="text",
            text=f"复制成功: {source} → {destination} ({new_size} 字节)"
        )]

    # ═══════════════════════════════════════════════════════
    #  8. cronopio_roots
    # ═══════════════════════════════════════════════════════
    elif name == "cronopio_roots":
        allowed_existing = [d for d in ALLOWED_ROOTS if os.path.isdir(d)]
        searchable_existing = [d for d in SEARCHABLE_DIRS if os.path.isdir(d)]
        cwd_info = (
            f"当前搜索目录: {CURRENT_SEARCH_DIR}"
            if CURRENT_SEARCH_DIR
            else "当前搜索目录: (未设置)"
        )
        return [TextContent(
            type="text",
            text=(
                f"{cwd_info}\n"
                f"\n安全白名单 (ALLOWED_ROOTS — 可读写/复制):\n"
                + "\n".join(f"  {d}" for d in allowed_existing)
                + f"\n\n搜索白名单 (SEARCHABLE_DIRS — cronopio_find 可遍历):\n"
                + "\n".join(f"  {d}" for d in searchable_existing)
            )
        )]

    # ═══════════════════════════════════════════════════════
    #  9. cronopio_stat
    # ═══════════════════════════════════════════════════════
    elif name == "cronopio_stat":
        path: str = _normalize_path(arguments["path"])
        if not _is_allowed(path):
            return [TextContent(type="text", text=f"路径不在安全白名单内: {path}")]
        if not os.path.exists(path):
            return [TextContent(type="text", text=f"路径不存在: {path}")]

        st = os.stat(path)
        mtime_iso = datetime.fromtimestamp(
            st.st_mtime, tz=timezone.utc
        ).astimezone().isoformat()
        ctime_iso = datetime.fromtimestamp(
            st.st_ctime, tz=timezone.utc
        ).astimezone().isoformat()
        entry_type = "目录" if os.path.isdir(path) else "文件"

        enc_note = ""
        nl_note = ""
        tombstone_note = ""
        style_note = ""
        if os.path.isfile(path):
            try:
                _, enc, _, ts_count, _ = _read_file_with_tombstones(path)
                nl = _detect_newline(path)
                enc_note = f"\n编码: {enc}"
                nl_note = f"\n换行符: {repr(nl)}"
                if ts_count > 0:
                    tombstone_note = f"\n墓碑行: {ts_count}"
            except Exception:
                pass
            style_note = _style_report(path)

        return [TextContent(
            type="text",
            text=(
                f"路径: {path}\n"
                f"类型: {entry_type}\n"
                f"大小: {st.st_size:,} 字节\n"
                f"修改时间 (mtime): {mtime_iso}\n"
                f"创建时间 (ctime): {ctime_iso}"
                f"{enc_note}"
                f"{nl_note}"
                f"{tombstone_note}"
                f"{style_note}"
            )
        )]

    # ═══════════════════════════════════════════════════════
    #  10. cronopio_ls
    # ═══════════════════════════════════════════════════════
    elif name == "cronopio_ls":
        path: str = _normalize_path(arguments["path"])
        pattern: str = arguments.get("pattern", "*")
        show_hidden: bool = arguments.get("show_hidden", True)

        if not _is_allowed(path):
            return [TextContent(type="text", text=f"路径不在安全白名单内: {path}")]
        if not os.path.isdir(path):
            return [TextContent(type="text", text=f"不是目录: {path}")]

        entries: list[tuple[str, str, str, str]] = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    # ── Hidden file filter ──
                    if not show_hidden and entry.name.startswith("."):
                        continue

                    if pattern != "*" and not fnmatch.fnmatch(entry.name, pattern):
                        continue

                    try:
                        st = entry.stat()
                    except OSError:
                        continue

                    mtime_iso = datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc
                    ).astimezone().isoformat()

                    if entry.is_symlink():
                        # ── Symlink resolution ──
                        try:
                            target = os.readlink(entry.path)
                        except OSError:
                            target = "?"

                        # Check if target exists to detect broken links
                        if not os.path.exists(entry.path):
                            display_name = f"{entry.name} → {target} (疑似断链)"
                        else:
                            display_name = f"{entry.name} → {target}"

                        try:
                            if os.path.isdir(entry.path):
                                type_str = "<LNK_D>"
                            elif os.path.isfile(entry.path):
                                type_str = "<LNK_F>"
                            else:
                                type_str = "<LNK?>"
                        except OSError:
                            type_str = "<LNK?>"

                        size_str = "-".rjust(9)
                        entries.append((mtime_iso, size_str, type_str, display_name))
                    elif entry.is_dir(follow_symlinks=False):
                        size_str = "-".rjust(9)
                        type_str = "<DIR>"
                        entries.append((mtime_iso, size_str, type_str, entry.name))
                    else:
                        size_str = f"{st.st_size:,}".rjust(9)
                        type_str = "<FILE>"
                        entries.append((mtime_iso, size_str, type_str, entry.name))
        except (PermissionError, OSError) as exc:
            return [TextContent(type="text", text=f"无法读取目录: {path}\n{exc}")]

        entries.sort(key=lambda e: e[3].lower())

        if not entries:
            hidden_note = "（隐藏文件被过滤，设置 show_hidden=true 显示）" if not show_hidden else ""
            return [TextContent(
                type="text",
                text=f"(目录为空或无匹配 '{pattern}': {path}) {hidden_note}"
            )]

        lines = [f"{path} 的目录列表 (pattern={pattern}, show_hidden={show_hidden}):\n"]
        lines.append(f"{'修改时间':<32} {'大小':>9}  {'类型':<6}  文件名")
        lines.append("-" * 80)
        for mtime_iso, size_str, type_str, name in entries:
            lines.append(f"{mtime_iso:<32} {size_str}  {type_str:<6}  {name}")

        return [TextContent(type="text", text="\n".join(lines))]

    # ═══════════════════════════════════════════════════════
    #  11. cronopio_scan
    # ═══════════════════════════════════════════════════════
    elif name == "cronopio_scan":
        return _scan_v2(arguments)
    elif name == "cronopio_convert":
        return _convert_v1(arguments)
    else:
        return [TextContent(type="text", text=f"未知工具: {name}")]







# ═══════════════════════════════════════════════════════════
#  rg-powered grep pipeline + scan format dispatch (v4.5)
#  行号口径: 全局 0-based（第 1 行 = 0）。rg 输出 1-based，此处统一 -1 转换。
# ═══════════════════════════════════════════════════════════

def _normalize_lines_nfc_lf(lines: list[str]) -> str:
    """行列表 → UTF-8 NFC + LF 归一化文本。行号保持（每行独立归一化，只统一换行符）。"""
    out: list[str] = []
    for ln in lines:
        out.append(unicodedata.normalize("NFC", ln.rstrip("\r\n")))
    return "\n".join(out) + ("\n" if out else "")


# ── rg 缺失时的降级通道（v4.8）──────────────────────────────
# 口径：rg 不可用**不是致命错误**，但**必须是显式的**——结果里带警告横幅、
# stderr 同步打印一次。禁止静默降级：用户必须能分辨"真的没找到"与
# "换了一套引擎、语义可能有差异"。
_RG_INSTALL_HINT = "pacman -S ripgrep（Arch）或 apt install ripgrep（Debian/Ubuntu）"
_python_fallback_warned = False


def _rg_fallback_warning(reason: str) -> str:
    """rg 不可用时统一构造警告横幅（结果内 + stderr 各一份，同一文案）。"""
    return (
        f"⚠️ 未检测到 ripgrep（rg）——{reason}。\n"
        f"   已回退到 Python re 引擎：基本正则等价；但 lookaround / 反向引用 /\n"
        f"   \\p{{...}} 等语法与 rg --pcre2 不同路，转义写法（如 \\b）也可能有差异。\n"
        f"   建议安装 ripgrep 以恢复完整语义：{_RG_INSTALL_HINT}"
    )


def _warn_rg_fallback(reason: str) -> str:
    """把降级警告落到 stderr（只首次打一次，避免刷屏），并返回横幅供结果内使用。

    stderr 专用：MCP 的 JSON-RPC 走 stdout，写 stderr 不会污染协议。
    """
    global _python_fallback_warned
    banner = _rg_fallback_warning(reason)
    if not _python_fallback_warned:
        _python_fallback_warned = True
        try:
            sys.stderr.write("\n" + banner + "\n\n")
            sys.stderr.flush()
        except Exception:
            pass          # stderr 不可写（已重定向/关闭）不应影响工具主功能
    return banner


def _python_regex_search(text: str, query: str, *, case_sensitive: bool = False,
                         fixed_string: bool = False) -> list[tuple[int, int, str]]:
    """rg 缺席时的回退实现：逐行 Python re，返回与 _rg_search_text 同构的结果。

    语义对齐的：
      · 行号 0-based（与全局内部口径一致；对外显示由调用方 +1 转 1-based）
      · 列 = **0-based 字符**偏移（匹配起点），与 rg 路径的 _byte_col_to_char_col 同义
      · 每行只取**首个**匹配（rg --column 的默认行为，见 --only-matching 未启用）
      · 不跨行匹配（逐行独立 re.search，与 rg 默认一致）
    语义**不同**的（横幅里已声明，不假装等价）：
      · 大小写不敏感时用 re.IGNORECASE（Unicode 感知，与 rg -i 基本一致）
      · 非法正则：rg 会报"regex parse error"，本回退报 Python re 的 error——两者都抛
        RuntimeError，绝不静默返回空结果
    """
    pattern = re.escape(query) if fixed_string else query
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        raise RuntimeError(f"回退引擎（Python re）无法编译该正则：{exc}；原始 query={query!r}")

    hits: list[tuple[int, int, str]] = []
    for i, line in enumerate(text.split("\n")):
        m = rx.search(line)
        if m is None:
            continue
        hits.append((i, m.start(), line))
    return hits


def _run_rg_raw(args: list[str], *, stdin_text: bytes | None = None,
                timeout: int = 30) -> tuple[list[str] | None, str, str]:
    """执行 rg，返回 (stdout 原始行, 错误文本, 失败类别)。

    只负责「跑 + 分类失败」，**不解析输出**——stdin 模式（`行:列:正文`）与
    目录模式（`路径:行:正文`）形状不同，由各自调用方解析，不搞"万能解析器"。

    失败类别（决定上层是降级还是报错）：
      · "missing"        —— rg 不在 PATH → **降级**到 Python re
      · "timeout"        —— 超时       → **降级**到 Python re（不能只留一句"超时"）
      · "invalid-regex"  —— 用户正则非法；回退引擎同样会炸，故**原样升级报错**
      · "rg-error"       —— 其它 rg 故障 → **原样升级报错**

    成功与「零命中」（rg exit 1）都返回 (hits, "", "")；此处不生成警告，
    由调用方决定口径——避免"没装 rg 但根本没搜索需求"也刷警告。
    """
    try:
        proc = subprocess.run(args, input=stdin_text,
                              capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return None, "", "missing"
    except subprocess.TimeoutExpired:
        return None, "", "timeout"

    if proc.returncode == 1:
        return [], "", ""
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        low = err.lower()
        kind = "invalid-regex" if ("regex parse error" in low
                                   or "error parsing glob" in low
                                   or "unrecognized escape" in low) else "rg-error"
        return None, err, kind

    return proc.stdout.decode("utf-8", errors="replace").splitlines(), "", ""


def _rg_search_text(text: str, query: str, *, case_sensitive: bool = False,
                    fixed_string: bool = False, enable_pcre2: bool = False,
                    timeout: int = 30) -> tuple[list[tuple[int, int, str]], str]:
    """对归一化文本跑 rg（stdin 管道）。返回 (命中, 警告横幅)。

    命中为 [(0-based 行号, 0-based 字符列, 匹配行文本), ...]。
    仅输出匹配行（上下文由调用方从原文件组装），rg 行号 1-based → 0-based。
    列用于把截断窗口居中到命中处；rg --column 给的是 1-based **字节** 偏移
    （实测 `中文字XYZMATCH` 报 13 而非 7），此处换算为字符偏移——
    Python 的 str 按字符索引，不换算就会在中文行上切错位。

    v4.8：rg 缺失 / 超时 → 降级 Python re 并返回警告横幅（调用方负责渲染）；
    正则非法 / 其它 rg 故障 → 抛 RuntimeError（回退救不了，静默兜底更糟）。
    """
    args = ["rg", "-n", "--column", "--no-heading", "--color", "never"]
    if not case_sensitive:
        args.append("-i")
    if fixed_string:
        args.append("-F")
    if enable_pcre2:
        args.append("--pcre2")
    args += ["--", query, "-"]

    rows, err, kind = _run_rg_raw(args, stdin_text=text.encode("utf-8"),
                                  timeout=timeout)

    if rows is not None:
        hits: list[tuple[int, int, str]] = []
        for raw in rows:
            c1 = raw.find(":")
            if c1 <= 0:
                continue
            c2 = raw.find(":", c1 + 1)  # stdin 下形状为 `行:列:正文`（无文件名）
            if c2 <= 0:
                continue
            try:
                lineno = int(raw[:c1]) - 1        # rg 1-based → 0-based
                byte_col = int(raw[c1 + 1:c2])
            except ValueError:
                continue
            line_text = raw[c2 + 1:]
            hits.append((lineno, _byte_col_to_char_col(line_text, byte_col), line_text))
        return hits, ""

    if kind == "missing":
        banner = _warn_rg_fallback("rg 不在 PATH 中")
        return _python_regex_search(text, query, case_sensitive=case_sensitive,
                                    fixed_string=fixed_string), banner
    if kind == "timeout":
        banner = _warn_rg_fallback(f"rg 搜索超时（>{timeout}s）")
        return _python_regex_search(text, query, case_sensitive=case_sensitive,
                                    fixed_string=fixed_string), banner

    if kind == "invalid-regex":
        raise RuntimeError(f"正则表达式非法（rg 拒绝）: {err[:300]}\n"
                           f"  原始 query: {query!r}\n"
                           f"  注：本机未装 rg 时改用 Python re，行为会一并报错，不会静默返回空结果。")
    raise RuntimeError(f"rg 搜索失败 (exit≠0): {err[:300]}")



# ═══════════════════════════════════════════════════════════
#  输出预算闸门（全局唯一口径：改这里，grep / scan 同时生效）
# ═══════════════════════════════════════════════════════════
# 闸门的目标是"整条工具结果不被 DSH 的 spill-policy 截断"。该策略的**计量单位
# 在 0.1.7 换过轴**，所以预算不能写成一个裸数字：
#
#   0.1.5 及以前  packages/bundle/base/cordis.patch.yml → maxInlineBytes: 50000
#                 ⇒ UTF-8 **字节**
#   0.1.7 起      packages/bundle/base/cordis.patch.yml → maxInlineTokens: 12500
#                 ⇒ token，定价式 ceil(UTF-16 码元数 / 4) + 4
#                   （packages/llm/token-meter/src/estimate.ts：CHARS_PER_TOKEN = 4）
#
# ⇒ 12500 token ≈ 50000 个 **UTF-16 码元**——与"字节"不是一回事：
#     ASCII：1 字节 = 1 码元      → 两轴近似等价
#     CJK  ：1 字 = 3 字节 = 1 码元 → 按字节计数只用到约 1/3 预算（浪费，不致命）
#   反过来按码元计数在 0.1.5 上会**超支**（48000 码元的 CJK = 144000 字节 > 50000），
#   整条结果被整体 spill。所以**不能**一刀切改口径，只能按版本选轴。
#
# 版本从 $DSH_HARNESS_DIR/DSH_VERSION（唯一真相源，与 start-web.sh / bin/dsh 同源）
# 读取；读不到就退回**字节轴** = 旧行为，保守且零回归。
# 升级基座后本文件无需改动：换 DSH_VERSION 即自动跟着换轴。
_HARNESS_DIR = os.environ.get("DSH_HARNESS_DIR", os.path.expanduser("~/deepseek-harness"))

# 两轴同数：50000 − 2000（MCP 包装余量）
_OUT_BUDGET = 48_000


def _read_dsh_version() -> str:
    """读 $DSH_HARNESS_DIR/DSH_VERSION 里的 DSH_VERSION= 行；任何失败返回空串。"""
    try:
        with open(os.path.join(_HARNESS_DIR, "DSH_VERSION"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("DSH_VERSION="):
                    return line.partition("=")[2].strip()
    except OSError:
        return ""
    return ""


def _is_token_axis(version: str) -> bool:
    """0.1.7 起 spill-policy 按 token（UTF-16 码元/4）计量，预算轴线随之改变。

    注意 DSH 的版本号是 `0.1.x` 形态——**递增的是第三段**（0.1.5 → 0.1.7），
    前两段恒为 0.1。所以必须比三段，只比 (major, minor) 会永远为假。
    """
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version or "")
    return m is not None and tuple(int(g) for g in m.groups()) >= (0, 1, 7)


# 解析一次，进程内不变（MCP server 生命周期内 harness 版本不会变）
DSH_VERSION_AT_LOAD = _read_dsh_version()
_OUT_AXIS = "code-unit" if _is_token_axis(DSH_VERSION_AT_LOAD) else "byte"


def _out_cost(text: str) -> int:
    """一条输出行占用的预算（含末尾换行 +1），单位随当前轴。"""
    if _OUT_AXIS == "code-unit":
        return len(text.encode("utf-16-le")) // 2 + 1
    return len(text.encode("utf-8")) + 1


def _escaped_out_cost(text: str) -> int:
    """先走转义再计预算——核算形态必须与最终输出形态一致。"""
    return _out_cost(_escape_zone(text))


# ── grep_file 输出闸门：预算 + 超长行截断 ──────────────────────────
# 口径见上方「输出预算闸门」块——它随 DSH 版本换轴（0.1.5 字节 / 0.1.7 码元）。
# 为什么 grep 也需要：会话日志的"一行"就是一整条事件（实测有 60KB+ 的 reasoning
# 块），不设闸门时一次 grep 就能把结果撑过 50KB 而被整体 spill 掉。
_GREP_OUTPUT_BUDGET = _OUT_BUDGET

# 命中行居中窗口：行首锚 + 中略 + 命中窗口（总字符数目标 _CTX_LINE_MAX_CHARS）
_CTX_LINE_MAX_CHARS = 500
_CTX_HEAD_ANCHOR_CHARS = 120      # 保留行首多少字符（jsonl 的事件身份在开头）
_CTX_MATCH_LEAD_CHARS = 120       # 命中点前保留多少字符

# ── 字素簇切分（PyICU）────────────────────────────────────────────
# 切点必须落在**可见字素簇**边界上。按码点切会把组合序列（é = e+U+0301）、
# ZWJ emoji（👨‍👩‍👧）、肤色修饰（👍🏽）、区域指示符（🇨🇳）、天城文连字（क्ष）
# 切成语义碎片——那样截出来的文本对语义判断没有意义。
# PyICU 的 character BreakIterator 即 UAX #29 扩展字素簇迭代器。
try:
    import icu as _icu
    _ICU_BI = _icu.BreakIterator.createCharacterInstance(_icu.Locale.getRoot())
    _ICU_AVAILABLE = True
except Exception:                 # 缺 PyICU 不静默：输出层会显式标注（见 _grapheme_warning）
    _icu = None
    _ICU_BI = None
    _ICU_AVAILABLE = False

_ICU_BOUND_TEXT: str | None = None    # BI 当前绑定的文本（同一行多次吸附只 setText 一次）


def _bi_bound_to(text: str):
    """把共享的 BreakIterator 绑到 text（按对象身份缓存，避免逐次 setText）。"""
    global _ICU_BOUND_TEXT
    if _ICU_BOUND_TEXT is not text:
        _ICU_BI.setText(text)
        _ICU_BOUND_TEXT = text
    return _ICU_BI


def _char_to_u16(text: str, idx: int) -> int:
    """Python 字符下标 → ICU 的 UTF-16 码元偏移。

    ⚠️ PyICU 的 BreakIterator 用 **UTF-16 码元** 偏移（ICU4C 原生编码），
    非 BMP 字符（emoji、CJK 扩展）占 **2** 个码元——把 Python 下标直接喂给
    isBoundary() 必然错位。实测：200 个 ZWJ 家庭 emoji 的行 Python len=1106，
    ICU 边界最大到 1706。必须显式换算。
    """
    if idx <= 0:
        return 0
    return len(text[:idx].encode("utf-16-le")) // 2


def _u16_to_char(text: str, u16: int) -> int:
    """ICU 的 UTF-16 码元偏移 → Python 字符下标（落在代理对中间时向下取整）。"""
    if u16 <= 0:
        return 0
    return len(text.encode("utf-16-le")[:u16 * 2].decode("utf-16-le", errors="ignore"))


def _snap_down(text: str, idx: int) -> int:
    """吸附到 ≤ idx 的最近字素簇边界（保证不切断簇）。idx 为 Python 字符下标。

    不用 ICU 的 preceding()：实测 PyICU 2.16 的 preceding(1) 会返回 DONE(-1)
    （首簇内部时），拿来做下标会切出 -1 的脏切片。字素簇长度很小（ZWJ 序列
    也就十几个码元），所以用 isBoundary 逐步回退——精确且开销可忽略。
    """
    idx = max(0, min(idx, len(text)))
    if not _ICU_AVAILABLE or idx == 0:
        return idx
    bi = _bi_bound_to(text)
    u16 = _char_to_u16(text, idx)
    while u16 > 0 and not bi.isBoundary(u16):
        u16 -= 1
    return _u16_to_char(text, u16)


def _snap_up(text: str, idx: int) -> int:
    """吸附到 ≥ idx 的最近字素簇边界（保证不切断簇）。idx 为 Python 字符下标。"""
    idx = max(0, min(idx, len(text)))
    if not _ICU_AVAILABLE or idx >= len(text):
        return idx
    bi = _bi_bound_to(text)
    u16 = _char_to_u16(text, idx)
    total = len(text.encode("utf-16-le")) // 2
    while u16 < total and not bi.isBoundary(u16):
        u16 += 1
    return _u16_to_char(text, u16)


def _byte_col_to_char_col(text: str, byte_col: int) -> int:
    """rg --column 的 1-based 字节偏移 → 0-based 字符偏移。

    截在字符边界上（errors="ignore" 丢弃尾部残缺字节），再吸附到字素簇边界。
    """
    if byte_col <= 1:
        return 0
    raw = text.encode("utf-8")[:byte_col - 1]
    return _snap_down(text, len(raw.decode("utf-8", errors="ignore")))


def _grapheme_warning() -> str:
    """PyICU 缺失时的显式告警行（宁可自曝，不静默按码点切）。"""
    if _ICU_AVAILABLE:
        return ""
    return ("⚠️ PyICU 不可用：截断点未对齐字素簇（可能切断组合序列/emoji），"
            "本段输出仅供参考——请 `pip install PyICU`。")


def _truncate_ctx_line(text: str, col: int | None = None) -> str:
    """上下文行超长截断；给了命中列则把窗口**居中到命中处**，切点吸附字素簇边界。

    jsonl 的事件行开头是结构噪声（"type"/"seq"/"time"…），命中词往往在几千字符
    之后：纯头部截断会产出"看得见行、看不见命中"的误导性输出。故改为
      行首锚（_CTX_HEAD_ANCHOR_CHARS）+ `…[中略 N B]` + 命中窗口 + `…[+N B]`
    两端省略量都按**真实字节数**报。col 缺省（上下文行本身无命中列）时退回头部截断。
    """
    if len(text) <= _CTX_LINE_MAX_CHARS:
        return text

    def tail_of(rest: str) -> str:
        return f"…[+{len(rest.encode('utf-8')):,} B]"

    if col is None or col < 0:
        cut = _snap_down(text, _CTX_LINE_MAX_CHARS)
        return f"{text[:cut]}{tail_of(text[cut:])}"

    head_end = _snap_down(text, _CTX_HEAD_ANCHOR_CHARS)
    body_len = _CTX_LINE_MAX_CHARS - _CTX_HEAD_ANCHOR_CHARS
    body_start = _snap_down(text, max(head_end, col - _CTX_MATCH_LEAD_CHARS))
    if body_start >= len(text):
        cut = _snap_down(text, _CTX_LINE_MAX_CHARS)
        return f"{text[:cut]}{tail_of(text[cut:])}"
    body_end = _snap_up(text, body_start + body_len)

    out = text[:head_end]
    if body_start > head_end:
        gap = text[head_end:body_start]
        out += f"…[中略 {len(gap.encode('utf-8')):,} B]"
    out += text[body_start:body_end]
    if body_end < len(text):
        out += tail_of(text[body_end:])
    return out


def _render_hit_blocks(lines: list[str], hits: list[tuple[int, int, str]], *,
                       before: int, after: int) -> list[tuple[int, list[str]]]:
    """每个命中 → (行号, 块正文行)。块粒度供预算核算与超限折叠。

    只有命中行带列（窗口居中）；上下文行没有命中列，退回头部截断。
    """
    blocks: list[tuple[int, list[str]]] = []
    for ln, col, _text in hits:
        body: list[str] = []
        ctx_start = max(0, ln - before)
        ctx_end = min(len(lines), ln + after + 1)
        for j in range(ctx_start, ctx_end):
            marker = ">>>" if j == ln else "   "
            text_j = lines[j].rstrip()
            # 上下文行标签同样按 1-based 显示——否则同一次输出里会出现
            # 「匹配 N (行 1)」配「>>>  0: ...」两种口径（内部 j 仍是 0-based 索引）
            body.append(f"{marker} {j + 1:6d}: "
                        f"{_truncate_ctx_line(text_j, col if j == ln else None)}")
        body.append("")
        blocks.append((ln, body))
    return blocks


def _grep_file_v2(arguments: dict, prefix: str = "") -> list[TextContent]:
    """cronopio_grep v2：rg 正则管线。

    保留旧 API（offset_lines/max_lines/reverse/before/after/max_results），
    搜索内核换成 rg（成熟正则语法）。**对外行号 1-based**（对齐 GNU grep -n）；
    内部 hits 仍是 0-based 索引，仅在渲染时 +1——「匹配 N (行 X)」的 X
    与上下文行标签（`>>>  N: 正文`）**都**是 1-based。
    ZIP 容器（DSH Session Log 导出归档）：解压指定成员后复用本函数（member 缺省=根日志）。

    prefix：容器前缀行（如 ZIP 成员标注）。**计入输出预算**——否则"预算内的正文 +
    预算外的前缀"会一起越线。
    """
    path = _normalize_path(arguments["path"])
    if not _is_allowed(path):
        return [TextContent(type="text", text=f"路径不在安全白名单内: {path}")]
    if not os.path.isfile(path):
        return [TextContent(type="text", text=f"不是文件: {path}")]

    if _is_zip_archive(path):
        return _grep_zip_file(arguments)

    query: str = arguments["query"]
    before = int(arguments.get("before_context", 0) or 0)
    after = int(arguments.get("after_context", 0) or 0)
    # 0 是合法值（=不限），不能被 `or 10` 吞掉——与 _scan_v2 同一坑，同修
    _raw_mr = arguments.get("max_results")
    max_results = 10 if _raw_mr is None else int(_raw_mr)
    reverse = bool(arguments.get("reverse", False))
    offset_lines = int(arguments.get("offset_lines", 0) or 0)
    max_lines = int(arguments.get("max_lines", -1) or -1)
    case_sensitive = bool(arguments.get("case_sensitive", False))
    fixed_string = bool(arguments.get("fixed_string", False))
    enable_pcre2 = bool(arguments.get("enable_pcre2", False))

    lines, enc, is_utf8, ts_count, total = _read_file_with_tombstones(path)

    start = _resolve_line_offset(total, offset_lines, clamp_to="start")
    end = total if max_lines == -1 else min(total, start + max_lines)
    slice_lines = lines[start:end]

    text = _normalize_lines_nfc_lf(slice_lines)
    hits, rg_warn = _rg_search_text(text, query, case_sensitive=case_sensitive,
                                    fixed_string=fixed_string, enable_pcre2=enable_pcre2)
    hits = [(start + ln, col, txt) for ln, col, txt in hits]   # 切片偏移补偿

    enc_note = "" if is_utf8 else f" [原编码: {enc}]"
    # 范围提示按 1-based 显示：切片边界 0-based 会渲染成「搜 0-100 行」，
    # 看着像存在第 0 行；end 是开区间，恰好等于 1-based 的末行号。
    range_note = f" [搜 {start + 1}-{end} 行 / 共 {total} 行]"
    direction_note = "（从尾部向前）" if reverse else ""
    # 降级横幅置顶：必须出现在"未找到"之前，否则用户会把"引擎降级"误读成"确实没有"
    prelude: list[str] = ([rg_warn] if rg_warn else [])

    if not hits:
        miss = f"在 {path} 中未找到 '{query}'{direction_note}{range_note}{enc_note}"
        body = f"{prefix}\n{miss}" if prefix else miss
        return [TextContent(type="text",
                            text="\n".join(prelude + [body]) if prelude else body)]

    # PyICU 缺失时显式自曝（不静默按码点切）
    gw = _grapheme_warning()
    hint = f"在 {path} 中找到 {len(hits)} 处匹配{direction_note}{range_note}{enc_note}:"
    head: list[str] = (prelude + ([prefix] if prefix else [])
                       + ([gw] if gw else []) + [hint, ""])
    budget = _GREP_OUTPUT_BUDGET
    out: list[str] = list(head)
    used = sum(_out_cost(s) for s in head)

    # max_results 先截（reverse 决定取头还是取尾），再对保留部分做预算闸门
    if reverse:
        shown = hits[-max_results:][::-1] if max_results > 0 else hits[::-1]
    else:
        shown = hits[:max_results] if max_results > 0 else hits
    hidden_by_max = len(hits) - len(shown)

    # 尾注预算预留：折叠行号清单最坏 ~40 个行号 + 提示语 ≈ 420B，留 600B 富余
    # （与 scan 的 tail_reserve 同思路——区尾注释不能逃逸预算）
    tail_reserve = 600
    folded: list[int] = []
    emitted = 0
    for ln, body in _render_hit_blocks(lines, shown, before=before, after=after):
        # 显示 1-based：与 GNU grep -n / rg -n 同口径（内部 ln 仍 0-based 索引）
        block = [f"─── 匹配 {emitted + 1} (行 {ln + 1}) ───"] + body
        cost = sum(_out_cost(s) for s in block)
        if used + cost <= budget - tail_reserve:
            out.extend(block)
            used += cost
            emitted += 1
        else:
            folded.append(ln)

    if folded:
        # 折叠清单与匹配块同为对外输出 → 同样转 1-based，否则同一份输出里两种口径
        nums = ", ".join(str(n + 1) for n in folded[:40])
        more = "" if len(folded) <= 40 else f" …等，共 {len(folded)} 处"
        out.append(f"（{len(folded)} 处超出返回预算 {budget}B，折叠为行号：{nums}{more}。"
                   f"请减小 max_results / before_context / after_context 分批取）")
        out.append("")
    if hidden_by_max > 0:
        out.append(f"（还有 {hidden_by_max} 处未显示，max_results={max_results}）")
    return [TextContent(type="text", text="\n".join(out))]


def _grep_files_v2(arguments: dict) -> list[TextContent]:
    """cronopio_grepdir：目录内容搜索（rg 管线）。

    统一转换为 UTF-8 NFC + LF 后写入临时目录，rg 搜索临时目录，
    行号 1-based（对齐 GNU grep -n）；内部 0-based 索引，仅显示时 +1。输出映射回原文件名。
    ZIP（DSH Session Log 导出归档）：中央目录分类（零解压）→ 只解压会话日志成员
    进同一个临时目录 → 复用同一次 rg 调用；输出标注「zip名::成员名」。
    """
    dir_arg = _normalize_path(arguments.get("dir", ""))
    query: str = arguments["query"]
    pattern: str = arguments.get("pattern", "") or ""
    # 0 是合法值（=不限），不能被 `or 10` 吞掉——与 _scan_v2 同一坑，同修
    _raw_mr = arguments.get("max_results")
    max_results = 10 if _raw_mr is None else int(_raw_mr)
    _raw_mpf = arguments.get("max_per_file")
    max_per_file = 3 if _raw_mpf is None else int(_raw_mpf)
    case_sensitive = bool(arguments.get("case_sensitive", False))
    fixed_string = bool(arguments.get("fixed_string", False))
    enable_pcre2 = bool(arguments.get("enable_pcre2", False))

    search_dir = _resolve_search_dir(dir_arg)

    if pattern:
        files = sorted(
            os.path.join(search_dir, f) for f in os.listdir(search_dir)
            if fnmatch.fnmatch(f, pattern)
            and os.path.isfile(os.path.join(search_dir, f))
        )
    else:
        files = sorted(
            os.path.join(search_dir, f) for f in os.listdir(search_dir)
            if os.path.isfile(os.path.join(search_dir, f))
            and f.lower().endswith((".md", ".txt", ".json", ".jsonl", ".zip"))
        )
    if not files:
        return [TextContent(type="text", text=f"目录中无匹配文件: {search_dir}")]

    with tempfile.TemporaryDirectory(prefix="fsgrep_") as tmp:
        mapping: dict[str, str] = {}
        skipped = 0
        for i, fp in enumerate(files):
            if _is_zip_archive(fp):
                # 分类零解压；非 DSH 导出 → 跳过（不硬啃二进制）
                try:
                    probe = _zip_probe(fp)
                except (OSError, zipfile.BadZipFile):
                    skipped += 1
                    continue
                if probe["kind"] != "dsh-session-export":
                    skipped += 1
                    continue
                try:
                    extracted = _zip_extract_members(fp, probe["logs"], tmp,
                                                      prefix=f"z{i:04d}_")
                except (OSError, zipfile.BadZipFile, RuntimeError):
                    skipped += 1
                    continue
                for member, dest in extracted.items():
                    try:
                        ml, _e, _u, _t, _tot2 = _read_file_with_tombstones(dest)
                    except (OSError, LookupError):
                        skipped += 1
                        continue
                    with open(dest, "w", encoding="utf-8") as fh:
                        fh.write(_normalize_lines_nfc_lf(ml))
                    mapping[os.path.basename(dest)] = f"{os.path.basename(fp)}::{member}"
                continue
            try:
                lines, _enc, _u8, _ts, _tot = _read_file_with_tombstones(fp)
            except (OSError, LookupError):
                skipped += 1
                continue
            text = _normalize_lines_nfc_lf(lines)
            tmp_name = f"f{i:04d}"
            with open(os.path.join(tmp, tmp_name), "w", encoding="utf-8") as fh:
                fh.write(text)
            mapping[tmp_name] = fp

        per_file: dict[str, list[tuple[int, str]]] = {}
        rg_warn = ""

        def _scan_with_python() -> dict:
            """rg 缺席时的目录回退：逐临时文件跑 Python re，产出与 rg 分支同构的 per_file。

            语义差异（由警告横幅显式声明）：Python re 无多行并行、无 rg 的
            智能特化，大目录会明显更慢。命中形状（每文件 [(0-based 行, 正文)]）与
            rg 分支一致——取行首匹配起点的 0-based 字符列，与 rg 的
            _byte_col_to_char_col 同义（目录模式不消费列，仅保持同构）。
            非法正则抛 RuntimeError（与 rg 分支同口径，绝不静默返回空结果）。
            """
            pattern = re.escape(query) if fixed_string else query
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                rx = re.compile(pattern, flags)
            except re.error as exc:
                raise RuntimeError(f"回退引擎（Python re）无法编译该正则：{exc}；原始 query={query!r}")
            acc: dict[str, list[tuple[int, str]]] = {}
            for tmp_name, orig in mapping.items():
                try:
                    with open(os.path.join(tmp, tmp_name), encoding="utf-8") as fh:
                        content = fh.read()
                except OSError:
                    continue
                for i, line in enumerate(content.split("\n")):
                    if rx.search(line):
                        acc.setdefault(orig, []).append((i, line))
            return acc

        args = ["rg", "-n", "--no-heading", "--color", "never"]
        if not case_sensitive:
            args.append("-i")
        if fixed_string:
            args.append("-F")
        if enable_pcre2:
            args.append("--pcre2")
        args += ["--", query, tmp]

        rows, rg_err, rg_kind = _run_rg_raw(args, timeout=60)

        if rows is None:
            if rg_kind == "invalid-regex":
                raise RuntimeError(
                    f"正则表达式非法（rg 拒绝）: {rg_err[:300]}\n"
                    f"  原始 query: {query!r}\n"
                    f"  注：本机未装 rg 时改用 Python re，行为会一并报错，不会静默返回空结果。")
            if rg_kind in ("missing", "timeout"):
                reason = ("rg 不在 PATH 中" if rg_kind == "missing"
                          else "rg 目录搜索超时（>60s）")
                rg_warn = _warn_rg_fallback(reason + "；目录模式为逐文件顺序扫描，明显慢于 rg 并行")
                per_file = _scan_with_python()
            else:
                raise RuntimeError(f"rg 目录搜索失败 (exit≠0): {rg_err[:300]}")
        else:
            for raw in rows:
                c1 = raw.find(":")
                c2 = raw.find(":", c1 + 1)
                if c1 <= 0 or c2 <= 0:
                    continue
                # rg 输出完整临时路径，取 basename 匹配 mapping
                tmp_name = raw[:c1].rsplit('/', 1)[-1]
                try:
                    ln = int(raw[c1 + 1:c2]) - 1      # rg 1-based → 0-based
                except ValueError:
                    continue
                orig = mapping.get(tmp_name, tmp_name)
                per_file.setdefault(orig, []).append((ln, raw[c2 + 1:]))

    ordered = sorted(per_file.items(), key=lambda kv: -len(kv[1]))
    head = [
        # 降级横幅置顶：与单文件口径一致，不能让"引擎降级"藏在命中数后面
        *([rg_warn] if rg_warn else []),
        f"在 {search_dir} 中搜索 '{query}'（{len(files)} 个文件"
        + (f"，跳过 {skipped} 个读取失败" if skipped else "") + "）：",
        f"命中 {len(ordered)} 个文件：",
        "",
    ]
    out: list[str] = list(head)
    used = sum(_out_cost(s) for s in head)
    for rank, (fp, hits) in enumerate(ordered, 1):
        if max_results > 0 and rank > max_results:
            out.append(f"（还有 {len(ordered) - max_results} 个文件未显示，max_results={max_results}）")
            break
        base = os.path.basename(fp)
        shown = hits[:max_per_file] if max_per_file > 0 else hits
        first = shown[0]
        block = [f"{rank}. {base} —— {len(hits)} 处"
                 f"（首命中 行 {first[0] + 1}: {first[1][:60]}）"]
        for ln, txt in shown[1:]:
            block.append(f"      行 {ln + 1}: {txt[:60]}")
        block.append("")
        cost = sum(_out_cost(s) for s in block)
        if used + cost > _GREP_OUTPUT_BUDGET - 400:
            out.append(f"（已列 {rank - 1}/{len(ordered)} 个文件，余下因 "
                       f"{_GREP_OUTPUT_BUDGET}B 预算闸门折叠；请用 pattern 或更窄的 query 分批取）")
            break
        out.extend(block)
        used += cost
    return [TextContent(type="text", text="\n".join(out))]


# ═══════════════════════════════════════════════════════════
#  read v2：读取实现（从 call_tool 抽出，供 ZIP 成员复用）
# ═══════════════════════════════════════════════════════════

def _read_v2(path: str, arguments: dict) -> list[TextContent]:
    """cronopio_read 的实体：行模式 / 字节模式（互斥）。

    原样从 call_tool 抽出——白名单/存在性检查留在调用方，ZIP 分支解压出
    成员后复用同一条路径，避免第二套读取语义。
    """
    offset_lines: int | None = arguments.get("offset_lines", None)
    max_lines: int | None = arguments.get("max_lines", None)
    offset_bytes: int | None = arguments.get("offset_bytes", None)
    max_bytes: int | None = arguments.get("max_bytes", None)

    line_mode = (offset_lines is not None) or (max_lines is not None)
    byte_mode = (offset_bytes is not None) or (max_bytes is not None)
    if line_mode and byte_mode:
        return [TextContent(
            type="text",
            text="offset_lines/max_lines 与 offset_bytes/max_bytes 不能同时指定。\n"
                 "行操作请用 offset_lines + max_lines；字节操作请用 offset_bytes + max_bytes。"
        )]

    # ── Byte mode ──
    if byte_mode or (not line_mode and not byte_mode and offset_bytes is not None):
        off_b = offset_bytes if offset_bytes is not None else 0
        max_b = max_bytes if max_bytes is not None else 4096

        file_size = os.path.getsize(path)
        if off_b < 0:
            off_b = max(0, file_size + off_b)
        else:
            off_b = min(off_b, file_size)
        if max_b == -1:
            max_b = file_size - off_b

        with open(path, "rb") as fh:
            fh.seek(off_b)
            data = fh.read(max_b)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")

        return [TextContent(
            type="text",
            text=f"[字节 {off_b}-{off_b + len(data)} / {file_size} 字节]\n{text}"
        )]

    # ── Line mode (default) — tombstone-aware ──
    off_l = offset_lines if offset_lines is not None else 0
    max_l = max_lines if max_lines is not None else 200

    all_lines, enc, is_utf8, tombstone_count, total = _read_file_with_tombstones(path)
    start = _resolve_line_offset(total, off_l, clamp_to="start")
    if max_l == -1:
        end = total
    else:
        end = min(total, start + max_l)

    if start >= total:
        return [TextContent(type="text", text=f"偏移量超出文件范围（文件共 {total} 行）。")]

    result = "".join(all_lines[start:end])
    if not result.endswith("\n"):
        result += "\n"

    enc_tag = "" if is_utf8 else f" [原编码: {enc}]"
    tombstone_note = f", 墓碑行: {tombstone_count}" if tombstone_count > 0 else ""
    return [TextContent(
        type="text",
        text=f"[行 {start}-{end - 1} / 共 {total} 行{tombstone_note}{enc_tag}]\n{result}"
    )]


# ═══════════════════════════════════════════════════════════
#  ZIP 容器（DSH Session Log 导出归档）
#
#  官方规范：packages/session-query/session-log-export（archive.ts + README.zh.md）
#    根日志   session[.vN].jsonl                  （v0 无后缀；当前 generation=3）
#    子会话   subagents/<safe-id>/session[.vN].jsonl
#    图片     media/<attachmentId>.<ext>          （attachmentId=sha256:<hex>，内容寻址）
#    通用文件 files/<digest[:2]>/<digest>/<name>
#  归档**不写 manifest**，所以路径本身就是唯一索引。
#
#  分类不靠解压：中央目录（零解压，~3ms）+ 根日志首行（读几十字节，不落盘）。
#  执行靠解压：只写点名的日志成员 → 复用既有真实文件代码路径 → finally 清理。
# ═══════════════════════════════════════════════════════════

_ZIP_MAGIC: tuple[bytes, ...] = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

_ZIP_LOG_BASENAME = re.compile(r"^session(?:\.v([1-9][0-9]*))?\.jsonl$")
_ZIP_LOG_ROOT = re.compile(r"^session(?:\.v([1-9][0-9]*))?\.jsonl$")
_ZIP_LOG_SUB = re.compile(r"^subagents/[^/]+/session(?:\.v([1-9][0-9]*))?\.jsonl$")
_ZIP_MEDIA = re.compile(r"^media/sha256:[0-9a-f]{64}\.(png|jpg|webp|gif)$")
_ZIP_FILE = re.compile(r"^files/[0-9a-f]{2}/[0-9a-f]{64}/[^/]+$")

# 官方 MEDIA_TYPE_EXTENSIONS（archive.ts）
_MEDIA_EXT_BY_TYPE: dict[str, str] = {
    "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif",
}
# 官方 fileEntryPath 的名字净化（archive.ts）
_INVALID_FILE_NAME_CHARS = re.compile(r"[\\/\x00-\x1f\x7f]")

_ZIP_HEAD_PROBE_BYTES = 4096          # 首行证据只读这么多
_ZIP_INVENTORY_MAX_LINES = 30         # 条目清单行数上限（超出折叠）
_ZIP_EXTRACT_MAX_BYTES = 200 * 1024 * 1024   # 解压总量上限（声明尺寸预筛）
_ZIP_MEMBER_MAX_BYTES = 200 * 1024 * 1024    # 单成员实际读取上限（权威闸）


def _is_zip_archive(path: str) -> bool:
    """按魔数判 ZIP——不看后缀（导出文件常被改名，如本次的中文名）。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) in _ZIP_MAGIC
    except OSError:
        return False


def _zip_log_version(name: str) -> int:
    """规范文件名的 generation：session.jsonl → 0，session.vN.jsonl → N。"""
    m = _ZIP_LOG_BASENAME.match(name.rsplit("/", 1)[-1])
    return int(m.group(1)) if (m and m.group(1)) else 0


def _zip_log_members(members: list[str]) -> list[str]:
    """会话日志成员：根日志优先（generation 最大者即在用者），随后子会话。"""
    roots = sorted((m for m in members if _ZIP_LOG_ROOT.match(m)),
                   key=lambda m: -_zip_log_version(m))
    return roots + [m for m in members if _ZIP_LOG_SUB.match(m)]


def _zip_member_role(name: str) -> str:
    """成员角色：log-root / log-sub / media / file / other。"""
    if _ZIP_LOG_ROOT.match(name):
        return "log-root"
    if _ZIP_LOG_SUB.match(name):
        return "log-sub"
    if _ZIP_MEDIA.match(name):
        return "media"
    if _ZIP_FILE.match(name):
        return "file"
    return "other"


def _zip_role_tag(name: str) -> str:
    return {"log-root": "日志", "log-sub": "子会话日志",
            "media": "图片", "file": "文件"}.get(_zip_member_role(name), "其他")


def _zip_probe(zip_path: str) -> dict:
    """中央目录分类（零解压）+ 根日志首行证据（读几十字节，不落盘）。

    Returns:
        kind    — 'dsh-session-export' | 'dsh-session-export-corrupt' | 'generic-zip'
                  形状像导出但首行不是 session 头 → corrupt（fail-loud，不静默降级）
        members — 全部文件条目名（中央目录顺序，排除目录条目）
        sizes   — 条目名 → 声明解压尺寸（**自称值**，只作预筛）
        logs/media/files/other — 分桶成员名
        header  — 根日志首行解析结果（kind=export 时非 None）
    """
    with zipfile.ZipFile(zip_path) as zf:
        pairs = [(i.filename, i.file_size) for i in zf.infolist() if not i.is_dir()]
        members = [n for n, _ in pairs]
        sizes = dict(pairs)
        logs = _zip_log_members(members)
        media = [m for m in members if _ZIP_MEDIA.match(m)]
        files = [m for m in members if _ZIP_FILE.match(m)]
        other = [m for m in members if _zip_member_role(m) == "other"]

        kind, header = "generic-zip", None
        if logs:
            kind = "dsh-session-export-corrupt"
            try:
                with zf.open(logs[0]) as fh:
                    first = fh.read(_ZIP_HEAD_PROBE_BYTES).split(b"\n", 1)[0]
                rec = json.loads(first.decode("utf-8", errors="replace"))
                if isinstance(rec, dict) and rec.get("type") == "session":
                    kind, header = "dsh-session-export", rec
            except Exception:
                pass
    return {"kind": kind, "members": members, "sizes": sizes, "logs": logs,
            "media": media, "files": files, "other": other, "header": header}


def _zip_extract_members(zip_path: str, members: list[str], dest_dir: str,
                         prefix: str = "m") -> dict[str, str]:
    """把点名的成员解压到 dest_dir，返回 {成员名: 实盘路径}。

    双闸守卫：
      1) 中央目录声明尺寸预筛（零成本）——超 _ZIP_EXTRACT_MAX_BYTES 直接拒绝；
      2) 逐成员实际读取计数（权威）——声明尺寸是"自称"、可伪造，只靠它会吃 zip bomb。
    输出文件名恒为 prefix+序号（自铸、无冒号、无扩展名），**绝不采信成员名**：
    既让 zip-slip 路径穿越结构性不可能，也避开 rg 输出的冒号歧义
    （DSH 的 media 成员名天生带冒号：sha256:…）。
    """
    written: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as zf:
        infos = {i.filename: i for i in zf.infolist()}
        declared = sum(infos[m].file_size for m in members if m in infos)
        if declared > _ZIP_EXTRACT_MAX_BYTES:
            raise RuntimeError(
                f"ZIP 声明解压总量 {declared:,} 字节，超出上限 "
                f"{_ZIP_EXTRACT_MAX_BYTES:,} —— 拒绝解压（只列清单）")
        for m in members:
            info = infos.get(m)
            if info is None or info.is_dir():
                continue
            dest = os.path.join(dest_dir, f"{prefix}{len(written):04d}")
            total = 0
            with zf.open(m) as src, open(dest, "wb") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _ZIP_MEMBER_MAX_BYTES:
                        raise RuntimeError(
                            f"ZIP 成员 {m!r} 实际解压超上限 {_ZIP_MEMBER_MAX_BYTES:,} 字节"
                            f"（声明仅 {info.file_size:,}）—— 疑似 zip bomb，拒绝")
                    dst.write(chunk)
            written[m] = dest
    return written


@contextlib.contextmanager
def _zip_scratch():
    """私有临时目录：退出时无条件清理（解压 → 跑 → 删，全程无状态）。"""
    d = tempfile.mkdtemp(prefix="ffs_zip_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _zip_file_entry_path(attachment_id: str, name: str) -> str:
    """复刻官方 fileEntryPath（archive.ts）：digest 去 sha256: 前缀 + 名字净化。"""
    digest = re.sub(r"^sha256:", "", str(attachment_id))
    safe = _INVALID_FILE_NAME_CHARS.sub("_", str(name))
    if safe in (".", "..", ""):
        safe = "file"
    return f"files/{digest[:2]}/{digest}/{safe}"


def _zip_walk_attachment_paths(node, found: set[str]) -> None:
    """深度遍历一个事件对象，把附件引用折算成归档条目名。

    与官方 collectAttachmentRefs 同语义：image → media/<id>.<ext>，
    file → files/<digest[:2]>/<digest>/<name>。嵌套 content 一并下钻。
    """
    if isinstance(node, dict):
        att = node.get("attachment")
        if isinstance(att, dict):
            aid = att.get("attachmentId")
            if node.get("type") == "image" and isinstance(aid, str):
                ext = _MEDIA_EXT_BY_TYPE.get(str(att.get("mediaType")))
                if ext:
                    found.add(f"media/{aid}.{ext}")
            elif node.get("type") == "file" and isinstance(aid, str) \
                    and isinstance(att.get("name"), str):
                found.add(_zip_file_entry_path(aid, att["name"]))
        for v in node.values():
            _zip_walk_attachment_paths(v, found)
    elif isinstance(node, list):
        for v in node:
            _zip_walk_attachment_paths(v, found)


def _zip_collect_referenced_attachments(log_path: str) -> set[str]:
    """从日志正文收集被引用的附件条目名（只 json.loads 含 attachmentId 的行）。"""
    found: set[str] = set()
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "attachmentId" not in line:
                continue
            try:
                _zip_walk_attachment_paths(json.loads(line), found)
            except Exception:
                continue
    return found


def _zip_media_audit_lines(log_path: str, media: list[str],
                           files: list[str]) -> list[str]:
    """媒体引用校验：日志引用的附件 vs 归档实际条目 —— 缺一个就显式报。

    这是导出"是否漏附件"的 fail-loud 信号（归档内容寻址、无 manifest，
    所以只能靠日志反查）。
    """
    refs = _zip_collect_referenced_attachments(log_path)
    present = set(media) | set(files)
    missing = sorted(refs - present)
    imgs = sum(1 for r in refs if r.startswith("media/"))
    out: list[str] = []
    if not refs:
        out.append("媒体引用: 该会话日志未引用任何附件。")
    elif not missing:
        out.append(f"媒体引用校验: ✅ {len(refs)}/{len(refs)} 齐全"
                   f"（图片 {imgs}，通用文件 {len(refs) - imgs}）")
    else:
        out.append(f"媒体引用校验: ⚠️ 缺 {len(missing)}/{len(refs)} —— "
                   f"日志引用了但归档里没有（导出漏附件）：")
        for m in missing[:10]:
            out.append(f"    · {_zip_display_name(m)}")
        if len(missing) > 10:
            out.append(f"    （还有 {len(missing) - 10} 个未列出）")
    extra = sorted(set(media) - refs)
    if extra:
        out.append(f"（归档另有 {len(extra)} 个未被日志引用的图片条目）")
    return out


def _zip_display_name(name: str) -> str:
    """清单显示名：media 的 64 位 sha256 缩写（前 12 + … + 后 6），日志名保持原样。

    图片成员本就不可作文本读取，缩写只为省预算；前缀 12 位足以区分。
    """
    if _ZIP_MEDIA.match(name) is None:
        return name
    return re.sub(r"sha256:([0-9a-f]{12})[0-9a-f]{46}([0-9a-f]{6})",
                  r"sha256:\1…\2", name)


def _zip_inventory_lines(probe: dict, target: str | None = None,
                         skip: str | None = None,
                         max_lines: int = _ZIP_INVENTORY_MAX_LINES) -> list[str]:
    """条目清单：成员名 + 声明尺寸 + 角色。受行数上限约束，超出折叠。"""
    members = [m for m in probe["members"] if m != skip]
    out: list[str] = []
    for idx, m in enumerate(members):
        if idx >= max_lines:
            out.append(f"    …（另有 {len(members) - idx} 个条目未列出）")
            break
        mark = "   ← 扫描目标" if m == target else ""
        out.append(f"    · {_zip_display_name(m)}"
                   f"  [{probe['sizes'].get(m, 0):,} B, {_zip_role_tag(m)}]{mark}")
    return out


def _zip_candidate_hint(probe: dict, bad_member: str) -> str:
    """member 不匹配时的可操作提示（列出真实候选，不猜）。"""
    lines = [
        f"member 不存在: {bad_member!r}",
        f"（{probe['kind']}，共 {len(probe['members'])} 个条目；会话日志 {len(probe['logs'])} 个）",
        "",
        "候选条目（member=<成员名>）：",
    ]
    lines += _zip_inventory_lines(probe, max_lines=15)
    return "\n".join(_escape_zone(l) for l in lines)


def _zip_pick_member(probe: dict, member: str | None) -> tuple[str | None, str | None]:
    """选目标成员 → (member, 错误文本)。不给 member 则默认根日志。"""
    if member is None:
        if not probe["logs"]:
            return None, (f"{probe['kind']}：无会话日志成员（根日志 session[.vN].jsonl 不存在）"
                          f"—— 非 DSH Session Log 导出。")
        return probe["logs"][0], None
    if member not in probe["members"]:
        if member.startswith("media/"):
            return None, (f"⚠️ {_zip_display_name(member)} 是二进制图片成员"
                          f"（清单里的名字是 sha256 缩写），不可作文本读取/搜索。"
                          f"需要看图请先落盘再用 read_image。")
        return None, _zip_candidate_hint(probe, member)
    if _ZIP_MEDIA.match(member):
        return None, (f"⚠️ {_zip_display_name(member)} 是二进制图片成员，不可作文本读取/搜索。"
                      f"（需要看图请先落盘再用 read_image）")
    return member, None


def _scan_zip(path: str, max_results: int, detail_lines: str | None,
              debug: str | None, member: str | None) -> list[TextContent]:
    """ZIP 分支：中央目录分类 → 解压目标成员 → 复用 dsh JSONL 双区扫描。

    容器头（清单 + 媒体校验）经 container_prelude 交给 _scan_dsh_jsonl 的
    emit 输出，因此与索引区**共享同一个输出预算**。
    """
    try:
        probe = _zip_probe(path)
    except zipfile.BadZipFile as exc:
        return [TextContent(type="text", text=f"ZIP 解析失败（损坏或非 ZIP）: {path}\n{exc}")]
    except OSError as exc:
        return [TextContent(type="text", text=f"ZIP 打开失败: {path}\n{exc}")]

    head: list[str] = [
        f"文件: {path}",
        f"ZIP 容器: {probe['kind']}",
        f"条目: {len(probe['members'])}（会话日志 {len(probe['logs'])}"
        f" + 图片 {len(probe['media'])} + 通用文件 {len(probe['files'])}"
        f" + 其他 {len(probe['other'])}）",
    ]

    if probe["kind"] != "dsh-session-export":
        head.append("")
        if probe["kind"] == "generic-zip":
            head.append("→ 中央目录无根日志 session[.vN].jsonl，非 DSH Session Log 导出。")
            head.append("  只列清单，不做索引/压缩扫描（不装懂）；搜文本请用 grep。")
        else:
            head.append("→ ⚠️ 形状像 DSH 导出，但根日志首行不是 session 头"
                        "（疑似损坏或仿冒）——拒绝按会话日志扫描。")
        head.append("")
        head += _zip_inventory_lines(probe)
        return [TextContent(type="text", text="\n".join(_escape_zone(l) for l in head))]

    target, err = _zip_pick_member(probe, member)
    if err is not None:
        return [TextContent(type="text", text="\n".join(
            _escape_zone(l) for l in head + ["", err]))]

    with _zip_scratch() as tmp:
        try:
            written = _zip_extract_members(path, [target], tmp, prefix="m")
        except (RuntimeError, zipfile.BadZipFile) as exc:
            return [TextContent(type="text", text="\n".join(
                _escape_zone(l) for l in head + ["", f"⚠️ {exc}"]))]
        log_path = written.get(target)
        if log_path is None:
            return [TextContent(type="text", text="\n".join(
                _escape_zone(l) for l in head + ["", f"⚠️ 成员解压失败: {target}"]))]

        head.append(f"    · {target}  [{os.path.getsize(log_path):,} B, "
                    f"{_zip_role_tag(target)}]   ← 扫描目标")
        head += _zip_inventory_lines(probe, target=target, skip=target)
        head += _zip_media_audit_lines(log_path, probe["media"], probe["files"])
        head.append("（解压到私有临时目录，本调用结束即删；媒体不落盘）")
        head.append("")
        res = _scan_dsh_jsonl(log_path, max_results, detail_lines, debug,
                              container_prelude=head)
    return res


def _grep_zip_file(arguments: dict) -> list[TextContent]:
    """cronopio_grep 的 ZIP 分支：解压单个成员 → 复用既有 rg 管线。"""
    zip_path = _normalize_path(arguments["path"])
    try:
        probe = _zip_probe(zip_path)
    except zipfile.BadZipFile as exc:
        return [TextContent(type="text", text=f"ZIP 解析失败（损坏或非 ZIP）: {zip_path}\n{exc}")]
    except OSError as exc:
        return [TextContent(type="text", text=f"ZIP 打开失败: {zip_path}\n{exc}")]

    target, err = _zip_pick_member(probe, arguments.get("member"))
    if err is not None:
        return [TextContent(type="text", text=err)]

    # 前缀行进预算核算；内部路径（临时文件）折回可读的容器引用，不留死路径
    tag = f"[ZIP 成员: {target} @ {os.path.basename(zip_path)}]"
    label = f"{os.path.basename(zip_path)}::{target}"
    with _zip_scratch() as tmp:
        try:
            written = _zip_extract_members(zip_path, [target], tmp, prefix="m")
        except (RuntimeError, zipfile.BadZipFile) as exc:
            return [TextContent(type="text", text=f"⚠️ {exc}")]
        log_path = written.get(target)
        if log_path is None:
            return [TextContent(type="text", text=f"⚠️ 成员解压失败: {target}")]
        inner = dict(arguments)
        inner["path"] = log_path
        res = _grep_file_v2(inner, prefix=tag)
    return [TextContent(type="text",
                        text="\n".join(c.text for c in res).replace(log_path, label))]


def _read_zip(path: str, arguments: dict) -> list[TextContent]:
    """ZIP 分支：不给 member → 回条目清单；给 → 解压该成员走 _read_v2。"""
    try:
        probe = _zip_probe(path)
    except zipfile.BadZipFile as exc:
        return [TextContent(type="text", text=f"ZIP 解析失败（损坏或非 ZIP）: {path}\n{exc}")]
    except OSError as exc:
        return [TextContent(type="text", text=f"ZIP 打开失败: {path}\n{exc}")]

    head: list[str] = [
        f"文件: {path}",
        f"ZIP 容器: {probe['kind']}, 条目 {len(probe['members'])}"
        f"（会话日志 {len(probe['logs'])} + 图片 {len(probe['media'])}"
        f" + 通用文件 {len(probe['files'])} + 其他 {len(probe['other'])}）",
    ]

    if arguments.get("member") is None:
        head.append("")
        head.append("未指定 member —— 请从下列条目中选一个"
                    "（member=<成员名>；图片名已缩写，且不可作文本读取）：")
        head += _zip_inventory_lines(probe)
        return [TextContent(type="text", text="\n".join(_escape_zone(l) for l in head))]

    target, err = _zip_pick_member(probe, arguments.get("member"))
    if err is not None:
        return [TextContent(type="text", text=err)]

    with _zip_scratch() as tmp:
        try:
            written = _zip_extract_members(path, [target], tmp, prefix="m")
        except (RuntimeError, zipfile.BadZipFile) as exc:
            return [TextContent(type="text", text=f"⚠️ {exc}")]
        log_path = written.get(target)
        if log_path is None:
            return [TextContent(type="text", text=f"⚠️ 成员解压失败: {target}")]
        res = _read_v2(log_path, arguments)
    tag = f"[ZIP 成员: {target} @ {os.path.basename(path)}]\n"
    return [TextContent(type="text", text=tag + "\n".join(_escape_zone(l) for l in head)
                        + "\n\n" + "\n".join(c.text for c in res))]


# ═══════════════════════════════════════════════════════════
#  cronopio_scan v2：格式分流 + 压缩记录 + U+2591 转义
# ═══════════════════════════════════════════════════════════

def _escape_zone(text: str) -> str:
    """输出层转义：U+2591 → [U+2591]，防止工具结果污染会话日志。"""
    return text.replace("\u2591", "[U+2591]")


def _detect_file_format(path: str) -> str:
    """检测文件格式: 'dsh-jsonl' | 'json-doc' | 'text'。

    `.zstd`（会话日志 session.vN.jsonl.zstd 是多帧拼接）先解压再嗅探。
    """
    try:
        head = _read_maybe_zstd(path, 4096)
    except (OSError, RuntimeError):
        return "text"
    head_text = head.decode("utf-8", errors="replace")
    lines = head_text.splitlines()
    jsonl_hits = 0
    for ln in lines[:5]:
        ln = ln.strip()
        if ln.startswith("{") and ('"type":"session"' in ln or '"seq"' in ln
                                   or '"type":"compaction/' in ln):
            jsonl_hits += 1
    if jsonl_hits >= 2:
        return "dsh-jsonl"
    stripped = head_text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        return "json-doc"
    return "text"


def _extract_compactions(lines: list[str]) -> list[dict]:
    """全量解析 jsonl，提取 compaction 事务（compactionId 配对）。

    返回按出现顺序的列表；status: completed / orphan（有 start 无 summary）。
    summary_text 为完整摘要全文（可能多 text 块拼接），供预算内全量输出。
    """
    records: dict[str, dict] = {}
    order: list[str] = []
    for lineno, line in enumerate(lines):
        line = line.strip()
        if not line or '"type":"compaction/' not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = rec.get("type", "")
        data = rec.get("data") or {}
        cid = data.get("compactionId")
        if not cid:
            continue
        if t == "compaction/start":
            records[cid] = {
                "compactionId": cid,
                "start_line": lineno,
                "turn": data.get("turn"),
                "status": "orphan",
            }
            order.append(cid)
        elif t == "compaction/summary" and cid in records:
            summary_text = ""
            try:
                blocks = data.get("summary") or []
                parts = [str(b.get("text", "")) for b in blocks
                         if isinstance(b, dict) and b.get("type") == "text"]
                summary_text = "\n".join(parts)
            except Exception:
                summary_text = ""
            records[cid].update({
                "summary_line": lineno,
                "shadowedRange": data.get("shadowedRange"),
                "shadowedSeqs": data.get("shadowedSeqs", []),
                "shadowedTokenCount": data.get("shadowedTokenCount"),
                "model": data.get("model"),
                "maxTokens": data.get("maxTokens"),
                "summary_text": summary_text,
                "status": "completed",
            })
        elif t == "compaction/end" and cid in records:
            records[cid]["end_line"] = lineno
    return [records[c] for c in order]


def _jsonl_extract_texts_excluding(rec: dict, audit_keys: set) -> list[str]:
    """与 _jsonl_extract_texts 同语义，但用调用方给定的排除集（调试对比用）。"""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                found.append(node["text"])
            else:
                for k, v in node.items():
                    if k in audit_keys:
                        continue
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(rec)
    return found


def _jsonl_extract_texts(rec: dict) -> list[str]:
    """从一条 jsonl 记录中提取所有面向模型的内容文本。

    深度遍历，收集形如 {"type": "text", "text": "..."} 的叶子块文本，
    保持出现顺序。用于把 jsonl 结构噪音洗掉、只留人读内容。

    排除三类"非正文"字段，三者都是同一内容的重复副本：
      · rawOutput      — LLM 调用的完整原始输出（审计转储）
      · llmStreamCall  — 同上，标记该次调用
                         （compaction/summary 里与 data.summary 重复）
      · stream         — 会话格式 v2 起，assistant/message 内嵌的 provider
                         原始流；其中 chunk.block 与 message.content 逐字相同
                         （DSH 官方 extractSessionEventText 也只读 content）
    实测：若不排除 stream，v3 会话的索引块会被整整抄两遍
    （15,216B → 7,608B），吃掉 35% 的输出预算。
    """
    _AUDIT_KEYS = {"rawOutput", "llmStreamCall", "stream"}
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                found.append(node["text"])
            else:
                for k, v in node.items():
                    if k in _AUDIT_KEYS:
                        continue
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(rec)
    return found


def _render_shadow_pointer(r: dict, brief: bool = False) -> list[str]:
    """输出"这个压缩点遮蔽了哪些 seq"——即回查原文的指针（D1）。

    有了它，模型可以用官方 session_event_read(seq=...) 或 scan 的
    detail_lines 把被压掉的原文原样取回，而不止拿到一句摘要。
    """
    seqs: list[int] = [s for s in (r.get("shadowedSeqs") or []) if isinstance(s, int)]
    if not seqs:
        return []
    # 用词注意：shadowedSeqs 是日志里 compaction/summary 自报的，**不是已核实的权威集合**。
    # 实测（6 个已迁移会话）：自报集与独立 fold 重放互不包含（如 240 vs 219），
    # 且本机全部带压缩的会话都是 v0→v3 迁移产物，payload 可能是迁移期重编号的结果。
    # 在这一点澄清之前，只能称"日志自报"，并提示模型核对。
    out = [f"被遮蔽 seq（{len(seqs)} 个，日志自报、未核实）: "
           + ", ".join(str(s) for s in seqs[:12])
           + (f" …(+{len(seqs) - 12})" if len(seqs) > 12 else "")]
    # 覆盖范围由 seq 集合算出（稀疏，只作量级提示）
    out.append(f"覆盖 seq 跨度: {min(seqs)}..{max(seqs)}（稀疏，非连续）")
    if brief:
        return out[:2]
    # shadowedRange 是 surface 位置跨度，不是 seq 区间：start 可以大于 end。
    # 单列出来并写明含义，避免被当成 seq 区间去 read。
    rng = r.get("shadowedRange") or {}
    if isinstance(rng, dict) and rng.get("start") is not None and rng.get("end") is not None:
        out.append(f"surface 位置跨度: {rng.get('start')} → {rng.get('end')}"
                   "（位置跨度，非 seq 区间；替换过的区间 start 会大于 end。取原文用上面的 seq）")
    return out


# ═══ DEBUG-BEGIN 测试期仪表 ══════════════════════════════════════════════
# 仅供"压缩→回忆"验收测试使用（验证 D10 收益、预筛测试关键词）。
# 产品化时删除：本区块 + inputSchema 的 debug 属性 + _scan_v2 / _scan_dsh_jsonl
# 的 debug 转发。normal 模式下这些代码一行都不会执行。
_DEBUG_MODES = ("stats", "keywords")
_DEBUG_KEYWORD_CAP = 18


def _partition_session(lines: list[str]) -> tuple[dict, set, list, dict]:
    """把会话行切成"被压区 vs 当前表层"。

    Returns: (seq→record, shadowed_seqs, checkpoint_seqs, type_counts)
    被遮蔽判定取自 compaction/summary.shadowedSeqs（与 DSH foldSurface 同源语义，
    但不做因果重放，仅用于测试期统计）。
    """
    records: dict = {}
    shadowed: set = set()
    checkpoints: list = []
    counts: dict = {}
    for i, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        rtype = str(rec.get("type", "?"))
        counts[rtype] = counts.get(rtype, 0) + 1
        seq = rec.get("seq")
        records[seq if isinstance(seq, int) else i] = rec
        if rtype == "compaction/summary":
            for s in (rec.get("data") or {}).get("shadowedSeqs", []) or []:
                if isinstance(s, int):
                    shadowed.add(s)
        if rtype == "user/message":
            src = (rec.get("data") or {}).get("source") or {}
            if isinstance(src, dict) and src.get("plugin") == "compact":
                checkpoints.append(seq if isinstance(seq, int) else i)
    return records, shadowed, checkpoints, counts


def _render_debug(lines: list[str], mode: str, emit, path: str,
                  raw_size: int, plain_size: int) -> None:
    """渲染测试期仪表输出（stats / keywords）。"""
    records, shadowed, checkpoints, counts = _partition_session(lines)
    surf = {s: r for s, r in records.items() if s not in shadowed}

    def text_of(rec: dict) -> str:
        return "\n".join(_jsonl_extract_texts(rec))

    shadowed_text = "\n".join(text_of(records[s]) for s in shadowed if s in records)
    surface_text = "\n".join(text_of(r) for r in surf.values())

    emit(f"═══ debug: {mode} ═══")
    emit(f"文件: {path}")
    if raw_size != plain_size:
        emit(f"zstd: 磁盘 {raw_size:,}B → 解压 {plain_size:,}B")
    emit(f"行数: {len(lines)}")
    emit(f"事件类型: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:12]))
    emit(f"compaction/start={counts.get('compaction/start', 0)} "
         f"summary={counts.get('compaction/summary', 0)} "
         f"end={counts.get('compaction/end', 0)} "
         f"prune={counts.get('compaction/prune', 0)}")
    emit(f"checkpoint(plugin=compact 的 user/message): {len(checkpoints)}")
    seq_note = ""
    if shadowed:
        lo, hi = min(shadowed), max(shadowed)
        seq_note = f" | seq 跨度 {lo}..{hi}（稀疏）"
        if hi >= len(lines):
            seq_note += " ⚠️ 超出行号范围：此文件的 shadowedSeqs 不在行号空间"
            seq_note += "（v0 分块逻辑编号），不能当行号指针用——需用 v3 会话"
    emit(f"被遮蔽 seq: {len(shadowed)} 个" + seq_note)
    emit(f"文本量: 被压区 {len(shadowed_text.encode()):,}B | 当前表层 {len(surface_text.encode()):,}B")

    if mode == "stats":
        # D10 收益核算：现行排除集 vs 加上 stream
        stream_lines = sum(1 for r in lines if '"stream":' in r)
        emit(f"含 \"stream\": 的行: {stream_lines} 行")
        if stream_lines:
            # 注意：_jsonl_extract_texts 本身已排除 stream（补丁 1 的修复）。
            # 对比的两端必须是「旧行为」与「新行为」，不能两边都排除。
            legacy = sum(
                len(t.encode()) for r in records.values()
                for t in _jsonl_extract_texts_excluding(r, {"rawOutput", "llmStreamCall"})
            )
            current = sum(len(t.encode()) for r in records.values() for t in _jsonl_extract_texts(r))
            saved = legacy - current
            pct = (saved / legacy * 100) if legacy else 0.0
            emit(f"清洗字节: 旧行为(不排 stream) {legacy:,}B → 现行 {current:,}B "
                 f"(省 {saved:,}B, {pct:.1f}%)")
        else:
            emit("清洗字节: 本文件无 stream 字段（v0 格式），D10 无影响")
        return

    # mode == keywords：只在被压区出现的候选词（供选测试关键词）
    emit("")
    emit("─── 只在被压区出现、当前表层已看不到的候选词 ───")
    hit = 0
    zh: dict = {}
    for s in re.findall(r"[\u4e00-\u9fff]{4,}", shadowed_text):
        for i in range(len(s) - 3):
            g = s[i:i + 4]
            if g not in surface_text:
                zh[g] = zh.get(g, 0) + 1
    top_zh = sorted(zh.items(), key=lambda kv: -kv[1])[:_DEBUG_KEYWORD_CAP]
    if top_zh:
        emit("中文4字串: " + "  ".join(f"{w}({c})" for w, c in top_zh))
        hit += len(top_zh)
    en: dict = {}
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_./-]{7,}", shadowed_text):
        if w not in surface_text:
            en[w] = en.get(w, 0) + 1
    top_en = sorted(en.items(), key=lambda kv: -kv[1])[:_DEBUG_KEYWORD_CAP]
    if top_en:
        emit("英数标识符: " + "  ".join(f"{w}({c})" for w, c in top_en))
        hit += len(top_en)
    if not hit:
        emit("（无候选——该会话可能没有真正的「被压掉且表层不可见」的内容）")
    emit("")
    emit("用法：挑一个语义词，去测 session_event_search(surfaces=[\"shadowed\"]) 能否命中。")
# ═══ DEBUG-END ═══════════════════════════════════════════════════════════


def _render_compaction_report(records: list[dict]) -> list[str]:
    """渲染压缩点报告（人读性在输出端补回，文件本身是 JSONL 不可读）。

    完整版：每个压缩点含完整 summary 全文（供预算内恢复上下文）。
    """
    if not records:
        return []
    orphan = sum(1 for r in records if r["status"] == "orphan")
    out = ["", f"压缩记录: {len(records)} 次" + (f"（其中 {orphan} 次孤儿/失败）" if orphan else "") + ":"]
    out.append("（摘要是有损的，可能失真；被遮蔽的原文仍在日志里——"
               "用 session_event_read(seq=<被遮蔽 seq>) 或官方 session_event_search(surfaces=[\"shadowed\"]) 取回。）")
    for i, r in enumerate(records, 1):
        out.append(f"─── 压缩点 {i} (start 行 {r['start_line']}, {r['status']}) ───")
        out.append(f"compactionId: {r['compactionId']}")
        if r.get("turn") is not None:
            out.append(f"turn: {r['turn']}")
        if r.get("shadowedTokenCount") is not None:
            out.append(f"遮蔽: {len(r.get('shadowedSeqs', []))} 条消息 · ~{r['shadowedTokenCount']} tokens"
                       + (f" · {r['model']}" if r.get("model") else ""))
        for line in _render_shadow_pointer(r):
            out.append(line)
        if r.get("summary_text"):
            out.append("─── 完整摘要 ───")
            out.append(r["summary_text"])
        out.append("")
    return out


def _render_compaction_degraded(records: list[dict], head_chars: int = 300) -> list[str]:
    """降级版压缩点报告：每个压缩点只给位置 + 摘要开头，提醒按行号取全。

    预算不够装全量 summary 时使用；head_chars 控制每块摘要开头长度。
    """
    if not records:
        return []
    orphan = sum(1 for r in records if r["status"] == "orphan")
    out = ["", f"压缩记录: {len(records)} 次" + (f"（其中 {orphan} 次孤儿/失败）" if orphan else "") + ":"]
    out.append("⚠️ 有损摘要，可能失真；如有需要请进一步查询。"
               "被遮蔽的原文仍在日志里——用 session_event_read(seq=<被遮蔽 seq>) 或 "
               "官方 session_event_search(surfaces=[\"shadowed\"]) 取回原文核对。")
    for i, r in enumerate(records, 1):
        out.append(f"─── 压缩点 {i} (start 行 {r['start_line']}, summary 行 {r.get('summary_line', '-')}, {r['status']}) ───")
        out.append(f"compactionId: {r['compactionId']}")
        if r.get("turn") is not None:
            out.append(f"turn: {r['turn']}")
        if r.get("shadowedTokenCount") is not None:
            out.append(f"遮蔽: {len(r.get('shadowedSeqs', []))} 条消息 · ~{r['shadowedTokenCount']} tokens"
                       + (f" · {r['model']}" if r.get("model") else ""))
        for line in _render_shadow_pointer(r, brief=True):
            out.append(line)
        if r.get("summary_text"):
            out.append(f"摘要开头: {r['summary_text'][:head_chars]}")
        out.append("")
    out.append("（压缩点摘要超出返回预算，仅显示开头。"
               "需要完整摘要请用 scan 的 detail_lines 参数按行号提取，"
               "或 read 对应 summary 行号解析 JSON。）")
    return out


def _scan_indices_text(path: str, max_results: int, fmt: str) -> list[TextContent]:
    """普通文本 / 单文档 JSON：扫对话索引（新版 U+2591 优先，旧版回退）。

    输出中 U+2591 转义为 [U+2591]（防污染）；行号 1-based（对齐 grep 生态）。
    """
    file_size = os.path.getsize(path)
    if path.lower().endswith(".zstd"):
        blob = _read_maybe_zstd(path)
        lines = blob.decode("utf-8", errors="replace").splitlines(keepends=True)
        enc, is_utf8 = "utf-8(zstd)", True
    else:
        enc, is_utf8 = _detect_encoding(path)
        with open(path, encoding=enc, errors="replace") as fh:
            lines = fh.readlines()
    total = len(lines)

    new_matches: list[int] = []
    for i in range(total - 1, -1, -1):
        if "░ 对话索引 ░" in lines[i]:
            new_matches.append(i)
    new_matches.reverse()

    old_matches: list[int] = []
    if not new_matches:
        for i in range(total - 1, -1, -1):
            if "对话索引" in lines[i]:
                old_matches.append(i)
        old_matches.reverse()

    matches = new_matches if new_matches else old_matches
    format_tag = "new (░ 标记)" if new_matches else "old (纯文本)"

    if not matches:
        return [TextContent(type="text", text=(
            f"未找到对话索引: {path}\n"
            f"文件: {total} 行, {file_size:,} 字节, 编码: {enc}\n"
            f"已搜索新版格式 `# ░ 对话索引 ░` 和旧版格式 `# 对话索引`（宽松）。"))]

    if max_results > 0 and len(matches) > max_results:
        matches = matches[-max_results:]

    fmt_note = " [单文档 JSON，索引在 content 字段]" if fmt == "json-doc" else ""
    enc_note = "" if is_utf8 else f", 编码: {enc}"
    out_lines = [
        f"文件: {path}",
        f"行数: {total}, 大小: {file_size:,} 字节{enc_note}",
        f"格式: {format_tag}{fmt_note}",
        f"",
        f"（注：索引标记 U+2591 已转义为 [U+2591] 显示；文件内容未变）",
        f"",
        f"找到 {len(matches)} 处对话索引 (max_results={max_results}):",
        f"",
    ]

    for idx, line_no in enumerate(matches):
        out_lines.append(f"─── 匹配 {idx + 1} (行 {line_no + 1}, 格式: {format_tag}) ───")
        metadata_lines: list[str] = []
        for j in range(line_no, min(total, line_no + 5)):
            stripped = lines[j].rstrip("\n\r")
            if stripped.startswith("**索引时间**") or stripped.startswith("**文档保存**"):
                metadata_lines.append(stripped)
        if metadata_lines:
            for ml in metadata_lines:
                out_lines.append(ml)
            out_lines.append("")
        ctx_start = max(0, line_no - 3)
        ctx_end = min(total, line_no + 41)
        for j in range(ctx_start, ctx_end):
            marker = ">>>" if j == line_no else "   "
            stripped = lines[j].rstrip("\n\r")
            # 上下文行标签 1-based：scan 输出整体是 1-based（见本函数匹配块），
            # 这里漏掉就会同一次输出里两种口径
            out_lines.append(f"{marker} {j + 1:6d}: {_escape_zone(stripped)}")
        out_lines.append("")

    if max_results > 0:
        raw_count = len(new_matches) if new_matches else len(old_matches)
        if raw_count > len(matches):
            out_lines.append(f"（还有 {raw_count - len(matches)} 处匹配未显示。"
                             f"如需查看全部，设置 max_results=0）")
    return [TextContent(type="text", text="\n".join(out_lines))]


# dsh JSONL 分支的输出预算：与 grep 同口径（见上方「输出预算闸门」块，随 DSH 版本换轴）
_JSONL_SCAN_BUDGET = _OUT_BUDGET

# ── .zstd 透明读取：会话日志 session.vN.jsonl.zstd 是多帧拼接 ────────────

def _zstd_read_python(path: str, max_bytes: int | None) -> bytes:
    """zstandard 回退读（含多帧）。

    ⚠️ 多帧是这里最容易踩的坑：DSH 会话日志是**多帧拼接**的，
    而 zstandard 的
      · `ZstdDecompressor().decompress(raw)` —— 多帧直接 `ZstdError`
        （实测报 "could not determine content size in frame header"）
      · `decompressobj()` / `read_to_iter()` —— **只读第一帧就返回**
        （实测 952KB 日志只给出 272 B，静默截断，比报错更危险）
    只有 `stream_reader` 会跨帧读到底（实测与 `zstdcat` **逐字节相同**）。
    故此处一律用 stream_reader，且**分块读**——`max_bytes` 只用于嗅探文件头时，
    绝不能把整个会话解压物化到内存里。
    """
    import zstandard                        # 延迟导入：未装时不影响非 zstd 路径
    # ⚠️ 这里**不能**拿 os.path.getsize 当上限：那是对**压缩后**字节数的度量，
    #    而本函数产出的是**解压后**字节。早先版本用 raw_size 当 want
    #    （max_bytes=None 时），恰好在"解压输出 == 压缩大小"处截断——
    #    真实日志上表现为 4,352,964 B 被砍成 952,956 B，且两个数字看起来都"合理"。
    want = max_bytes if max_bytes is not None else None
    out = bytearray()
    try:
        with open(path, "rb") as fh:
            with zstandard.ZstdDecompressor().stream_reader(fh) as sr:
                while want is None or len(out) < want:
                    n = _ZSTD_PY_CHUNK if want is None else min(_ZSTD_PY_CHUNK, want - len(out))
                    chunk = sr.read(n)
                    if not chunk:
                        break
                    out += chunk
    except zstandard.ZstdError as exc:
        # max_bytes 的语义是"够用即停"，读到头部的 4096B 就停会切在帧中间——
        # 那是**预期**的截断，不是损坏；已读到的部分照常返回。
        # 但若一个字节都没读到，说明文件真的有病 → fail-loud，不静默返回空。
        if not out:
            raise RuntimeError(f"zstd 解压失败（{path}）：{exc}") from exc
    return bytes(out)


def _read_maybe_zstd(path: str, max_bytes: int | None = None) -> bytes:
    """读文件原始字节；zstd 压缩则先解压（多帧安全）。

    后端优先级：zstd/zstdcat CLI → zstandard 模块 → 都无则 fail-loud。
    max_bytes 用于只嗅探文件头：**读够即停**——
      · CLI 路径读够就杀掉子进程
      · 回退路径分块读够就停（见 _zstd_read_python）

    非 zstd 文件（无 `.zstd` 后缀 **且** 无 zstd 魔数）直接原样读。
    """
    # ── 判定是否 zstd：后缀优先，无后缀则嗅探魔数（后缀可能撒谎/缺失）──
    if not path.lower().endswith(".zstd"):
        with open(path, "rb") as fh:
            if not fh.read(4).startswith(_ZSTD_MAGIC):
                fh.seek(0)
                return fh.read() if max_bytes is None else fh.read(max_bytes)
        # 魔数命中 → 落下去按 zstd 解

    exe = shutil.which("zstdcat") or shutil.which("zstd")
    if exe is not None:
        args = ([exe, path] if os.path.basename(exe).startswith("zstdcat")
                else [exe, "-dc", path])
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            data = proc.stdout.read() if max_bytes is None else proc.stdout.read(max_bytes)
        finally:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
        return data

    try:
        return _zstd_read_python(path, max_bytes)
    except ImportError as exc:
        raise RuntimeError(
            "读取 zstd 需要下列任一项：\n"
            "  · zstd / zstdcat 可执行文件（推荐；pacman -S zstd）\n"
            "  · Python `zstandard` 模块（pip install zstandard，本机 3.12 venv 已装）\n"
            "  · Python 3.14+ 的标准库 compression.zstd\n"
            f"（原始错误：{exc}）") from exc



def _read_jsonl_lines(path: str) -> tuple[list[str], int, int]:
    """读 JSONL（`.zstd` 透明）→ (lines, 磁盘字节数, 解压后字节数)。"""
    raw_size = os.path.getsize(path)
    blob = _read_maybe_zstd(path)
    return blob.decode("utf-8", errors="replace").splitlines(), raw_size, len(blob)


def _wash_jsonl_line(raw_line: str, line_no: int) -> tuple[str, str] | None:
    """洗单条 jsonl：json.loads 后提取全部面向模型文本。

    Returns:
        (type, joined_text) — 成功；None — 解析失败或无可提取文本。
    """
    try:
        rec = json.loads(raw_line)
    except Exception:
        return None
    texts = _jsonl_extract_texts(rec)
    if not texts:
        return None
    return (str(rec.get("type", "?")), "\n".join(texts))


def _render_dsh_index_section(lines: list[str], total: int, max_results: int,
                              used_budget: list[int], budget: int,
                              emit) -> None:
    """dsh JSONL 索引区：候选限定 assistant/message 消息（避开 request/header
    140KB persona 全文、tool/result 回显等污染面），倒序取最后 max_results 条，
    洗 JSON 提取 text 全文 + 行号。U+2591 索引命中优先保（先占预算）。

    max_results=0 → 全部候选（由预算闸门兜底：超预算时折叠为行号清单）。
    预算按"整条候选"核算（含标题/空行），避免结构行逃逸预算。
    """
    # 精确候选：assistant/message 且含 ░ 精确标记（jsonl 内为真实 UTF-8 字符）
    precise: list[int] = []
    for i in range(total - 1, -1, -1):
        if '"type":"assistant/message"' in lines[i] and "░ 对话索引 ░" in lines[i]:
            precise.append(i)
    precise.reverse()

    # 0 精确命中 → 宽松回退：assistant/message 且含 '对话索引' 四字
    loose: list[int] = []
    if not precise:
        for i in range(total - 1, -1, -1):
            if '"type":"assistant/message"' in lines[i] and "对话索引" in lines[i]:
                loose.append(i)
        loose.reverse()

    candidates = precise if precise else loose
    raw_count = len(candidates)
    if max_results > 0 and raw_count > max_results:
        candidates = candidates[-max_results:]

    match_tag = "精确 ░ 标记" if precise else "宽松回退"
    if not candidates:
        emit("未找到对话索引相关 assistant 消息（本会话可能未生成索引块）。")
        emit("")
        return

    # 预洗所有候选：先构造"整条候选"的输出行，预算按整条核算
    prepared: list[tuple[int, list[str]]] = []  # (line_no, entry_lines)
    for line_no in candidates:
        stripped = lines[line_no].rstrip("\n\r")
        washed = _wash_jsonl_line(stripped, line_no)
        if washed is None:
            entry = [f"行 {line_no + 1}: (jsonl 记录无可提取文本，跳过)", ""]
        else:
            rec_type, text = washed
            raw_bytes = len(stripped.encode("utf-8"))
            text_bytes = len(text.encode("utf-8"))
            entry = [f"行 {line_no + 1} [{rec_type}] (原始 {raw_bytes}B → 洗后 {text_bytes}B):"]
            entry.extend(text.split("\n"))
            entry.append("")
        prepared.append((line_no, entry))

    emit(f"─── 对话索引（assistant/message，{match_tag}，倒序取 {len(candidates)} 条）───")
    folded: list[int] = []
    # 尾注预留：折叠提示/命中提示等区尾注释行逃逸预算的修正（实测 ~240B 最坏）
    tail_reserve = 400
    for line_no, entry in prepared:
        entry_cost = sum(_escaped_out_cost(s) for s in entry)
        if used_budget[0] + entry_cost <= budget - tail_reserve:
            for s in entry:
                emit(s)
        else:
            folded.append(line_no)
    if folded:
        # fold_note 会被 LLM **原样填回 detail_lines**（1-based 输入）——
        # 必须同口径 +1，否则复制粘贴正好错一行
        fold_note = ", ".join(str(n + 1) for n in folded)
        emit(f"（{len(folded)} 条超出剩余预算，折叠为行号。需要完整内容请用 "
             f"detail_lines=\"{fold_note}\" 提取）")
        emit("")
    if max_results > 0 and raw_count > len(candidates):
        emit(f"（共 {raw_count} 处 assistant/message 命中，显示最后 {len(candidates)} 处。"
             f"max_results=0 显示全部）")
        emit("")


def _scan_dsh_jsonl(path: str, max_results: int, detail_lines: str | None = None,
                    debug: str | None = None,
                    container_prelude: list[str] | None = None) -> list[TextContent]:
    """dsh JSONL：U+2591 索引区（assistant/message 候选，优先保）+ compaction
    压缩记录（type 白名单，无污染风险）。共享输出预算：索引区先占，
    压缩记录用剩余；装不下压缩记录则降级为位置+开头并提醒 detail_lines。

    detail_lines: 逗号分隔 **1-based** 行号（与输出行号同口径，可原样复制）→
    只洗这些行提取文本（绕过索引/压缩扫描，供降级后二次提取）。
    """
    if path.lower().endswith(".zstd"):
        # 会话日志：zstd 多帧拼接，直接解压读（无墓碑语义）
        lines, file_size, plain_size = _read_jsonl_lines(path)
        ts_count, total = 0, len(lines)
    else:
        lines, enc, is_utf8, ts_count, total = _read_file_with_tombstones(path)
        file_size = plain_size = os.path.getsize(path)

    out: list[str] = []
    used: list[int] = [0]
    budget = _JSONL_SCAN_BUDGET

    def emit(text: str) -> None:
        out.append(_escape_zone(text))
        used[0] += _escaped_out_cost(text)

    # ── 调试仪表（DEBUG 区块）：走独立输出，不叠标准表头 ──
    if debug:
        if debug not in _DEBUG_MODES:
            emit(f"未知 debug 模式: {debug!r}（可用: {', '.join(_DEBUG_MODES)}）")
        else:
            _render_debug(lines, debug, emit, path, file_size, plain_size)
        return [TextContent(type="text", text="\n".join(out))]

    if container_prelude is None:
        emit(f"文件: {path}")
        if file_size != plain_size:
            emit(f"行数: {total}, zstd 磁盘 {file_size:,} → 解压 {plain_size:,} 字节")
        else:
            emit(f"行数: {total}, 大小: {file_size:,} 字节")
        emit("格式: dsh-jsonl (DeepSeek Harness 会话日志)")
    else:
        # ZIP 容器：调用方备好的容器头（含条目清单/媒体校验），与索引区共享同一预算
        for line in container_prelude:
            emit(line)
        emit(f"行数: {total}, 大小: {plain_size:,} 字节（ZIP 成员解压后）")
        emit("格式: dsh-jsonl (DeepSeek Harness 会话日志)")
    emit("")
    emit("（注：索引标记 U+2591 已转义为 [U+2591] 显示；文件内容未变）")
    emit("")

    # ── detail_lines 模式：只洗指定行，绕过索引/压缩扫描 ──
    if detail_lines:
        wanted: list[int] = []
        for part in detail_lines.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ln = int(part)
            except ValueError:
                emit(f"detail_lines 含非行号: {part!r}")
                continue
            # detail_lines 是 1-based 输入（与 scan 输出行号同口径）——
            # LLM 可把输出里看到的行号原样填回，无需 -1 换算
            if ln < 1 or ln > total:
                emit(f"detail_lines 行号越界 (1..{total}): {ln}")
                continue
            wanted.append(ln - 1)
        if wanted:
            emit(f"─── detail_lines 提取 ({len(wanted)} 行) ───")
            # 预洗所有点名行，按整条核算预算
            prepared_dl: list[tuple[int, list[str]]] = []
            for line_no in wanted:
                stripped = lines[line_no].rstrip("\n\r")
                washed = _wash_jsonl_line(stripped, line_no)
                if washed is None:
                    entry = [f"行 {line_no + 1}: (jsonl 记录解析失败或无 text 块)", ""]
                else:
                    rec_type, text = washed
                    raw_bytes = len(stripped.encode("utf-8"))
                    text_bytes = len(text.encode("utf-8"))
                    entry = [f"行 {line_no + 1} [{rec_type}] (原始 {raw_bytes}B → 洗后 {text_bytes}B):"]
                    entry.extend(text.split("\n"))
                    entry.append("")
                prepared_dl.append((line_no, entry))
            folded_dl: list[int] = []
            for line_no, entry in prepared_dl:
                entry_cost = sum(_escaped_out_cost(s) for s in entry)
                if used[0] + entry_cost <= budget - 300:
                    for s in entry:
                        emit(s)
                else:
                    folded_dl.append(line_no)
            if folded_dl:
                emit(f"（{len(folded_dl)} 行超出返回预算 {budget}B，未展开。"
                     # 同样会被原样填回 detail_lines（1-based）→ 同口径 +1
                     f"请减小 detail_lines 行数分批提取：\"{','.join(str(n + 1) for n in folded_dl)}\"）")
                emit("")
        return [TextContent(type="text", text="\n".join(out))]

    # ── 区 1：U+2591 索引（优先保） ──
    _render_dsh_index_section(lines, total, max_results, used, budget, emit)

    # ── 区 2：compaction 压缩记录（type 白名单，无污染风险，全量提取） ──
    records = _extract_compactions(lines)
    if not records:
        emit("未发现 compaction 压缩记录（本会话未发生自动压缩）。")
    else:
        # 预算判断：先试完整渲染，超预算则降级
        full_lines = _render_compaction_report(records)
        full_cost = sum(_escaped_out_cost(s) for s in full_lines)
        if used[0] + full_cost <= budget:
            for s in full_lines:
                emit(s)
        else:
            remaining = budget - used[0]
            emit(f"（压缩记录全量 {full_cost}B 超出剩余预算 {remaining}B，降级为位置+摘要开头。）")
            for s in _render_compaction_degraded(records):
                emit(s)

    return [TextContent(type="text", text="\n".join(out))]


def _scan_v2(arguments: dict) -> list[TextContent]:
    """cronopio_scan v2：按文件格式分流。

    · 普通文本 / 单文档 JSON → 扫对话索引（新版 U+2591 优先，旧版回退）
    · dsh JSONL → U+2591 索引区（assistant/message 候选）+ compaction 压缩记录，
      共享输出预算（计量单位随 DSH 版本：0.1.5 字节 / 0.1.7 UTF-16 码元）
    · ZIP（DSH Session Log 导出归档）→ 中央目录分类选中成员 → 解压后走 dsh JSONL 分支
    输出中 U+2591 转义为 [U+2591]（防污染，不影响理解）。行号 1-based（对齐 grep 生态）。
    """
    path = _normalize_path(arguments["path"])
    raw_mr = arguments.get("max_results")
    # 0 是合法值（=全部），不能用 `or 2` 吞掉
    max_results = 2 if raw_mr is None else int(raw_mr)
    detail_lines = arguments.get("detail_lines")
    debug = arguments.get("debug")
    member = arguments.get("member")
    if not _is_allowed(path):
        return [TextContent(type="text", text=f"路径不在安全白名单内: {path}")]
    if not os.path.isfile(path):
        return [TextContent(type="text", text=f"不是文件: {path}")]

    # ZIP 容器（DSH Session Log 导出归档）先分流——按魔数判，不看后缀
    if _is_zip_archive(path):
        return _scan_zip(path, max_results, detail_lines, debug, member)

    fmt = _detect_file_format(path)
    if fmt == "dsh-jsonl":
        return _scan_dsh_jsonl(path, max_results, detail_lines, debug)

    res = _scan_indices_text(path, max_results, fmt)

    # detail_lines 只对 dsh-jsonl（含 .jsonl.zstd）与 ZIP 打包格式有意义。
    # 传给了别的格式 → **显式告知**，不静默吞掉参数；否则调用者会以为
    # "我提了、它没给"→ 误判成"这里没有内容"。
    if detail_lines:
        note = (
            "⚠️ 本格式不支持 detail_lines，已忽略该参数。\n"
            f"   本文件被判定为：{fmt}（普通文本 / 单文档 JSON）。\n"
            "   detail_lines 只对下列两类输入有意义：\n"
            "     · dsh-jsonl —— DSH 会话日志。原生保存是 **.jsonl.zstd**（zstd 压缩），\n"
            "       浏览器界面下载得到的是 **ZIP 打包**版，两者都走 jsonl 分支、都支持；\n"
            "       裸 .jsonl（未压缩）同样支持。\n"
            "     · ZIP —— DSH Session Log 导出归档（session*.jsonl[.vN] + media/）。\n"
            "   本模式已直接展示匹配行的前后上下文，无需再按行号二次提取。"
        )
        head_text = getattr(res[0], "text", "") if res else ""
        res = [TextContent(type="text",
                           text=(note + "\n\n" + head_text) if head_text else note)]
    return res


# ═══════════════════════════════════════════════════════════
#  Path normalisation — auto-convert Windows ↔ WSL paths
# ═══════════════════════════════════════════════════════════

_WINDOWS_DRIVE_PATTERN = re.compile(r'^([A-Za-z]):[/\\]')

_RUNTIME: str | None = None


def _detect_runtime() -> str:
    """Detect the runtime environment: 'wsl', 'windows', or 'linux'."""
    global _RUNTIME
    if _RUNTIME is not None:
        return _RUNTIME

    import platform
    system = platform.system()
    if system == 'Windows':
        _RUNTIME = 'windows'
    elif system == 'Linux':
        try:
            with open('/proc/sys/fs/binfmt_misc/WSLInterop', 'r') as f:
                _RUNTIME = 'wsl'
                return _RUNTIME
        except (FileNotFoundError, PermissionError):
            pass
        release = platform.release().lower()
        if 'microsoft' in release or 'wsl' in release:
            _RUNTIME = 'wsl'
        else:
            _RUNTIME = 'linux'
    else:
        _RUNTIME = 'linux'
    return _RUNTIME


def _normalize_path(path: str) -> str:
    """Auto-convert between Windows and WSL path formats based on runtime.

    WSL runtime:     D:\\foo\\bar  → /mnt/d/foo/bar
    Windows runtime: /mnt/d/foo   → D:\\foo
    Linux runtime:   no conversion (passthrough)
    """
    runtime = _detect_runtime()

    if runtime == 'wsl':
        # Convert Windows paths to WSL
        path = path.replace('\\', '/')
        m = _WINDOWS_DRIVE_PATTERN.match(path)
        if m:
            drive = m.group(1).lower()
            remainder = path[2:]  # strip "X:"
            if not remainder.startswith('/'):
                remainder = '/' + remainder
            path = f'/mnt/{drive}{remainder}'
    elif runtime == 'windows':
        # Convert WSL /mnt/x/ paths to Windows
        m = re.match(r'^/mnt/([a-zA-Z])/', path)
        if m:
            drive = m.group(1).upper()
            remainder = path[5:]  # after /mnt/x/
            remainder = remainder.replace('/', '\\')
            path = f'{drive}:\\{remainder}'
        else:
            # Also normalise forward slashes to backslashes
            path = path.replace('/', '\\')
    # else: linux — no conversion

    # Collapse multiple consecutive slashes
    while '//' in path:
        path = path.replace('//', '/')

    return path


# ═══════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())