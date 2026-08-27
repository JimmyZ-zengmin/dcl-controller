#!/usr/bin/env python3
"""
读 jitter ring buffer → 统计真实 ISR 抖动分布
===========================
流程:
  1. 读取 ring buffer (8192 个 DWT 时间戳)
  2. 计算相邻时间差 = ISR 周期
  3. 统计 均值/σ/min/max/离群

  没有 halt/采样污染,每个时间戳都是真实 ISR 入口时刻

用法:
  python tools/flash/read_jitter.py
"""
import os, sys, time, struct
import numpy as np  # pip install numpy

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'ide', 'compiler'))
from pyocd.core.helpers import ConnectHelper

REC_BUF_ADDR = 0x2000E000   # 假设与固件定义一致
REC_BUF_SIZE = 8192
DWT_HZ       = 240_000_000  # DWT TRACECLK 实测


def main():
    with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
        t = session.target

        # 读 ring buffer (halt 时读,避免 DMA/SWD 冲突)
        t.halt()
        raw = t.read_memory_block8(REC_BUF_ADDR, REC_BUF_SIZE * 4)
        t.resume()

        ts = np.array(struct.unpack(f'<{REC_BUF_SIZE}I', bytes(raw)), dtype=np.uint64)

        # 找最后一个非零 index (0 = 未写入)
        nonzero_mask = ts > 0
        if not nonzero_mask.any():
            print("Buffer empty (all zeros) — 可能未启用 record_enable")
            return

        last_idx = np.max(np.where(nonzero_mask))
        ts_valid = ts[: last_idx + 1]

        if len(ts_valid) < 2:
            print("Less than 2 samples")
            return

        print(f"有效样本: {len(ts_valid)}")

        # 计算相邻时间差 (ISR 周期)
        dt = np.diff(ts_valid.astype(np.int64))  # 处理 wrap

        # 过滤异常 (wrap 产生的负值)
        dt_valid = dt[dt > 0]
        if len(dt_valid) < 2:
            print("No valid periods")
            return

        dt_ns = dt_valid / DWT_HZ * 1e9
        dt_us = dt_ns / 1000

        mean_ns = np.mean(dt_valid)
        std_ns = np.std(dt_valid)
        min_ns = np.min(dt_valid)
        max_ns = np.max(dt_valid)

        print(f"=== ISR 周期抖动统计 ===")
        print(f"DWT 频率:       {DWT_HZ/1e6:.0f}MHz")
        print(f"样本数:         {len(dt_valid)}")
        print(f"周期 均值:      {mean_ns:.2f} ns")
        print(f"周期 σ:         {std_ns:.2f} ns")
        print(f"周期 min:       {min_ns:.2f} ns")
        print(f"周期 max:       {max_ns:.2f} ns")
        print(f"3σ:             {3*std_ns:.2f} ns (99.7% 包络)")
        print(f"占空比:         {std_ns/mean_ns*1e6:.1f} ppm")

        # P95, P99
        p95 = np.percentile(dt_valid, 95)
        p99 = np.percentile(dt_valid, 99)
        print(f"P95:            {p95:.2f} ns")
        print(f"P99:            {p99:.2f} ns")

        # 直方图
        bins = 20
        hist, edges = np.histogram(dt_valid, bins=bins)
        print(f"\n直方图:")
        max_h = max(hist)
        for i, h in enumerate(hist):
            lo = edges[i] / DWT_HZ * 1e9
            hi = edges[i + 1] / DWT_HZ * 1e9
            bar = '#' * int(h / max_h * 40)
            print(f"  {lo:7.1f}-{hi:7.1f} ns | {h:5d} {bar}")

        # 离群 (>3σ)
        outliers = dt_valid[np.abs(dt_valid - mean_ns) > 3 * std_ns]
        print(f"\n离群 (>3σ): {len(outliers)} 个")
        if len(outliers) > 0:
            print(f"  离群值: {outliers[:10]} ns")


if __name__ == "__main__":
    main()
