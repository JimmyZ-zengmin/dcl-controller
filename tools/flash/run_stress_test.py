#!/usr/bin/env python3
"""
DCL 综合压测 + 抖动测量

流程:
  1. 编译 stress_test.dcl → 二进制
  2. reset_and_halt + 部署 (ROUTE_TABLE + PARAM_TABLE + N_ROUTES)
  3. 读 SAMPLES 3 次算频率
  4. 读 PERIOD_MIN/MAX + EXEC_MIN/MAX (DWT 抖动)
  5. 读 GPIOE_ODR 看 PE2 toggle 情况
  6. 读 ACT/WIRE 值验证正确性

用法:
  python tools/flash/run_stress_test.py
  python tools/flash/run_stress_test.py --dcl mytest.dcl
"""
import os, sys, time, struct, argparse

os.chdir(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, 'ide/compiler')
from dcl_compiler import DCLCompiler
from pyocd.core.helpers import ConnectHelper

DCL_FILE = 'ide/compiler/samples/stress_test.dcl'
ROUTE_TABLE = 0x20001700
PARAM_TABLE = 0x20005700
N_ROUTES    = 0x200000F0
SAMPLES     = 0x20000010
PERIOD_MIN  = 0x20000008
PERIOD_MAX  = 0x2000000C
EXEC_MIN    = 0x20000000
EXEC_MAX    = 0x20000004
LAST_ENTRY  = 0x20000014
HEARTBEAT   = 0x20000018
ACT_BASE    = 0x20000200
WIRE_BASE   = 0x20000300
SCRATCH2    = 0x20000100
GPIOE_ODR   = 0x58021014


def compile_dcl(path):
    src = open(path, encoding='utf-8').read()
    c = DCLCompiler()
    c.parse(src)
    c.topological_sort()
    bin = c.generate_binary()
    n = bin[0] | (bin[1] << 8)
    n_p = bin[2] | (bin[3] << 8)
    off = 4
    rb = bin[off:off + n * 16]; off += n * 16
    pb = bin[off:off + n_p * 16]
    print(f"  [OK] {n} routes, {n_p} params, {len(bin)} bytes")
    return n, rb, pb, c.wire_index


def deploy(t, n_routes, route_blob, param_blob):
    t.reset_and_halt()
    t.write32(SCRATCH2, 0xDEADBEEF)
    t.write_memory_block8(ROUTE_TABLE, bytes(1024 * 16))
    t.write_memory_block8(PARAM_TABLE, bytes(512 * 16))
    t.write_memory_block8(ROUTE_TABLE, bytes(route_blob))
    t.write_memory_block8(PARAM_TABLE, bytes(param_blob))
    t.write32(N_ROUTES, n_routes)
    t.resume()


def measure(t, dur=2.0):
    """dur 秒内采样 SAMPLES 计算频率 + 读 DWT 抖动"""
    # 清除 timing 变量 (通过写默认值,ISR 会自动更新)
    t.write32(PERIOD_MIN, 0xFFFFFFFF)
    t.write32(PERIOD_MAX, 0)
    t.write32(EXEC_MIN, 0xFFFFFFFF)
    t.write32(EXEC_MAX, 0)
    time.sleep(0.2)  # 等 ISR 至少跑一次,更新 MIN/MAX

    t0 = time.time()
    s0 = t.read32(SAMPLES)
    time.sleep(dur)
    s1 = t.read32(SAMPLES)
    dt = time.time() - t0
    freq = (s1 - s0) / dt

    pmin = t.read32(PERIOD_MIN)
    pmax = t.read32(PERIOD_MAX)
    emin = t.read32(EXEC_MIN)
    emax = t.read32(EXEC_MAX)
    odr = t.read32(GPIOE_ODR)

    return {
        'samples_delta': s1 - s0,
        'duration': dt,
        'freq': freq,
        'period_min': pmin,
        'period_max': pmax,
        'exec_min': emin,
        'exec_max': emax,
        'period_jitter': pmax - pmin if pmax > pmin else 0,
        'exec_jitter': emax - emin if emax > emin else 0,
        'gpio_odr': odr,
    }


def read_wires(t, wire_index, count=16):
    off = wire_index
    data = t.read_memory_block8(WIRE_BASE + off * 4, count * 4)
    return struct.unpack(f'<{count}f', bytes(data))


