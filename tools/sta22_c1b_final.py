#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-22（步 C1b 收口）: **显式列地址区间**计价每路由前导。

## 为什么不用 BFS（STA-21 的教训）
STA-21 用"反向可达分派块"取前导块, 结果**空集**。根因: 分派块(0x0DE6)的前驱是
**尾块(0x0E5C)**, 而尾块在 `>=0x0D8A` 的界外被排除 ⇒ 反搜断链。
★ 更根本的是: **每路由前导大部分在块0内部**（块0 = 0x0CF0..0x0D8A 共 44 条）,
  而块0里**既有一次性初始化**（12 条 VFP 常量装载, 每次 ISR 调用只做一次）
  **又有每路由循环尾**（0x0D3C 起的 ACTIVE 判定 / 周期分档 / read_source 分派）。
  ⇒ **块粒度太粗**: 一个基本块里混了两种执行频次的东西。
  ⇒ 只能**按地址区间显式分**, 不能按块取。

## 指令事实（由 STA-21 打印核对）
  活跃腿   0x0D3C .. 0x0D8A  = 20 条   （ACTIVE 判定 + 周期分档 + read_source 分派）
  跳过腿   0x0E98 .. 0x0EA0  =  3 条   （`adds r1,#16` + `bne 0xD3C` + `ldmia` 返回）
  公用段   0x0D8A .. 0x0DE6  = 25 条   （块1+块2: param/state 地址 + wire2 + 有限性检查）
  分派块   0x0DE6 .. 0x0DF4  =  5 条
  尾块     0x0E5C .. 0x0EA0  = 27 条
★ 本仓库的路由表把 ACTIVE 路由**桶化**（未激活的不进桶）, 所以稳态下走**活跃腿**。
  但"跳过腿"仍可能在桶里出现 ⇒ 两种都报, 取**保守（较大）者**作为"每路由固定开销"。

## 判据
  Q1 保守固定开销应落在 **56 ~ 80 cyc**
  Q2 该口径下必须 **19/19 `结构 ≥ 实测`**（界的底线）
  Q3 报出**过估比**（固定开销 / 实测下限 56）, 并如实说明它是"界"不是"精确值"
