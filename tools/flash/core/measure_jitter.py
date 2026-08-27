#!/usr/bin/env python3
"""
抖动测量 — 不修改固件
===================
方案:
  固定 TIM1 周期 (100μs),引擎长时间跑,
  周期性采样 PERIOD_MIN/MAX + EXEC_MIN/MAX,
  采样 N 次后统计推断真实 jitter 分布。

  每次采样:
    1. halt (短暂, ~100μs)
    2. 读 PERIOD_MIN/MAX/EXEC_MIN/MAX + SAMPLES
    3. 重置 MIN=0xFFFFFFFF, MAX=0
    4. resume
    5. wait 1s

  推断:
    - 每轮 MIN~MAX 是 1s 内 (~10000 次 ISR) 的峰值
    - 收集 N 轮后,峰值的分布近似真实抖动的包络
    - σ ≈ mean(max - min) / 6


用法:
  python tools/flash/measure_jitter.py --dcl ide/compiler/samples/stress_test.dcl --samples 60
"""
import os, sys, time, struct, argparse
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'ide', 'compiler'))
from dcl_compiler import DCLCompiler
from pyocd.core.helpers import ConnectHelper

ROUTE_TABLE = 0x20001700
PARAM_TABLE = 0x20005700
N_ROUTES    = 0x200000F0
SAMPLES     = 0x20000010
PERIOD_MIN  = 0x20000008
PERIOD_MAX  = 0x2000000C
EXEC_MIN    = 0x20000000
EXEC_MAX    = 0x20000004
SCRATCH2    = 0x20000100
GPIOE_ODR   = 0x58021014

