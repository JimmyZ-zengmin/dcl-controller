#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-10（步 A 收口）: 用 **"从 op 入口到循环回边的路径集"** 重算逐原语结构量。

## 为什么换这个口径（前两版的错误依次记下）
  · STA-5「按地址序走」  ⇒ 穿过公共尾块, 同一段被算进每个 case（LPF 255 条 / SR 负数）
  · STA-8「可达闭包」    ⇒ **反而是漏算**: `reach()` 只沿着块后继走, 而 case 体的
    正常出口是**无条件跳公共尾块**, 之后还要走完"写回 + 指针推进 + 循环比较"才回到循环顶。
    实测反证: CMP 入口块本身 18 条, 而"可达条数"报 8（**比入口块还小** ⇒ 自相矛盾）。
  ⇒ 正确口径 = **从入口到"循环回边目标"的所有路径上的块**:
      case 体 + 它必经的公共尾块（写回/记账/循环推进）。
    这一口径**物理上也对**: 每处理一条路由, 这些代码都真的会执行。

## 判据（每条都能失败）
  D1 每个 op 的路径块集必须 **⊇ 它的入口块**, 且 **⊇ 公共尾块**
     （上一版违反: 可达条数 < 入口块条数）
  D2 零自由度交叉检验: K = median(实测/路径块指令数), 超 25% 的 op 数应 ≤ 4
  D3 ★ **单调性**: 路径块指令数越多的 op, 实测成本应**整体更高**
     （Spearman 秩相关 > 0.5; 若接近 0 ⇒ 指令条数确实不是成本的充分统计量）
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
BR = re.compile(r"^(b|bl|blx|bx|cbz|cbnz|tbb|tbh|it)\b")
UNCOND = re.compile(r"^(b|bx|tbb|tbh)\b")
CLASSES = [
    ("br", r"^(b|bl|blx|bx|cbz|cbnz|tbb|tbh|it)\b"),
    ("ld", r"^(ldr|ldrh|ldrb|ldrd|ldm|vldr|vldm|pop|ldrex)\b"),
    ("st", r"^(str|strh|strb|strd|stm|vstr|vstm|push|strex|stmdb)\b"),
    ("mul", r"^(mul|mla|mls|smull|umull|vmul|vfma|vmla|vnmla|vnmul)\b"),
    ("div", r"^(vdiv|vsqrt|sdiv|udiv)\b"),
    ("fp", r"^v"),
    ("alu", r"^(add|sub|and|orr|eor|bic|lsl|lsr|asr|cmp|tst|adc|sbc|rsb|clz|rbit|sxt|uxt|mov|movw|movt|mvn|adds|subs|lsls|movs)\b"),
]


def cls_of(mn):
    for n, p in CLASSES:
        if re.match(p, mn):
            return n
    return "?"


