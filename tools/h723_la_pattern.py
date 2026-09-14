#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze_la.py — 分析 LA 导出的 8 通道跳变时间戳，做三件事：
  ① 从 PE0..PE6 重建码型序列 ⇒ 核对"是否每拍 +1、有无丢拍"
  ② 由 PE0 边沿算周期与抖动 σ/极差（外部测量）
  ③ 由 PA8 算拍标志周期，交叉验证两拍关系

★ 数据格式 (Saleae export_raw_data_binary 的 digital 文件):
    magic "<SALEAE>" 8B | ... | ntr:u64@36 | ts:double[]@44  (长度 = 44 + 8*ntr)
   时间戳是**跳变时刻(秒)**，精度高于采样网格 —— 这是它比"整窗计数"准的原因。
"""
import struct
import sys
import os
import math

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_transitions(path):
    with open(path, "rb") as f:
        b = f.read()
    assert b[:8] == b"<SALEAE>", "magic 不对"
    ntr = struct.unpack_from("<Q", b, 36)[0]
    n = min(ntr, (len(b) - 44) // 8)
    ts = struct.unpack_from("<%dd" % n, b, 44)
    return list(ts)


def level_at(ts_list, t):
    """在时刻 t 的电平: 跳变次数为奇数 = 高 (假设初始低)"""
    lo, hi = 0, len(ts_list)
    while lo < hi:
        mid = (lo + hi) // 2
        if ts_list[mid] <= t:
            lo = mid + 1
        else:
            hi = mid
    return lo & 1


def stats(xs):
    n = len(xs)
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    return m, math.sqrt(var), min(xs), max(xs)


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "build/tmp/la8"
    chans = {}
    for i in range(8):
        p = os.path.join(d, "digital_%d.bin" % i)
        chans[i] = load_transitions(p)
        print("CH%d: %d 个跳变" % (i, len(chans[i])))

    t0 = chans[0][0]
    t1 = chans[0][-1]
    span = t1 - t0
    print("\n采集跨度: %.6f s  (CH0 跳变 %d 个)" % (span, len(chans[0])))

    # ---- ② CH0 边沿间隔: 周期 / 抖动 ----
    ch0 = chans[0]
    gaps = [ch0[i + 1] - ch0[i] for i in range(len(ch0) - 1)]
    if gaps:
        m, sd, lo, hi = stats(gaps)
        print("\n=== ② CH0(PE0) 边沿间隔 ===")
        print("  样本 %d  均值 %.9f s (%.4f kHz)  σ %.3f ns" %
              (len(gaps), m, 1.0 / m / 1000.0, sd * 1e9))
        print("  最小 %.6f ns  最大 %.6f ns  极差 %.3f ns" %
              (lo * 1e9, hi * 1e9, (hi - lo) * 1e9))
        print("  → 采样网格 1/12MHz = %.1f ns ⇒ 量化 σ 理论值 %.1f ns"
              % (1e9 / 12e6, 1e9 / 12e6 / math.sqrt(12)))
        # 反推真实抖动
        q = (1e9 / 12e6) / math.sqrt(12)
        if sd * 1e9 > q:
            jit = math.sqrt((sd * 1e9) ** 2 - q ** 2)
            print("  → 扣除量化后, 真实抖摆 σ ≈ %.3f ns" % jit)
        else:
            print("  → σ 已 ≤ 量化理论值 ⇒ 真实抖摆**测不出**(低于分辨率)")

    # ---- ① 码型重建与核对 ----
    print("\n=== ① 码型核对 (PE0..PE6, 在 CH0 每个上升沿采样) ===")
    edges = []
    for i, t in enumerate(ch0):
        if level_at(ch0, t) == 1 and (i == 0 or level_at(ch0, t - 1e-12) == 0):
            edges.append(t)
    pat = []
    for t in edges:
        p = 0
        for b in range(7):
            if level_at(chans[b], t + 1e-9):   # 略延后, 避开同刻竞争
                p |= (1 << b)
        pat.append(p)

    if len(pat) >= 4:
        print("  重建 %d 个码型样本:" % len(pat))
        print("  前 24 个: %s" % " ".join(str(x) for x in pat[:24]))
        inc = sum(1 for i in range(len(pat) - 1) if (pat[i + 1] - pat[i]) % 128 == 1)
        bad = len(pat) - 1 - inc
        print("\n  相邻 +1 的个数: %d / %d   异常: %d" % (inc, len(pat) - 1, bad))
        if bad == 0:
            print("  ✓ 码型严格 +1 递增 (模 128) ⇒ **内容核对通过, 无丢拍/无跳变**")
        else:
            print("  ✗ 有 %d 处不连续 ⇒ 逐条列出:" % bad)
            shown = 0
            for i in range(len(pat) - 1):
                if (pat[i + 1] - pat[i]) % 128 != 1:
                    print("     样本 %d: %d → %d (步进 %d)" %
                          (i, pat[i], pat[i + 1], (pat[i + 1] - pat[i]) % 128))
                    shown += 1
                    if shown >= 8:
                        break

    # ---- ③ CH7 (PA8) 拍标志 ----
    print("\n=== ③ CH7(PA8) 拍标志 ===")
    ch7 = chans[7]
    if len(ch7) > 2:
        g7 = [ch7[i + 1] - ch7[i] for i in range(len(ch7) - 1)]
        m, sd, lo, hi = stats(g7)
        print("  跳变 %d 个, 平均半周期 %.6f µs (即 %.4f kHz 方波)"
              % (len(ch7), m * 1e6, 1.0 / (2 * m) / 1000.0))
        print("  σ %.3f ns  极差 %.3f ns" % (sd * 1e9, (hi - lo) * 1e9))


if __name__ == "__main__":
    main()
