#!/usr/bin/env python3
"""Read chip ID, reset source, and check for IWDG."""
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

    # MCU ID
    dbgmcu_idcode = t.read32(0x5C001000)  # DBGMCU_IDCODE
    print(f"DBGMCU_IDCODE = 0x{dbgmcu_idcode:08X}")
    print(f"  REV_ID[31:16] = 0x{(dbgmcu_idcode>>16)&0xFFFF:04X}")
    print(f"  DEV_ID[11:0]  = 0x{dbgmcu_idcode&0xFFF:03X}")

    # RCC_RSR (Reset source)
    rcc_rsr = t.read32(0x58024470)  # RCC_RSR offset 0x70? Let me check
    print(f"\nRCC_RSR (0x58024470) = 0x{rcc_rsr:08X}")

    # IWDG
    iwdg_kr = t.read32(0x58004800)   # IWDG_KR
    iwdg_sr = t.read32(0x5800480C)   # IWDG_SR
    print(f"\nIWDG_KR = 0x{iwdg_kr:08X}")
    print(f"IWDG_SR = 0x{iwdg_sr:08X}")

    # Current PC and state
    pc = t.read_core_register("pc")
    print(f"\nPC = 0x{pc:08X}")

    # PWR registers
    cr1 = t.read32(0x58024800)
    cr3 = t.read32(0x5802480C)
    print(f"\nPWR_CR1 = 0x{cr1:08X}  SVOS={(cr1>>14)&3}  ACTVOSRDY={(cr1>>13)&1}  ACTVOS={(cr1>>11)&3}")
    print(f"PWR_CR3 = 0x{cr3:08X}  LDOEN={(cr3>>1)&1}  BYPASS={cr3&1}")

    # RCC
    rcc_cr = t.read32(0x58024400)
    print(f"\nRCC_CR = 0x{rcc_cr:08X}  HSION={rcc_cr&1}  HSIRDY={(rcc_cr>>1)&1}")