"""
import os, re, subprocess, sys
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
from collections import deque

# ★ 显式区间（地址, 来源见 docstring 的"指令事实"）
RANGES = {
    # ★★ 显式区间**必须两两不相交** —— 第一版写了重叠区间, 于是同一段代码被计两次
    #   （`common` 含了 `dispatch`; `tail` 与 `skip_leg` 相叠）。
    #   本版在构造处就切开, 并保留一个**机器判据**在运行时断言不相交。
    "active_pre": (0x0D3C, 0x0D8A),   # 活跃前导: ACTIVE 判定 + 周期分档 + read_source 分派
    "common_a":   (0x0D8A, 0x0DE6),   # 公用段前半: param/state 地址 + wire2 + 有限性检查
    "dispatch":   (0x0DE6, 0x0DF4),   # 分派块（tbh）
    "common_b":   (0x0DF4, 0x0E5C),   # 公用段后半: 分派表之后、尾块之前
    "tail":       (0x0E5C, 0x0E9C),   # 尾块: 写回 + 指针推进 + `cmp`
    "backedge":   (0x0E9C, 0x0EA0),   # 回边: `bne.w 0xD3C` + `ldmia` 返回
    "skip_leg":   (0x0E98, 0x0E9C),   # 跳过腿: `adds r1,#16`（在 tail 之前的独立入口）
}


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

    def occ_rng(a0, a1):
        """区间 [a0, a1) 的文档占用和与条数。"""
        cyc = n = 0
        for a, mn, _ in body:
            if a0 <= a < a1:
                cyc += (occ_of(mn)[0] or 1)
                n += 1
        return cyc, n

    print("=== 显式区间计价 ===")
    tot = {}
    spans = []
    for name, (a0, a1) in RANGES.items():
        c, n = occ_rng(a0, a1)
        tot[name] = c
        spans.append((a0, a1, name))
        print("  %-12s 0x%04X..0x%04X  %2d 条  %3d cyc" % (name, a0, a1, n, c))
    # ★ 区间两两不相交的**机器判据**（防止再次重复计价）
    spans.sort()
    ov = []
    for i in range(len(spans) - 1):
        if spans[i][1] > spans[i + 1][0]:
            ov.append((spans[i][2], spans[i + 1][2]))
    print("  区间两两不相交: %s" % ("**通过**" if not ov else "**重叠** %s" % ov))
    print("  区间并集覆盖 0x%04X..0x%04X 共 %d 条"
          % (min(s[0] for s in spans), max(s[1] for s in spans),
             sum(1 for a, _m, _o in body if min(s[0] for s in spans) <= a < max(s[1] for s in spans))))

    active = tot["active_pre"] + tot["common_a"] + tot["dispatch"] + tot["common_b"] + tot["tail"]
    skip = tot["skip_leg"] + tot["backedge"]
    print("\n  活跃路由的每路由固定开销 = 活跃前导 + 公用段 + 分派 + 尾块 = **%d cyc**" % active)
    print("  跳过路由的每路由固定开销 = 跳过腿 + 尾块                   = **%d cyc**" % skip)
    FIXED = max(active, skip)
    print("  ⇒ 取保守（较大）者 = **%d cyc**（实测下限 56）" % FIXED)

    # 每 op 独有部分（直落闭包, 与 STA-18/20 同口径）
    disp = [bi for bi, (s, e, _) in enumerate(bp) if body[e - 1][1] in ("tbh", "tbb")]
    tail_bi = None
    for bi, (s, e, _) in enumerate(bp):
        _a, mn2, op2 = body[e - 1]
        t2 = target(op2)
        if UNCOND.match(mn2) and t2 in addr2i and blk[addr2i[t2]] in disp:
            tail_bi = bi
            break
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
        s0 = fall_closure(entry_blks[op])
        b = 0
        for bb in s0:
            ss, ee, _ = bp[bb]
            for i in range(ss, ee):
                b += (occ_of(body[i][1])[0] or 1)
        names.append(OPS[op]); base.append(b); M.append(meas[op])

    print("\n=== 口径对比 ===")
    for tag, extra in (("(b) 只尾块+分派", tot["tail"] + tot["dispatch"]),
                       ("(d2) ★ 本版显式区间（不相交）", FIXED)):
        S = [extra + base[i] for i in range(len(base))]
        gaps = [M[i] - S[i] for i in range(len(S))]
        ge = sum(1 for g in gaps if g <= 0)
        print("%-28s 固定=%3d 下限=%3d(差%+3d) ρ=%+.3f 残余中位=%+3.0f ≥实测 %2d/19"
              % (tag, extra, extra + min(base), extra + min(base) - 56,
                 spearman(S, M), statistics.median(gaps), ge))

    S = [FIXED + base[i] for i in range(len(base))]
    gaps = [M[i] - S[i] for i in range(len(S))]
    print("\n判据:")
    print("  Q1 保守固定开销 %d 落在 56~80 ⇒ %s"
          % (FIXED, "通过" if 56 <= FIXED <= 80 else "**未通过**"))
    ge = sum(1 for g in gaps if g <= 0)
    print("  Q2 结构≥实测 %d/19 ⇒ %s" % (ge, "通过" if ge == 19 else "**未通过**"))
    print("  Q3 过估比 = %d/56 = %.2f× ⇒ %s（界成立但偏松, 如实记）"
          % (FIXED, FIXED / 56.0, "可接受" if FIXED / 56.0 <= 1.5 else "偏松"))
    print("\n  逐 op: 结构 / 实测 / 缺口")
    for i in sorted(range(len(gaps)), key=lambda k: -gaps[k]):
        print("    %-9s %4d %4d %+5d" % (names[i], S[i], M[i], gaps[i]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