TIM1_ARR    = 0x4001002C  # +1 = period ticks
TIMER_HZ    = 240_000_000  # TIM1 外设时钟 (APB2 × 2)


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
    p = argparse.ArgumentParser()
    p.add_argument('--dcl', default='ide/compiler/samples/stress_test.dcl')
    p.add_argument('--samples', type=int, default=30, help='采样次数')
    p.add_argument('--interval', type=float, default=1.0, help='采样间隔(秒)')
    args = p.parse_args()

    n, route_blob, param_blob = compile_dcl(args.dcl)
    print(f"[编译] {n} 路由")
    print(f"[配置] 采样 {args.samples} 次,间隔 {args.interval}s,预计耗时 {args.samples * args.interval:.0f}s")

    with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
        t = session.target

        # ── 部署 ──
        t.reset_and_halt()
        t.write_memory_block8(ROUTE_TABLE, bytes(1024 * 16))
        t.write_memory_block8(PARAM_TABLE, bytes(512 * 16))
        t.write_memory_block8(ROUTE_TABLE, bytes(route_blob))
        t.write_memory_block8(PARAM_TABLE, bytes(param_blob))
        t.write32(N_ROUTES, n)

        arr = 11999
        TIM1_HZ = 120_000_000  # TIM1 = APB2 = 120MHz (高级定时器未倍频)
        DWT_HZ = 240_000_000    # DWT_CYCCNT 实测 240MHz (TRACECLK)
        period_us = (arr + 1) / (TIM1_HZ / 1e6)  # 100μs
        print(f"[TIM1] period={period_us:.1f} μs (ARR={arr},@{TIM1_HZ/1e6:.0f}MHz)")
        print(f"[DWT]  freq={DWT_HZ/1e6:.0f}MHz (from calibration)")
        nominal_cyc = int(period_us * DWT_HZ / 1e6)  # 24000 cyc
        print(f"[PERIOD] nominal = {nominal_cyc} cyc")

        # 初始清除
        t.write32(PERIOD_MIN, 0xFFFFFFFF)
        t.write32(PERIOD_MAX, 0)
        t.write32(EXEC_MIN, 0xFFFFFFFF)
        t.write32(EXEC_MAX, 0)
        t.write32(SAMPLES, 0)
        t.resume()

        # ── 采样循环 ──
        per_mins = []
        per_maxs = []
        exec_mins = []
        exec_maxs = []

        print(f"\n[采样] 开始...")
        t_start = time.time()

        SKIP_AFTER_RESUME = 3  # 跳过每次 resume 后的前 N 个 ISR

        for i in range(args.samples):
            time.sleep(args.interval)

            t.halt()
            # 重置 timing 变量
            t.write32(PERIOD_MIN, 0xFFFFFFFF)
            t.write32(PERIOD_MAX, 0)
            t.write32(EXEC_MIN, 0xFFFFFFFF)
            t.write32(EXEC_MAX, 0)
            t.resume()

            time.sleep(SKIP_AFTER_RESUME / 10000.0)  # 等 ISR 跑 SKIP 次,丢弃

            t.halt()
            pmin = t.read32(PERIOD_MIN)
            pmax = t.read32(PERIOD_MAX)
            emin = t.read32(EXEC_MIN)
            emax = t.read32(EXEC_MAX)
            s    = t.read32(SAMPLES)
            odr  = t.read32(GPIOE_ODR)
            t.resume()

            # 过滤无效 (ISR 没有更新过 MIN)
            if pmin == 0xFFFFFFFF: pmin = pmax
            if emax == 0: emax = emin

            per_mins.append(pmin)
            per_maxs.append(pmax)
            exec_mins.append(emin)
            exec_maxs.append(emax)

            if (i + 1) % 10 == 0 or i == 0:
                print(f"  [{i+1:3d}/{args.samples}] period {pmin}~{pmax} (jit={pmax-pmin:4d}), "
                      f"exec {emin}~{emax} (jit={emax-emin:4d}), ODR=0x{odr:02X}")

        dt = time.time() - t_start
        print(f"\n[完成] {dt:.1f}s")

        # ── 统计分析 ──
        print("\n" + "=" * 70)
        print("抖动统计分析")
        print("=" * 70)

        def stats_max_only(name, maxs, freq):
            """只用 MAX (MIN 被 halt-resume 污染)"""
            nominal = int(period_us * (freq / 1e6))
            mean_max = sum(maxs) / len(maxs)
            max_max = max(maxs)
            min_max = min(maxs)
            drift_max = max_max - nominal
            drift_mean = mean_max - nominal

            print(f"\n{name}:")
            print(f"  标称:      {nominal} cyc ({period_us:.2f} μs)")
            print(f"  MAX 均值:  {mean_max:.0f} cyc = {mean_max/(freq/1e6):.3f} μs")
            print(f"  MAX 范围:  {min_max} ~ {max_max} cyc")
            print(f"  最大漂移:  +{drift_max} cyc = +{drift_max/(freq/1e6)*1000:.0f} ns")
            print(f"  均值漂移:  +{drift_mean:.0f} cyc = +{drift_mean/(freq/1e6)*1000:.0f} ns")

        def stats_full(name, mins, maxs, freq):
            """MIN、MAX 都信 (EXEC 可信)"""
            spreads = [M - m for m, M in zip(mins, maxs)]
            mean_spread = sum(spreads) / len(spreads)
            est_sigma = mean_spread / 6
            est_sigma_ns = est_sigma / (freq / 1e6) * 1000

            print(f"\n{name}:")
            print(f"  Spread μ:  {mean_spread:.1f} cyc")
            print(f"  推断 σ:    {est_sigma:.1f} cyc = {est_sigma_ns:.0f} ns")
            print(f"  3σ:        {3*est_sigma:.0f} cyc = {3*est_sigma_ns:.0f} ns")

        stats_max_only("PERIOD (只用 MAX,MIN 被污染)", per_maxs, TIMER_HZ)
        stats_full("EXEC   (MIN、MAX 都可信)", exec_mins, exec_maxs, TIMER_HZ)

        print(f"\n周期标称: {period_us:.1f} μs")


if __name__ == "__main__":
    main()
