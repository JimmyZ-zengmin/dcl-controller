#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-7: **不拟合**的结构交叉检验 —— 用一个结构常数去预测实测成本。

为什么必须做这一步（它是对 STA-6 的自我纠错）:
  STA-6 用 8 个特征在 19 个样本上做最小二乘 ⇒ **过定参数化**, 结果:
    · 中位 LOO 误差 29.1 cyc, 而"只数指令条数"的基线只有 5.6 cyc ⇒ 加权模型更差
    · 系数出现**物理上不可能**的负值（fp −4.60 / alu −8.69）
  ⇒ 那份回归**不能**作为"结构→成本"的证据, 只能作为"别这么干"的证据。

本步改成一个**没有自由度**的检验:
    K = median( 实测成本_i / 指令条数_i )        ← 由 19 个原语共同定出**一个**数
    预测_i = K × 指令条数_i
  ⇒ 零拟合自由度（K 只是取中位, 不是优化出来的）, 所以残差是**真残差**。
  ⇒ 它能失败: 若残差很大, 说明"指令条数"这个结构量不够, 必须上依赖链分析。

★ 诚实边界: K 仍来自实测表。所以本步**不产生**可算的执行时间,
  它只回答前置问题: **"结构量里有没有信息"**。有 ⇒ 值得做依赖链; 没有 ⇒ 路断在指令级。
"""
import re, statistics, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINE_C = os.path.join(ROOT, "src", "engine.c")

# 指令条数与分类直方图来自 STA-5（按控制流走出来的区间）
ROWS = [
    ("DIRECT", 29, "br=1 ld=4 st=2 fp=1 alu=14 ?=7"),
    ("CMP", 23, "br=2 ld=4 st=1 mul=2 div=1 fp=8 alu=2 ?=3"),
    ("HYST", 12, "br=1 ld=3 st=1 fp=5 ?=2"),
    ("CLAMP", 12, "br=1 ld=2 fp=6 ?=3"),
    ("LPF", 15, "br=1 ld=2 st=1 mul=2 div=1 fp=6 ?=2"),
    ("PID", 32, "br=1 ld=7 st=2 mul=2 div=1 fp=14 alu=1 ?=4"),
    ("RATE", 7, "br=1 ld=1 st=1 div=1 fp=2 ?=1"),
    ("DEADBAND", 12, "br=1 ld=2 st=1 fp=6 ?=2"),
    ("MUX", 9, "br=1 ld=2 fp=3 alu=2 ?=1"),
    ("EDGE", 31, "br=2 ld=4 st=1 fp=14 alu=4 ?=6"),
    ("LUT", 17, "br=1 ld=2 mul=1 fp=7 alu=3 ?=3"),
    ("CNT", 29, "br=1 ld=3 st=2 fp=15 alu=2 ?=6"),
    ("TIMER", 19, "br=1 ld=3 st=1 fp=8 alu=2 ?=4"),
    ("ARITH", 26, "br=2 ld=4 st=1 fp=10 alu=4 ?=5"),
    ("SCALE", 7, "br=1 ld=2 mul=1 fp=2 ?=1"),
    ("AND", 5, "br=1 fp=3 ?=1"),
    ("OR", 9, "br=1 ld=1 fp=5 ?=2"),
    ("NOT", 7, "br=1 fp=4 alu=1 ?=1"),
    ("SR", 42, "br=1 ld=6 st=2 fp=8 alu=15 ?=10"),
]


def main():
    txt = open(ENGINE_C, encoding="utf-8", errors="replace").read()
    # ★ 变量名不能叫 m: 下面推导式里的 m_ 会**遮蔽**它, 报出
    #   "unsupported operand type(s) for /: 're.Match' and 'float'" ——
    #   一个纯粹由命名造成的假错误（本项目"同一个语义两处存放"的命名版）。
    mm = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", txt, re.S)
    if not mm:
        print("!! 抓不到 k_op_cost_itcm"); return 2
    meas = [int(x) for x in re.findall(r"\d+", mm.group(1))]
    if len(meas) < len(ROWS):
        print("!! 实测表只有 %d 项, 需要 %d" % (len(meas), len(ROWS))); return 2

    data = [(nm, n, meas[i], h) for i, (nm, n, h) in enumerate(ROWS)]
    K = statistics.median(cost / float(n) for _, n, cost, _ in data)

    print("单一结构常数 K = %.3f cyc/指令" % K)
    print("  （= 19 个原语各自 实测/指令数 的**中位数**；无拟合自由度, 残差是真残差）")
    print()
    print("%-9s %-6s %-7s %-9s %-9s %s" % ("op", "指令", "实测", "结构预测", "残差", "偏差"))
    print("-" * 96)
    errs = []
    for nm, n, m_, h in data:
        pred = K * n
        d = m_ - pred
        errs.append((nm, n, m_, pred, d))
        flag = ""
        if m_ and abs(d) > 0.25 * m_:
            flag = "★>25%"
        print("%-9s %-6d %-7d %-9.1f %+9.1f  %s" % (nm, n, m_, pred, d, flag))

    ae = sorted(abs(d) for *_, d in errs)
    rel = [abs(d) / m_ for _, _, m_, _, d in errs if m_]
    print()
    print("残差: 中位 %.1f cyc / 最大 %.1f cyc / 平均相对 %.1f%%"
          % (statistics.median(ae), max(ae), 100 * sum(rel) / len(rel)))
    print()
    print("超差 >25% 的原语（必须点名, 不许静默）:")
    bad = [(nm, n, m_, pred, d) for nm, n, m_, pred, d in errs if m_ and abs(d) > 0.25 * m_]
    for nm, n, m_, pred, d in bad:
        print("   %-9s 指令%-4d 实测%-5d 结构预测%-7.1f 残差%+7.1f  (%.0f%%)"
              % (nm, n, m_, pred, d, 100 * abs(d) / m_))
    if not bad:
        print("   （无）")

    print()
    print("=== 判定 ===")
    if bad:
        print("  结构量（指令条数）**不足以**单独解释成本: %d/%d 个原语超 25%%。"
              % (len(bad), len(errs)))
        print("  ⇒ 必须上**依赖链 + 指令类别延迟**（真正的 STA）, 而不是停在这一层。")
    else:
        print("  指令条数即可解释成本（全部落在 25%% 内）⇒ 结构模型在**指令级**成立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
