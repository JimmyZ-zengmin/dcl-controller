#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze_sweep.py — 复现本轮实验的**决定性表格**（相位扫掠）。

用法（在工程根目录）:
    python docs/exp-2026-09-14-mdma-trigger/analyze_sweep.py

数据格式见 tools/analyze_la_phase.py（Saleae `export_raw_data_binary` 的 digital 文件）：
    magic "<SALEAE>" 8B | ... | ntr:u64@36 | ts:double[]@44
即**跳变时刻**列表（被量化到采样网格：16 MS/s ⇒ 62.5 ns）。
"""
import bisect
import os
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))


def load(path):
    with open(path, "rb") as f:
        b = f.read()
    if b[:8] != b"<SALEAE>":
        raise SystemExit("magic 不对: %s" % path)
    n = struct.unpack_from("<Q", b, 36)[0]
    n = min(n, (len(b) - 44) // 8)
    return list(struct.unpack_from("<%dd" % n, b, 44))


def nearest(y, t):
    i = bisect.bisect_left(y, t)
    best = None
    for k in (i - 1, i, i + 1):
        if 0 <= k < len(y) and (best is None or abs(y[k] - t) < abs(y[best] - t)):
            best = k
    return y[best] if best is not None else None


def stats(xs):
    m = sum(xs) / len(xs)
    return m, (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5, min(xs), max(xs)


def gap_sigma(ts, drop=0.001):
    g = sorted(ts[i + 1] - ts[i] for i in range(len(ts) - 1))
    k = max(1, int(len(g) * drop))
    return stats(g[k:-k])[1]


CAPS = [
    ("la_trig_up",  "旧: sel=0 TIM2_UP        @16MS (方案②未生效前)"),
    ("la_trig_cc4", "旧: sel=1 CC4@50µs       @16MS (DMAMUX 改了但 DIER 没改 ⇒ 无效)"),
    ("la_UP_r1",    "新: sel=0 TIM2_UP        @16MS (修好后基线)"),
    ("la_CC4_r1",   "新: sel=1 CC4@50µs       @16MS (路由+门控都改对了)"),
    ("la_dw800",    "双写 NOP=800 (≈3.35µs)"),
    ("la_dw_r1",    "双写 NOP=1600(≈6.32µs)"),
    ("la_dw3200",   "双写 NOP=3200(≈12.28µs)"),
    ("la_nop1600",  "★ 对照: 只空转 NOP=1600 **不写**"),
]

print("=" * 96)
print("表 1 —— 抖动与相位（PE0 = MDMA 锁存输出；PA8 = CPU 在 ISR 入口直写）")
print("=" * 96)
print("  %-12s %-46s %8s %8s %11s" % ("capture", "说明", "PE0 σ", "PA8 σ", "PE0−PA8"))
for name, desc in CAPS:
    d = os.path.join(HERE, "raw", name)
    try:
        a = load(os.path.join(d, "digital_0.bin"))
        b = load(os.path.join(d, "digital_7.bin"))
    except Exception as e:
        print("  %-12s %-46s  (读失败: %s)" % (name, desc, e))
        continue
    dl = [t - nearest(b, t) for t in a]
    m = sum(dl) / len(dl)
    print("  %-12s %-46s %6.1fns %6.1fns %+8.0fns" %
          (name, desc, gap_sigma(a) * 1e9, gap_sigma(b) * 1e9, m * 1e9))

print()
print("=" * 96)
print("表 2 —— ★ 决定性扫掠：锁存时刻跟随的是「影子写入」还是「ISR 位置」")
print("=" * 96)
print("  在 ISR 里对影子做第二次写（把 bit0 取反），扫两次写之间的 NOP 轮数：")
for name, nop, wrote in (("la_UP_r1", 0, "—（只写一次）"),
                         ("la_dw800", 800, "写"),
                         ("la_dw_r1", 1600, "写"),
                         ("la_dw3200", 3200, "写"),
                         ("la_nop1600", 1600, "★ 不写（对照）")):
    d = os.path.join(HERE, "raw", name)
    a = load(os.path.join(d, "digital_0.bin"))
    b = load(os.path.join(d, "digital_7.bin"))
    dl = [t - nearest(b, t) for t in a]
    m, sd, lo, hi = stats(dl)
    print("   NOP=%-5d 第二次写=%-16s ⇒ PE0−PA8 均值 %+9.1f ns  (σ %6.1f ns, 范围 %+.2f…%+.2f µs)"
          % (nop, wrote, m * 1e9, sd * 1e9, lo * 1e6, hi * 1e6))

print()
print("⇒ 结论：NOP 轮数与相位 **线性**（≈3.9 ns/轮），而**只空转不写**的对照组相位**不动**。")
print("⇒ 锁存时刻 = CPU 最后一次写影子的时刻 + 0.12 µs。**输出引脚时刻由 CPU 决定，")
print("   硬件定时器对输出时刻没有贡献** ⇒ “计算/输出解耦、引脚由硬件锚定”当前未达成。")
