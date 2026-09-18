#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-20（步 C1 最终）: 把**每路由前导**也算进去, 补完那块固定开销。

## 进度（前两步已经缩小了缺口）
  只尾块                        : 固定 34 cyc, 下限差 **−22**, 中位残余 +29
  尾块 + 分派块(8)              : 固定 42 cyc, 下限差 **−14**, 中位残余 +21
  ⇒ 还差 ~14 cyc, 且中位残余仍 +21 ⇒ 还有**每路由**的活没计价。

## 本步要加的: **每路由前导**
逐条路由在"取 src / 取 param / 取 state / 判 wire2 / 有限性检查"上花的指令,
它们在 **op 入口之前**（所以既不在尾块、也不在任何 op 的入口块里）。
落点是循环前导块 0（0x0CF0..0x0D8A）里**每圈都会走一遍**的那一段:
  0x0D3C  `ldrb r6,[r1,#2117]`   ← 路由 flags
  0x0D40  `lsls r3,r6,#31` / `bpl`  ← ACTIVE 判定
  0x0D46  `ldrb r3,[r1,#2126]` / `and #3` / `cmp/beq/cmp` ← period 分档（dv）
  0x0D56  `ldrb r4,[r1,#2112]` / `ldrb r3,[r1,#2113]` / `vseleq` ← src_type/src_index
  0x0D62  `cmp r4,#1` / `beq` / `bcc` / `cmp r4,#2` / `bne` ← read_source 分派
  0x0D8A..0x0DE2  ← param/state 地址、wire2_valid、有限性检查
本脚本把这**整段**按"每路由一次"计价（文档占用和）, 加进固定开销。

## 判据
  N1 结构下限应落在 56 ± 12 cyc（**这是主判据** —— 实测下限就是 DIRECT 的 56）
  N2 残余缺口中位应 ≤ 15 cyc
  N3 "结构 ≥ 实测"的比例应 ≥ 8/19
  N4 若仍有缺口 ⇒ **点名它有多大**, 并说明它**不可能**再靠"找更多块"补上
     （因为扫描体里每路由执行一次的块已经被逐个点过了）
