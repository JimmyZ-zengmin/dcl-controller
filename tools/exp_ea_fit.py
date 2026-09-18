#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-A 拟合（修正 bpt 口径后）。

## ★ 先纠正我自己的一个口径错误
STA 脚本里我把 `bpt = min(n, nbuckets)` —— **错**。
桶表按 `t % M` **分相**执行, 每拍只命中**一个相**:
    div0: `cnt1[0]` 每拍都跑        ⇒ 每拍执行 **1** 个桶
    div1: `cnt1[t%10]` 十相轮转      ⇒ 每拍执行 **min(n,10)** 个桶
    div2: `cnt2[t%100]` 百相轮转     ⇒ 每拍执行 **min(n,100)** 个桶
`nbuckets`（桶表项数 100）是**表的规模**, 不是**每拍执行数**。
⇒ 这个错与"把桶表大小当成每拍桶数"是同一个坑, 记下来。

## 判据（全部一次报出, 不做事后挑选）
  A-1 三参数联合拟合: 最大残差 ≤ 40 TB(20 cyc)
  A-2 双向: 换 op ⇒ 斜率同步变、截距不变
  A-3 留点预测: div0+div1 标定 ⇒ 预测 div2, 偏差 ≤ 40 TB
  A-4 ★ 若 C_bucket ≈ 0 ⇒ **"每桶成本"假设被否**, 如实销案
