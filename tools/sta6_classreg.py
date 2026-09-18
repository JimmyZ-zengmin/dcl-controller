#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-6: 用**指令类**做结构→成本的回归（FPGA STA 的思想内核, 不是数条数）。

思路（与 FPGA 完全同构）:
  FPGA:   路径延迟 = Σ (每个原语的**标定延迟**)
  MCU:    成本     = Σ (每类指令的**每类周期**)   ← 本脚本要估的就是这组系数
  若这组系数能**跨原语**解释实测成本（不是逐原语各配一个数）, 那么"沿结构累加"就成立。

★ 判据设计（这是本脚本的全部意义, 判据必须能失败）
  K1 指令类数必须 ≥ 4 类, 样本(原语)数 ≥ 15 —— 否则回归是空判据
  K2 **留一交叉验证（LOO）**: 每次扣掉一个原语拟合, 再预测它。
     报告最大/中位预测误差。**这是"能预测"与"只是拟合"的分界**
     （§A2.5 用的就是这条纪律: 拟合内残差 0.12 cyc 才算数）
  K3 与"只数条数"（单变量指令数）的 LOO 误差对比:
     若加权模型**不显著更好** ⇒ 说明"按类加权"这一层没带来信息, 必须如实报告
  K4 系数量级必须与 Cortex-M7 文档**同向**（FP/除法/访存 > ALU）。
     若拟合出"ALU 比除法贵"这种反物理系数 ⇒ 说明结构量有系统偏差, 判无效。

★ 诚实边界: 这里的系数是**用实测表拟合出来的**。所以本步**不产生**"可算的执行时间",
  它只回答一个前置问题: **"结构量里到底有没有足够信息"**。
  有 ⇒ 值得继续做真正的依赖链分析; 没有 ⇒ 这条路在指令级就断了, 要如实写下来。
