#!/usr/bin/env python3
"""fast_filesystem / servers/fast_filesystem.py 回归网（可移植版）

摘要: 三组断言——【行号口径】对外报告 1-based（grep/scan）vs 索引参数 0-based；
      【三方一致性】匹配标签 × 上下文标签 × 上游 GNU grep 逐条核对行号与内容；
      【降级】rg 缺失/超时 → Python re 回退 + 双向警告；非法正则 → 显式报错。
      可在发布件与自用侧两种布局下运行：自动发现目标、自动适配工具名
      （自用侧的工具名从管线仓 publish/ 下的映射文件读入，本文件不含其字面量）。

用法:
    python3 regress_fast_filesystem.py              # 自动发现
    python3 regress_fast_filesystem.py <模块路径>    # 显式指定
    FASTFS_MODULE=/path/to/module.py python3 regress_fast_filesystem.py

退出码: 0=全过 / 1=有失败 / 2=找不到目标
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _find_in_ancestors(rel: str) -> Path | None:
    """从本文件所在目录向上找 rel（发布件 tests/ → servers/ 即此路径）。"""
    cur = HERE
    for _ in range(4):
        cand = cur / rel
        if cand.is_file():
            return cand
        cur = cur.parent
    return None


def find_target() -> Path | None:
    env = os.environ.get("FASTFS_MODULE")
    if env:
        p = Path(env)
        return p if p.is_file() else None
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        p = Path(sys.argv[1])
        return p if p.is_file() else None
    # 发布件布局：tests/ 的兄弟目录 servers/
    hit = _find_in_ancestors("servers/fast_filesystem.py")
    if hit:
        return hit
    # 自用布局：向下（本目录内）与向上（树根下的工具目录）各找一次
    for pattern in ("*/fast_filesystem.py", "*/*/fast_filesystem.py"):
        hits = sorted(HERE.glob(pattern))
        if hits:
            return hits[0]
    cur = HERE
    for _ in range(5):
        for d in sorted(cur.glob("*/fast_filesystem.py")):
            if d.is_file():
                return d
        cur = cur.parent
    return None


def load_module(target: Path):
    """加载目标模块。

    发布件带占位符启动守卫（未填占位符即 SystemExit）→ 生成"已填"的临时副本
    再导入（与发布件自带的功能测试同一手法）。
    """
    src = target.read_text(encoding="utf-8")
    tmpdir = None
    if ('"<root-1>"' in src) or ('"<search-1>"' in src):
        tmpdir = tempfile.mkdtemp(prefix="ffs_regress_mod_")
        for ph in ("<root-1>", "<root-2>", "<root-3>", "<search-1>"):
            src = src.replace('"%s"' % ph, '"%s"' % tmpdir)
        target = Path(tmpdir) / "fakefs_under_test.py"
        target.write_text(src, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("fakefs_under_test", target)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fakefs_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod, tmpdir


def work_dir():
    """测试数据落点。

    发布件有路径白名单（首启动时由占位符填成）——测试数据必须落在**它允许的
    根之下**，否则一切调用都返回"路径不在安全白名单内"。
    load_module 会把占位符补成同一个临时根，这里复用之。
    """
    root = globals().get("PATCHED_ROOT")
    return Path(root) if root else Path(tempfile.mkdtemp(prefix="ffs_regress_"))


# 角色 → 发布件里的工具名（规范键；自用侧名字由下方 _selfuse_names() 派生）
ROLES = {
    "grep_file": "cronopio_grep",
    "grep_dir":  "cronopio_grepdir",
    "read":      "cronopio_read",
    "write":     "cronopio_write",
    "gc":        "cronopio_gc",
    "scan":      "cronopio_scan",
}

# 改名映射表文件名（其内容含自用侧旧名，但**该文件不发布**，故本文件不含旧名）
MAP_REL = ("publish/rules.tsv", "cronopios/publish/rules.tsv")


def _load_map() -> dict:
    """向上逐级找改名映射（旧名 → 新名）；读不到返回空（=假定未改名）。

    必须向上搜索而非写死相对层数：本文件在源树里位于
    <管线仓>/assets/tests/，与 publish/ 差三级；在发布件里与 servers/ 同级。
    """
    m: dict = {}
    cur = HERE
    for _ in range(5):
        for rel in MAP_REL:
            c = cur / rel
            if c.is_file():
                for line in c.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        m[parts[0]] = parts[1]
                if m:
                    return m
        cur = cur.parent
    return m


def _selfuse_names(mod_names: set) -> dict:
    """规范名(发布名) → 实际模块里的名字。

    自用侧的旧名从映射表读入，再用**通用**去公共子串规则转成实际名：
    旧名的构词是 <动词>_<公共中缀>_<名词>，去掉中缀即得实际名——
    本文件不出现该中缀字面量。
    """
    m = _load_map()
    out = {}
    for canon in ROLES.values():
        if canon in mod_names:
            out[canon] = canon
            continue
        # 自用侧：在映射表里找 new==canon 的那条，取其旧名并去中缀
        old = next((k for k, v in m.items() if v == canon), None)
        if old is None:
            raise SystemExit("模块里既无 %r，映射表里也没有指向它的旧名" % canon)
        # 映射表的 key 就是自用侧的实际工具名——直接用，无需重建。
        # （本文件因此不含自用侧名字的字面量，可过发布件的改名闸门。）
        if old not in mod_names:
            raise SystemExit(
                "映射表给出的名字 %r 不在模块工具清单里；实际清单前 5 项：%s"
                % (old, sorted(mod_names)[:5]))
        out[canon] = old
    return out


def build_names(mod) -> dict:
    import asyncio
    actual = {t.name for t in asyncio.run(mod.list_tools())}
    resolved = _selfuse_names(actual)
    # 转换回本测试体使用的角色键
    return {role: resolved[canon] for role, canon in ROLES.items()}

# ══════════════════════════════════════════════════════════════════
#  断言框架
# ══════════════════════════════════════════════════════════════════

fails: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(("[OK]   " if ok else "[FAIL] ") + label
          + ("" if ok else "  got=%r want=%r" % (got, want)))
    if not ok:
        fails.append(label)


def upstream_grep(path, pattern):
    """上游基准：GNU grep -n → {行号(1-based): 行内容}。独立于被测工具。"""
    p = subprocess.run(["grep", "-n", "-E", "--", pattern, str(path)],
                       capture_output=True, text=True)
    out = {}
    for line in p.stdout.splitlines():
        i = line.find(":")
        if i <= 0:
            continue
        out[int(line[:i])] = line[i + 1:]
    return out


def render(tool_result) -> str:
    return tool_result[0].text


async def main() -> None:
    TMP = str(work_dir())
    N = TMP_FN
    L = TMP_LOADER

    print("=" * 66)
    print("A. 行数口径三等价：total == readlines() == splitlines()")
    print("=" * 66)
    for label, text in [("10行+尾\\n", "".join("L%d\n" % i for i in range(10))),
                        ("10行无尾\\n", "\n".join("L%d" % i for i in range(10))),
                        ("单行+尾\\n", "only\n"), ("空文件", ""),
                        ("仅换行", "\n"), ("尾空行", "a\n\n")]:
        p = os.path.join(TMP, "c.txt")
        Path(p).write_text(text, encoding="utf-8")
        _l, _e, _u, _t, total = L._read_file_with_tombstones(p)
        rl = len(open(p, encoding="utf-8", newline="").readlines())
        check("A %-10s" % label, total, rl if text else 1)

    print()
    print("=" * 66)
    print("B. 【三方一致性】匹配标签 × 上下文标签 × 上游 GNU grep")
    print("=" * 66)
    p = os.path.join(TMP, "tri.txt")
    Path(p).write_text("".join("L%d\n" % i for i in range(1, 61)), encoding="utf-8")
    for q in [r"^L1$", r"^L30$", r"^L60$", r"^L[1-9]$", r"^L[1-5][0-9]$"]:
        up = upstream_grep(p, q)
        txt = render(await L.call_tool(N["grep_file"],
                                       {"path": p, "query": q, "max_results": 0,
                                        "before_context": 1, "after_context": 1}))
        blocks = re.split(r"─── 匹配 \d+ \(行 (\d+)\) ───", txt)
        tool = {}
        for k in range(1, len(blocks), 2):
            mlabel = int(blocks[k])
            ctx = {}
            for line in blocks[k + 1].splitlines():
                m = re.match(r"^(?:>>>|   )\s+(\d+): (.*)$", line)
                if m:
                    ctx[int(m.group(1))] = m.group(2)
            tool[mlabel] = ctx
        check("B [%s] 匹配标签==上游行号" % q, sorted(tool), sorted(up))
        check("B [%s] 匹配行内容==上游" % q,
              all(tool[ln].get(ln) == up[ln] for ln in up), True)
        raw = Path(p).read_text(encoding="utf-8").split("\n")
        check("B [%s] 上下文标签内容自洽" % q,
              all(raw[ln - 1] == c for ctx in tool.values()
                  for ln, c in ctx.items() if 1 <= ln <= 60), True)
        check("B [%s] 命中行在上下文内且行号==标签" % q,
              all(ml in tool[ml] for ml in tool), True)

    print()
    print("=" * 66)
    print("C. 幽灵尾行 / 范围提示")
    print("=" * 66)
    txt = render(await L.call_tool(N["grep_file"],
                                   {"path": p, "query": "L60", "max_results": 1}))
    check("C 末行报 行 60", "(行 60)" in txt, True)
    check("C 总行数 60（无幽灵行）", "共 60 行" in txt, True)
    check("C 范围提示 1-based", "搜 1-60 行" in txt, True)
    txt = render(await L.call_tool(N["grep_file"],
                                   {"path": p, "query": "^$", "max_results": 5}))
    check("C 空行正则无幽灵命中", "未找到" in txt, True)

    print()
    print("=" * 66)
    print("D. 目录 grep 首命中对齐上游")
    print("=" * 66)
    d = os.path.join(TMP, "sub"); os.makedirs(d, exist_ok=True)
    pm = os.path.join(d, "m.txt")
    Path(pm).write_text("".join("L%d\n" % i for i in range(1, 11)), encoding="utf-8")
    up7 = upstream_grep(pm, r"^L7$")
    txt = render(await L.call_tool(N["grep_dir"], {"dir": d, "query": "^L7$"}))
    # 闭合括号是全角「）」U+FF09，不是 ASCII ")"
    m = re.search(r"首命中 行 (\d+): (.*)$", txt, re.M)
    check("D 首命中行号==上游", int(m.group(1)) if m else None, sorted(up7)[0])
    check("D 首命中内容==上游",
          m.group(2).rstrip("）") if m else None, up7[sorted(up7)[0]])

    print()
    print("=" * 66)
    print("E. 0-based 索引面（read/write 的 at_line / offset_lines）")
    print("=" * 66)
    pw = os.path.join(TMP, "w.txt")
    Path(pw).write_text("a\nb\nc\n", encoding="utf-8")
    txt = render(await L.call_tool(N["write"],
                                   {"path": pw, "content": "X", "at_line": 2,
                                    "replace": True}))
    check("E at_line=2 → 第 3 行 c（0-based）", ">>>      2: c" in txt, True)
    txt = render(await L.call_tool(N["write"],
                                   {"path": pw, "content": "X", "at_line": 3,
                                    "replace": True}))
    check("E at_line=3 越界", "超出文件范围" in txt, True)
    txt = render(await L.call_tool(N["read"],
                                   {"path": p, "offset_lines": -1, "max_lines": 1}))
    check("E read offset=-1 → 末行 0-based 59", "[行 59-59 / 共 60 行]" in txt, True)

    print()
    print("=" * 66)
    print("F. 墓碑 / GC 尾换行保形")
    print("=" * 66)

    def expect_tombstone(data: bytes, nl: bytes, at_i: int, cnt: int) -> bytes:
        """预期由输入**程序化推导**：墓碑 = 等长替换（第 at 行内容字节数 = 墓碑数）。
        CRLF 下 b"a\\r\\nb\\r\\nc" 第 1 行内容是 b"b" 即 1 字节——\\r\\n 是分隔符。"""
        parts = data.split(nl)
        if parts and parts[-1] == b"":
            parts.pop()
        for i in range(at_i, min(at_i + cnt, len(parts))):
            parts[i] = b"\xff" * len(parts[i])
        out = nl.join(parts)
        return out + (nl if data.endswith(nl) else b"")

    for lbl, init, nl, at, cnt, after_gc in [
        ("UTF-8 删中间", b"aaa\nbbb\nccc\n", b"\n", 1, 1, b"aaa\nccc\n"),
        ("UTF-8 删末行", b"a\nb\nc\n", b"\n", 2, 1, b"a\nb\n"),
        ("无尾换行", b"a\nb\nc", b"\n", 1, 1, b"a\nc"),
        ("CRLF", b"a\r\nb\r\nc\r\n", b"\r\n", 1, 1, b"a\r\nc\r\n"),
    ]:
        pg = os.path.join(TMP, "gc.txt")
        Path(pg).write_bytes(init)
        await L.call_tool(N["write"], {"path": pg, "mode": "delete_lines",
                                       "at_line": at, "count": cnt})
        check("F %-12s 墓碑后" % lbl, Path(pg).read_bytes(),
              expect_tombstone(init, nl, at, cnt))
        await L.call_tool(N["gc"], {"path": pg})
        check("F %-12s GC 后" % lbl, Path(pg).read_bytes(), after_gc)

    print()
    print("=" * 66)
    print("G. rg 降级通道（缺失/超时回退 Python re；非法正则显式报错）")
    print("=" * 66)
    pa = os.path.join(TMP, "a.txt")
    Path(pa).write_text("hello\nFOO\nfoo\nnone\n", encoding="utf-8")
    _l, _e, _u, _t, _tot = L._read_file_with_tombstones(pa)
    go = L._normalize_lines_nfc_lf(_l)
    for q, cs, fx in [("foo", False, False), ("foo", True, False),
                      ("L[0-9]+", False, False), ("a.b", False, True),
                      ("^no", False, False)]:
        check("G 两引擎一致 q=%r cs=%s Fx=%s" % (q, cs, fx),
              L._rg_search_text(go, q, case_sensitive=cs, fixed_string=fx)[0],
              L._python_regex_search(go, q, case_sensitive=cs, fixed_string=fx))

    _real = L.subprocess.run

    def _boom(*a, **k):
        raise FileNotFoundError(2, "no rg")

    L.subprocess.run = _boom
    L._python_fallback_warned = False
    txt = render(await L.call_tool(N["grep_file"], {"path": pa, "query": "foo"}))
    check("G 缺失: 有横幅", "未检测到 ripgrep" in txt, True)
    check("G 缺失: 说明回退方案", "已回退到 Python re 引擎" in txt, True)
    check("G 缺失: 含安装建议", "pacman -S ripgrep" in txt, True)
    check("G 缺失: 横幅在命中之前",
          txt.index("未检测到 ripgrep") < txt.index("找到 2 处匹配"), True)
    txt = render(await L.call_tool(N["grep_file"], {"path": pa, "query": "zzz_absent"}))
    check("G 缺失+零命中: 警告仍在", "未检测到 ripgrep" in txt, True)
    txt = render(await L.call_tool(N["grep_file"], {"path": pa, "query": "^FOO$"}))
    check("G 缺失时行号口径仍 1-based", "(行 2)" in txt, True)
    L.subprocess.run = _real
    try:
        await L.call_tool(N["grep_file"], {"path": pa, "query": "(unclosed"})
        check("G 非法正则: 必须报错", "未抛错", "应抛错")
    except Exception as exc:
        check("G 非法正则: 显式报错",
              "正则" in str(exc) or "regex" in str(exc).lower(), True)


    print()
    print("=" * 66)
    print("H. detail_lines：支持的输入形态 + 不支持时显式提示")
    print("=" * 66)
    # H1: dsh-jsonl（裸 .jsonl）—— detail_lines 应生效
    # 记录形状须含 {"type":"text","text":...} 叶子块（_jsonl_extract_texts 的判据），
    # 故照真实 assistant/message 的嵌套结构造：
    pj = os.path.join(TMP, "j.jsonl")
    Path(pj).write_text(
        '{"type":"session","version":3,"seq":1}\n'
        '{"type":"assistant/message","seq":2,"data":{"message":{"role":"assistant",'
        '"content":[{"type":"text","text":"MARKER_JSONL"}]}}}\n',
        encoding="utf-8")
    txt = render(await L.call_tool(N["scan"], {"path": pj, "max_results": 5,
                                               "detail_lines": "2"}))
    check("H1 jsonl + detail_lines 生效（出现提取段）",
          "detail_lines 提取" in txt, True)
    check("H1 jsonl + detail_lines 取到正文", "MARKER_JSONL" in txt, True)

    # H2: 普通文本 —— 不支持的参数必须**显式告知**，不得静默吞
    pt = os.path.join(TMP, "p.txt")
    Path(pt).write_text("hello\nworld\n", encoding="utf-8")
    txt = render(await L.call_tool(N["scan"], {"path": pt, "max_results": 2,
                                               "detail_lines": "1"}))
    check("H2 文本 + detail_lines 给出显式提示",
          "本格式不支持 detail_lines" in txt, True)
    check("H2 提示里点名支持的两类输入",
          ("dsh-jsonl" in txt) and ("ZIP" in txt), True)
    check("H2 提示里说明本模式已直接展示上下文", "已直接展示匹配行" in txt, True)

    # H3: 不传 detail_lines —— 不得出现该提示
    txt = render(await L.call_tool(N["scan"], {"path": pt, "max_results": 2}))
    check("H3 未传参时不出现提示", "本格式不支持 detail_lines" in txt, False)



    print()
    print("=" * 66)
    print("I. zstd 读取：CLI 与 zstandard 回退必须产出**同字节**")
    print("=" * 66)
    # 自给自足：本文件不依赖任何外部会话日志，测试自己造一个多帧 .zstd。
    # 多帧是 DSH 会话日志的真实形状，也是回退路径最容易静默截断的地方。
    payload = ("".join("LINE_%06d %s\n" % (i, "x" * 60) for i in range(4000))).encode()
    zstd_path = os.path.join(TMP, "mf.jsonl.zstd")
    made = False
    try:
        import zstandard as _zs
        cctx = _zs.ZstdCompressor()
        # 拆成两帧写入 —— 显式构造多帧
        with open(zstd_path, "wb") as out:
            out.write(cctx.compress(payload[:len(payload) // 2]))
            out.write(cctx.compress(payload[len(payload) // 2:]))
        made = True
        maker = "zstandard"
    except Exception:
        exe = shutil.which("zstd")
        if exe:
            subprocess.run([exe, "-q", "-f", "-o", zstd_path], input=payload,
                           capture_output=True)
            made = os.path.getsize(zstd_path) > 0
            maker = "zstd CLI"
        else:
            # 连一个后端都没有 → 造不出样本，**显式跳过**（不静默当通过）
            print("  [SKIP] 本机既无 zstandard 也无 zstd CLI，无法造样本")

    if made:
        comp_size = os.path.getsize(zstd_path)
        # 固定 mtime：避免 harness 的 mtime 钳制影响（与本测试无关）
        check("I 多帧样本已生成（压缩 %d B → 原始 %d B）" % (comp_size, len(payload)),
              comp_size > 0 and comp_size != len(payload), True)

        full = L._read_maybe_zstd(zstd_path)
        check("I 完整读 == 原始字节（未被截断在压缩大小处）", full, payload)
        # ↑ 这条就是本次回归的靶心：早先版本在"解压输出 == 压缩大小"处截断，
        #   两个数字都看起来合理，只有独立比对本字节才能发现。

        sniff = L._read_maybe_zstd(zstd_path, 4096)
        check("I 嗅探读精确停在 max_bytes", len(sniff), 4096)
        check("I 嗅探内容 == 原文前 4096", sniff, payload[:4096])

        # 屏蔽 CLI → 强制回退路径，要求与 CLI **逐字节相同**
        real_which = L.shutil.which
        L.shutil.which = lambda n: None if n in ("zstd", "zstdcat") else real_which(n)
        try:
            fb = L._read_maybe_zstd(zstd_path)
            check("I 回退路径 == CLI 路径（逐字节）", fb, full)
            fb_sniff = L._read_maybe_zstd(zstd_path, 4096)
            check("I 回退嗅探 == CLI 嗅探", fb_sniff, sniff)
        except Exception as exc:
            # zstandard 未装时不算失败（CLI 路径已覆盖），但要显式说明
            if "zstandard" in str(exc) or isinstance(exc, ImportError):
                print("  [SKIP] 回退路径：本机无 zstandard")
            else:
                check("I 回退路径未抛错", str(exc), "")
        finally:
            L.shutil.which = real_which


    # 不在此清理：TMP 可能就是补丁根，由 _run 统一收尾


# ══════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════

def _run() -> int:
    target = find_target()
    if target is None:
        print("找不到被测模块。搜索位置：")
        print("  ", HERE.parent / "servers" / "fast_filesystem.py", "（发布件布局）")
        print("  ", HERE, "下的 */fast_filesystem.py（自用布局）")
        print("请用参数或 FASTFS_MODULE 环境变量显式指定。")
        return 2
    print("被测模块:", target)
    mod, tmpdir = load_module(target)
    try:
        globals()["PATCHED_ROOT"] = tmpdir
        names = build_names(mod)
        globals()["TMP_FN"] = names
        globals()["TMP_LOADER"] = mod
        import asyncio
        asyncio.run(main())
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    print()
    print("=" * 66)
    if fails:
        print("失败 %d 项: %s" % (len(fails), fails))
    else:
        print("全部通过 ✅")
    print("=" * 66)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_run())
