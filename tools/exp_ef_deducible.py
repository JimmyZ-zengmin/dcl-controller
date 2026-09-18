#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-F: `m_op` 能否**从结构推导**? —— 用干净的区间 + 有物理依据的吞吐模型。

## 为什么再试一次（前两次失败的原因不同, 这次修掉了）
1. **STA-16（文档模型）失败**: `Spearman=0.583`, 19/19 全低估。
   但它用的是**被污染**的区间（`address-order walk`, LPF 得 255 条 —— 见 §5.48 的 T1）。
2. **STA-13/14（指令类加权）失败**: 那是**对实测成本做回归**, 属于自证, 不算推导。
3. **本次**: 用**干净的区间**（`fall-through closure`, 已证明 L1 无串味）
   + **有物理依据的吞吐模型**（不是随便配系数）。
   ★ 且**判据先定**: 全部 19 个点一次报出, 不做事后挑选（§5.44/§5.49 的教训）。

## 吞吐模型（Cortex-M7 的**约束**，不是拟合出来的系数）
M7 每周期至多:
  · 发射 **2** 条指令（双发射, 且要求成对）
  · 访存 **1** 次（单一 load/store 端口）
  · 浮点 **1** 条
⇒ 一个代码块的**周期下界** = max( 指令数/2 , 访存数 , 浮点数 )
   这是**下界**, 不是估计 —— 所以它**必能失败**（若实测低于它, 说明模型错）。

★ 关键: 这个式子**没有任何自由参数**。
  若它与 `m_op` 相关 ⇒ "可推导"成立; 若不相关 ⇒ 如实记否。
