#!/usr/bin/env python3
"""
DCL Controller 实时检查 - 多次采样确认 ISR 状态
"""
import time
from pyocd.core.helpers import ConnectHelper

SERIAL = '000000805059ed5520a4400013dd0702a5a5a5a59796990e'

def read32(core, addr):
    return core.read_memory(addr, 32)

def main():
    with ConnectHelper.session_with_chosen_probe(
        target_override='stm32h723xx',
        connect_overwrite_unique_id=SERIAL
    ) as session:
        core = session.target.selected_core_or_raise
        
        print("连续采样 5 次，每次间隔 0.5s")
        print("-" * 80)
        print(f"{'#':>3} | {'SAMPLES':>10} | {'HEARTBEAT':>10} | {'PERIOD_MIN':>10} | {'PERIOD_MAX':>10} | {'DWT_CYCCNT':>12} | {'WIRE[0]':>10}")
        print("-" * 80)
        
        prev_samples = None
        for i in range(5):
            samples = read32(core, 0x2000000C)
            heartbeat = read32(core, 0x20000010)
            period_min = read32(core, 0x20000000)
            period_max = read32(core, 0x20000004)
            dwt = read32(core, 0xE0001004)
            
            # 读取 WIRE[0]
            wire_data = core.read_memory_block8(0x20000300, 4)
            import struct
            wire0 = struct.unpack('<f', bytes(wire_data))[0]
            
            delta = ""
            if prev_samples is not None:
                delta = f" (+{samples - prev_samples})"
            
            print(f"{i:>3} | {samples:>10}{delta:<8} | {heartbeat:>10} | {period_min:>10} | {period_max:>10} | {dwt:>12} | {wire0:>10.4f}")
            
            prev_samples = samples
            if i < 4:
                time.sleep(0.5)
        
        print("-" * 80)
        
        # 最终状态
        samples = read32(core, 0x2000000C)
        period_min = read32(core, 0x20000000)
        period_max = read32(core, 0x20000004)
        
        print(f"\n最终 SAMPLES = {samples}")
        print(f"周期: MIN={period_min} cycles, MAX={period_max} cycles")
        print(f"抖动: {period_max - period_min} cycles = {(period_max - period_min) * 7.4:.1f}ns")
        
        if period_min > 0 and period_max > 0:
            print(f"\n结论: ISR 正在运行!")
            # 计算实际频率
            cycles_avg = (period_min + period_max) // 2
            freq_mhz = 136  # 假设 136MHz
            period_us = cycles_avg / freq_mhz
            print(f"平均周期: {cycles_avg} cycles = {period_us:.1f}us")
            if 90 < period_us < 110:
                print(f"符合 100us 设定!")
            elif 0.5 < period_us < 2:
                print(f"符合 1us 设定 (高速模式)!")
        else:
            print(f"\n结论: ISR 未运行 (周期为0)")

if __name__ == '__main__':
    main()
