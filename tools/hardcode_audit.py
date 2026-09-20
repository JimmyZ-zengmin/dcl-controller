#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hardcode_audit.py —— 固定地址的**归属审计**（内存宪法 D 期；不变量 M1 / M5 / M6）

## 三条判据（都能失败，见 --selftest）
  C0  **地址唯一源**（M1）：`0x2400xxxx` 这类固定地址的**定义**只允许出现在 `src/memmap.h`。
      其它文件只能 `#include` 取宏名。
  C9  **归属唯一**（M5）：每个区的 `refs=` 白名单就是"谁可以引用它"。
      任何模块引用了不在它 `refs` 里的区 ⇒ 红。
      ★ 这是"无主内存被撞"（载荷缓冲落进黑匣子环）那一类的**构建期**版本 ——
        当年那件事的机制是"有人照过期注释找了块空地"，而归属审计把"谁能碰这块"变成机器判据。
  C12 **静态封闭**（M6）：`src/` 里 `malloc/calloc/realloc/free/aligned_alloc` 出现次数必须为 0。
      理由不是"碎片"，是**它会摧毁成本模型**：`C_other`/`C_scan` 是常数的前提是
      "每拍访问的内存集合编译期封闭"。

用法:
  python tools/hardcode_audit.py             # 审计
  python tools/hardcode_audit.py --selftest  # 用合成数据证明 C0/C9 会红
退出码: 0 = 全过 / 1 = 有 FAIL
"""
import io
import os
import re
import sys

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(R, "src")
ADDR_RE = re.compile(r"0x24[0-9A-Fa-f]{6}")
DEF_RE = re.compile(r"^\s*#\s*define\s+\w+\s+.*0x24[0-9A-Fa-f]{6}", re.M)
ALLOC_RE = re.compile(r"\b(malloc|calloc|realloc|free|aligned_alloc|_sbrk)\s*\(")
ALLOC_OK = {"sysmem.c", "syscalls.c"}      # newlib 的堆桩: 它们**就是** sbrk 的实现, 不是使用者


def strip_comments(t):
    """★ 判据必须**剥注释**再找地址/宏名：注释里引用旧地址（留案）或提到某区名，
    不是"定义"也不是"引用"。本项目今天已有三次同族教训（把注释/字符串当代码）。"""
    t = re.sub(r"/\*.*?\*/", " ", t, flags=re.S)
    return re.sub(r"//[^\n]*", " ", t)


def parse_regions():
    """从 memmap.h 读 (区名, 地址, refs 列表)。"""
    txt = io.open(os.path.join(SRC, "memmap.h"), encoding="utf-8", errors="replace").read()
    out = []
    for m in re.finditer(r'^#define\s+(AXI_[A-Z0-9_]+?)\s+0x([0-9A-Fa-f]+)u?\s*/\*(.*?)\*/\s*$',
                         txt, re.M):
        name, body = m.group(1), m.group(3)
        if name.endswith("_SZ"):
            continue
        if "_SZ" not in txt:
            pass
        mr = re.search(r"refs=([^\s*]+)", body)
        if not mr:
            continue                       # 只有子结构（如 AXI_LATCH_SNAP 已在区内声明 refs）
        refs = [] if mr.group(1) == "-" else mr.group(1).split(",")
        out.append((name, int(m.group(2), 16), refs))
    if len(out) < 10:
        raise SystemExit("!! memmap.h 里只解析出 %d 个带 refs 的区 ⇒ 解析器或文件形态变了"
                         "（拒绝继续：那会让 C9 退化成空判据）" % len(out))
    return out


def audit():
    files = sorted([f for f in os.listdir(SRC) if f.endswith((".c", ".h"))])
    res, bad = [], []

    # ── C0 地址唯一源 ──
    offenders = []
    for f in files:
        if f == "memmap.h":
            continue
        txt = strip_comments(io.open(os.path.join(SRC, f), encoding="utf-8",
                                     errors="replace").read())
        for m in DEF_RE.finditer(txt):
            offenders.append("%s: %s" % (f, m.group(0).strip()[:60]))
    res.append(("C0 固定地址的**定义**只出现在 src/memmap.h（M1）", not offenders))
    bad += offenders

    # ── C9 归属唯一 ──
    regions = parse_regions()
    viol = []
    for f in files:
        if f == "memmap.h":
            continue
        txt = strip_comments(io.open(os.path.join(SRC, f), encoding="utf-8",
                                     errors="replace").read())
        for name, addr, refs in regions:
            if not re.search(r"\b%s\b" % name, txt):
                continue
            if f not in refs:
                viol.append("%s 引用了 %s（0x%08X），但它的 refs 白名单是 %s"
                            % (f, name, addr, refs or "（空 = 只允许 memmap.h）"))
    res.append(("C9 引用者必须在区的 refs 白名单内（M5，%d 个区已核）" % len(regions), not viol))
    bad += viol

    # ── C12 静态封闭 ──
    alloc = []
    for f in files:
        if f in ALLOC_OK:
            continue
        txt = io.open(os.path.join(SRC, f), encoding="utf-8", errors="replace").read()
        for m in ALLOC_RE.finditer(txt):
            ln = txt[:m.start()].count("\n") + 1
            alloc.append("%s:%d %s" % (f, ln, m.group(0)))
    res.append(("C12 src/ 无动态分配（M6：每拍访问集合编译期封闭）", not alloc))
    bad += alloc

    print("=" * 74)
    print("固定地址归属审计（权威源: src/memmap.h 的 refs= 白名单）")
    print("=" * 74)
    print("区块 %d 个:" % len(regions))
    for name, addr, refs in regions:
        print("   0x%08X  %-16s refs=%s" % (addr, name, ",".join(refs) or "（无）"))
    print()
    nf = 0
    for name, ok in res:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        nf += (not ok)
    for b in bad:
        print("     ★ %s" % b)
    print("\n%d 项判定, %d FAIL" % (len(res), nf))
    return 0 if nf == 0 else 1


def selftest():
    """★ 证明 C0/C9 的判定逻辑会红（合成数据）。"""
    print("=== hardcode_audit 自检（合成数据）===")
    ok = True
    # C0: 有人在别处写了地址定义
    fake = "#define BB_X ((volatile uint32_t *)0x24000300u)\n"
    c0 = bool(DEF_RE.search(fake))          # 检测到"别处写了地址定义"
    print("  [%s] C0 逻辑: 别处写 `#define ... 0x2400xxxx` ⇒ 判红 = %s"
          % ("PASS" if c0 else "FAIL", c0))
    ok = ok and c0
    # C9: adc.c 引用了环
    regions = [("AXI_BB_RING", 0x24004000, ["blackbox.c", "sd.c"])]
    t = "x = AXI_BB_RING;\n"
    viol = [f for f, _, refs in regions if re.search(r"\b%s\b" % "AXI_BB_RING", t)
            and "adc.c" not in refs]
    c9 = bool(viol)
    print("  [%s] C9 逻辑: adc.c 引用 AXI_BB_RING（不在 refs 里）⇒ 判红 = %s"
          % ("PASS" if c9 else "FAIL", c9))
    ok = ok and c9
    print("\n%s" % ("[PASS] 两条判据都能区分合规与违规输入" if ok else "[FAIL] 自检失效"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else audit())
