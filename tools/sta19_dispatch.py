#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-19（步 C1 收口）: 把**分派块**算进"每路由固定开销", 看缺口是否被补上。

## 假设（由 L3 的数字直接给出）
  尾块(34) + 最小 op 闭包(0) = **34 cyc**, 而实测下限 = **56 cyc** ⇒ 缺 **22 cyc**。
  候选① 的量化: 分派块 `ldrb r6,[r1,#2116] / subs r6,#1 / cmp r6,#17 / bhi / tbh`
  按文档占用累加 ≈ 2+1+1+1+3 = **8~22 cyc 量级**。
  ★ 而分派块在步 A 里被我归为**循环携带**（每圈一次）—— 但**一圈只处理一条路由**,
    所以"每圈一次"**就是"每路由一次"**。步 A 的归类在"逐 op 结构量"上是对的
    （它不区分 op）, 但在"逐路由固定开销"上**把它漏掉了**。

## 判据（都能失败）
  M1 补上分派块后, 结构下限 `尾块+分派块` 应落在 56±12 cyc
  M2 残余缺口中位数应从 **+29** 显著下降（目标 ≤ 15 cyc）
  M3 "结构 ≥ 实测"的比例应从 **0/19** 升上去（目标 ≥ 8/19）
  M4 ★ 若 M1~M3 有任一项仍不过 ⇒ **点名剩余缺口**, 并列出还没被覆盖的候选
     （`read_source` 间接跳转 / 每路由 `_finite_f` 检查 / `wire2_valid` 判定）

## 本脚本同时报"两种把分派算进去"的方式
  (a) 分派块**全价**（文档和）
  (b) 分派块**只算 `ldrb+subs/cmp/bhi`**（不含 `tbh` —— `tbh` 的开销可能已含在跳转里）
     ⇒ 两种都报, 让数据说话, **不挑那个好看的**（本轮纪律: 判据先定, 不事后选）。
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


def build():
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
    return body, N, addr2i, bp, blk, entry


def main():
    body, N, addr2i, bp, blk, entry = build()
    disp = [bi for bi, (s, e, _) in enumerate(bp) if body[e - 1][1] in ("tbh", "tbb")]
    tail_bi = None
    for bi, (s, e, _) in enumerate(bp):
        _a, mn2, op2 = body[e - 1]
        t2 = target(op2)
        if UNCOND.match(mn2) and t2 in addr2i and blk[addr2i[t2]] in disp:
            tail_bi = bi
            break

    def occ(bi):
        s, e, _ = bp[bi]
        return sum((occ_of(body[i][1])[0] or 1) for i in range(s, e))

    # 主分派块 = 含有 19 项表的那一个（最低地址的 tbh 块）
    disp_main = min(disp)
    ds, de, _ = bp[disp_main]
    print("尾块   = 块 %d（%d 条, 文档和 %d cyc）" % (tail_bi, bp[tail_bi][1] - bp[tail_bi][0], occ(tail_bi)))
    print("分派块 = 块 %d（%d 条, 文档和 %d cyc）" % (disp_main, de - ds, occ(disp_main)))
    print("  分派块内容:")
    for i in range(ds, de):
        o, c = occ_of(body[i][1])
        print("     %08X  %-10s %-26s → %s cyc" % (body[i][0], body[i][1], body[i][2], o))

    # (b) 不含 tbh 的分派价
    disp_no_tbh = sum((occ_of(body[i][1])[0] or 1) for i in range(ds, de)
                      if body[i][1] not in ("tbh", "tbb"))
    print("  两种口径: (a) 全价 = %d cyc;  (b) 不含 tbh = %d cyc" % (occ(disp_main), disp_no_tbh))

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
        C = fall_closure(entry_blks[op])
        names.append(OPS[op])
        base.append(sum(occ(b) for b in C))
        M.append(meas[op])

    for tag, extra in (("(a) 尾块+分派全价", occ(tail_bi) + occ(disp_main)),
                       ("(b) 尾块+分派(不含tbh)", occ(tail_bi) + disp_no_tbh),
                       ("(c) 只尾块（步 C 原口径）", occ(tail_bi))):
        S = [extra + base[i] for i in range(len(base))]
        rho = spearman(S, M)
        gaps = [M[i] - S[i] for i in range(len(S))]
        ge = sum(1 for g in gaps if g <= 0)
        mn = min(base)
        print("\n=== %s ===" % tag)
        print("  固定开销 = %d cyc;  结构下限 = %d + %d = **%d**（实测下限 56, 差 %+d）"
              % (extra, extra, mn, extra + mn, extra + mn - 56))
        print("  Spearman(结构, 实测) = %+.3f" % rho)
        print("  残余缺口: 中位 %+.0f cyc, 最大 %+d,  结构≥实测 %d/%d"
              % (statistics.median(gaps), max(gaps), ge, len(gaps)))
        bad = sorted(range(len(gaps)), key=lambda i: -gaps[i])[:5]
        print("  前 5 大缺口: %s" % ", ".join("%s(%+d)" % (names[i], gaps[i]) for i in bad))

    print("\n判据判定（以 (b) 为主口径, 因为 (a) 可能把 tbh 重复计价）:")
    extra = occ(tail_bi) + disp_no_tbh
    S = [extra + base[i] for i in range(len(base))]
    gaps = [M[i] - S[i] for i in range(len(S))]
    mn = min(base)
    m1 = abs((extra + mn) - 56) <= 12
    m2 = statistics.median(gaps) <= 15
    m3 = sum(1 for g in gaps if g <= 0) >= 8
    print("  M1 结构下限落在 56±12: %d ⇒ %s" % (extra + mn, "通过" if m1 else "**未通过**"))
    print("  M2 残余缺口中位 ≤15: %+.0f ⇒ %s" % (statistics.median(gaps), "通过" if m2 else "**未通过**"))
    print("  M3 结构≥实测 ≥8/19: %d ⇒ %s" % (sum(1 for g in gaps if g <= 0), "通过" if m3 else "**未通过**"))
    if m1 and m2 and m3:
        print("\n  ⇒ **三项全过**: 缺失的固定开销 = 尾块 + 分派块, 已补齐。")
    else:
        print("\n  M4 剩余缺口要点名（还没被覆盖的候选）:")
        print("     · `read_source()` 的间接跳转（每个 op 入口之前, 按 src_type 分派）")
        print("     · 每路由的 `_finite_f(src)` / `_finite_f(wb)` 检查")
        print("     · `wire2_valid()` 判定 + `params/states` 地址计算")
        print("     ⇒ 这些都在**扫描体的循环前导块**里; 下一版应把它们按'每路由'计价再试。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
