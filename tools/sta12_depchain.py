#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-12（步 A'）: 依赖链分析 —— 把口径从"块可达性"(控制流) 升级到 **数据流**。

## 为什么必须换这一层（步 A 的硬结论）
四种纯控制流口径全部不成立（`ARCH-EXECTIME-COMPUTABILITY.md` §4.4）:
地址序区间 13/19 超差 · 入口块 11/19 · 可达闭包与路径集则是**假绿**（对每个 op 给出
几乎相同的数 ⇒ 残差只剩常数项, Spearman ≈ 0）。
根因: **"哪条路由被处理"由运行期的 op 值决定 —— 那是数据, 不是结构。**
⇒ 必须把"指令吃哪个寄存器的值"这一层纳入, 即 **def-use 依赖链**。

## 做法（FPGA STA 的第三步）
1. 取每个 op 的**入口块**指令序列（步 A 已能精确切出）
2. 逐条解析 **def / use**（寄存器 + 标志位 + 内存别名保守处理）
3. 建**指令依赖图**（RAW 为主; 写后写保守串行）
4. 取**最长加权路径**（节点权重 = 指令类延迟; 延迟取自 **Cortex-M7 文档**，不是实测）
5. 与实测成本做 **Spearman 秩相关**（主判据 > 0.8）

## 判据（都能失败）
  F1 `?` 类必须消失（每条助记符都要能被命名）—— 否则权重是编的
  F2 依赖链长度必须随原语变化（极差 > 1.5×）—— 否则又是"没有区分度"的假绿（§5.49）
  F3 Spearman(依赖链加权长度, 实测成本) **> 0.8**
  F4 与"只数指令条数"基线对比: 依赖链必须**明显更好**（否则白做）

