#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-14: 修正 Spearman（**平局秩**）并重算全部相关系数。

## 为什么必须单独做这一步（自我纠错）
STA-13 打出 `Spearman = -8.576 / -11.439` —— **超出 [-1, 1] 是数学上不可能的**。
根因: 秩是这样算的
    rx = {v: i for i, v in enumerate(sorted(xs))}
**平局（相同数值）会让字典把多个样本映到同一个秩下标**, 于是 `rx[xs[i]]` 撞车 /
秩和不再守恒 ⇒ ρ 跑到区间外。
★ 而它**不报错**, 只是给一个荒谬但"看起来像相关系数"的数。
⇒ 这正是本项目反复记的那一族: **能算出数、但数没有意义**。

## 正确做法
平均秩（average rank）: 相同值取它们秩位的平均。这样 Σd² 才有定义。
"""
import os, re, subprocess, sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
       "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32."
       "7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-")
ELF = os.path.join(ROOT, "build", "dcl_h723")
ENGINE_C = os.path.join(ROOT, "src", "engine.c")
OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
       "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]


def avg_ranks(xs):
    """平均秩（处理平局）。返回与 xs 等长的秩列表。"""
    idx = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and xs[idx[j + 1]] == xs[idx[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[idx[k]] = avg
        i = j + 1
    return r


def spearman(xs, ys):
    """正确版: Pearson on average ranks。自带区间断言（能失败）。"""
    rx, ry = avg_ranks(xs), avg_ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n))
    dy = sum((ry[i] - my) ** 2 for i in range(n))
    if dx == 0 or dy == 0:
        return None                      # 常数序列 ⇒ 无定义（**不是 0**）
    rho = num / (dx * dy) ** 0.5
    assert -1.0001 <= rho <= 1.0001, "Spearman 越界: %r ⇒ 秩算错了" % rho
    return rho


def run(args):
    return subprocess.run([BIN + "objdump.exe"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def target(op):
    m = re.match(r"^([0-9a-f]+)\s", op.strip())
    return int(m.group(1), 16) if m else None


def main():
    # 先自检: 秩函数本身要有造好的正/反对照
    print("秩函数自检:")
    checks = [
        ([1, 2, 3, 4], [1, 2, 3, 4], 1.0, "完全同序"),
        ([1, 2, 3, 4], [4, 3, 2, 1], -1.0, "完全反序"),
        ([1, 1, 2, 2], [1, 1, 2, 2], 1.0, "带平局同序"),
        ([1, 1, 1, 2], [5, 5, 5, 9], 1.0, "三平局"),
    ]
    ok = True
    for xs, ys, want, tag in checks:
        got = spearman(xs, ys)
        good = got is not None and abs(got - want) < 1e-9
        ok &= good
        print("  %-12s ρ=%s 期望 %s ⇒ %s" % (tag, got, want, "OK" if good else "**错**"))
    print("  ⇒ 秩函数 %s\n" % ("通过" if ok else "**未通过, 后面的数都不可信**"))
    if not ok:
        return 2

    out = run(["-d", "--no-show-raw-insn", ELF])
    ins = []
    for line in out.splitlines():
        m = re.match(r"\s*([0-9a-f]+):\s+([a-z][a-z0-9.]*)\s*(.*)$", line)
        if m:
            ins.append((int(m.group(1), 16), m.group(2), m.group(3).strip()))
    hdrs = sorted((int(m.group(1), 16), m.group(2))
                  for m in re.finditer(r"^([0-9a-f]{8}) <([^>]+)>:", out, re.M))
    lo = next(a for a, n in hdrs if n == "engine_scan_itcm")
    hi = next((a for a, _ in hdrs if a > lo), lo + 0x4000)
    body = [x for x in ins if lo <= x[0] < hi]
    N = len(body)
    addr2i = {a: i for i, (a, _, _) in enumerate(body)}
    BR = re.compile(r"^(b|bl|blx|bx|cbz|cbnz|tbb|tbh|it)\b")
    leaders = {0}
    for i, (a, mn, opnd) in enumerate(body):
        if BR.match(mn):
            t = target(opnd)
            if t in addr2i:
                leaders.add(addr2i[t])
            if i + 1 < N:
                leaders.add(i + 1)
    leaders = sorted(leaders)
    bp = [(s, (leaders[j + 1] if j + 1 < len(leaders) else N), body[s][0])
          for j, s in enumerate(leaders)]
    blk = {}
    for bi, (s, e, _) in enumerate(bp):
        for i in range(s, e):
            blk[i] = bi
    sec = run(["-s", "--section=.itcm_text", ELF])
    mem = {}
    for line in sec.splitlines():
        m = re.match(r"\s*([0-9a-f]+)\s+((?:[0-9a-f]{2,8}\s+){1,4})", line)
        if m:
            a = int(m.group(1), 16)
            for i, b in enumerate(bytes.fromhex(m.group(2).replace(" ", ""))):
                mem[a + i] = b
    TBL = 0x0DF4
    tg = [TBL + 2 * (mem[TBL + 2 * i] | (mem[TBL + 2 * i + 1] << 8)) for i in range(18)]
    entry = {i + 1: t for i, t in enumerate(tg)}
    entry[0] = 0x0E56
    txt = open(ENGINE_C, encoding="utf-8", errors="replace").read()
    mm = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", txt, re.S)
    meas = [int(x) for x in re.findall(r"\d+", mm.group(1))]

    names, sizes, fp, mem_, alu, br = [], [], [], [], [], []
    for op in range(19):
        e = entry[op]
        if e not in addr2i:
            continue
        bi = blk[addr2i[e]]
        s, en, _ = bp[bi]
        names.append(OPS[op]); sizes.append(en - s)
        c = Counter()
        for i in range(s, en):
            mn = body[i][1].lower()
            k = ("fp" if mn.startswith("v") and not mn.startswith(("vmrs", "vmsr"))
                 else "mem" if mn.startswith(("ldr", "str", "ldm", "stm", "push", "pop"))
                 else "br" if re.match(r"^(b|cbz|cbnz|bl|bx|tbh)", mn)
                 else "alu")
            c[k] += 1
        fp.append(c["fp"]); mem_.append(c["mem"]); br.append(c["br"]); alu.append(c["alu"])

    M = [meas[OPS.index(n)] for n in names]
    print("重算（**修正秩之后**）:")
    print("-" * 62)
    for tag, xs in (("入口块总条数", sizes), ("FP 指令数", fp), ("访存指令数", mem_),
                    ("分支数", br), ("ALU 数", alu)):
        r = spearman(xs, M)
        print("  Spearman(%-10s, 实测) = %s" % (tag, "n/a（常数）" if r is None else "%+.3f" % r))
    print("\n  逐 op 明细:")
    print("  %-9s %-7s %-6s %-6s %-6s %-6s %s" % ("op", "条数", "FP", "访存", "分支", "ALU", "实测"))
    for i, nm in enumerate(names):
        print("  %-9s %-7d %-6d %-6d %-6d %-6d %d"
              % (nm, sizes[i], fp[i], mem_[i], br[i], alu[i], M[i]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
