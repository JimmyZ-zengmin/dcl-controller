#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-13（步 A' 的诊断）: 既然"关键路径"最差、"指令条数"最好, 就该问**是什么在限制吞吐**。

## 现象（STA-12 实测）
  Spearman(依赖链, 实测) = 0.180   ← 最差
  Spearman(串行和, 实测) = 0.586
  Spearman(指令条数, 实测) = 0.639 ← 最好
关键路径最差, 说明**不是"等最长的依赖"在决定时间** ⇒ 典型**吞吐受限**
（M7 双发射 + 转发 + 这些原语体是直线代码, 独立指令可以重叠）。
⇒ 那么"谁占发射口/谁的吞吐率低"才是解释量。

## 本脚本要回答的（判据都能失败）
  G1 分别对**每一类指令的条数**与实测成本做 Spearman ⇒ 找出**单一最好的结构量**
  G2 用"占用份额"模型: cost ≈ Σ (该类条数 × 该类吞吐占用), 占用取自 Cortex-M7 文档
     判据: Spearman > 0.8
  G3 **稳健性**: 上面两个排序必须与"实测成本"无关地成立
     （即: 不许用实测去挑特征 —— 那会变成自证。这里**一次把 8 类全报出来**,
       而不是"试到哪个好就用哪个"。）

## ★ 为什么必须一次全报（方法论）
  逐个试特征直到 ρ>0.8, 就是在**对 19 个样本做特征选择** —— 必然会挑到一个"看起来很好"的。
  本项目纪律: **判据必须先定, 不能事后挑。** ⇒ 本脚本打印**全部**相关系数, 不做挑选。
"""
import os, re, subprocess, sys, statistics
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
       "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32."
       "7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-")
ELF = os.path.join(ROOT, "build", "dcl_h723")
ENGINE_C = os.path.join(ROOT, "src", "engine.c")

# 类别划分（见 STA-12 的 LAT 表；这里只做**计数**, 不做延迟加权）
CATS = [
    ("fp_arith", r"^v(add|sub|mul|mla|fma|mls|fms|nmla|nmls|nmul|abs|neg)"),
    ("fp_div",   r"^v(div|sqrt)"),
    ("fp_cvt",   r"^v(cvt|cvtr)"),
    ("fp_cmp",   r"^v(cmp|c mpe)"),
    ("fp_mov",   r"^v(mov|dup|seleq)"),
    ("fp_mem",   r"^v(ldr|str|ldm|stm|push|pop)"),
    ("core_mem", r"^(ldr|str|ldrh|strh|ldrb|strb|ldrd|strd|ldm|stm|push|pop)"),
    ("mul_div",  r"^(mul|mla|mls|smull|umull|sdiv|udiv)"),
    ("alu",      r"^(add|sub|and|orr|eor|bic|lsl|lsr|asr|cmp|tst|mov|movw|movt|mvn|adc|sbc|rsb|clz|rbit|sxt|uxt)"),
    ("br",       r"^(b|cbz|cbnz|bl|bx|tbh|tbb)"),
    ("sys",      r"^(vmrs|vmsr|it|ite|nop|dsb|isb|dmb)"),
]
# Cortex-M7 **吞吐占用**模型（cycles/条, 文档值; 不是延迟而是"占发射口的份额"）
OCC = {"fp_arith": 1, "fp_div": 12, "fp_cvt": 1, "fp_cmp": 1, "fp_mov": 1,
       "fp_mem": 2, "core_mem": 2, "mul_div": 2, "alu": 1, "br": 1, "sys": 1}


def run(args):
    return subprocess.run([BIN + "objdump.exe"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def target(op):
    m = re.match(r"^([0-9a-f]+)\s", op.strip())
    return int(m.group(1), 16) if m else None


def spearman(xs, ys):
    n = len(xs)
    rx = {v: i for i, v in enumerate(sorted(xs))}
    ry = {v: i for i, v in enumerate(sorted(ys))}
    d2 = sum((rx[xs[i]] - ry[ys[i]]) ** 2 for i in range(n))
    return 1 - 6 * d2 / (n * (n * n - 1))


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
    OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
           "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]
    txt = open(ENGINE_C, encoding="utf-8", errors="replace").read()
    mm = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", txt, re.S)
    meas = [int(x) for x in re.findall(r"\d+", mm.group(1))]

    names, feats = [], []
    for op in range(19):
        e = entry[op]
        if e not in addr2i:
            continue
        bi = blk[addr2i[e]]
        s, en, _ = bp[bi]
        c = Counter()
        for i in range(s, en):
            mn = body[i][1].lower()
            hit = None
            for cat, rx in CATS:
                if re.match(rx, mn):
                    hit = cat
                    break
            c[hit or "?"] += 1
        names.append(OPS[op])
        feats.append((c, en - s))

    print("G1 逐类条数 与 实测成本 的 Spearman（★ **一次全报**, 不做特征挑选）")
    print("-" * 68)
    print("%-12s %-12s %s" % ("类别", "Spearman", "说明"))
    res = []
    for cat, _ in CATS:
        xs = [f[0].get(cat, 0) for f in feats]
        if max(xs) == min(xs):
            print("%-12s %-12s 常数（该类的条数在所有 op 里相同 ⇒ 无区分度）" % (cat, "n/a"))
            continue
        r = spearman(xs, meas)
        res.append((cat, r))
        print("%-12s %-12.3f" % (cat, r))
    xs = [f[1] for f in feats]
    r_cnt = spearman(xs, meas)
    print("%-12s %-12.3f  ← 基线: 入口块总条数" % ("TOTAL", r_cnt))
    res.sort(key=lambda z: -abs(z[1]))
    print("\n  绝对相关最强的三类: %s" % ", ".join("%s(%.3f)" % (c, r) for c, r in res[:3]))

    print("\nG2 吞吐占用模型: cost = Σ(该类条数 × OCC[类])")
    print("    OCC = %s" % OCC)
    xs = [sum(f[0].get(cat, 0) * OCC[cat] for cat, _ in CATS) for f in feats]
    r_occ = spearman(xs, meas)
    print("    Spearman(占用和, 实测) = %.3f ⇒ %s"
          % (r_occ, "**通过**（>0.8）" if r_occ > 0.8 else "**未通过**（目标 >0.8）"))
    print("    对照: 条数基线 %.3f / 占用和 %.3f ⇒ 占用模型%s优于条数"
          % (r_cnt, r_occ, "**" if r_occ > r_cnt + 0.1 else "**未**"))

    print("\n  逐 op 明细（占用和 vs 实测）:")
    print("    %-9s %-8s %-9s %-9s" % ("op", "条数", "占用和", "实测"))
    for i, nm in enumerate(names):
        print("    %-9s %-8d %-9d %-9d" % (nm, feats[i][1], xs[i], meas[i]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
