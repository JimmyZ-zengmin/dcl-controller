#!/usr/bin/env python3
"""Verify flash literal pool matches ELF - check if new firmware is flashed."""
PROBE = "00000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper

# Literal pool addresses for SystemInit (from ELF objdump)
LITPOOL_BASE = 0x08002048
LITPOOL_SIZE = 40  # 10 words

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.halt()

    # Read literal pool from flash
    print(f"Flash literal pool @ 0x{LITPOOL_BASE:08X}:")
    for offset in range(0, LITPOOL_SIZE, 4):
        addr = LITPOOL_BASE + offset
        val = t.read32(addr)
        # Decode known addresses
        label = ""
        if val == 0xE000ED88:
            label = " = CPACR"
        elif val == 0x58024808:
            label = " = PWR_CR3 (NEW/correct)"
        elif val == 0x5802480C:
            label = " = PWR_CSR1 (OLD/wrong!)"
        elif val == 0x58024800:
            label = " = PWR_CR1"
        elif val == 0x58024400:
            label = " = RCC_CR"
        elif val == 0x58024428:
            label = " = RCC_PLLCKSELR"
        elif val == 0x58024430:
            label = " = RCC_PLL1DIVR"
        print(f"  0x{addr:08X}: 0x{val:08X}{label}")

    # Also read PWR registers
    print(f"\nPWR registers:")
    print(f"  PWR_CR1  (0x58024800) = 0x{t.read32(0x58024800):08X}")
    print(f"  PWR_CR3  (0x58024808) = 0x{t.read32(0x58024808):08X}")
    print(f"  PWR_CSR1 (0x5802480C) = 0x{t.read32(0x5802480C):08X}")
    
    pc = t.read_core_register("pc")
    print(f"\nPC = 0x{pc:08X}")
