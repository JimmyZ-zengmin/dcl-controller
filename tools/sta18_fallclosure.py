#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-18（步 C1 收口）: 用**块图上的"直落闭包"**代替"线性走地址"。

## 为什么要换（STA-17 的教训, 记下来）
STA-17 沿"地址顺序 + 遇条件分支取下落"走, 两次都**走进下一个 op 的代码**
（K1 判据连续两次失败: LPF 路径 15 条 > 自己的块 9 条, AND 10 > 5, NOT 7 > 4）。
根因: **线性走地址隐含"代码按地址按 op 排布"**, 而 §4 的 T1 已经把这条否掉了
（跳转表项序 ≠ 代码布局序）。⇒ 同一个错误换了个地方又犯了一次。

## 本版做法（在块图上做, 不碰地址顺序）
  1. 建**块级 CFG**（与 sta10 同一套: 分支目标 + `tbh` 表解析）
  2. `<op> 的直落闭包` = 从入口块出发, 只走**直落边**（顺序后继, 不含跳转目标）
     能到达的块集合; 且**遇到共享尾块或分派块即止**
  3. 该闭包就是"不跳走时这个 op 会顺序执行到的代码"
     ⇒ 它是**下界**（真正执行还要加上跳转目标那部分）, 但**不会越界到别的 op**

## 判据
  L1 闭包内的块必须**不属于其它 op 的入口块**（否则仍串味）
  L2 `Spearman(直落闭包占用, 实测)` —— 与 STA-17 的 0.893 / 整块 0.583 / 条数 0.641 对比
  L3 结构截距: 用尾块文档和(34) + 最小 op 闭包占用, 与实测下限 56 cyc 比
  L4 残余缺口中位数（若仍 ~+30 ⇒ 那块固定开销**不在扫描体的任何已识别块里**, 要点名）
"""
import os, re, subprocess, sys
from collections import deque

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
    ts, te, _ = bp[tail_bi]
    c0_doc = sum((occ_of(body[i][1])[0] or 1) for i in range(ts, te))
    print("共享尾块 = 块 %d（%d 条, 文档和 %d cyc）" % (tail_bi, te - ts, c0_doc))

    entry_blks = {op: blk[addr2i[entry[op]]] for op in range(19) if entry[op] in addr2i}
    other_entry = set(entry_blks.values())

    def doc_of(bi):
        s, e, _ = bp[bi]
        return sum((occ_of(body[i][1])[0] or 1) for i in range(s, e))

    def fall_closure(start_bi):
        """只走**直落边**（顺序后继）的闭包; 遇尾块/分派块即止。"""
        seen, q = set(), deque([start_bi])
        while q:
            b = q.popleft()
            if b in seen:
                continue
            if b == tail_bi or b in disp:
                continue                       # 止步（尾块单独计价）
            seen.add(b)
            s, e, _ = bp[b]
            last = body[e - 1]
            if last[1] in ("tbh", "tbb"):
                continue
            if BRANCH.match(last[1]):
                # 条件分支可直落; 无条件分支不直落
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

    names, S, n_instr, M = [], [], [], []
    print("\n%-9s %-9s %-8s %-8s %s" % ("op", "闭包块数", "闭包占用", "闭包条数", "实测"))
    print("-" * 58)
    for op in range(19):
        if op not in entry_blks:
            continue
        C = fall_closure(entry_blks[op])
        occ = sum(doc_of(b) for b in C)
        n = sum(bp[b][1] - bp[b][0] for b in C)
        names.append(OPS[op]); S.append(c0_doc + occ); n_instr.append(n); M.append(meas[op])
        print("%-9s %-9d %-8d %-8d %d" % (OPS[op], len(C), occ, n, meas[op]))

    # L1: 闭包不得含别的 op 入口块
    bad = []
    for op in range(19):
        if op not in entry_blks:
            continue
        C = fall_closure(entry_blks[op])
        for b in C:
            if b in other_entry and b != entry_blks[op]:
                bad.append((OPS[op], hex(bp[b][2])))
    print("\nL1 闭包是否串到别的 op: %s"
          % ("**通过**（无）" if not bad else "**未通过**: %s" % bad[:6]))

    rho = spearman(S, M)
    rho_n = spearman(n_instr, M)
    print("\nL2 排序对比:")
    print("    Spearman(直落闭包占用, 实测) = %+.3f" % rho)
    print("    Spearman(闭包条数,     实测) = %+.3f" % rho_n)
    print("    ---- 历史基线 ----")
    print("    STA-17 线性路径条数 = +0.893（口径有 bug, 会串到邻块）")
    print("    入口块整块占用      = +0.583")
    print("    入口块条数          = +0.641")
    best = max(rho, rho_n)
    print("    ⇒ 本口径最好 %.3f ⇒ %s 0.641 基线" % (best, "**优于**" if best > 0.641 else "**未优于**"))

    import statistics
    mn_occ = min(S[i] - c0_doc for i in range(len(S)))
    print("\nL3 结构下限: 尾块 %d + 最小 op 闭包 %d = **%d cyc**（实测下限 56）⇒ 差 %+d"
          % (c0_doc, mn_occ, c0_doc + mn_occ, c0_doc + mn_occ - 56))

    gaps = [M[i] - S[i] for i in range(len(S))]
    print("\nL4 残余缺口: 全正 %d/%d, 中位 %+.0f cyc, 最大 %+d（%s）"
          % (sum(1 for g in gaps if g > 0), len(gaps), statistics.median(gaps),
             max(gaps), names[gaps.index(max(gaps))]))
    print("    前 5 大缺口: %s"
          % ", ".join("%s(%+d)" % (names[i], gaps[i])
                      for i in sorted(range(len(gaps)), key=lambda i: -gaps[i])[:5]))
    print("    ★ 若中位缺口 ≈ 0 且全正比例低 ⇒ 固定开销补齐;")
    print("      若中位仍 ~+30 ⇒ 那块开销**不在扫描体的任何已识别块里** —— 要点名:")
    print("        候选① 分派块本身（`ldrb/subs/cmp/bhi/tbh`）—— 每路由都要过一次分派")
    print("        候选② `read_source` 的间接跳转（每个 op 入口之前）")
    print("        候选③ 每路由的 `_finite_f(src)` / `_finite_f(wb)` 检查")
    return 0


if __name__ == "__main__":
    sys.exit(main())
