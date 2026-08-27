#!/usr/bin/env python3
"""Trace PC rapidly (every 5ms, 20 samples) to distinguish stuck-at-Reset vs reset-loop."""
import sys, time
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

PROBE = "000000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.resume()

    print(f"{'time':>6s}  {'PC':>12s}  {'RCC_CR':>12s}  {'PWR_CR1':>12s}")
    print("─" * 50)
    for i in range(20):
        t.halt()
        pc = t.read_core_register("pc")
        cr = t.read32(0x58024400)
        pwr = t.read32(0x58024800)
        t.resume()
        print(f"{(i*5):5d}ms  0x{pc:08X}  0x{cr:08X}  0x{pwr:08X}")
        if i < 19:
            time.sleep(0.005)

    # Final state
    time.sleep(0.100)
    t.halt()
    pc = t.read_core_register("pc")
    print(f"\nAfter 200ms free run: PC=0x{pc:08X}")

    # Dump registers
    rcc_cr = t.read32(0x58024400)
    cr1   = t.read32(0x58024800)
    cfgr  = t.read32(0x58024410)
    print(f"RCC_CR    = 0x{rcc_cr:08X}  HSION={rcc_cr&1} HSIRDY={(rcc_cr>>1)&1} PLLON={(rcc_cr>>24)&1}")
    print(f"PWR_CR1   = 0x{cr1:08X}  SVOS={(cr1>>14)&3} ACTVOSRDY={(cr1>>13)&1}")
    print(f"RCC_CFGR  = 0x{cfgr:08X}  SW[1:0]={cfgr&3} SWS={(cfgr>>2)&3}")
    t.resume()
