#!/usr/bin/env python3
"""Trace PC over 50ms to see if CPU is actually executing or stuck."""
import sys, time
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

PROBE = "00000805059ed5520a4400013dd0702a5a5a5a59796990e"
# correct full uid
PROBE = "000000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.halt()
    pc_start = t.read_core_register("pc")
    print(f"PC initial (halted) = 0x{pc_start:08X}")

    # Run for 50ms then re-halt
    t.resume()
    time.sleep(0.050)
    t.halt()
    pc_end = t.read_core_register("pc")
    print(f"PC after 50ms run    = 0x{pc_end:08X}")
    print(f"  -> {'MOVING (CPU executing)' if pc_start != pc_end else 'STUCK at Reset_Handler'}")

    # Also dump RCC_CR and PWR_CR1 at this time
    rcc_cr = t.read32(0x58024400)
    cr1   = t.read32(0x58024800)
    cfgr  = t.read32(0x58024410)  # try alternate offset
    print(f"\nAfter 50ms:")
    print(f"  RCC_CR   = 0x{rcc_cr:08X}  HSION={rcc_cr&1} HSIRDY={(rcc_cr>>1)&1}")
    print(f"  PWR_CR1  = 0x{cr1:08X}  SVOS[15:14]={(cr1>>14)&3} ACTVOSRDY={(cr1>>13)&1}")
    print(f"  RCC_CFGR @0x58024410 = 0x{cfgr:08X}")

    # Try alternate CFGR offset
    cfgr2 = t.read32(0x58024408)
    print(f"  RCC_CFGR @0x58024408 = 0x{cfgr2:08X}")
    t.resume()
