#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-B: 桶开销确认（E-A 已把 C_bucket 拟合为 ≈0, 本步用一个**独立对照**复核）。

## E-A 的结论
三参数拟合给出 `C_bucket = −0.05 TB (−0.1 cyc) ≈ 0` ⇒ 「每桶成本」假设被否。
但**拟合出的 0 不等于"真的没有"** —— 也可能是"样本里桶数与条数共线, 分不开"。
⇒ 本条用一个**共线性最小的对照**复核: 同样的 n, 桶数差 100 倍。

## 做法（不需要改固件）
    div0 : n=128 ⇒ 每拍 1 桶, nrun=128
    div2 : n=128 ⇒ 每拍 100 桶, nrun=1.28
两者 nrun 差 100 倍 ⇒ 不能直接比。改用**同 nrun 不同桶数**的组合:
    div0 n=1   ⇒ 1 桶/拍,  nrun = 1     ← 组 A
    div1 n=10  ⇒ 10 桶/拍, nrun = 1     ← 组 B   （n=10, 每拍 1 条）
    div2 n=100 ⇒ 100 桶/拍,nrun = 1     ← 组 C
★ 三组的 **nrun 都 = 1**, 而每拍桶数 1 / 10 / 100。
  若「每桶花时间」, 组 C 应显著贵于组 A; 若三组**相同** ⇒ 桶开销 ≈ 0, 销案。
★ 这条对照的价值: 它把 `nrun` **钉住**, 让桶数成为唯一变量 —— 这是 E-A 的拟合做不到的。

## 判据
  B-1 三组均值差 ≤ 15 TB (30 cyc) ⇒ 桶开销可忽略（与 E-A 一致）
  B-2 若组 C 明显更贵 ⇒ E-A 的 C_bucket≈0 是"共线性导致的假零", 要重做
"""
import os, re, subprocess, sys, statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "tools", "h723_tick_ring.py")
OUT = os.path.join(HERE, "eb_out")
os.makedirs(OUT, exist_ok=True)

# (op, div, n, 期望每拍条数, 期望每拍桶数)
CASES = [
    (0, 0, 1,   1.0, 1),     # DIRECT: 1 桶
    (0, 1, 10,  1.0, 10),    # DIRECT: 10 桶
    (0, 2, 100, 1.0, 100),   # DIRECT: 100 桶
    (5, 0, 1,   1.0, 1),     # PID 同上（换个 op 复核）
    (5, 1, 10,  1.0, 10),
    (5, 2, 100, 1.0, 100),
]


def run_one(op, div, n):
    cmd = [sys.executable, TOOL, "--prog", "%d,%d,%d" % (op, div, n)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=ROOT, timeout=300)
    out = r.stdout or ""
    open(os.path.join(OUT, "op%d_div%d_n%d.log" % (op, div, n)), "w",
         encoding="utf-8").write(out)
    m = re.search(r"实测 di\s*:\s*均值 ([\d.]+) TB tick\s+标准差 ([\d.]+)", out)
    b = re.search(r"平均每拍跑 ([\d.]+) 条", out)
    ok = ("[PASS] P0a" in out) and ("[PASS] P0b" in out)
    if not (m and b):
        return None
    return dict(mean=float(m.group(1)), sd=float(m.group(2)),
                nrun=float(b.group(1)), ok=ok)


def main():
    print("=== E-B: 把 nrun 钉在 1.0, 只让「每拍桶数」变 ===")
    print("%-8s %-5s %-6s %-9s %-9s %s" % ("op", "div", "n", "实测nrun", "每拍桶数", "均值 (TB)"))
    print("-" * 62)
    got = {}
    for op, div, n, want_nrun, want_bpt in CASES:
        r = run_one(op, div, n)
        name = {0: "DIRECT", 5: "PID"}[op]
        if not r:
            print("%-8s %-5d %-6d **取数失败**" % (name, div, n)); continue
        got[(op, div)] = r
        print("%-8s %-5d %-6d %-9.2f %-9d %-9.1f %s"
              % (name, div, n, r["nrun"], want_bpt, r["mean"],
                 "" if r["ok"] else "**前置不良**"))

    print("\n=== 判据 ===")
    for op, name in ((0, "DIRECT"), (5, "PID")):
        vals = [got[(op, d)]["mean"] for d in (0, 1, 2) if (op, d) in got]
        if len(vals) < 3:
            print("  %s: 点不足" % name); continue
        rng = max(vals) - min(vals)
        print("  %-8s 1桶=%.1f  10桶=%.1f  100桶=%.1f  ⇒ 极差 %.1f TB (%.0f cyc)"
              % (name, vals[0], vals[1], vals[2], rng, rng * 2))
        print("           B-1 %s（判据 极差 ≤15 TB）"
              % ("**通过** ⇒ 桶开销可忽略, 与 E-A 一致" if rng <= 15
                 else "**未通过** ⇒ 桶数确实有影响, E-A 的 C_bucket≈0 是共线性假零"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