def read_actuators(t, base=0, count=16):
    data = t.read_memory_block8(ACT_BASE + base * 4, count * 4)
    return struct.unpack(f'<{count}f', bytes(data))


def main():
    p = argparse.ArgumentParser(description='DCL 压测 + 抖动')
    p.add_argument('--dcl', default=DCL_FILE)
    p.add_argument('--dur', type=float, default=2.0)
    p.add_argument('--monitor', type=float, default=0)
    args = p.parse_args()

    print(f"=== DCL 压测 ===")
    print(f"  文件: {args.dcl}")

    n, rb, pb, wires = compile_dcl(args.dcl)

    with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
        t = session.target

        # ── 部署 ──
        print("\n[部署]")
        deploy(t, n, rb, pb)

        # ── 测量 ──
        wait = max(1.0, args.dur)
        print(f"\n[测量 {wait}s]")
        time.sleep(0.5)  # 等引擎稳定
        m = measure(t, wait)

        isr_hz = m['freq']
        period_us = (m['period_min'] + m['period_max']) / 2 / 120  # @120MHz
        exec_us = (m['exec_min'] + m['exec_max']) / 2 / 120
        jitter_p = m['period_jitter'] / 120
        jitter_e = m['exec_jitter'] / 120

        print(f"  SAMPLES/m      = {m['samples_delta']} / {m['duration']:.1f}s")
        print(f"  ISR freq       = {isr_hz:.0f} Hz (目标 10000)")
        print(f"  PERIOD_MIN     = {m['period_min']} cyc = {m['period_min']/120:.1f} μs")
        print(f"  PERIOD_MAX     = {m['period_max']} cyc = {m['period_max']/120:.1f} μs")
        print(f"  EXEC_MIN       = {m['exec_min']} cyc = {m['exec_min']/120:.1f} μs")
        print(f"  EXEC_MAX       = {m['exec_max']} cyc = {m['exec_max']/120:.1f} μs")
        print(f"  周期抖动        = {m['period_jitter']} cyc = {jitter_p:.2f} μs")
        print(f"  执行抖动        = {m['exec_jitter']} cyc = {jitter_e:.2f} μs")
        print(f"  GPIOE_ODR      = 0x{m['gpio_odr']:08X}")

        if 9000 < isr_hz < 11000:
            print(f"\n  [PASS] 频率正常")
        elif isr_hz == 0:
            print(f"\n  [FAIL] ISR 没在跑!")
        else:
            print(f"\n  [WARN] 频率异常")

        if m['period_jitter'] < 100:
            print(f"  [PASS] 抖动极小 (<1μs)")
        elif m['period_jitter'] < 1000:
            print(f"  [PASS] 抖动可接受 (<10μs)")
        else:
            print(f"  [WARN] 抖动较大 (>10μs)")

        # ── 读 WIRE/ACTUATOR 输出 ──
        print("\n[输出] Wire 总线:")
        wire_vals = read_wires(t, 0, 50)
        for i, v in enumerate(wire_vals):
            if abs(v) > 0.001:
                print(f"    WIRE[{i:3d}] = {v:12.4f}")

        print("\n[输出] Actuator 状态:")
        act_vals = read_actuators(t, 0, 16)
        for i, v in enumerate(act_vals):
            if abs(v) > 0.001:
                print(f"    ACT[{i:2d}] = {v:12.4f}")

        # ── 连续监控 ──
        if args.monitor > 0:
            print(f"\n[连续监控 {args.monitor}s]")
            tb = time.time()
            step = 0
            prev_s = t.read32(SAMPLES)
            while time.time() < tb + args.monitor:
                time.sleep(0.2)
                s = t.read32(SAMPLES)
                odr = t.read32(GPIOE_ODR)
                per = t.read32(PERIOD_MAX)
                emx = t.read32(EXEC_MAX)
                rate = (s - prev_s) / 0.2
                print(f"  t={time.time() - tb:5.1f}s  rate={rate:6.0f}/s  "
                      f"PERIOD={per:6d}cyc={per/120:6.1f}μs  "
                      f"EXEC_MAX={emx:5d}cyc={emx/120:5.1f}μs  "
                      f"ODR=0x{odr:08X}  PE2={1 if (odr>>2)&1 else 0}")
                prev_s = s
                step += 1


if __name__ == "__main__":
    main()
