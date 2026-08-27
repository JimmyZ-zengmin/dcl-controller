#!/usr/bin/env python3
"""Debug PWR registers to confirm LDO state."""
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

    cr1  = t.read32(0x58024800)
    cr2  = t.read32(0x58024804)
    cr3  = t.read32(0x5802480C)
    d3cr = t.read32(0x58024818)

    print("═══ PWR Registers ═══")
    print(f"PWR_CR1  = 0x{cr1:08X}")
    print(f"  SVOS[15:14]    = {(cr1>>14)&3}")
    print(f"  ACTVOSRDY[13]  = {(cr1>>13)&1}")
    print(f"  ACTVOS[12:11]  = {(cr1>>11)&3}")
    print(f"  reserved[31:16] = 0x{(cr1>>16)&0xFFFF:04X}")

    print(f"\nPWR_CR2  = 0x{cr2:08X}")
    print(f"PWR_CR3  = 0x{cr3:08X}")
    print(f"  LDOEN[1]       = {(cr3>>1)&1}")
    print(f"  BYPASS[0]      = {cr3&1}")
    print(f"PWR_D3CR = 0x{d3cr:08X}")

    pc = t.read_core_register("pc")
    print(f"\nPC = 0x{pc:08X}")

    if not ((cr3 >> 1) & 1):
        print("\n  ✗ BUG: LDO is DISABLED (LDOEN=0)!")
        print("  → ACTVOSRDY will never be 1 without LDO enabled")
