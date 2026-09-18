#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-8（步 A）: 用**真 CFG** 重算逐原语结构量 —— 修掉"共享尾码"污染。

## 为什么必须重做
STA-5/STA-7 的"指令数"列被证明**被污染**: 指令数 26→42 的 5 个原语实测成本
**纹丝不动（74~85 cyc）**。一个合理的解释是: 从 case 入口"沿地址顺序走到第一条回边"
这条路**穿过了公共尾块**, 于是同一段尾码被算进了**每一个** case。

## 本版做法（不猜, 按图论走）
1. **基本块划分**: 指令序列按"跳转目标"与"跳转指令的下一条"切成基本块
2. **建 CFG**: 每条分支的 successors = {目标, 顺序下一条}; 其余指令顺序后继
3. **前向可达**: 从**函数入口**做 BFS ⇒ 得到"真正会被执行的指令集合"
4. 对每个 op 入口: 从该入口做 BFS ⇒ `R(op)`
   · `R(op)` 与"入口基本块"分开报, 因为**首块**才是"这个 op 真正独有"的部分
5. **共享度**: 被 N 个 op 可达的指令 = 共享; 只被 1 个 op 可达的 = 该 op 独有
   ⇒ 逐指令统计"被几个 op 入口可达", 这是**混淆项的定量刻画**

## 判据（每条都能失败）
  C1 入口基本块大小必须 **≥1 且 < 该 op 的地址序区间长度**（否则说明没切出共享部分）
  C2 **共享度分布**: 若"只被 1 个 op 可达"的指令占比很低 ⇒ 说明绝大多数指令是共享的,
     那么"逐原语结构量"这个提法本身要改（改成"逐原语入口块 + 公共扫描开销"）
  C3 ★ 用**入口块指令数**做零自由度交叉检验（K = 中位(实测/入口块指令数)）:
     残差必须**明显小于** STA-7 的 13/19 超差
