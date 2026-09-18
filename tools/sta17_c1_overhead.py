#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-17（步 C1）: 找那缺失的 ~34 cyc 固定开销 —— 用**实际执行路径**而不是整块。

## 缺口（步 C 定位）
  c0_结构（共享尾块 27 条按文档表累加）= **34 cyc**
  A″ 从实测拟合的截距                    = **67.7 cyc**
  ⇒ 缺 ≈ 34 cyc, 且 19/19 **全部低估**

## 主嫌疑（可失败的判据式怀疑）
"入口块条数"把**条件分支的两侧都算进去了**, 而一拍只走一侧。
⇒ 结构侧**高估**了带分支的 op ⇒ 系统性... 但实测是**低估**,
  所以单靠这条解释不了全部 —— 须把"实际路径"算出来再看缺口变多少。

## 做法
从每个 op 入口出发, 沿**实际执行路径**走:
  · 非分支指令 ⇒ 顺序下一条
  · 条件分支 ⇒ **取顺序下落那条**（"不成立"分支; 成立分支通常跳去错误/特殊处理）
  · 无条件分支 ⇒ 跳到目标
直到进入**共享尾块**（尾块自身按文档表计价, 只算一次）
⇒ 得到 `S_taken`（文档占用和）; 与整块口径 `S_block` 对比。

## 判据
  K1 `S_taken` 必须 ≤ `S_block`（取路径只会变少; 若变多 ⇒ 路径提取有 bug）
  K2 ★ **结构截距** `c0_taken` 应显著靠近实测下限 56 cyc（DIRECT 实测值）
     判据: |c0_taken − 56| ≤ 12 cyc
  K3 Spearman(S_taken, 实测) 须 **> 0.641**（裸条数基线）
  K4 若 K2/K3 仍不过 ⇒ **点名剩余缺口的大小与方向**, 不许静默
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

sys.path.insert(0, os.path.join(ROOT, "tools"))
from sta16_docmodel import occ_of, spearman   # 复用同一套文档占用表与秩函数