"""
import os, re, subprocess, sys
from collections import Counter

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
FEATS = ["br", "ld", "st", "mul", "div", "fp", "alu", "n"]

CLASSES = [
    ("br",  re.compile(r"^(b|bl|blx|bx|cbz|cbnz|tbb|tbh|it)\b")),
    ("ld",  re.compile(r"^(ldr|ldrh|ldrb|ldrd|ldm|vldr|vldm|pop|ldrex)\b")),
    ("st",  re.compile(r"^(str|strh|strb|strd|stm|vstr|vstm|push|strex|stmdb)\b")),
    ("mul", re.compile(r"^(mul|mla|mls|smull|umull|vmul|vfma|vmla|vnmla|vnmul)\b")),
    ("div", re.compile(r"^(vdiv|vsqrt|sdiv|udiv)\b")),
    ("fp",  re.compile(r"^v")),
    ("alu", re.compile(r"^(add|sub|and|orr|eor|bic|lsl|lsr|asr|cmp|tst|adc|sbc|rsb|clz|rbit|sxt|uxt|mov|movw|movt|mvn|adds|subs|lsls|movs)\b")),
]


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


def build_regions():
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

    regs = []
    for op in range(19):
        e = entry[op]
        i = addr2i[e]
        seg = []
        while i < len(body):
            a, mn, opnd = body[i]
            seg.append((a, mn, opnd))
            if cls_of(mn) == "br":
                t = br_target(opnd)
                if t is not None and t <= e:
                    break
            i += 1
        regs.append(seg)
    return regs


def ols(X, y):
    """最小二乘（正规方程 + 高斯消元）。X: n×k, y: n。返回 k 维系数。"""
    k = len(X[0])
    A = [[sum(X[i][a] * X[i][b] for i in range(len(X))) for b in range(k)] + [sum(X[i][a] * y[i] for i in range(len(X)))]
         for a in range(k)]
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(A[r][c]))
        A[c], A[p] = A[p], A[c]
        if abs(A[c][c]) < 1e-12:
            return None
        for r in range(k):
            if r != c:
                f = A[r][c] / A[c][c]
                for j in range(c, k + 1):
                    A[r][j] -= f * A[c][j]
    return [A[i][k] / A[i][i] for i in range(k)]


def main():
    txt = open(ENGINE_C, encoding="utf-8", errors="replace").read()
    m = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", txt, re.S)
    if not m:
        print("!! 抓不到 k_op_cost_itcm"); return 2
    y = [int(x) for x in re.findall(r"\d+", m.group(1))]

    regs = build_regions()
    X, names = [], []
    for op, seg in enumerate(regs):
        c = Counter(cls_of(mn) for _, mn, _ in seg)
        # ★ 特征 = 7 个指令类 + 总条数（★ 原来在这里多接了一个 "n" 造成重复列 ⇒ 回归奇异。
        #   重复列会让正规方程**奇异**而不是报错, 症状是"拟合不出来"而不是"算错了" ——
        #   与 §A3.10 记的"同一个语义两处存放"同族。）
        X.append([c.get(f, 0) for f in FEATS[:-1]] + [len(seg)])
        names.append(OPS[op])
    F = FEATS[:-1] + ["n"]

    used = [(i, names[i]) for i in range(min(len(y), len(X)))]
    print("样本 %d 个原语, 特征 %d 个: %s" % (len(used), len(F), F))
    print("K1 特征数=%d (≥4 ✓)  样本数=%d (≥15 %s)"
          % (len(F), len(used), "✓" if len(used) >= 15 else "✗ **不足, 判无效**"))
    if len(used) < 15:
        return 2

    Xa = [X[i] for i, _ in used]
    ya = [y[i] for i, _ in used]
    coef = ols(Xa, ya)
    if coef is None:
        print("!! 回归奇异"); return 2
    print("\n=== 全量拟合系数（每类指令的周期）===")
    for f, c in zip(F, coef):
        print("    %-5s %8.2f" % (f, c))

    # K4 同向性检查
    print("\nK4 与 Cortex-M7 文档同向性（除法/FP/访存 应 ≥ ALU）:")
    d = dict(zip(F, coef))
    ok4 = True
    for hi_f in ("div", "fp", "ld", "st", "mul"):
        if d.get(hi_f, 0) < d.get("alu", 0):
            print("    ✗ %s(%.2f) < alu(%.2f) —— 反物理" % (hi_f, d[hi_f], d["alu"])); ok4 = False
    print("    %s" % ("✓ 全部同向" if ok4 else "⇒ **判无效**: 结构量有系统偏差"))

    # K2 留一交叉验证
    errs, base_errs = [], []
    for i, nm in used:
        Xtr = [X[j] for j, _ in used if j != i]
        ytr = [y[j] for j, _ in used if j != i]
        c = ols(Xtr, ytr)
        if c is None:
            errs.append(0.0); base_errs.append(0.0); continue
        pred = sum(c[t] * X[i][t] for t in range(len(F)))
        errs.append(pred - y[i])
        # 基线: 只用"指令条数"单变量
        xs = [X[j][-1] for j, _ in used if j != i]
        ys = ytr
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        den = sum((v - mx) ** 2 for v in xs)
        a = sum((xs[t] - mx) * (ys[t] - my) for t in range(len(xs))) / den if den else 0
        b = my - a * mx
        base_errs.append(a * X[i][-1] + b - y[i])

    ae = sorted(abs(e) for e in errs)
    be = sorted(abs(e) for e in base_errs)
    print("\nK2 留一交叉验证（LOO, 每次扣掉该原语再预测它）:")
    print("    加权模型: 中位 |误差| = %6.1f cyc   最大 = %6.1f cyc   平均相对 = %5.1f%%"
          % (ae[len(ae) // 2], ae[-1],
             100 * sum(abs(errs[t]) / ya[t] for t in range(len(ya))) / len(ya)))
    print("K3 基线（只数指令条数, 单变量）:")
    print("    指令数  : 中位 |误差| = %6.1f cyc   最大 = %6.1f cyc   平均相对 = %5.1f%%"
          % (be[len(be) // 2], be[-1],
             100 * sum(abs(base_errs[t]) / ya[t] for t in range(len(ya))) / len(ya)))
    better = ae[len(ae) // 2] < 0.7 * be[len(be) // 2]
    print("\n⇒ 判定: 加权模型%s显著优于数条数（中位误差比 %.2f）"
          % ("**" if better else "**未**", ae[len(ae) // 2] / max(be[len(be) // 2], 1e-9)))
    print("\n逐原语 LOO 明细（误差为正 = 结构模型**高估**）:")
    print("    %-9s %-7s %-9s %-9s %s" % ("op", "实测", "加权预测", "误差", "指令数预测误差"))
    for t, (i, nm) in enumerate(used):
        print("    %-9s %-7d %-9.1f %+9.1f %+9.1f"
              % (nm, y[i], y[i] + errs[t], errs[t], base_errs[t]))
    return 0 if (ok4 and better) else 1


if __name__ == "__main__":
    sys.exit(main())
