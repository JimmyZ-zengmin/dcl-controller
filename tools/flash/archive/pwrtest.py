#!/usr/bin/env python3
"""读全部 H723 PWR 寄存器 (CR1/CR3/D3CR/CSR1/WKUPCR/WKUPFR),验证 VOS 真实位置"""
import sys
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

PROBE = "000000805059ed5520a4400013dd0702a5a5a5a59796990e"

# H723 RM0468 PWR registers
PWR = {
    "PWR_CR1":   0x58024800,
    "PWR_CR2":   0x58024804,
    "PWR_CR3":   0x5802480C,
    "PWR_CPUCR": 0x58024810,
    "PWR_SVMCR": 0x58024818,
    "PWR_D3CR":  0x58024818,   # ⚠️ RM0468: VOS[17:16] 在 D3CR @ 0x58024818
    "PWR_WKUPCR":0x58024820,
    "PWR_WKUPFR":0x58024824,
    "PWR_CSR1":  0x58024814,
}

from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.halt()
    pc = t.read_core_register("pc")
    print(f"PC = 0x{pc:08X}")
    print("\nH723 PWR Registers (RM0468 §5.8):")
    for name, addr in PWR.items():
        v = t.read32(addr)
        print(f"  {name:12s} @ 0x{addr:08X} = 0x{v:08X}")

    d3cr = t.read32(0x58024818)
    print(f"\nPWR_D3CR VOS[17:16] = {(d3cr>>16)&3}")
    print("  11=VOS0 (最高), 10=VOS1, 01=VOS2, 00=VOS3 (最低)")
    print("  VOS0 才支持 544MHz; 否则 PLL 锁不上")
    t.resume()
