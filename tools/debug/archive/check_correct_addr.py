#!/usr/bin/env python3
"""
DCL Controller 检查 - 使用正确的 TIMING_BASE 地址
"""
import time
import struct
from pyocd.core.helpers import ConnectHelper

SERIAL = '000000805059ed5520a4400013dd0702a5a5a5a59796990e'

# TIMING_BASE @ 0x20000000
TIMING = {
    'EXEC_MIN':    0x20000000,
    'EXEC_MAX':    0x20000004,
    'PERIOD_MIN':  0x20000008,
    'PERIOD_MAX':  0x2000000C,
    'SAMPLES':     0x20000010,
    'LAST_ENTRY':  0x20000014,
    'HEARTBEAT':   0x20000018,
    'CLOCK_HZ':    0x2000001C,
    'TIMER_HZ':    0x20000020,
}

def read32(core, addr):
    return core.read_memory(addr, 32)

def main():
    with ConnectHelper.session_with_chosen_probe(
        target_override='stm32h723xx',
        connect_overwrite_unique_id=SERIAL
    ) as session:
        core = session.target.selected_core_or_raise
        
        # 复位并 halt
        core.reset_and_halt()
        time.sleep(0.05)
        
        # 清零
        core.write_memory(0x20000010, 0)  # SAMPLES
        core.write_memory(0x20000018, 0)  # HEARTBEAT
        
        print("清零后运行，连续采样 10 次:")
        print("-" * 90)
        print(f"{'#':>3} | {'SAMPLES':>12} | {'HEARTBEAT':>12} | {'PERIOD_MIN':>10} | {'PERIOD_MAX':>10} | {'WIRE[0]':>10} | {'WIRE[1]':>10}")
        print("-" * 90)
        
        core.resume()
        
        prev_samples = None
        for i in range(10):
            time.sleep(0.2)
            
            samples = read32(core, 0x20000010)
            heartbeat = read32(core, 0x20000018)
            period_min = read32(core, 0x20000008)
            period_max = read32(core, 0x2000000C)
            
            wire0_data = core.read_memory_block8(0x20000300, 4)
            wire1_data = core.read_memory_block8(0x20000304, 4)
            wire0 = struct.unpack('<f', bytes(wire0_data))[0]
            wire1 = struct.unpack('<f', bytes(wire1_data))[0]
            
            delta = ""
            if prev_samples is not None:
                diff = samples - prev_samples if samples >= prev_samples else 0
                delta = f" (+{diff})"
            
            print(f"{i:>3} | {samples:>12}{delta:<8} | {heartbeat:>12} | {period_min:>10} | {period_max:>10} | {wire0:>10.4f} | {wire1:>10.4f}")
            
            prev_samples = samples
        
        print("-" * 90)
        
        # 最终状态
        samples = read32(core, 0x20000010)
        heartbeat = read32(core, 0x20000018)
        period_min = read32(core, 0x20000008)
        period_max = read32(core, 0x2000000C)
        
        print(f"\n最终: SAMPLES={samples}, HEARTBEAT={heartbeat}")
        print(f"周期: MIN={period_min}, MAX={period_max}, 抖动={period_max-period_min} cycles")
        
        if period_max > period_min and period_min > 0:
            cycles_avg = (period_min + period_max) // 2
            period_us = cycles_avg / 136  # 假设 136MHz
            print(f"平均周期: {period_us:.1f}us")
        
        if samples > 0:
            print("\n=== ISR 正在运行! ===")
        else:
            print("\n=== ISR 未运行 ===")

if __name__ == '__main__':
    main()
