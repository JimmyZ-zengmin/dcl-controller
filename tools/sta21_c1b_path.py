#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-21（步 C1b）: 把"每路由前导"按**实际路径**计价, 并打印其块结构以便人工核对。

## 上一版的错
STA-20 把地址区间 `0x0D3C..0x0DE6`（47 条, 文档和 56 cyc）整体当成"每路由前导"。
但那段里含**"路由非 ACTIVE ⇒ 跳过"那条分支的腿**（`bpl.w e98` @0x0D42）,
它不是每路由都执行 ⇒ 56 cyc 是**多算**的。
（同时可能还有少算: 前导里还有别的分支腿。）

## 本脚本
1. 打印 `0x0CF0..0x0E18`（循环前导 + 分派）的**块清单**（地址/条数/末条/后继），
   供人工核对控制流 —— **先看清结构, 再计价**（不再"按区间求和"）
2. 用块图求 **"每路由前导块集"**:
     起点 = 分派块; 反向收集**只经由 `0x0D40..0x0D8A` 这段**能到达分派块的前驱块。
     为避免把 case 体卷进来, 反向搜索**限制在地址 < 0x0D8A 的块**内。
3. 对两个"腿"都计价并**分别报告**: ACTIVE 成立腿 / 跳过腿（后者不应计入每路由）

## 判据
  P1 前导块集必须 **不含** 任何 case 体块（地址 < 0x0D8A 自然满足, 但要显式断言）
  P2 修正后的固定开销应落在 **56 ~ 70 cyc**
  P3 修正后仍须 **19/19 `结构 ≥ 实测`**（这是"界"的底线, 不许为了贴 56 而破坏它）
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

    # ── 1. 打印循环前导 + 分派的块结构 ──
    print("=== 循环前导 + 分派（0x0CF0..0x0E18）的块清单 ===")
    for bi, (s, e, a0) in enumerate(bp):
        if a0 > 0x0E18:
            break
        la, lmn, lop = body[e - 1]
        t = target(lop)
        nxt = ""
        if BRANCH.match(lmn):
            if t is not None and t in addr2i:
                nxt += " →0x%X(块%d)" % (t, blk[addr2i[t]])
            if not UNCOND.match(lmn) and e < N:
                nxt += " ↓0x%X(块%d)" % (body[e][0], blk[e])
        print("  块%-3d @0x%08X %2d 条  末条 %-9s %-24s%s"
              % (bi, a0, e - s, lmn, lop, nxt))

    # ── 2. 每路由前导块集（限制在 < 0x0D8A）──
    BOUND = 0x0D8A
    disp = [bi for bi, (s, e, _) in enumerate(bp) if body[e - 1][1] in ("tbh", "tbb")]
    disp_main = min(disp)
    # 反向后继表（只在 bound 之内建边）
    rev = {}
    for bi, (s, e, _) in enumerate(bp):
        if bp[bi][2] >= BOUND:
            continue
        la, lmn, lop = body[e - 1]
        tgt = []
        if BRANCH.match(lmn):
            t = target(lop)
            if t is not None and t in addr2i:
                tgt.append(blk[addr2i[t]])
            if not UNCOND.match(lmn) and e < N:
                tgt.append(blk[e])
        elif e < N:
            tgt.append(blk[e])
        for t2 in tgt:
            if bp[t2][2] < BOUND:
                rev.setdefault(t2, []).append(bi)
    # 从分派块反向走到 0x0D3C
    start_blk = blk[addr2i[0x0D3C]]
    seen, q = set(), deque([disp_main])
    while q:
        b = q.popleft()
        if b in seen or bp[b][2] >= BOUND:
            continue
        seen.add(b)
        for r in rev.get(b, []):
            if r not in seen:
                q.append(r)
    pre_blocks = sorted(seen)
    print("\n=== 每路由前导块集（反向可达分派块, 限制 <0x%X）===" % BOUND)
    print("  %s" % [(b, hex(bp[b][2]), bp[b][1] - bp[b][0]) for b in pre_blocks])

    def occ_range(i0, i1):
        return sum((occ_of(body[i][1])[0] or 1) for i in range(i0, i1))

    pre_cyc = sum(occ_range(*bp[b][:2]) for b in pre_blocks)
    print("  文档占用和 = **%d cyc**（%d 个块, %d 条）"
          % (pre_cyc, len(pre_blocks), sum(bp[b][1] - bp[b][0] for b in pre_blocks)))

    # P1: 前导块集不得含 case 体
    bad = [b for b in pre_blocks if bp[b][2] >= 0x0E18]
    print("  P1 不含 case 体块: %s" % ("**通过**" if not bad else "**未通过** %s" % bad))

    tail_bi = None
    for bi, (s, e, _) in enumerate(bp):
        _a, mn2, op2 = body[e - 1]
        t2 = target(op2)
        if UNCOND.match(mn2) and t2 in addr2i and blk[addr2i[t2]] in disp:
            tail_bi = bi
            break
    tail_cyc = occ_range(*bp[tail_bi][:2])
    disp_cyc = occ_range(*bp[disp_main][:2])

    entry_blks = {op: blk[addr2i[entry[op]]] for op in range(19) if entry[op] in addr2i}
    other_entry = set(entry_blks.values())

    def fall_closure(start_bi):
        s0, q = set(), deque([start_bi])
        while q:
            b = q.popleft()
            if b in s0 or b == tail_bi or b in disp:
                continue
            s0.add(b)
            _s, e, _ = bp[b]
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
        return s0

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

    print("\n=== 固定开销口径对比 ===")
    for tag, extra in (("(c) 只尾块", tail_cyc),
                       ("(b) 尾块+分派", tail_cyc + disp_cyc),
                       ("(d0) STA-20 区间前导(47条)", tail_cyc + disp_cyc + 56),
                       ("(d1) ★ 路径前导(本版)", tail_cyc + disp_cyc + pre_cyc)):
        S = [extra + base[i] for i in range(len(base))]
        gaps = [M[i] - S[i] for i in range(len(S))]
        ge = sum(1 for g in gaps if g <= 0)
        print("%-30s 固定=%3d 下限=%3d(差%+3d) ρ=%+.3f 残余中位=%+3.0f ≥实测 %2d/19"
              % (tag, extra, extra + min(base), extra + min(base) - 56,
                 spearman(S, M), statistics.median(gaps), ge))

    extra = tail_cyc + disp_cyc + pre_cyc
    S = [extra + base[i] for i in range(len(base))]
    gaps = [M[i] - S[i] for i in range(len(S))]
    print("\n判据（主口径 = 尾块+分派+路径前导）:")
    print("  P2 固定开销 %d 落在 56~70 ⇒ %s"
          % (extra, "通过" if 56 <= extra <= 70 else "**未通过**"))
    print("  P3 结构≥实测 %d/19 ⇒ %s"
          % (sum(1 for g in gaps if g <= 0),
             "通过" if sum(1 for g in gaps if g <= 0) >= 19 else "**未通过（界被破坏）**"))
    print("\n  逐 op（结构 vs 实测 vs 缺口）:")
    for i in sorted(range(len(gaps)), key=lambda k: -gaps[k]):
        print("    %-9s 结构%4d 实测%4d %+5d" % (names[i], S[i], M[i], gaps[i]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