"""
import os, re, subprocess, sys, statistics
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
       "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32."
       "7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-")
ELF = os.path.join(ROOT, "build", "dcl_h723")
OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
       "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]
# E-C 实测（TB/条）—— **只作对照, 不参与结构计算**
M_OP = {"DIRECT": 29.51, "CMP": 37.51, "HYST": 39.01, "CLAMP": 38.50, "LPF": 49.50,
        "PID": 64.00, "RATE": 35.01, "DEADBAND": 42.01, "MUX": 36.49, "EDGE": 45.01,
        "LUT": 46.01, "CNT": 41.00, "TIMER": 45.01, "ARITH": 37.01, "SCALE": 35.00,
        "AND": 39.01, "OR": 35.50, "NOT": 35.01, "SR": 39.09}

BR = re.compile(r"^(b|bl|blx|bx|cbz|cbnz|tbb|tbh|it)\b")
UNCOND = re.compile(r"^(b|bx|tbb|tbh)\b")
MEM = re.compile(r"^(ldr|str|ldrh|strh|ldrb|strb|ldrd|strd|ldm|stm|push|pop|vldr|vstr|vldm|vstm|vpush|vpop|ldrex|strex)\b")
FP = re.compile(r"^v(?!mrs|msr)")


def run(args):
    return subprocess.run([BIN + "objdump.exe"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def target(op):
    m = re.match(r"^([0-9a-f]+)\s", op.strip())
    return int(m.group(1), 16) if m else None


def spearman(xs, ys):
    def ar(v):
        idx = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and v[idx[j + 1]] == v[idx[i]]:
                j += 1
            a = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[idx[k]] = a
            i = j + 1
        return r
    rx, ry = ar(xs), ar(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n))
    dy = sum((ry[i] - my) ** 2 for i in range(n))
    if dx == 0 or dy == 0:
        return None
    r = num / (dx * dy) ** 0.5
    assert -1.0001 <= r <= 1.0001
    return r


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
    a2i = {a: i for i, (a, _, _) in enumerate(body)}
    leaders = {0}
    for i, (a, mn, opnd) in enumerate(body):
        if BR.match(mn):
            t = target(opnd)
            if t in a2i:
                leaders.add(a2i[t])
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
    # ★★ 分派表地址**动态求**（找 `engine_scan_itcm` 里第一个 `tbh`）。
    #   原来硬编码 `TBL = 0x0DF4` —— 那是从**加 E-D 域之前的 ELF** 上量的。
    #   重编译后整个函数与表都**平移了 +0x50**（`0x0CF0→0x0D40`, `0x0DF4→0x0E44`），
    #   于是 19 个入口地址一个都对不上 ⇒ `rows` 为空 ⇒ 判据在**空集**上跑。
    #   ★ 这与 §5.63（硬编码 SHM 地址随构建静默失效）是**同一族、同一个下午的第二次**。
    #   ⇒ 纪律: **凡是从某次构建量出来的绝对地址, 一律动态求。**
    tbh_list = [(a, body[i][1], body[i][2])
                for i, (a, mn, _o) in enumerate(body) if mn in ("tbh", "tbb")]
    if not tbh_list:
        print("!! 在 engine_scan_itcm 里找不到 tbh ⇒ 判无效"); return 2
    TBL = (tbh_list[0][0] + 4) & ~3
    print("分派表（动态求得）: tbh @0x%08X ⇒ 表 @0x%08X（共 %d 个跳转表）"
          % (tbh_list[0][0], TBL, len(tbh_list)))
    # ★ 表长取到下一个基本块起址（§5.48 的 T3 教训: 表长不能猜）
    _nxt = [bp[b][2] for b in range(len(bp)) if bp[b][2] > tbh_list[0][0]]
    _n_ent = max(0, (min(_nxt) - TBL) // 2) if _nxt else 18
    print("    表长（按下一块起址截断）= %d 项" % _n_ent)
    tg = [TBL + 2 * (mem[TBL + 2 * i] | (mem[TBL + 2 * i + 1] << 8)) for i in range(18)]
    entry = {i + 1: t for i, t in enumerate(tg)}
    entry[0] = 0x0E56          # DIRECT 的 fall-through（旧 ELF 值）
    # ★ fall-through 也必须动态: 它是"越界"分支 (`bhi`) 的目标
    _bhi = [target(body[i][2]) for i, (a, mn, _o) in enumerate(body)
            if mn.startswith("bhi") and target(body[i][2]) is not None]
    if _bhi:
        entry[0] = _bhi[0]
        print("    DIRECT fall-through（动态求得）= 0x%08X" % entry[0])
    missing = [OPS[o] for o in range(19) if entry[o] not in a2i]
    if missing:
        print("!! 这些 op 的入口不在函数体内: %s ⇒ 判无效" % missing); return 2

    disp = [bi for bi, (s, e, _) in enumerate(bp) if body[e - 1][1] in ("tbh", "tbb")]
    other_entry = {blk[a2i[entry[o]]] for o in range(19) if entry[o] in a2i}

    def fall_closure(start_bi):
        """直落闭包（STA-18 已验证: L1 无串味）。★ 干净区间。

        ★★ 2026-09-18 修正: 原版对 `CMP` / `ARITH` 得到 **0 条指令** ——
           因为它们的入口块以**无条件 `b` 跳公共尾块**结尾, 直落闭包立刻停。
           而它们的实际工作**在跳转目标那一侧**（`0x10F4 …` 之类的分支体）。
           ⇒ 直落闭包对这两类是**空的**, 于是结构下界被算成 0（比值 29.51 那种荒谬数）。
           ★ 症状: 表里它们的"指令"列是 0 —— 而 0 条指令的 op **不可能存在于真实代码里**。
             这是本项目"空判据"的又一形态: **0 是缺失, 不是测量值。**
           修法: 若直落闭包为空（或极小）, 改用**"可达闭包"**作为回退,
                 并**在输出里标注该 op 用了回退口径**（不许静默）。
        """
        seen, q = set(), deque([start_bi])
        while q:
            b = q.popleft()
            if b in seen or b in disp:
                continue
            seen.add(b)
            s, e, _ = bp[b]
            last = body[e - 1]
            if last[1] in ("tbh", "tbb"):
                continue
            if BR.match(last[1]):
                if not UNCOND.match(last[1]) and e < N:
                    nb = blk[e]
                    if nb not in other_entry:
                        q.append(nb)
            elif e < N:
                nb = blk[e]
                if nb not in other_entry:
                    q.append(nb)
        return seen

    def reach_closure(start_bi):
        """回退口径: 含跳转目标的闭包（会偏大, 但非空）。"""
        out = set()
        for t in (0,):
            pass
        seen, q = set(), deque([start_bi])
        while q:
            b = q.popleft()
            if b in seen or b in disp:
                continue
            seen.add(b)
            s, e, _ = bp[b]
            last = body[e - 1]
            if last[1] in ("tbh", "tbb"):
                continue
            t = target(last[2])
            if t in a2i:
                nb = blk[a2i[t]]
                if nb not in other_entry:
                    q.append(nb)
            if BR.match(last[1]):
                if not UNCOND.match(last[1]) and e < N:
                    nb = blk[e]
                    if nb not in other_entry:
                        q.append(nb)
            elif e < N:
                nb = blk[e]
                if nb not in other_entry:
                    q.append(nb)
        return seen

    print("=== E-F: 逐原语的**结构下界**（无自由参数）===")
    print("    周期下界 = max(指令数/2, 访存数, 浮点数)   ← M7 的三条硬约束")
    print()
    print("%-9s %-6s %-6s %-6s %-9s %-9s %s"
          % ("op", "指令", "访存", "浮点", "结构下界", "实测m_op", "比值"))
    print("-" * 74)
    rows = []
    for op in range(19):
        e = entry[op]
        if e not in a2i:
            continue
        C = fall_closure(blk[a2i[e]])
        fallback = False
        if len(C) <= 1:                      # ★ 空/极小 ⇒ 用回退口径, 并标注
            C = reach_closure(blk[a2i[e]])
            fallback = True
        n = memc = fpc = 0
        for b in C:
            for i in range(bp[b][0], bp[b][1]):
                mn = body[i][1]
                n += 1
                if MEM.match(mn):
                    memc += 1
                elif FP.match(mn):
                    fpc += 1
        lb = max(n / 2.0, float(memc), float(fpc))
        m = M_OP[OPS[op]]
        rows.append(dict(op=OPS[op], n=n, mem=memc, fp=fpc, lb=lb, m=m))
        print("%-9s %-6d %-6d %-6d %-9.1f %-9.2f %.2f%s"
              % (OPS[op], n, memc, fpc, lb, m, m / lb if lb else 0,
                 "  ← 回退口径" if fallback else ""))

    print("\n=== 判据 ===")
    lbs = [r["lb"] for r in rows]
    ms = [r["m"] for r in rows]
    if max(lbs) == min(lbs):
        print("  结构下界在所有 op 上相同 ⇒ **无区分度, 判据无效**")
        return 2
    rho = spearman(lbs, ms)
    print("  E-F-1 Spearman(结构下界, 实测 m_op) = %+.3f ⇒ %s"
          % (rho, "**通过**（>0.8）" if rho and rho > 0.8 else "**未通过**（目标 >0.8）"))
    # 必须有单调换算关系: 比值应大致恒定
    ratios = [r["m"] / r["lb"] for r in rows if r["lb"]]
    print("  E-F-2 比值 m_op/下界: 中位 %.2f, 极差 %.2f ~ %.2f（极差 %.1f×）"
          % (statistics.median(ratios), min(ratios), max(ratios),
             max(ratios) / max(min(ratios), 1e-9)))
    print("        ⇒ %s"
          % ("**比值近似恒定 ⇒ 可推导**" if max(ratios) / max(min(ratios), 1e-9) <= 1.5
             else "**比值不恒定 ⇒ 不能只靠这一个下界推导**"))
    # 各约束谁是瓶颈
    print("\n  E-F-3 哪个约束是瓶颈（决定下界的项）:")
    for r in rows:
        which = max([("指令/2", r["n"] / 2.0), ("访存", float(r["mem"])), ("浮点", float(r["fp"]))],
                    key=lambda z: z[1])
        print("     %-9s 下界由 **%s** 决定（%.0f）" % (r["op"], which[0], which[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
