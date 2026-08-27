#!/usr/bin/env python3
"""Wait 400ms then check CPU state (PC, clock regs, DMA)."""
import sys, time
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

PROBE = "000000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    # No halt yet — check if it's still running
    pc = t.read_core_register("pc")
    print(f"PC (live) = 0x{pc:08X}")

    t.halt()
    pc = t.read_core_register("pc")
    rcc_cr = t.read32(0x58024400)
    rcc_cfgr = t.read32(0x58024410)
    cr1 = t.read32(0x58024800)
    d3cr = t.read32(0x58024818)
    ahb1 = t.read32(0x580244D8)

    print(f"\n═══ After 200ms run (halted) ═══")
    print(f"PC            = 0x{pc:08X}")
    print(f"RCC_CR        = 0x{rcc_cr:08X}   HSION={rcc_cr&1}  HSIRDY={(rcc_cr>>1)&1}  PLLON={(rcc_cr>>24)&1}  PLLRDY={(rcc_cr>>25)&1}")
    print(f"RCC_CFGR      = 0x{rcc_cfgr:08X}   SW[1:0]={rcc_cfgr&3}  SWS[3:2]={(rcc_cfgr>>2)&3}")
    print(f"PWR_CR1       = 0x{cr1:08X}   SVOS[15:14]={(cr1>>14)&3}  ACTVOSRDY[13]={(cr1>>13)&1}")
    print(f"PWR_D3CR      = 0x{d3cr:08X}")
    print(f"RCC_AHB1ENR   = 0x{ahb1:08X}")

    if pc > 0x08003000:
        print("\n  ✓ CPU 已进入 main() 区域")
    elif pc > 0x08002938 and pc < 0x08003000:
        print("\n  ? CPU 在 library/init 代码区")
    elif pc == 0x08002938:
        print("\n  ✗ CPU 仍在 Reset_Handler — 可能有 reset 循环")

    t.resume()