"""
import os, re, subprocess, sys
from collections import deque
import statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
       "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32."
       "7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-")
ELF = os.path.join(ROOT, "build", "dcl_h723")
ENGINE_C = os.path.join(ROOT, "src", "engine.c")
OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
       "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]
sys.path.insert(0, os.path.join(ROOT, "tools"))
from sta16_docmodel import occ_of, spearman


def run(args):
    return subprocess.run([BIN + "objdump.exe"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def target(op):
    m = re.match(r"^([0-9a-f]+)\s", op.strip())
    return int(m.group(1), 16) if m else None


UNCOND = re.compile(r"^(b|bx|tbb|tbh)\b")
BRANCH = re.compile(r"^(b|bl|blx|bx|cbz|cbnz|tbb|tbh|it)\b")


def main():
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
    leaders = {0}
    for i, (a, mn, opnd) in enumerate(body):
        if BRANCH.match(mn):
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

    disp = [bi for bi, (s, e, _) in enumerate(bp) if body[e - 1][1] in ("tbh", "tbb")]
    tail_bi = None
    for bi, (s, e, _) in enumerate(bp):
        _a, mn2, op2 = body[e - 1]
        t2 = target(op2)
        if UNCOND.match(mn2) and t2 in addr2i and blk[addr2i[t2]] in disp:
            tail_bi = bi
            break
    disp_main = min(disp)

    def occ_range(i0, i1):
        return sum((occ_of(body[i][1])[0] or 1) for i in range(i0, i1))

    # ★ 每路由前导: 从 0x0D3C（`ldrb r6,[r1,#2117]`, 路由 flags 读入）到分派块之前
    pre_lo = addr2i[0x0D3C]
    pre_hi = addr2i[bp[disp_main][2]]          # 分派块起点
    pre_cyc = occ_range(pre_lo, pre_hi)
    print("每路由前导 0x%08X..0x%08X（%d 条）= **%d cyc**"
          % (0x0D3C, bp[disp_main][2], pre_hi - pre_lo, pre_cyc))
    print("  （含: ACTIVE 判定 / period 分档 / read_source 分派 / param·state 地址 /\n"
          "    wire2_valid / _finite_f 检查 —— 这些在 **op 入口之前**, 此前完全没计价）")

    tail_cyc = occ_range(*bp[tail_bi][:2])
    disp_cyc = occ_range(*bp[disp_main][:2])
    print("尾块 = %d cyc;  分派块 = %d cyc" % (tail_cyc, disp_cyc))

    entry_blks = {op: blk[addr2i[entry[op]]] for op in range(19) if entry[op] in addr2i}
    other_entry = set(entry_blks.values())

    def fall_closure(start_bi):
        seen, q = set(), deque([start_bi])
        while q:
            b = q.popleft()
            if b in seen or b == tail_bi or b in disp:
                continue
            seen.add(b)
            s, e, _ = bp[b]
            last = body[e - 1]
            if last[1] in ("tbh", "tbb"):
                continue
            if BRANCH.match(last[1]):
                if not UNCOND.match(last[1]) and e < N:
                    nb = blk[e]
                    if nb not in other_entry:
                        q.append(nb)
            elif e < N:
                nb = blk[e]
                if nb not in other_entry:
                    q.append(nb)
        return seen

    txt = open(ENGINE_C, encoding="utf-8", errors="replace").read()
    mm = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", txt, re.S)
    meas = [int(x) for x in re.findall(r"\d+", mm.group(1))]

    names, base, M = [], [], []
    for op in range(19):
        if op not in entry_blks:
            continue
        names.append(OPS[op])
        base.append(sum(occ_range(*bp[b][:2]) for b in fall_closure(entry_blks[op])))
        M.append(meas[op])

    variants = [
        ("(c) 只尾块", tail_cyc),
        ("(b) 尾块+分派", tail_cyc + disp_cyc),
        ("(d) 尾块+分派+前导", tail_cyc + disp_cyc + pre_cyc),
        ("(e) 尾块+前导（不含分派）", tail_cyc + pre_cyc),
    ]
    print()
    for tag, extra in variants:
        S = [extra + base[i] for i in range(len(base))]
        gaps = [M[i] - S[i] for i in range(len(S))]
        ge = sum(1 for g in gaps if g <= 0)
        print("%-26s 固定=%3d  下限=%3d(差%+3d)  ρ=%+.3f  残余中位=%+3.0f  ≥实测 %2d/19"
              % (tag, extra, extra + min(base), extra + min(base) - 56,
                 spearman(S, M), statistics.median(gaps), ge))

    extra = tail_cyc + disp_cyc + pre_cyc
    S = [extra + base[i] for i in range(len(base))]
    gaps = [M[i] - S[i] for i in range(len(S))]
    print("\n判据（主口径 = 尾块+分派+前导）:")
    n1 = abs((extra + min(base)) - 56) <= 12
    n2 = statistics.median(gaps) <= 15
    n3 = sum(1 for g in gaps if g <= 0) >= 8
    print("  N1 结构下限 %d 落在 56±12 ⇒ %s" % (extra + min(base), "通过" if n1 else "**未通过**"))
    print("  N2 残余缺口中位 %+.0f ≤15 ⇒ %s" % (statistics.median(gaps), "通过" if n2 else "**未通过**"))
    print("  N3 结构≥实测 %d/19 ≥8 ⇒ %s"
          % (sum(1 for g in gaps if g <= 0), "通过" if n3 else "**未通过**"))
    print("\n  逐 op 缺口（正 = 结构仍低估）:")
    for i in sorted(range(len(gaps)), key=lambda k: -gaps[k]):
        print("    %-9s 结构%4d 实测%4d %+5d" % (names[i], S[i], M[i], gaps[i]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
