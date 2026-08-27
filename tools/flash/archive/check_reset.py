#!/usr/bin/env python3
"""Read actual reset source from correct RCC_RSR address."""
import sys
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

PROBE = "000000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.halt()

    # RCC_RSR at 0x580244D0 for STM32H723
    rcc_rsr = t.read32(0x580244D0)
    print(f"RCC_RSR (0x580244D0) = 0x{rcc_rsr:08X}")
    if rcc_rsr != 0:
        if rcc_rsr & (1 << 31): print("  RSTF[31] = 1")
        if rcc_rsr & (1 << 30): print("  BOR_RSTF[30] = 1")
        if rcc_rsr & (1 << 29): print("  PIN_RSTF[29] = 1")
        if rcc_rsr & (1 << 28): print("  POR_RSTF[28] = 1")
        if rcc_rsr & (1 << 27): print("  SFTRSTF[27] = 1")
        if rcc_rsr & (1 << 26): print("  IWDG1RSTF[26] = 1")
        if rcc_rsr & (1 << 25): print("  WWDG1RSTF[25] = 1")

    # Poll PC multiple times to check if it's moving
    print("\n═══ PC Trace (every 10ms, 20 samples) ═══")
    t.resume()
    for i in range(20):
        import time
        time.sleep(0.01)
        t.halt()
        pc = t.read_core_register("pc")
        cr1 = t.read32(0x58024800)
        print(f"  {i:2d}: PC=0x{pc:08X}  PWR_CR1=0x{cr1:08X}  SVOS={(cr1>>14)&3} ACTVOS[12:11]={(cr1>>11)&3} ACTVOSRDY={(cr1>>13)&1}")
        t.resume()
