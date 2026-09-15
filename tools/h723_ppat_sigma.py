#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_ppat_sigma.py — 读固件累積的写入间隔 σ（0x39 op=16）

为什么不用 LA 测这个
--------------------
本项目此前的抖动判据是 LA 测引脚，但 LA 有两个硬伤：
  · 采样网格 62.5 ns（16 MS/s）⇒ 量化 σ 就有 25 ns，比要测的抖动还大
  · 档间 σ 波动 40 ns（同配置 8 档 52~91 ns）⇒ 幅度判据不可复现
固件侧用 `DWT_CYCCNT`（2.5 ns 分辨率）累積 `Σe` 与 `Σe²`（e = d − 40000），
可以给出**真正可复现**的 σ。

★ 口径（务必记住，否则会读错）
  · `DCL_DO_LATCH=0`（CPU 直写档）：这次写**就是写引脚** ⇒ 本 σ = **引脚写入抖动**
  · `DCL_DO_LATCH=1`（影子+MDMA 锁存）：这次写只写**影子** ⇒ 本 σ = 写影子抖动；
    引脚还要叠加 MDMA 采样环节（实测 `PE0 − PA8` = 125 ns ± 45 ns，见 ASSESS 文档 §P0-2）
"""
import math
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from h723_client import Dcl   # noqa: E402

C = 0x39


def read_sigma(d):
    sts, p = d.send(C, bytes([0x10]))
    if sts != "ACK" or len(p) < 32:
        return None
    w = struct.unpack("<8I", p[:32])
    n = w[0]
    s64 = w[2] | (w[3] << 32)
    q64 = w[4] | (w[5] << 32)
    if n == 0:
        return dict(n=0)
    # 补码还原
    if s64 >= (1 << 63):
        s64 -= (1 << 64)
    mean = s64 / n
    var = q64 / n - mean * mean
    return dict(n=n, mean_cyc=mean, sigma_cyc=math.sqrt(var) if var > 0 else 0.0,
                pmin=w[6], pmax=w[7])


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM18"
    label = sys.argv[2] if len(sys.argv) > 2 else "?"
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0
    d = Dcl(port, wait=0.5)
    try:
        d.send(C, bytes([0x07]))     # 重新校时
        d.send(C, bytes([0x11]))     # 引擎 RUN
        time.sleep(0.2)
        print("== %s ==  (固件侧 σ, DWT 2.5ns 分辨率)" % label)
        for k in range(2):
            d.send(C, bytes([0x01]))  # 开诊断 + 清 σ
            time.sleep(secs)
            r = read_sigma(d)
            if r is None or r["n"] == 0:
                print("   第%d轮: 无样本 (诊断没在跑?)" % (k + 1))
                continue
            print("   第%d轮: n=%d  均值偏差 %+.1f cyc (±0.1ns)  **σ = %.1f cyc = %.1f ns**"
                  "   min/max=%d/%d (极差 %d cyc = %.0f ns)"
                  % (k + 1, r["n"], r["mean_cyc"], r["sigma_cyc"], r["sigma_cyc"] * 2.5,
                     r["pmin"], r["pmax"], r["pmax"] - r["pmin"], (r["pmax"] - r["pmin"]) * 2.5))
    finally:
        d.close()


if __name__ == "__main__":
    main()
