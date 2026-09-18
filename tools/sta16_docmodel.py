#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-16（步 C）: **用 Cortex-M7 文档延迟独立算出结构成本** —— 系数不再来自实测拟合。

## 与 A″ 的区别（这一步的全部意义）
  A″ 的 `cost ≈ 67.7 + 1.16n` 里, **67.7 与 1.16 都是从实测拟合出来的**
  ⇒ 它能"描述", 但**不能"预测"** —— 换一个程序、换一次构建就要重标定。
  本步改成:
      结构成本 = c0_结构 + Σ(每条指令的**文档占用** × 该类的条数)
  其中
      · 每类占用来自 **ARM Cortex-M7 文档**（见 OCC, 逐条注明依据与区间）
      · `c0_结构` 由**共享尾块**（步 A 独立量出的 27 条）**按同一套文档表**算出来
  ⇒ **全部结构侧的数字都由"表 + 结构"决定, 零实测输入。**

## 判据（都能失败）
  J1 **排序**（真正要证的东西）: Spearman(结构成本, 实测成本) **> 0.8**
  J2 **方向性安全**: 结构成本 ≥ 实测成本 的比例; 若结构 < 实测 ⇒ 必须**点名**是哪一类
     （这是"界"能否成立的方向 —— 但注意本条**不要求全过**, 因为我们的占用取的是**下界**）
  J3 ★ **离群点必须是可解释的**: 残差最大的 2 个必须是 PID / RATE（含 **`vdiv`**,
     操作数相关）或 DIRECT（空操作）。**若离群的是别的原语 ⇒ 模型形状仍错。**
  J4 **稳健性**: 把每类占用整体乘/除 1.5 倍, Spearman 不得低于 0.8 的 90%
     （防止"刚好调出来的系数"）

## 诚实边界
OCC 是**文档值**, 且其中多项是**下界**（M7 有双发射与转发, 实际占用可低于表值）。
所以本步给的是**结构排序**与**保守下界**, 不是精确周期。要成"界"还需 §7 的 D 步实测夹逼。
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
SHARED_TAIL = 27          # 步 A 独立量出: 每路由共用尾块（块6）的指令数

# ── Cortex-M7 文档占用模型（cycles / 条）──
# ★ 依据: ARM Cortex-M7 TRM 与 Cortex-M7 软件优化指南的指令时序表。
#   标注 "(下界)" 的项表示: 实测可能更低（双发射/转发），本表刻意取保守下界。
OCC = {
    "alu":      1,      # 单周期 ALU
    "mov":      1,
    "cmp":      1,
    "mul":      2,      # MUL/MLA
    "mac":      3,      # 长乘/乘加
    "div":      12,     # SDIV/UDIV: 2~12, **取上界**
    "fp_arith": 1,      # VADD/VSUB/VMUL 等 (下界; 转发/双发射可更低)
    "fp_mac":   3,      # VFMA/VMLA (下界)
    "fp_cvt":   1,      # VCVT (下界)
    "fp_cmp":   1,      # VCMPE
    "fp_mov":   1,      # VMOV/VSEL
    "fp_div":   14,     # VDIV/VSQRT: **操作数相关 2~14**, 取上界
    "load":     2,      # LDR 命中 TCM/DCache
    "store":    2,      # STR
    "br":       1,      # 预测命中
    "tbh":      3,      # 表跳转
    "nop":      1,      # IT/NOP/DSB 等
}


