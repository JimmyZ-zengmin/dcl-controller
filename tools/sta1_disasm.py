#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-1: 把扫描体与各原语体反汇编出来, 按 **FPGA 静态时序分析** 的路子做第一步:
拿到"每个基本块有哪些指令、分别是哪一类", 供"沿结构累加延迟"用。

为什么先做这一步（不是直接拟合一个总成本）:
  FPGA 工具**从不跑电路**就能算出一个周期够不够 —— 它靠"原语延迟已知 + 结构已知"。
  MCU 上要照抄这条路, 第一件事就是把**结构**（指令序列）取出来。
  本项目额外有利: 路由表把程序展开成**线性序列**, 无循环/无递归/无间接分派边界 ⇒
  最长路径分析在结构上是可做的（通用 WCET 的老大难在这里不存在）。

输出: 每个符号的指令清单 + 指令类直方图 + 分支/加载/存储数（供后续赋延迟）
"""
import os, re, subprocess, sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OBJDUMP = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
           "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32."
           "7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-objdump.exe")
ELF = os.path.join(ROOT, "build", "dcl_h723")

# 指令类（Cortex-M7 里延迟特性不同的几族）
CLASSES = [
    ("branch",  re.compile(r"^(b|bl|blx|bx|cbz|cbnz|tbb|tbh)\b")),
    ("load",    re.compile(r"^(ldr|ldrh|ldrb|ldrd|ldm|vldr|vldm|ldrex|pop)\b")),
    ("store",   re.compile(r"^(str|strh|strb|strd|stm|vstr|vstm|strex|push)\b")),
    ("mul",     re.compile(r"^(mul|mla|mls|smull|umull|sdiv|udiv|vmul|vfma|vmla|vdiv|vsqrt)\b")),
    ("fp",      re.compile(r"^v[a-z]+")),
    ("move",    re.compile(r"^(mov|movw|movt|mvn)\b")),
    ("alu",     re.compile(r"^(add|sub|and|orr|eor|bic|lsl|lsr|asr|cmp|tst|adc|sbc|rsb|clz|rbit|sxt|uxt)\b")),
]


def cls_of(mn):
    for name, rx in CLASSES:
        if rx.match(mn):
            return name
    return "other"


def objdump(args):
    r = subprocess.run([OBJDUMP] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT)
    return r.stdout or ""


def disasm_all():
    """★ objdump 2.30 **不支持** `--disassemble=<sym>`（会报 "option doesn't allow
    an argument"）⇒ 只能整份反汇编一次, 再按地址区间切。整份只取一次, 后面所有符号
    复用（原版按符号反复调用既慢又拿不到东西）。"""
    global _ALL
    if _ALL is None:
        out = objdump(["-d", "--no-show-raw-insn", ELF])
        _ALL = []
        for line in out.splitlines():
            m = re.match(r"\s*([0-9a-f]+):\s+([a-z][a-z0-9.]*)\s*(.*)$", line)
            if m:
                _ALL.append((int(m.group(1), 16), m.group(2), m.group(3).strip()))
        # 同时抓 "<sym>:" 形式的函数头, 用来判断符号边界
        _HDRS.clear()
        for m in re.finditer(r"^([0-9a-f]{8}) <([^>]+)>:", out, re.M):
            _HDRS.append((int(m.group(1), 16), m.group(2)))
        _HDRS.sort()
    return _ALL


_ALL = None
_HDRS = []


def sym_range(sym):
    """按函数头表取该符号的地址区间（取到下一个函数头为止）。"""
    disasm_all()
    for i, (addr, name) in enumerate(_HDRS):
        if name == sym:
            nxt = _HDRS[i + 1][0] if i + 1 < len(_HDRS) else (1 << 32)
            return addr, nxt
    return None


def disasm_symbol(sym):
    rng = sym_range(sym)
    if not rng:
        return []
    lo, hi = rng
    return [(a, mn, op) for a, mn, op in disasm_all() if lo <= a < hi]


def report(sym):
    ins = disasm_symbol(sym)
    print("\n" + "=" * 78)
    print("符号 %s —— %d 条指令" % (sym, len(ins)))
    print("=" * 78)
    if not ins:
        print("  （没取到 —— 符号名可能被 inline 或改名, 需在 map 里核对）")
        return None
    c = Counter(cls_of(mn) for _, mn, _ in ins)
    tot = sum(c.values())
    print("  指令类直方图:")
    for k, v in c.most_common():
        print("    %-8s %4d  (%5.1f%%)" % (k, v, 100.0 * v / tot))
    # 列出分支（最长路径分析里它们是"路径选择点"）
    brs = [(a, mn, op) for a, mn, op in ins if cls_of(mn) == "branch"]
    print("  分支 %d 条:" % len(brs))
    for a, mn, op in brs[:24]:
        print("    %08X  %-8s %s" % (a, mn, op))
    if len(brs) > 24:
        print("    …（另有 %d 条）" % (len(brs) - 24))
    return ins


def main():
    if not os.path.exists(OBJDUMP):
        print("!! 找不到 objdump: %s" % OBJDUMP); return 2
    if not os.path.exists(ELF):
        print("!! 找不到 ELF: %s" % ELF); return 2
    print("ELF   = %s" % ELF)
    print("objdump = %s" % OBJDUMP)
    syms = sys.argv[1:] or ["engine_scan_itcm", "prim_exec",
                            "read_source", "wire2_valid"]
    res = {}
    for s in syms:
        res[s] = report(s)
    if len(sys.argv) <= 1:
        print("\n★ 提示: 传符号名可只看指定函数, 例如 `prim_exec`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