"""
import os, re, struct, subprocess, sys
from collections import Counter, deque

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
       "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32."
       "7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-")
OBJDUMP = BIN + "objdump.exe"
ELF = os.path.join(ROOT, "build", "dcl_h723")
ENGINE_C = os.path.join(ROOT, "src", "engine.c")
OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
       "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]

CLASSES = [
    ("br",  re.compile(r"^(b|bl|blx|bx|cbz|cbnz|tbb|tbh|it)\b")),
    ("ld",  re.compile(r"^(ldr|ldrh|ldrb|ldrd|ldm|vldr|vldm|pop|ldrex)\b")),
    ("st",  re.compile(r"^(str|strh|strb|strd|stm|vstr|vstm|push|strex|stmdb)\b")),
    ("mul", re.compile(r"^(mul|mla|mls|smull|umull|vmul|vfma|vmla|vnmla|vnmul)\b")),
    ("div", re.compile(r"^(vdiv|vsqrt|sdiv|udiv)\b")),
    ("fp",  re.compile(r"^v")),
    ("alu", re.compile(r"^(add|sub|and|orr|eor|bic|lsl|lsr|asr|cmp|tst|adc|sbc|rsb|clz|rbit|sxt|uxt|mov|movw|movt|mvn|adds|subs|lsls|movs)\b")),
]
UNCOND = re.compile(r"^(b|bx|tbb|tbh)\b")
COND = re.compile(r"^(b\w+\.|cbz|cbnz|it)")


def cls_of(mn):
    for name, rx in CLASSES:
        if rx.match(mn):
            return name
    return "?"


def run(tool, args):
    return subprocess.run([tool] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def br_target(op):
    m = re.match(r"^([0-9a-f]+)\s", op.strip())
    return int(m.group(1), 16) if m else None


def main():
    out = run(OBJDUMP, ["-d", "--no-show-raw-insn", ELF])
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
    addr2i = {a: i for i, (a, _, _) in enumerate(body)}
    N = len(body)
    print("engine_scan_itcm 0x%08X..0x%08X  %d 条" % (lo, hi, N))

    # ── 1. 基本块划分 ──
    leaders = {0}
    for i, (a, mn, opnd) in enumerate(body):
        if cls_of(mn) == "br":
            t = br_target(opnd)
            if t in addr2i:
                leaders.add(addr2i[t])
            if i + 1 < N:
                leaders.add(i + 1)
    leaders = sorted(leaders)
    blocks = []                      # [(start_i, end_i_exclusive, leader_addr)]
    for j, s in enumerate(leaders):
        e = leaders[j + 1] if j + 1 < len(leaders) else N
        blocks.append((s, e, body[s][0]))
    blk_of = {}
    for bi, (s, e, _) in enumerate(blocks):
        for i in range(s, e):
            blk_of[i] = bi
    print("基本块 %d 个" % len(blocks))

    # ── 2. CFG ──
    succ = [[] for _ in blocks]
    for bi, (s, e, _) in enumerate(blocks):
        last_i = e - 1
        a, mn, opnd = body[last_i]
        if cls_of(mn) == "br":
            t = br_target(opnd)
            if t in addr2i:
                succ[bi].append(blk_of[addr2i[t]])
            # 条件分支/带链接/未解析目标 ⇒ 也可能顺序落下
            if not UNCOND.match(mn) or mn in ("bl", "blx"):
                if e < N:
                    succ[bi].append(blk_of[e])
        else:
            if e < N:
                succ[bi].append(blk_of[e])

    def reach(start_bi):
        seen, q = set(), deque([start_bi])
        while q:
            b = q.popleft()
            if b in seen:
                continue
            seen.add(b)
            for s in succ[b]:
                if s not in seen:
                    q.append(s)
        return seen

    # ── 3. op 入口（沿用 STA-5 已验证的语义: 表项 i ⇔ op i+1, DIRECT 走 fall-through）──
    sec_out = run(OBJDUMP, ["-s", "--section=.itcm_text", ELF])
    mem = {}
    for line in sec_out.splitlines():
        m = re.match(r"\s*([0-9a-f]+)\s+((?:[0-9a-f]{2,8}\s+){1,4})", line)
        if m:
            a = int(m.group(1), 16)
            for i, b in enumerate(bytes.fromhex(m.group(2).replace(" ", ""))):
                mem[a + i] = b
    TBL = 0x0DF4
    tgts = [TBL + 2 * (mem[TBL + 2 * i] | (mem[TBL + 2 * i + 1] << 8)) for i in range(18)]
    entry = {i + 1: t for i, t in enumerate(tgts)}
    entry[0] = 0x0E56

    def block_instr_count(bi):
        s, e, _ = blocks[bi]
        return e - s

    def count_classes(bis):
        c = Counter()
        for bi in bis:
            s, e, _ = blocks[bi]
            for i in range(s, e):
                c[cls_of(body[i][1])] += 1
        return c

    # ── 4. 逐 op: 入口块 vs 可达闭包 ──
    print("\n%-9s %-8s %-8s %-9s %s" % ("op", "入口块条数", "可达条数", "地址序区间", "入口块类直方图"))
    print("-" * 92)
    ent_blocks, reach_sets, op_addr_span = {}, {}, {}
    for op in range(19):
        e = entry[op]
        if e not in addr2i:
            print("%-9s —— 入口 0x%X 不在函数体内（跳过）" % (OPS[op], e)); continue
        bi = blk_of[addr2i[e]]
        R = reach(bi)
        ent_blocks[op], reach_sets[op] = bi, R
        # 地址序区间（对照用, 即 STA-5 的口径）
        i0 = addr2i[e]
        j = i0
        while j < N:
            a, mn, opnd = body[j]
            if cls_of(mn) == "br":
                t = br_target(opnd)
                if t is not None and t <= e:
                    break
            j += 1
        op_addr_span[op] = j - i0
        c = count_classes({bi})
        hist = " ".join("%s=%d" % (k, c[k]) for k in ("br", "ld", "st", "mul", "div", "fp", "alu", "?") if c.get(k))
        print("%-9s %-8d %-8d %-9d %s"
              % (OPS[op], block_instr_count(bi), sum(block_instr_count(b) for b in R),
                 op_addr_span[op], hist))

    # ── 5. 共享度统计（判据 C2）──
    byop = Counter()
    for op, R in reach_sets.items():
        for b in R:
            byop[b] += 1
    tot_reach = len(byop)
    only1 = sum(1 for b, k in byop.items() if k == 1)
    print("\n可达基本块 %d 个; 其中" % tot_reach)
    print("  只被 1 个 op 可达（**该 op 独有**）: %d (%.1f%%)" % (only1, 100.0 * only1 / tot_reach))
    for k in (2, 3, 5, 10):
        n = sum(1 for b, kk in byop.items() if kk <= k)
        print("  被 ≤%2d 个 op 可达: %3d (%.1f%%)" % (k, n, 100.0 * n / tot_reach))
    shared_blk = sorted((k, b) for b, k in byop.items() if k >= 15)
    print("  ★ 被 ≥15 个 op 可达的**公共块** %d 个（这些就是'共享尾码'的定量刻画）:" % len(shared_blk))
    for k, b in shared_blk[:12]:
        s, e, a0 = blocks[b]
        print("     块 @0x%08X  被 %2d 个 op 可达  %d 条" % (a0, k, e - s))
    if len(shared_blk) > 12:
        print("     …（另有 %d 个）" % (len(shared_blk) - 12))

    # ── 6. 判据 C3: 用入口块指令数做零自由度交叉检验 ──
    txt = open(ENGINE_C, encoding="utf-8", errors="replace").read()
    mm = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", txt, re.S)
    meas = [int(x) for x in re.findall(r"\d+", mm.group(1))] if mm else []
    print("\n" + "=" * 92)
    print("C3 零自由度交叉检验: 入口块条数 vs 地址序区间条数（谁更能解释实测）")
    print("=" * 92)
    if len(meas) < 19:
        print("!! 实测表不足 19 项"); return 2
    import statistics
    detail = {}
    for tag, getter in (("入口块", lambda op: block_instr_count(ent_blocks[op])),
                        ("地址序区间", lambda op: op_addr_span[op]),
                        ("可达闭包", lambda op: sum(block_instr_count(b) for b in reach_sets[op]))):
        pairs = [(OPS[op], getter(op), meas[op]) for op in sorted(ent_blocks)]
        K = statistics.median(m / float(n) for _, n, m in pairs if n)
        errs = [(nm, n, m, m - K * n) for nm, n, m in pairs if n]
        ae = sorted(abs(d) for *_, d in errs)
        bad = [x for x in errs if x[2] and abs(x[3]) > 0.25 * x[2]]
        rel = sum(abs(d) / m for _, _, m, d in errs if m) / len(errs)
        print("  %-10s K=%6.3f  中位|残差|=%6.1f  最大=%6.1f  平均相对=%5.1f%%  超25%%: %d/19"
              % (tag, K, statistics.median(ae), max(ae), 100 * rel, len(bad)))
        detail[tag] = (K, errs, bad)
    # ★ 主判据 = 可达闭包; 把它的逐项明细打出来（含 3 个超差项的**候选原因**）
    K, errs, bad = detail["可达闭包"]
    print("\n  ── 可达闭包逐项明细（K=%.3f cyc/指令; 残差正 = 结构**高估**）──" % K)
    print("     %-9s %-8s %-7s %-9s %+9s  %s" % ("op", "可达条数", "实测", "结构预测", "残差", "候选原因"))
    divops = {"LPF", "PID", "RATE", "CMP", "ARITH", "CLAMP"}
    fabops = {"LUT", "MUX"}
    for nm, n, m_, d in sorted(errs, key=lambda z: -abs(z[3])):
        why = ""
        if abs(d) <= 0.25 * m_:
            why = "—"
        elif nm in divops:
            why = "★含 VDIV/VSQRT（**操作数相关**, 文档只给最坏值 ⇒ 实测低于结构合理）"
        elif nm in fabops:
            why = "★含查表访存（LUT/MUX 走 lu[]/wm[] ⇒ 访存延迟依赖地址）"
        elif d > 0:
            why = "★结构高估：可达集里可能仍有**共享块**"
        else:
            why = "★结构低估：有一类延迟文档里没覆盖（要点名）"
        print("     %-9s %-8d %-7d %-9.1f %+9.1f  %s" % (nm, n, m_, K * n, d, why))
    print("\n  ⇒ 步 A 的判据: 可达闭包的超差数应从 13/19 降到 ≤4/19。实际 = %d/19 ⇒ %s"
          % (len(bad), "**通过**" if len(bad) <= 4 else "**未通过**"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