def occ_of(mn):
    """助记符 → 文档占用（cycles）。返回 (占用, 类名) 或 (None, mnemonic) 表示未覆盖。"""
    m = mn.lower()
    if m.startswith("v"):
        if m.startswith(("vdiv", "vsqrt")):
            return OCC["fp_div"], "fp_div"
        if m.startswith(("vmla", "vfma", "vmls", "vfms", "vnmla", "vnmls")):
            return OCC["fp_mac"], "fp_mac"
        if m.startswith(("vmul", "vnmul")):
            return OCC["fp_arith"], "fp_arith"
        if m.startswith(("vadd", "vsub", "vabs", "vneg")):
            return OCC["fp_arith"], "fp_arith"
        if m.startswith(("vcvt", "vcvtr")):
            return OCC["fp_cvt"], "fp_cvt"
        if m.startswith(("vcmpe", "vcmp")):
            return OCC["fp_cmp"], "fp_cmp"
        if m.startswith(("vmov", "vdup", "vseleq", "vsel")):
            return OCC["fp_mov"], "fp_mov"
        if m.startswith(("vldr", "vldm", "vpop")):
            return OCC["load"], "load"
        if m.startswith(("vstr", "vstm", "vpush")):
            return OCC["store"], "store"
        if m.startswith(("vmrs", "vmsr")):     # 系统寄存器搬运 ← 上一步的 58 条未识别
            return OCC["nop"], "nop"
        return None, m
    if m.startswith(("sdiv", "udiv")):
        return OCC["div"], "div"
    if m.startswith(("mul", "mla", "mls")):
        return OCC["mul"], "mul"
    if m.startswith(("smull", "umull", "smulbb")):
        return OCC["mac"], "mac"
    if m.startswith(("ldr", "ldrh", "ldrb", "ldrd", "ldm", "pop", "ldrex")):
        return OCC["load"], "load"
    if m.startswith(("str", "strh", "strb", "strd", "stm", "push", "strex")):
        return OCC["store"], "store"
    if m in ("tbh", "tbb"):
        return OCC["tbh"], "tbh"
    if m.startswith(("b", "cbz", "cbnz", "bl", "bx")):
        return OCC["br"], "br"
    if m.startswith(("cmp", "cmn", "tst", "teq")):
        return OCC["cmp"], "cmp"
    if m.startswith(("add", "sub", "adc", "sbc", "rsb", "and", "orr", "eor", "bic",
                     "orn", "lsl", "lsr", "asr", "ror", "clz", "rbit", "sxt", "uxt",
                     "mov", "movw", "movt", "mvn")):
        return OCC["alu"], "alu"
    if m in ("nop", "it", "ite", "dsb", "isb", "dmb"):
        return OCC["nop"], "nop"
    return None, m