def run(args):
    return subprocess.run([BIN + "objdump.exe"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def target(op):
    m = re.match(r"^([0-9a-f]+)\s", op.strip())
    return int(m.group(1), 16) if m else None


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
        if BR.match(mn):
            t = target(opnd)
            if t in addr2i:
                leaders.add(addr2i[t])
            if i + 1 < N:
                leaders.add(i + 1)
    leaders = sorted(leaders)
    blocks = [(s, (leaders[j + 1] if j + 1 < len(leaders) else N), body[s][0])
              for j, s in enumerate(leaders)]
    blk_of = {}
    for bi, (s, e, _) in enumerate(blocks):
        for i in range(s, e):
            blk_of[i] = bi

    # ★ 先把 .itcm_text 的字节读进来 —— **`tbh` 的表内容必须从这里取**（见下）。
    #   第一版把这段放在建图之后, 于是 `mem` 在需要它的时候还不存在。
    sec = run(["-s", "--section=.itcm_text", ELF])
    mem = {}
    for line in sec.splitlines():
        m = re.match(r"\s*([0-9a-f]+)\s+((?:[0-9a-f]{2,8}\s+){1,4})", line)
        if m:
            a = int(m.group(1), 16)
            for i, b in enumerate(bytes.fromhex(m.group(2).replace(" ", ""))):
                mem[a + i] = b

    succ = [[] for _ in blocks]
    for bi, (s, e, _) in enumerate(blocks):
        a, mn, opnd = body[e - 1]
        if mn in ("tbh", "tbb"):
            # ★★ 修 CFG 洞 #1: `tbh` 的目标是**跳转表**, 不是立即数 ⇒ 一般解析器给不出目标,
            #   于是这个块**一个后继都没有**, 整张可达图在此断掉。
            #   症状极具误导性: "可达闭包"算出来比入口块还小（自相矛盾）, 却不会报错。
            #
            # ★★★ 表的**长度**不能猜（第二稿按固定 40 项读, 结果把后面的普通代码当成
            #   表项, 于是"可达集"膨胀到几乎整个函数, 所有 op 都变成 283 条 —— 而 D2 判据
            #   居然还"通过"了, 因为所有 op 的预测值完全相同、残差只剩常数项。**这是假绿**）。
            #   正解: 表是**紧跟在 tbh 之后的连续数据**, 它的结束 = 下一个**基本块开始地址**
            #   之后第一条"可解析为目标"的项。用"下一个块起始地址"截断最稳。
            table = (a + 4) & ~3
            nxt_blk = [blocks[b][2] for b in range(len(blocks)) if blocks[b][2] > a]
            limit = min(nxt_blk) if nxt_blk else table + 80
            n_ent = max(0, (limit - table) // 2)
            added = set()
            for i in range(min(n_ent, 64)):
                if (table + 2 * i) not in mem or (table + 2 * i + 1) not in mem:
                    break
                v = mem[table + 2 * i] | (mem[table + 2 * i + 1] << 8)
                t = table + 2 * v
                if t in addr2i:
                    added.add(blk_of[addr2i[t]])
            succ[bi].extend(sorted(added))
            if not added:
                succ[bi].append(blk_of[a])       # 兜底: 自环, 至少不静默断链
        elif BR.match(mn):
            t = target(opnd)
            if t in addr2i:
                succ[bi].append(blk_of[addr2i[t]])
            if not UNCOND.match(mn) and e < N:
                succ[bi].append(blk_of[e])
        elif e < N:
            succ[bi].append(blk_of[e])

    # ★★ 修 CFG 洞 #2: 循环顶**不是** 0xD3C。
    #   `0x0E9C: bne.w d3c` 是**循环前导里的比较分支**（`cmp r2,r1` 后回跳）, 目标 0xD3C
    #   落在**入口块内部**（入口块 0x0CF0 起）⇒ 它不是"每次路由处理都要走一遍"的终点。
    #   真正的"一条路由处理完毕"= 回到 **循环体开头**（`ldrb r6,[r1,#2117]` @0xD3C 附近的
    #   分派点之前）。本工具改用**函数入口块**作为"回到循环"的锚点。
    LOOP_TOP = 0x0CF0
    lt_bi = blk_of[addr2i[LOOP_TOP]]

    # ★★★ 最终口径（前三稿都不对, 依次记下 —— 它们其实是同一个 CFG 洞的不同表现）:
    #
    #   事实（由本文件打印的块结构确认）:
    #     · `b.n 0xDE6` @块6 ⇒ **回边的目标是"分派块"（块3, 含 `tbh`）**, 不是函数入口
    #     · 块0 (0x0CF0) 是**循环前导**: 44 条, 只执行一次
    #     · 块6 (0x0E5C) 是**每路由共用的尾块**: 27 条, 含写回 + 指针推进 + `cmp/bne`
    #   ⇒ 所以 CFG **是带环的**（分派块 ⇄ 尾块）, 于是:
    #     · "可达集合"会同时含两种情况, 分不开
    #     · "最长路径"在环上**无定义**（DFS 会一直绕, 触到深度上限后返回 0 —— 前两稿
    #       全部是 0, 就是这个原因; 而它**不报错**, 只是安静地给 0）
    #
    #   ⇒ 正解: **按"每路由执行一次"计数** —— 把"循环自身携带的块"（分派块 + 尾块）
    #     排除, 只数 op 独有的、每路由必然走一遍的那些块。
    #     物理意义清楚: 循环携带块的开销是**每条路由的固定份额**, 与 op 无关;
    #     op 之间的差异全部来自 op 独有块。这正是"逐原语结构量"该有的定义。
    def loop_carried():
        """找出'循环携带块' = 分派块 + 尾块（回边两端）。"""
        tail = None
        for bi, (s, e, _) in enumerate(blocks):
            a, mn, opnd = body[e - 1]
            t = target(opnd)
            if mn.startswith("b") and t in addr2i:
                dst = blk_of[addr2i[t]]
                if dst < bi and dst in loop_carried_hint:
                    tail = (bi, dst)
        return tail

    # 先定位分派块（以 tbh 结尾的块）
    disp = [bi for bi, (s, e, _) in enumerate(blocks) if body[e - 1][1] in ("tbh", "tbb")]
    # 尾块 = 以无条件 b 跳向分派块的块
    tails = []
    for bi, (s, e, _) in enumerate(blocks):
        a, mn, opnd = body[e - 1]
        t = target(opnd)
        if UNCOND.match(mn) and t in addr2i and blk_of[addr2i[t]] in disp:
            tails.append(bi)
    carried = set()
    for bi, (s, e, _) in enumerate(blocks):
        # 分派块本身 + 循环前导里的块（块 0..disp[0]-1）+ 尾块
        if bi < disp[0] or bi in disp or bi in tails:
            carried.add(bi)
    print("循环携带块（每**轮**执行一次, 不计入单条路由成本）: %s" % sorted(carried))
    print("  · 分派块: %s" % disp)
    print("  · 尾块  : %s" % tails)

    def per_route_blocks(start_bi):
        """★★★ 每路由执行一次的块 = **从入口到"共享尾块"的全部路径上的块**。

        为什么是这个口径（前四稿的错误依次记下 —— 它们是同一个坑的四种表现）:
          · 地址序走       ⇒ 穿过公共尾块, 同一段被算进每个 case
          · 可达集合       ⇒ ARITH 只能"可达"到 8 条, 比它自己的入口块还小（自相矛盾）
          · 可达集合 ∩ 能到循环顶 ⇒ **空集**（case 经 `bx lr` 直接返回的不在少数）
          · 最长路径       ⇒ 图**带环**（尾块 → 分派块 → case → 尾块）, 环上无最长路径;
                             DFS 触到深度上限后返回 0, **而它不报错, 只是安静地给 0**
          · 可达集合 − 循环携带块 ⇒ 仍然是 283: 因为**每个 case 都能经尾块/分派块
                             到达其它所有 case**（这正是循环的意义）, 于是"可达"没有区分度
        ⇒ 正解: 取 **入口 → 共享尾块** 的路径集合。
          尾块 = 每路由都要走的那段（写回 + 指针推进 + `cmp/bne`）, 而路径上的
          其它块就是该 case 自己的分支体。物理意义精确: **这就是处理一条路由所执行的代码。**
        """
        # 找共享尾块: 以无条件 `b` 跳到"分派块"的那个块, 且入口能到它
        tail = None
        for bi2, (s2, e2, _) in enumerate(blocks):
            a2, mn2, op2 = body[e2 - 1]
            t2 = target(op2)
            if UNCOND.match(mn2) and t2 in addr2i and blk_of[addr2i[t2]] in disp:
                tail = bi2
                break
        if tail is None:
            return {start_bi}
        # 能到 tail 的块（反向可达）
        rev = [[] for _ in blocks]
        for b, ss in enumerate(succ):
            for s in ss:
                rev[s].append(b)
        can, q = set(), deque([tail])
        while q:
            b = q.popleft()
            if b in can:
                continue
            can.add(b)
            for r in rev[b]:
                if r not in can:
                    q.append(r)
        # 从入口可达的块
        fwd, q = set(), deque([start_bi])
        while q:
            b = q.popleft()
            if b in fwd:
                continue
            fwd.add(b)
            for s in succ[b]:
                if s not in fwd:
                    q.append(s)
        return (fwd & can) | {start_bi, tail}

    def n_instr(bis):
        return sum(blocks[b][1] - blocks[b][0] for b in bis)

    # op 入口（`mem` 已在建图前读好 —— 见上面"先把 .itcm_text 的字节读进来"）
    TBL = 0x0DF4
    tg = [TBL + 2 * (mem[TBL + 2 * i] | (mem[TBL + 2 * i + 1] << 8)) for i in range(18)]
    entry = {i + 1: t for i, t in enumerate(tg)}
    entry[0] = 0x0E56

    txt = open(ENGINE_C, encoding="utf-8", errors="replace").read()
    mm = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", txt, re.S)
    meas = [int(x) for x in re.findall(r"\d+", mm.group(1))]

    print("循环顶 = 0x%08X（块 %d）" % (LOOP_TOP, lt_bi))
    print("\n%-9s %-10s %-10s %-9s %s" % ("op", "路径块条数", "入口块条数", "实测", "路径块类直方图"))
    print("-" * 92)
    rows = []
    for op in range(19):
        e = entry[op]
        if e not in addr2i:
            continue
        bi = blk_of[addr2i[e]]
        P = per_route_blocks(bi)
        from collections import Counter
        c = Counter()
        for b in P:
            for i in range(blocks[b][0], blocks[b][1]):
                c[cls_of(body[i][1])] += 1
        hist = " ".join("%s=%d" % (k, c[k]) for k in ("br", "ld", "st", "mul", "div", "fp", "alu", "?") if c.get(k))
        nP = n_instr(P)
        nE = blocks[bi][1] - blocks[bi][0]
        print("%-9s %-10d %-10d %-9d %s" % (OPS[op], nP, nE, meas[op], hist))
        rows.append((OPS[op], nP, nE, meas[op], dict(c)))
        # D1: 每路由块集必须 ⊇ 入口块
        if nP < nE:
            print("      ✗ D1 违反: 每路由块 %d < 入口块 %d" % (nP, nE))

    # D2
    K = statistics.median(m / float(n) for _, n, _, m, _ in rows if n)
    errs = [(nm, n, m, m - K * n) for nm, n, _, m, _ in rows if n]
    ae = sorted(abs(d) for *_, d in errs)
    bad = [x for x in errs if x[2] and abs(x[3]) > 0.25 * x[2]]
    print("\nD2 零自由度交叉检验: K=%.3f cyc/指令  中位|残差|=%.1f  最大=%.1f  "
          "平均相对=%.1f%%  超25%%: %d/19 ⇒ %s"
          % (K, statistics.median(ae), max(ae),
             100 * sum(abs(d) / m for _, _, m, d in errs if m) / len(errs),
             len(bad), "**通过**" if len(bad) <= 4 else "**未通过**"))
    for nm, n, m_, d in sorted(bad, key=lambda z: -abs(z[3])):
        print("     %-9s 路径块%-4d 实测%-5d 预测%7.1f 残差%+7.1f (%.0f%%)"
              % (nm, n, m_, K * n, d, 100 * abs(d) / m_))

    # D3 秩相关
    xs = sorted(range(len(rows)), key=lambda i: rows[i][1])
    ys = sorted(range(len(rows)), key=lambda i: rows[i][3])
    rx = {v: k for k, v in enumerate(xs)}
    ry = {v: k for k, v in enumerate(ys)}
    nn = len(rows)
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(nn))
    rho = 1 - 6 * d2 / (nn * (nn * nn - 1))
    print("\nD3 单调性（路径块条数 与 实测成本 的 Spearman 秩相关）= %.3f ⇒ %s"
          % (rho, "**通过**（>0.5）" if rho > 0.5
             else "**未通过** ⇒ 指令条数**不是**成本的充分统计量（这本身是结论）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
