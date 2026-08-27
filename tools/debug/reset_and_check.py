#!/usr/bin/env python3
"""
DCL Controller 复位并检查
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
        
        print("=" * 60)
        print("1. 复位并 halt...")
        print("=" * 60)
        
        # 复位并 halt
        core.reset_and_halt()
        time.sleep(0.1)
        
        pc = core.read_core_register('pc')
        print(f"  PC = 0x{pc:08X}")
        
        # 读取向量表
        vt0 = read32(core, 0x08000000)
        vt1 = read32(core, 0x08000004)
        print(f"  向量表[0] (初始SP) = 0x{vt0:08X}")
        print(f"  向量表[1] (Reset_Handler) = 0x{vt1:08X}")
        
        # 检查复位后 RCC
        rcc_cr = read32(core, 0x58024400)
        rcc_cfgr = read32(core, 0x58024410)
        print(f"  RCC_CR = 0x{rcc_cr:08X}")
        print(f"  RCC_CFGR = 0x{rcc_cfgr:08X}")
        
        print("\n" + "=" * 60)
        print("2. 运行并连续监测 ISR...")
        print("=" * 60)
        
        # 开始运行
        core.resume()
        
        # 连续采样
        print("\n连续采样 8 次，每次间隔 0.2s:")
        print("-" * 80)
        print(f"{'#':>3} | {'SAMPLES':>10} | {'HEARTBEAT':>10} | {'WIRE[0]':>10} | {'WIRE[1]':>10} | {'TIM1_CNT':>10}")
        print("-" * 80)
        
        prev_samples = None
        for i in range(8):
            samples = read32(core, 0x2000000C)
            heartbeat = read32(core, 0x20000010)
            
            import struct
            wire0_data = core.read_memory_block8(0x20000300, 4)
            wire1_data = core.read_memory_block8(0x20000304, 4)
            wire0 = struct.unpack('<f', bytes(wire0_data))[0]
            wire1 = struct.unpack('<f', bytes(wire1_data))[0]
            
            tim1_cnt = read32(core, 0x40010024) & 0xFFFF
            
            delta = ""
            if prev_samples is not None:
                delta = f" (+{samples - prev_samples})"
            
            print(f"{i:>3} | {samples:>10}{delta:<8} | {heartbeat:>10} | {wire0:>10.4f} | {wire1:>10.4f} | {tim1_cnt:>10}")
            
            prev_samples = samples
            if i < 7:
                time.sleep(0.2)
        
        print("-" * 80)
        
        # 最终状态检查
        samples = read32(core, 0x2000000C)
        if samples > 0:
            print(f"\n✓ ISR 正在运行！SAMPLES = {samples}")
        else:
            print(f"\n✗ ISR 未运行 (SAMPLES = {samples})")
            
            # 检查 TIM1 和 NVIC 状态
            print("\n检查 TIM1 和 NVIC 状态:")
            core.halt()
            tim1_cr1 = read32(core, 0x40010000)
            tim1_sr = read32(core, 0x40010010)
            nvic_iser1 = read32(core, 0xE000E104)
            rcc_cr = read32(core, 0x58024400)
            
            print(f"  TIM1_CR1 = 0x{tim1_cr1:08X} (CEN={tim1_cr1&1})")
            print(f"  TIM1_SR  = 0x{tim1_sr:08X} (UIF={tim1_sr&1})")
            print(f"  NVIC_ISER1 = 0x{nvic_iser1:08X} (bit11={((nvic_iser1>>11)&1)})")
            print(f"  RCC_CR   = 0x{rcc_cr:08X} (PLL1RDY={(rcc_cr>>25)&1})")

if __name__ == '__main__':
    main()