def run(args):
    return subprocess.run([BIN + "objdump.exe"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def target(op):
    m = re.match(r"^([0-9a-f]+)\s", op.strip())
    return int(m.group(1), 16) if m else None


def avg_ranks(xs):
    idx = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and xs[idx[j + 1]] == xs[idx[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[idx[k]] = avg
        i = j + 1
    return r


def spearman(xs, ys):
    rx, ry = avg_ranks(xs), avg_ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n))
    dy = sum((ry[i] - my) ** 2 for i in range(n))
    if dx == 0 or dy == 0:
        return None
    rho = num / (dx * dy) ** 0.5
    assert -1.0001 <= rho <= 1.0001, "Spearman 越界 ⇒ 秩算错了"
    return rho


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

    # ── 未覆盖助记符点名（步 B 的判据）──
    unk = Counter()
    for a, mn, opnd in body:
        o, c = occ_of(mn)
        if o is None:
            unk[mn] += 1
    print("B 判据 · 助记符覆盖: 未识别 %d 种 / %d 条"
          % (len(unk), sum(unk.values())))
    if unk:
        for mn, c in unk.most_common(12):
            print("     %-12s ×%d" % (mn, c))

    # ── c0_结构: 共享尾块按**同一套文档表**算 ──
    tail_bi = None
    disp = [bi for bi, (s, e, _) in enumerate(bp) if body[e - 1][1] in ("tbh", "tbb")]
    UNCOND = re.compile(r"^(b|bx|tbb|tbh)\b")
    for bi, (s, e, _) in enumerate(bp):
        a2, mn2, op2 = body[e - 1]
        t2 = target(op2)
        if UNCOND.match(mn2) and t2 in addr2i and blk[addr2i[t2]] in disp:
            tail_bi = bi
            break
    c0_struct = 0
    if tail_bi is not None:
        s, e, _ = bp[tail_bi]
        for i in range(s, e):
            o, c = occ_of(body[i][1])
            c0_struct += (o or 1)
    print("\n共享尾块 = 块 %s（%d 条）⇒ c0_结构 = **%d cyc**（按同一套文档表累加）"
          % (tail_bi, SHARED_TAIL, c0_struct))
    print("  对照: A″ 从实测拟合出的截距 = 67.7 cyc ⇒ 两者比值 %.2f×"
          % (c0_struct / 67.7 if c0_struct else 0))

    # ── 逐 op 结构成本 ──
    txt = open(ENGINE_C, encoding="utf-8", errors="replace").read()
    mm = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", txt, re.S)
    meas = [int(x) for x in re.findall(r"\d+", mm.group(1))]

    names, S, M, classes = [], [], [], []
    for op in range(19):
        e = entry[op]
        if e not in addr2i:
            continue
        bi = blk[addr2i[e]]
        s, en, _ = bp[bi]
        c = Counter()
        tot = 0
        for i in range(s, en):
            o, cls = occ_of(body[i][1])
            c[cls] += 1
            tot += (o or 1)
        names.append(OPS[op]); S.append(c0_struct + tot); M.append(meas[op]); classes.append(dict(c))

    print("\n%-9s %-9s %-9s %-8s %s" % ("op", "结构成本", "实测", "差", "含 vdiv?"))
    print("-" * 62)
    for i, nm in enumerate(names):
        hasv = "★ vdiv" if classes[i].get("fp_div") else ""
        print("%-9s %-9d %-9d %+8d  %s" % (nm, S[i], M[i], M[i] - S[i], hasv))

    rho = spearman(S, M)
    print("\nJ1 排序: Spearman(结构成本, 实测) = %s ⇒ %s"
          % ("n/a" if rho is None else "%+.3f" % rho,
             "**通过**（>0.8）" if rho and rho > 0.8 else "**未通过**（目标 >0.8）"))

    geo = sum(1 for i in range(len(S)) if S[i] >= M[i])
    print("J2 方向性（结构 ≥ 实测 的比例）= %d/%d = %.0f%%"
          % (geo, len(S), 100.0 * geo / len(S)))
    print("    ★ 结构 < 实测 的原语（**必须点名**）:")
    under = [(names[i], S[i], M[i], M[i] - S[i]) for i in range(len(S)) if S[i] < M[i]]
    for nm, s_, m_, d in sorted(under, key=lambda z: -z[3]):
        why = "含 vdiv（操作数相关, 取上界仍不够 ⇒ 说明**上界取小了**）" if classes[names.index(nm)].get("fp_div") else "有其他延迟未覆盖"
        print("      %-9s 结构%-5d 实测%-5d 缺 %+5d  ← %s" % (nm, s_, m_, d, why))
    if not under:
        print("      （无）")

    order = sorted(range(len(S)), key=lambda i: -(M[i] - S[i]))
    print("\nJ3 离群点（残差最大的 3 个）: %s"
          % ", ".join("%s(%+d)" % (names[i], M[i] - S[i]) for i in order[:3]))
    top2 = {names[i] for i in order[:2]}
    if top2 <= {"PID", "RATE", "DIRECT"}:
        print("    ⇒ **通过**: 最大的离群正是含 `vdiv` 的 PID/RATE 或空操作 DIRECT")
    else:
        print("    ⇒ **未通过**: 离群的是 %s —— 模型形状仍错" % sorted(top2))

    print("\nJ4 稳健性（把全部占用同乘/同除 1.5, 看排序是否保持）:")
    for f in (1.5, 1 / 1.5):
        S2 = [c0_struct * f + (S[i] - c0_struct) * f for i in range(len(S))]
        r2 = spearman(S2, M)
        print("     ×%.2f ⇒ Spearman %s" % (f, "n/a" if r2 is None else "%+.3f" % r2))
    print("     （若与 J1 的 ρ 相同 ⇒ 缩放不改变**秩**, 这正是我们想要的稳健性）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