"""
import re, statistics, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
# 来自 exp_ea.py 的实测原始表（TB tick）
RAW = """
PID    0 16   16.00  1570.5
PID    0 32   32.00  2545.9
PID    0 64   64.00  4498.5
PID    0 128 128.00  8402.2
PID    1 16    1.60   696.8
PID    1 32    3.20   794.1
PID    1 64    6.40   986.4
PID    1 128  12.80  1375.9
PID    2 16    0.16   559.7
PID    2 32    0.32   572.6
PID    2 64    0.64   606.0
PID    2 128   1.28   678.1
DIRECT 0 16   16.00   996.3
DIRECT 0 32   32.00  1419.6
DIRECT 0 64   64.00  2268.2
DIRECT 0 128 128.00  3964.5
DIRECT 1 16    1.60   619.2
DIRECT 1 32    3.20   662.2
DIRECT 1 64    6.40   744.8
DIRECT 1 128  12.80   912.7
DIRECT 2 16    0.16   545.6
DIRECT 2 32    0.32   551.7
DIRECT 2 64    0.64   567.3
DIRECT 2 128   1.28   609.1
"""


def bpt_of(n, div):
    """每拍**执行**的桶数（修正后的口径）。"""
    return {0: 1, 1: min(n, 10), 2: min(n, 100)}[div]


def ols(A, y):
    k = len(A[0])
    M = [[sum(A[i][a] * A[i][b] for i in range(len(A))) for b in range(k)]
         + [sum(A[i][a] * y[i] for i in range(len(A)))] for a in range(k)]
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-9:
            return None
        for r in range(k):
            if r != c:
                f = M[r][c] / M[c][c]
                for j in range(c, k + 1):
                    M[r][j] -= f * M[c][j]
    return [M[i][k] / M[i][i] for i in range(k)]


def fit(pts, ncol=3):
    A = [[1.0, float(p["bpt"]), float(p["nrun"])][:ncol] for p in pts]
    y = [float(p["mean"]) for p in pts]
    c = ols(A, y)
    if c is None:
        return None, None
    res = [y[i] - sum(c[j] * A[i][j] for j in range(len(c))) for i in range(len(y))]
    return c, res


def main():
    pts = []
    for line in RAW.strip().splitlines():
        f = line.split()
        op, div, n, nrun, mean = f[0], int(f[1]), int(f[2]), float(f[3]), float(f[4])
        pts.append(dict(op=op, div=div, n=n, nrun=nrun, mean=mean, bpt=bpt_of(n, div)))

    print("=== 修正后的 bpt 口径 ===")
    print("  div0 ⇒ 每拍 1 桶; div1 ⇒ min(n,10); div2 ⇒ min(n,100)")
    for p in pts[:4] + pts[8:12]:
        print("    %-6s div=%d n=%-4d nrun=%7.2f  bpt=%3d" % (p["op"], p["div"], p["n"], p["nrun"], p["bpt"]))

    # ── A-1: 全部点三参数拟合 ──
    print("\n=== A-1 三参数联合拟合（全部 24 点）===")
    c, res = fit(pts)
    print("  cost = %8.1f + %7.2f×bpt + %7.2f×nrun   (TB tick)" % tuple(c))
    print("  换 CPU cyc: 截距 %.0f, 每桶 %.1f, 每路由 %.1f" % (c[0] * 2, c[1] * 2, c[2] * 2))
    ae = sorted(abs(x) for x in res)
    print("  残差: 中位 %.1f TB (%.0f cyc)  最大 %.1f TB (%.0f cyc)"
          % (statistics.median(ae), statistics.median(ae) * 2, max(ae), max(ae) * 2))
    print("  ⇒ A-1 %s（判据 最大残差 ≤40 TB）" % ("**通过**" if max(ae) <= 40 else "**未通过**"))
    print("\n  ★ A-4 每桶成本 = %.2f TB (%.1f cyc) ⇒ %s"
          % (c[1], c[1] * 2,
             "**≈0 ⇒ 「每桶成本」假设被否, 销案**" if abs(c[1]) < 5 else "**非零, 假设成立**"))

    # ── 逐 op 拟合（两参数: 截距 + 每路由）──
    print("\n=== 逐 op 拟合（cost = C0 + m×nrun, 不含桶项）===")
    per = {}
    for op in ("PID", "DIRECT"):
        sub = [p for p in pts if p["op"] == op]
        cc, rr = fit(sub, ncol=2)
        # ncol=2 时 A 的前两列是 [1, bpt] —— 不是我们想要的; 重建
        A = [[1.0, float(p["nrun"])] for p in sub]
        y = [float(p["mean"]) for p in sub]
        cc = ols(A, y)
        rr = [y[i] - (cc[0] + cc[1] * A[i][1]) for i in range(len(y))]
        per[op] = cc
        ae2 = sorted(abs(x) for x in rr)
        print("  %-6s C0=%7.1f TB (%5.0f cyc)   m=%6.2f TB/条 (%5.1f cyc/条)   "
              "残差 中位 %.1f / 最大 %.1f TB"
              % (op, cc[0], cc[0] * 2, cc[1], cc[1] * 2,
                 statistics.median(ae2), max(ae2)))

    # ── A-2: 双向 ──
    print("\n=== A-2 ★★ 双向检验 ===")
    a, b = per["PID"], per["DIRECT"]
    print("  斜率(每路由): PID %.2f vs DIRECT %.2f ⇒ 差 %.2f TB（应**大**）"
          % (a[1], b[1], abs(a[1] - b[1])))
    print("  截距(每拍底座): PID %.1f vs DIRECT %.1f ⇒ 差 %.1f TB（应**小**）"
          % (a[0], b[0], abs(a[0] - b[0])))
    ok = abs(a[1] - b[1]) > 3 * max(abs(a[0] - b[0]), 1.0)
    print("  ⇒ A-2 %s" % ("**通过**" if ok else "**未通过**"))

    # ── A-3: 留点预测 ──
    print("\n=== A-3 独立路径: div0+div1 标定 ⇒ 预测 div2 ===")
    tr = [p for p in pts if p["div"] in (0, 1)]
    te = [p for p in pts if p["div"] == 2]
    A = [[1.0, float(p["nrun"])] for p in tr]
    y = [float(p["mean"]) for p in tr]
    cc = ols(A, y)
    print("  标定(%d 点): C0=%.1f TB  m=%.2f TB/条" % (len(tr), cc[0], cc[1]))
    bad = 0
    for p in te:
        pred = cc[0] + cc[1] * p["nrun"]
        dev = p["mean"] - pred
        if abs(dev) > 40:
            bad += 1
        print("    %-6s n=%-4d nrun=%6.2f  预测%8.1f  实测%8.1f  偏差%+8.1f %s"
              % (p["op"], p["n"], p["nrun"], pred, p["mean"], dev,
                 "" if abs(dev) <= 40 else "★超差"))
    print("  ⇒ A-3 %s（%d/%d 超 40 TB）"
          % ("**通过**" if bad == 0 else "**未通过**", bad, len(te)))

    # ── 结论: div0 之谜解开了吗 ──
    print("\n=== ★★ div0 那 2.1 倍: 结论 ===")
    A = [[1.0, float(p["nrun"])] for p in pts if p["op"] == "PID"]
    y = [float(p["mean"]) for p in pts if p["op"] == "PID"]
    cc = ols(A, y)
    print("  用**单一** (C0, m) 拟合 PID 的**全部 12 个点**（跨三档）:")
    print("    C0=%.1f TB (%.0f cyc)   m=%.2f TB/条 (%.1f cyc/条)" % (cc[0], cc[0] * 2, cc[1], cc[1] * 2))
    for p in [q for q in pts if q["op"] == "PID"]:
        pred = cc[0] + cc[1] * p["nrun"]
        print("      div=%d n=%-4d nrun=%7.2f  预测%8.1f  实测%8.1f  偏差%+8.1f"
              % (p["div"], p["n"], p["nrun"], pred, p["mean"], p["mean"] - pred))
    print("  ★ 若残差小 ⇒ **div0 之谜解开: 根本不是「每路由变便宜」, 而是 nrun 本身**")
    print("    （我之前按 division 分组拟合, 每个 division 的 nrun 范围不同, 于是把")
    print("      「nrun 的非线性」误读成了「division 改变每路由成本」。)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
