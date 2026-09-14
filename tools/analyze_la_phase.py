#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze_trig.py — MDMA 触发源 A/B 实验的判定仪器

★ 本文件存在的理由 (2026-09-14 一次差点误判的教训):
  第一版分析只报 "PE0 的 σ" 和一张直方图。直方图里出现**间距恰好 62.5 ns 的簇**
  —— 62.5 ns = 1/16MHz, 正是那次采集的**采样网格**。
  ⇒ 先要证明"看到的抖动不是采样网格本身", 才能谈抖动。
  所以本仪器第一步永远是"**判掉量化伪影**", 而不是直接报 σ。

三个判据 (每个都能失败):
  ① 网格同一性: 所有跳变时间戳是否都落在 k·(1/fs) 上 ⇒ 是则说明时间戳被**量化**了
  ② 逐档统计: PE0/PA8 的边沿间隔 σ (稳健, 去 0.1% 极值), 并给出量化地板
  ③ 相对相位: PE0 边沿相对最近 PA8 边沿的偏移 —— **方案②如果生效, 这个偏移必须
     变成约 +50 µs (CCR4=10000 tick)**; 若仍≈0 ⇒ 触发源根本没换。
     ★ 为什么这一条最硬: 它是**形状判据**(整段平移 50µs), 不受噪声/量化影响。
     ★ 它同时是"漂移共模消除"器: 两通道同一时刻的边沿一起漂, 差里只剩真实相对延迟。
"""
import bisect
import hashlib
import math
import os
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load(path):
    with open(path, "rb") as f:
        b = f.read()
    if b[:8] != b"<SALEAE>":
        raise SystemExit("magic 不对: %s" % path)
    ntr = struct.unpack_from("<Q", b, 36)[0]
    n = min(ntr, (len(b) - 44) // 8)
    return list(struct.unpack_from("<%dd" % n, b, 44))


def sha8(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


def stats(xs):
    n = len(xs)
    m = sum(xs) / n
    return m, math.sqrt(sum((x - m) ** 2 for x in xs) / n), min(xs), max(xs)


def robust(xs, frac=0.001):
    s = sorted(xs)
    k = max(1, int(len(s) * frac))
    return stats(s[k:-k])


def detect_grid(ts):
    """反推采样网格：**候选率 + 整除性检验**。

    ★ 为什么不能用"最小相邻间隔"猜（本文件第一版就是这么错的）：
      跳变列表的最小间隔可能远大于网格（比如 PE0 每 100 µs 才跳一次，
      而其中还夹着 37 µs 的毛刺）⇒ 猜出来的"网格"是 99812 ns 这种荒唐值，
      再去判"是否整除"就必然失败，把量化事实判成"未量化"。
    ★ 正确做法：拿 Saleae 支持的档位逐个试，看**全部**时间戳是否落在 k/fs 上。
    """
    cands = [100e6, 50e6, 25e6, 24e6, 20e6, 16e6, 12.5e6, 12e6, 10e6, 8e6, 5e6, 4e6, 2e6, 1e6]
    best = None
    for fs in cands:
        g = 1.0 / fs
        good = sum(1 for t in ts[:4000] if abs(t / g - round(t / g)) < 1e-6)
        if good >= len(ts[:4000]) - 2:          # 容 2 个边界样本
            if best is None or fs > best[1]:
                best = (g, fs)
    if best is None:
        return None, False
    g, _ = best
    n = ts[:4000]
    ok = sum(1 for t in n if abs(t / g - round(t / g)) < 1e-6) >= len(n) - 2
    return g, ok


def nearest(y, t):
    i = bisect.bisect_left(y, t)
    best = None
    for k in (i - 1, i, i + 1):
        if 0 <= k < len(y):
            if best is None or abs(y[k] - t) < abs(y[best] - t):
                best = k
    return y[best] if best is not None else None


def main():
    dirs = sys.argv[1:]
    if not dirs:
        print("用法: analyze_trig.py <目录1> [目录2 ...]")
        return

    print("=" * 98)
    print("① 网格同一性 (时间戳是否被量化到采样网格上?)")
    print("=" * 98)
    grids = {}
    for d in dirs:
        t0 = load(os.path.join(d, "digital_0.bin"))
        g, ok = detect_grid(t0)
        grids[d] = g
        print("  %-24s 最小间隔 %9.4f ns (%.3f MS/s)  全部时间戳为其整数倍? %s"
              % (os.path.basename(d), g * 1e9, 1e-6 / g, "★是 ⇒ 已量化" if ok else "否"))
    print()

    print("=" * 98)
    print("② 逐档: 边沿间隔统计  (gap 越小说明该路径越确定)")
    print("=" * 98)
    for d in dirs:
        g = grids[d]
        a = load(os.path.join(d, "digital_0.bin"))
        b = load(os.path.join(d, "digital_7.bin"))
        print("  【%s】" % os.path.basename(d))
        for tag, ts in (("PE0 (MDMA锁存)", a), ("PA8 (CPU直写)", b)):
            gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
            m, sd, _, _ = stats(gaps)
            _, sdt, lot, hit = robust(gaps)
            print("     %-16s n=%5d  μ=%10.4f µs  σ(原始)%8.2f ns  σ(稳健)%7.2f ns  极差(稳健)%8.2f ns"
                  % (tag, len(gaps), m * 1e6, sd * 1e9, sdt * 1e9, (hit - lot) * 1e9))
        if g:
            print("     └ 量化地板: 网格 %.2f ns ⇒ 间隔差的量化 σ ≈ %.2f ns (小于它的差异不可信)"
                  % (g * 1e9, g / math.sqrt(12) * math.sqrt(2) * 1e9))

        # ---- ③ 相对相位 ----
        deltas = [t - nearest(b, t) for t in a]
        deltas = [x for x in deltas if x is not None]
        m, sd, lo, hi = stats(deltas)
        print("     ③ PE0 − PA8 相位:  均值 %+9.4f µs  σ %7.2f ns  范围 [%+.4f, %+.4f] µs"
              % (m * 1e6, sd * 1e9, lo * 1e6, hi * 1e6))
        buckets = {}
        for v in deltas:
            k = int(round(v / 2.5e-7))
            if -4 <= k <= 4:
                buckets[k] = buckets.get(k, 0) + 1
        for k0 in sorted(buckets):
            print("        %+6.2f~%+6.2f µs | %5d %s"
                  % (k0 * 0.25, k0 * 0.25 + 0.25, buckets[k0],
                     "#" * min(48, buckets[k0] * 48 // max(buckets.values()))))
        print()


if __name__ == "__main__":
    main()