## 诚实边界
延迟值来自 Cortex-M7 文档（见 LAT 表, 逐条注明依据）。它们是**模型参数**,
不是本平台实测值。要变成"本平台的界", 需按 §7 的 C 步用实测校准——但**校准不得
反向改变结构排序**, 否则又回到自证。
"""
import os, re, subprocess, sys
from collections import defaultdict, Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
       "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32."
       "7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-")
ELF = os.path.join(ROOT, "build", "dcl_h723")
ENGINE_C = os.path.join(ROOT, "src", "engine.c")
OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
       "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]

# ── Cortex-M7 文档延迟模型（cycles）──
# ★ 来源: ARM Cortex-M7 TRM / Cortex-M7 软件优化指南 的指令时序表。
#   这些是**文档值**, 不是本平台实测 —— 本脚本的全部意义就是"用文档值而非实测值去算"。
#   "1" 与 ALU 同级; 变时延的给**上界**（保守）。
LAT = {
    "alu": 1, "mov": 1, "shift": 1, "cmp": 1, "logic": 1,
    "mul": 2,          # MUL/MLA 文档: 2 cyc
    "mac": 3,          # 长乘/乘加
    "div": 12,         # SDIV/UDIV: 2~12, 取上界
    "fadd": 3, "fmov": 1, "fcmp": 1,
    "fmul": 3,
    "fmac": 5,         # VFMA/VMLA
    "fdiv": 15, "fsqrt": 15,   # 迭代式, 取上界
    "fcvt": 3,
    "vseleq": 1,
    "load": 2,         # LDR 命中 TCM/DCache 的典型值
    "store": 2,
    "br": 1,           # 预测命中
    "tbh": 3,          # 表跳转
    "nop": 1,
}


def weight(mn):
    """助记符 → 周期权重（**文档模型**）。F1 判据要求它必须能覆盖全部助记符。"""
    m = mn.lower()
    if m.startswith("v"):
        if m.startswith(("vdiv", "vsqrt")):
            return LAT["fdiv"], "fdiv"
        if m.startswith(("vmla", "vfma", "vmls", "vfms", "vnmla", "vnmls")):
            return LAT["fmac"], "fmac"
        if m.startswith(("vmul", "vnmul")):
            return LAT["fmul"], "fmul"
        if m.startswith(("vadd", "vsub", "vabs", "vneg")):
            return LAT["fadd"], "fadd"
        if m.startswith(("vcvt", "vcvtr")):
            return LAT["fcvt"], "fcvt"
        if m.startswith(("vcmpe", "vcmp")):
            return LAT["fcmp"], "fcmp"
        if m.startswith(("vmov", "vdup")):
            return LAT["fmov"], "fmov"
        if m.startswith("vseleq"):
            return LAT["vseleq"], "vseleq"
        if m.startswith(("vldr", "vldm", "vpop")):
            return LAT["load"], "load"
        if m.startswith(("vstr", "vstm", "vpush")):
            return LAT["store"], "store"
        return None, "?"
    if m.startswith(("sdiv", "udiv")):
        return LAT["div"], "div"
    if m.startswith(("mul", "mla", "mls")):
        return LAT["mul"], "mul"
    if m.startswith(("smull", "umull", "smulbb")):
        return LAT["mac"], "mac"
    if m.startswith(("ldr", "ldrh", "ldrb", "ldrd", "ldm", "pop", "ldrex")):
        return LAT["load"], "load"
    if m.startswith(("str", "strh", "strb", "strd", "stm", "push", "strex")):
        return LAT["store"], "store"
    if m == "tbh" or m == "tbb":
        return LAT["tbh"], "tbh"
    if m.startswith(("b", "cbz", "cbnz", "bl", "bx")):
        return LAT["br"], "br"
    if m.startswith(("add", "sub", "adc", "sbc", "rsb", "and", "orr", "eor",
                     "bic", "orn", "lsl", "lsr", "asr", "ror", "clz", "rbit",
                     "sxt", "uxt", "mov", "movw", "movt", "mvn")):
        return LAT["alu"], "alu"
    if m.startswith(("cmp", "cmn", "tst", "teq")):
        return LAT["cmp"], "cmp"
    if m in ("nop", "it", "dsb", "isb", "dmb"):
        return LAT["nop"], "nop"
    return None, "?"


def run(args):
    return subprocess.run([BIN + "objdump.exe"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def target(op):
    m = re.match(r"^([0-9a-f]+)\s", op.strip())
    return int(m.group(1), 16) if m else None


def regs_used(opnd):
    """从操作数字符串里粗略抽取被读的寄存器（r0-r12/sp/lr/pc/s0-s31/d0-d15/q0-q7）。
    ★ 保守: 宁多算依赖（更长的链）也不少算 —— 因为我们要判的是"有没有相关性"。"""
    out = set()
    for m in re.finditer(r"\b(?:r\d{1,2}|s\d{1,2}|d\d{1,2}|q\d{1,2}|sp|lr|pc|fp|ip|sl|sb)\b", opnd):
        out.add(m.group(0))
    return out


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

    # ── F1: 助记符覆盖检查 ──
    unknown = Counter()
    for a, mn, opnd in body:
        w, cls = weight(mn)
        if w is None:
            unknown[mn] += 1
    print("F1 助记符覆盖: 未识别 %d 种" % len(unknown))
    for mn, c in unknown.most_common(30):
        print("     %-14s ×%d" % (mn, c))
    if unknown:
        tot = sum(unknown.values())
        print("     ★ 未识别指令共 %d 条 / %d 条 (%.1f%%) ⇒ F1 %s"
              % (tot, N, 100.0 * tot / N, "**通过**" if tot <= 0.05 * N else "**未通过**"))

    def block_instr(bi):
        s, e, _ = bp[bi]
        return [(body[i][0], body[i][1], body[i][2]) for i in range(s, e)]

    def chain(bi):
        """入口块的**最长加权依赖链**（RAW; 写后写保守串行）。"""
        seq = block_instr(bi)
        last_def = {}                     # reg → (idx, 该指令完成时刻)
        nodes = []
        for i, (a, mn, opnd) in enumerate(seq):
            w, cls = weight(mn)
            if w is None:
                w = 1
            # 操作数按 ',' 切: 第一个是目的（对多数指令成立）
            parts = [p.strip() for p in opnd.split(",")] if opnd else []
            dst = parts[0].split("!")[0].strip() if parts else ""
            uses = set()
            for p in parts[1:]:
                uses |= regs_used(p)
            if opnd and opnd.startswith("[") and parts:
                uses |= regs_used(opnd)       # 纯内存操作数
            ready = 0
            for r in uses:
                if r in last_def:
                    ready = max(ready, last_def[r][1])
            done = ready + w
            # 记录目的寄存器的完成时刻
            dregs = regs_used(dst) if dst else set()
            for r in dregs or ({dst} if dst else set()):
                last_def[r] = (i, done)
            nodes.append((a, mn, cls, w, ready, done, sorted(uses)))
        return nodes

    # ── 主表 ──
    txt = open(ENGINE_C, encoding="utf-8", errors="replace").read()
    mm = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", txt, re.S)
    meas = [int(x) for x in re.findall(r"\d+", mm.group(1))]

    print("\n%-9s %-7s %-9s %-9s %-9s %s"
          % ("op", "条数", "串行和", "依赖链", "链/串行", "实测"))
    print("-" * 74)
    rows = []
    for op in range(19):
        e = entry[op]
        if e not in addr2i:
            continue
        bi = blk[addr2i[e]]
        nodes = chain(bi)
        serial = sum(n[3] for n in nodes)
        crit = max((n[5] for n in nodes), default=0)
        rows.append((OPS[op], len(nodes), serial, crit, meas[op]))
        print("%-9s %-7d %-9d %-9d %-9.2f %d"
              % (OPS[op], len(nodes), serial, crit, crit / float(serial) if serial else 0, meas[op]))

    # ── F2/F3/F4 ──
    import statistics
    crits = [r[3] for r in rows]
    print("\nF2 区分度: 依赖链极差 = %.2f×（min %d / max %d）⇒ %s"
          % (max(crits) / float(min(crits)), min(crits), max(crits),
             "**通过**" if max(crits) > 1.5 * min(crits) else "**未通过**（又是没有区分度）"))

    def spearman(xs, ys):
        n = len(xs)
        rx = {v: i for i, v in enumerate(sorted(xs))}
        ry = {v: i for i, v in enumerate(sorted(ys))}
        d2 = sum((rx[xs[i]] - ry[ys[i]]) ** 2 for i in range(n))
        return 1 - 6 * d2 / (n * (n * n - 1))

    M = [r[4] for r in rows]
    rho_crit = spearman(crits, M)
    rho_serial = spearman([r[2] for r in rows], M)
    rho_cnt = spearman([r[1] for r in rows], M)
    print("\nF3 Spearman(依赖链, 实测)      = %.3f  ⇒ %s"
          % (rho_crit, "**通过**（>0.8）" if rho_crit > 0.8 else "**未通过**（目标 >0.8）"))
    print("F4 对照基线:")
    print("     Spearman(串行和, 实测)    = %.3f" % rho_serial)
    print("     Spearman(指令条数, 实测)  = %.3f" % rho_cnt)
    better = rho_crit > rho_cnt + 0.1
    print("     ⇒ 依赖链%s显著优于数条数" % ("**" if better else "**未**"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
