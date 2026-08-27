#!/usr/bin/env python3
"""
完整流程: 部署 DCL → 启用 ring buffer → 等 buffer 满 → 读 buffer → 统计抖动
===========================

零污染测量:
  - 引擎运行中,ISR 自动写 DWT 时间戳到 ring buffer
  - buffer 满后自动停止记录
  - 最后一次性 halt 读 buffer
  - 引擎全程无 halt 干扰

用法:
  python tools/flash/run_and_measure.py --dcl ide/compiler/samples/stress_test.dcl
"""
import os, sys, time, struct
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'ide', 'compiler'))
from dcl_compiler import DCLCompiler
from pyocd.core.helpers import ConnectHelper

ROUTE_TABLE  = 0x20001700
PARAM_TABLE  = 0x20005700
N_ROUTES     = 0x200000F0
SCRATCH2     = 0x20000100
REC_ENABLE   = 0x20000040  # record_enable (TIMING 之后,空闲)
REC_IDX      = 0x20000044  # record_idx (TIMING 之后,空闲)
REC_BUF_ADDR = 0x2000E000
REC_BUF_SIZE = 8192
DWT_HZ       = 240_000_000


def compile_dcl(path):
    src = open(path, encoding='utf-8').read()
    c = DCLCompiler(); c.parse(src); c.topological_sort()
    bin = c.generate_binary()
    n = bin[0] | (bin[1] << 8)
    off = 4
    rb = bin[off:off + n * 16]; off += n * 16
    pb = bin[off:off + n * 16]
    return n, rb, pb


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--dcl', default='ide/compiler/samples/stress_test.dcl')
    args = p.parse_args()

    n, rb, pb = compile_dcl(args.dcl)
    print(f"[OK] {n} routes compiled")

    with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
        t = session.target

        # ── 部署 ──
        t.reset_and_halt()
        t.write_memory_block8(ROUTE_TABLE, bytes(1024 * 16))
        t.write_memory_block8(PARAM_TABLE, bytes(512 * 16))
        t.write_memory_block8(ROUTE_TABLE, bytes(rb))
        t.write_memory_block8(PARAM_TABLE, bytes(pb))
        t.write32(N_ROUTES, n)
        # 清 ring buffer
        t.write32(REC_ENABLE, 0)
        t.write32(REC_IDX, 0)
        t.resume()
        print(f"[OK] deployed {n} routes")

        # ── 启用记录 ──
        t.write32(REC_ENABLE, 1)
        print(f"[OK] recording enabled")

        # ── 等 buffer 满 ──
        # 8192 samples @ 10kHz = ~0.8s
        wait = REC_BUF_SIZE / 10000 + 0.5
        print(f"[等待] {wait:.1f}s for buffer to fill...")
        time.sleep(wait)

        # 验证已满
        t.halt()
        idx = t.read32(REC_IDX)
        enable = t.read32(REC_ENABLE)
        t.resume()
        print(f"[状态] record_idx={idx}, record_enable={enable} (0=已满)")

        if idx < 100:
            print(f"[ERROR] buffer 未填充 (idx={idx})")
            return

        # ── 读 buffer ──
        t.halt()
        raw = t.read_memory_block8(REC_BUF_ADDR, min(idx, REC_BUF_SIZE) * 4)
        t.resume()

        ts = np.array(struct.unpack(f'<{len(raw)//4}I', bytes(raw)), dtype=np.uint64)

        # ── 统计 ──
        nonzero = ts[ts > 0]
        if len(nonzero) < 2:
            print("[ERROR] 不足 2 个样本")
            return

        dt = np.diff(nonzero.astype(np.int64))
        dt_valid = dt[dt > 0]

        if len(dt_valid) < 2:
            print("[ERROR] 无有效 period")
            return

        dt_ns = dt_valid / DWT_HZ * 1e9

        mean_ns = np.mean(dt_ns)
        std_ns = np.std(dt_ns)
        min_ns = np.min(dt_ns)
        max_ns = np.max(dt_ns)

        print(f"\n{'='*60}")
        print(f"ISR 抖动统计 (零污染测量)")
        print(f"{'='*60}")
        print(f"样本数:     {len(dt_valid)}")
        print(f"周期 均值:  {mean_ns:.1f} ns ({mean_ns/1000:.3f} μs)")
        print(f"周期 σ:     {std_ns:.1f} ns")
        print(f"周期 min:   {min_ns:.1f} ns")
        print(f"周期 max:   {max_ns:.1f} ns")
        print(f"3σ 包络:    {3*std_ns:.1f} ns")
        print(f"占空比:     {std_ns/mean_ns*1e6:.1f} ppm")
        print(f"P95:        {np.percentile(dt_ns, 95):.1f} ns")
        print(f"P99:        {np.percentile(dt_ns, 99):.1f} ns")

        # 直方图
        bins = 15
        hist, edges = np.histogram(dt_ns, bins=bins)
        max_h = max(hist) if max(hist) > 0 else 1
        print(f"\n直方图:")
        for i, h in enumerate(hist):
            lo, hi = edges[i], edges[i + 1]
            bar = '#' * int(h / max_h * 30)
            print(f"  {lo:7.1f}-{hi:7.1f} ns | {h:5d} {bar}")

        # 离群
        outliers = dt_ns[np.abs(dt_ns - mean_ns) > 3 * std_ns]
        print(f"\n离群 (>3σ): {len(outliers)} 个")
        if len(outliers) > 0:
            print(f"  示例: {outliers[:5]}")


if __name__ == "__main__":
    main()
