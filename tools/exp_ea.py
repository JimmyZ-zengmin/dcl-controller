#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-A: 分离"每路由成本"与"每桶成本"（三参数联合拟合 + 双向预测检验）。

## 假设
    E[cost] = C_base + C_bucket × buckets_per_tick + C_route × nrun

## 为什么是这个设计（我在写方案时修正过一次）
"独立扫桶数与条数"**做不到**: division 由 `period` 决定, 桶数是 division 的函数。
⇒ 只能**联合拟合**, 再用**双向判据**检验。

## 判据（都能失败, 全部一次报出 —— 不许事后挑）
  A-1 ★ 单一 (C_bucket, C_route) 能否同时拟合三档? 残差 ≤ 20 cyc 判通过
  A-2 ★★ 换 op (PID ↔ DIRECT) 时: **斜率应同步变化, 截距(每桶成本)应不变**
         这是双向预测, 假解释变量过不了
  A-3 ★ 独立路径自洽: 用部分点标定, 预测**没参与拟合**的点, 偏差 ≤ 20 cyc

## 纪律
  全部点一次性报出, 不做特征挑选（本轮已因事后挑特征栽过两次: §5.44 / §5.49）
"""
import os, re, subprocess, sys, statistics, itertools

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "tools", "h723_tick_ring.py")
OUT = os.path.join(HERE, "ea_out")
os.makedirs(OUT, exist_ok=True)

# (op, 名字, 备注)
OPS = [(5, "PID", "最贵原语, 含 vdiv"), (0, "DIRECT", "最便宜原语")]
NS = [16, 32, 64, 128]
DIVS = [0, 1, 2]


def run_one(op, div, n):
    log = os.path.join(OUT, "op%d_div%d_n%d.log" % (op, div, n))
    cmd = [sys.executable, TOOL, "--prog", "%d,%d,%d" % (op, div, n)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=ROOT, timeout=300)
    out = r.stdout or ""
    open(log, "w", encoding="utf-8").write(out)
    # 均值（TB tick）
    m = re.search(r"实测 di\s*:\s*均值 ([\d.]+) TB tick\s+标准差 ([\d.]+)", out)
    # nrun 与每拍桶数
    b = re.search(r"结构预测: nrun ∈ \[([\d, ]+)\]\s+⇒ 平均每拍跑 ([\d.]+) 条", out)
    nb = re.search(r"本拍跑\s+\d+\s+条 ⇒ 占\s+\d+/(\d+) 拍", out)
    ok = ("[PASS] P0a" in out) and ("[PASS] P0b" in out)
    if not (m and b):
        return None
    # 桶表项数 = 最后那个 "占 N/M 拍" 的 M
    bs = re.findall(r"占\s+\d+/(\d+)\s*拍", out)
    nbuckets = max(int(x) for x in bs) if bs else None
    nrun = float(b.group(2))
    # 每拍**执行**的桶数 = min(桶表项数, 能覆盖全部路由所需)
    #   桶表按 t % M 分相 ⇒ 每拍命中 1 相; 该相里的桶数 = 该相覆盖的路由组数
    #   简化且可判定的口径: 每拍执行的桶数 = min(n, M)
    bpt = min(n, nbuckets) if nbuckets else n
    return dict(op=op, div=div, n=n, mean=float(m.group(1)), sd=float(m.group(2)),
                nrun=nrun, nbuckets=nbuckets, bpt=bpt, ok=ok)


def ols(A, y):
    """最小二乘（正规方程 + 高斯消元）。A: n×k。"""
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


def fit(points):
    """points: [(bpt, nrun, mean)] → (C_base, C_bucket, C_route, 残差列表)"""
    A = [[1.0, float(p[0]), float(p[1])] for p in points]
    y = [float(p[2]) for p in points]
    c = ols(A, y)
    if c is None:
        return None
    res = [y[i] - (c[0] + c[1] * A[i][1] + c[2] * A[i][2]) for i in range(len(y))]
    return c, res


def main():
    print("=== E-A: 采集（%d 配置 × %d op = %d 次）===" % (len(NS) * len(DIVS), len(OPS),
                                                    len(NS) * len(DIVS) * len(OPS)))
    data = {}
    for op, name, _note in OPS:
        data[op] = []
        for div in DIVS:
            for n in NS:
                r = run_one(op, div, n)
                if not r:
                    print("  op=%-6s div=%d n=%-4d **取数失败**" % (name, div, n))
                    continue
                data[op].append(r)
                print("  op=%-6s div=%d n=%-4d  nrun=%6.2f  桶表=%4s  每拍桶=%4d  "
                      "均值=%8.1f TB  σ=%5.1f  %s"
                      % (name, div, n, r["nrun"], r["nbuckets"], r["bpt"],
                         r["mean"], r["sd"], "OK" if r["ok"] else "**前置不良**"))

    # ── A-1: 全部点联合拟合 ──
    print("\n=== A-1 单一 (C_bucket, C_route) 同时拟合三档 ===")
    allpts = [(r["bpt"], r["nrun"], r["mean"])
              for op, _, _ in OPS for r in data[op] if r["ok"]]
    if len(allpts) < 4:
        print("  有效点不足"); return 2
    c, res = fit(allpts)
    print("  拟合: cost = %.1f + %.1f×bpt + %.1f×nrun   (TB tick)" % (c[0], c[1], c[2]))
    print("  换成 CPU cyc: 截距 %.0f, 每桶 %.0f, 每路由 %.0f" % (c[0] * 2, c[1] * 2, c[2] * 2))
    ae = sorted(abs(x) for x in res)
    print("  残差: 中位 %.1f TB  最大 %.1f TB  (= %.0f / %.0f cyc)"
          % (statistics.median(ae), max(ae), statistics.median(ae) * 2, max(ae) * 2))
    print("  ⇒ A-1 %s（判据: 最大残差 ≤ 40 TB = 20 cyc）"
          % ("**通过**" if max(ae) <= 40 else "**未通过**"))

    # ── A-2: 换 op 时斜率同步变、截距不变（双向预测）──
    print("\n=== A-2 ★★ 双向检验: 换 op ⇒ 斜率同步变、截距不变 ===")
    per = {}
    for op, name, _n in OPS:
        pts = [(r["bpt"], r["nrun"], r["mean"]) for r in data[op] if r["ok"]]
        if len(pts) < 4:
            continue
        cc, rr = fit(pts)
        per[op] = cc
        print("  op=%-6s 截距=%8.1f  每桶=%7.1f  每路由=%7.1f  最大残差=%6.1f TB"
              % (name, cc[0], cc[1], cc[2], max(abs(x) for x in rr)))
    if len(per) == 2:
        a, b = per[5], per[0]
        d_route = abs(a[2] - b[2])
        d_buck = abs(a[1] - b[1])
        print("\n  PID 的每路由 %.1f vs DIRECT 的 %.1f ⇒ 差 %.1f TB（应**大**）"
              % (a[2], b[2], d_route))
        print("  PID 的每桶   %.1f vs DIRECT 的 %.1f ⇒ 差 %.1f TB（应**小**）"
              % (a[1], b[1], d_buck))
        print("  ⇒ A-2 %s"
              % ("**通过**（斜率差 > 截距差 3 倍）"
                 if d_route > 3 * max(d_buck, 1.0) else "**未通过**（斜率的 op 依赖不显著）"))
        print("  ★ 判读: 若两者都差 ⇒ 每桶成本也依赖 op, 假设要改;")
        print("          若斜率不差 ⇒ 这个 op 对不构成对照（DIRECT 太便宜）")

    # ── A-3: 留点预测 ──
    print("\n=== A-3 独立路径: 用 div0+div1 标定, 预测 div2 ===")
    tr = [(r["bpt"], r["nrun"], r["mean"]) for op, _, _ in OPS
          for r in data[op] if r["ok"] and r["div"] in (0, 1)]
    te = [(r["bpt"], r["nrun"], r["mean"], r["div"], r["op"]) for op, _, _ in OPS
          for r in data[op] if r["ok"] and r["div"] == 2]
    c2, _ = fit(tr)
    print("  标定 (div0+div1, %d 点): 截距=%.1f 每桶=%.1f 每路由=%.1f" % (len(tr), c2[0], c2[1], c2[2]))
    bad = 0
    for bpt, nrun, mean, div, op in te:
        pred = c2[0] + c2[1] * bpt + c2[2] * nrun
        dev = mean - pred
        tol = 40.0
        if abs(dev) > tol:
            bad += 1
        print("    op=%-6s bpt=%-4d nrun=%6.2f  预测%8.1f  实测%8.1f  偏差%+8.1f %s"
              % ({5: "PID", 0: "DIRECT"}[op], bpt, nrun, pred, mean, dev,
                 "" if abs(dev) <= tol else "★超差"))
    print("  ⇒ A-3 %s（%d/%d 点超 40 TB = 20 cyc）"
          % ("**通过**" if bad == 0 else "**未通过**", bad, len(te)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