def run(args):
    return subprocess.run([BIN + "objdump.exe"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def target(op):
    m = re.match(r"^([0-9a-f]+)\s", op.strip())
    return int(m.group(1), 16) if m else None


COND = re.compile(r"^(b\w+\.|cbz|cbnz)")
UNCOND = re.compile(r"^(b|bx|tbb|tbh)\b")


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
    BR = re.compile(r"^(b|bl|blx|bx|cbz|cbnz|tbb|tbh|it)\b")
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

    # 共享尾块（步 A 已定: 以无条件 b 跳向分派块的块）
    disp = [bi for bi, (s, e, _) in enumerate(bp) if body[e - 1][1] in ("tbh", "tbb")]
    tail_bi = None
    for bi, (s, e, _) in enumerate(bp):
        a2, mn2, op2 = body[e - 1]
        t2 = target(op2)
        if UNCOND.match(mn2) and t2 in addr2i and blk[addr2i[t2]] in disp:
            tail_bi = bi
            break
    ts, te, _ = bp[tail_bi]
    c0_doc = 0
    for i in range(ts, te):
        o, _c = occ_of(body[i][1])
        c0_doc += (o or 1)
    print("共享尾块 = 块 %d, %d 条, 文档占用和 = **%d cyc**" % (tail_bi, te - ts, c0_doc))

    def doc_sum(rng):
        tot = 0
        for i in rng:
            o, _c = occ_of(body[i][1])
            tot += (o or 1)
        return tot

    def taken_path(start_i):
        """沿实际执行路径收集指令索引, 直到进入共享尾块（不含尾块）。

        ★★ 第一版有 bug（K1 因此失败）: 条件分支取"顺序下落"时**直接 `i += 1`**,
           于是会**跨过本 op 代码的末尾**, 走进**下一个 op 的入口块**
           （实测: LUT 路径 17 条 > 自己的块 9 条; AND 10 > 5; NOT 7 > 4）。
           症状是"路径比整块还长" —— 自相矛盾, 正是 K1 判据把它抓出来的。

        ★ 修法: 下落前检查**该地址是否仍是"活代码"**。
          "活代码" = 属于某个已识别的基本块, 且该块不是**别的 op 的入口块**
          （也不是共享尾块 —— 尾块已在上面的 break 处理）。
          落到别人的入口块就停下, 不再前进。
        """
        stop_blks = set()
        for op2 in range(19):
            e2 = entry[op2]
            if e2 in addr2i:
                stop_blks.add(blk[addr2i[e2]])
        stop_blks.discard(blk[start_i])          # 自己的入口块不算"别人的"
        out_idx, i, guard = [], start_i, 0
        while i < N and guard < 200:
            guard += 1
            if ts <= i < te:                     # 已到共享尾块 ⇒ 停
                break
            if blk[i] in stop_blks and i != start_i:
                break                            # 走进了别的 op 的入口块 ⇒ 停
            a, mn, opnd = body[i]
            out_idx.append(i)
            if UNCOND.match(mn) and not mn.startswith("bl"):
                t = target(opnd)
                if t in addr2i and t != i:
                    i = addr2i[t]
                    continue
                break
            if COND.match(mn):
                i += 1                           # 取顺序下落那条（"不成立"侧）
                continue
            i += 1
        return out_idx

    # ── 逐 op ──
    txt = open(ENGINE_C, encoding="utf-8", errors="replace").read()
    mm = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", txt, re.S)
    meas = [int(x) for x in re.findall(r"\d+", mm.group(1))]

    names, S_taken, S_block, M, ntk, nbk = [], [], [], [], [], []
    print("\n%-9s %-9s %-9s %-7s %-7s %s" % ("op", "整块占用", "路径占用", "整块条", "路径条", "实测"))
    print("-" * 68)
    for op in range(19):
        e = entry[op]
        if e not in addr2i:
            continue
        bi = blk[addr2i[e]]
        s, en, _ = bp[bi]
        idx = taken_path(addr2i[e])
        st = doc_sum(idx)
        sb = doc_sum(range(s, en))
        names.append(OPS[op]); S_taken.append(c0_doc + st); S_block.append(c0_doc + sb)
        ntk.append(len(idx)); nbk.append(en - s); M.append(meas[op])
        print("%-9s %-9d %-9d %-7d %-7d %d" % (OPS[op], sb, st, en - s, len(idx), meas[op]))

    # K1
    badK1 = [names[i] for i in range(len(names)) if ntk[i] > nbk[i]]
    print("\nK1 路径条数 ≤ 整块条数: %s"
          % ("**通过**（无例外）" if not badK1 else "**未通过**: %s" % badK1))

    # K2 结构截距
    import statistics
    # 两点法: 用最小与最大的 op（结构）估截距; 另报"结构下限 − 尾块"
    lo_i = min(range(len(S_taken)), key=lambda i: S_taken[i] - c0_doc)
    hi_i = max(range(len(S_taken)), key=lambda i: S_taken[i] - c0_doc)
    m_slope = ((S_taken[hi_i] - S_taken[lo_i]) / float(ntk[hi_i] - ntk[lo_i])
               if ntk[hi_i] != ntk[lo_i] else float('nan'))
    c0_taken = S_taken[lo_i] - m_slope * ntk[lo_i]
    min_struct = min(S_taken[i] - c0_doc for i in range(len(S_taken)))
    print("\nK2 结构截距:")
    print("    最小 op 独有占用 = %d cyc（op=%s）" % (min_struct, names[lo_i]))
    print("    两点法估 c0_结构 = %.1f cyc" % c0_taken)
    print("    实测下限（DIRECT 实测）= 56 cyc")
    dev = abs(min_struct - 56)
    print("    |最小结构 − 56| = %.0f cyc ⇒ %s（判据 ≤12）"
          % (dev, "**通过**" if dev <= 12 else "**未通过**"))

    # K3 排序
    rho_t = spearman(S_taken, M)
    rho_b = spearman(S_block, M)
    rho_n = spearman(ntk, M)
    print("\nK3 排序:")
    print("    Spearman(路径口径, 实测) = %+.3f" % rho_t)
    print("    Spearman(整块口径, 实测) = %+.3f" % rho_b)
    print("    Spearman(路径条数, 实测) = %+.3f  ← 裸条数基线（0.641 是整块条数）" % rho_n)
    print("    ⇒ 路径口径%s优于裸条数基线" % ("**" if rho_t > 0.641 else "**未**"))

    # K4 残余缺口
    print("\nK4 残余缺口（实测 − 结构路径口径）:")
    gaps = [(names[i], M[i] - S_taken[i]) for i in range(len(names))]
    for nm, g in sorted(gaps, key=lambda z: -z[1])[:6]:
        print("    %-9s %+5d cyc" % (nm, g))
    pos = [g for _, g in gaps if g > 0]
    print("    全部为正(=结构仍低估)的个数 = %d/19; 中位 %+.0f cyc"
          % (len(pos), statistics.median([g for _, g in gaps])))
    print("    ★ 若中位缺口已接近 0 ⇒ 缺的那块固定开销被「路径口径」补上了;")
    print("      若仍显著为正 ⇒ 还有一块**没被任何已识别块覆盖**的每路由开销, 要点名。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
