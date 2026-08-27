#!/usr/bin/env python3
"""Diagnose PWR voltage regulator issue."""
import sys, time
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

PROBE = "000000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.halt()

    print("═══ PWR Full Register Dump ═══")
    cr1  = t.read32(0x58024800)
    cr2  = t.read32(0x58024804)
    cr3  = t.read32(0x58024808)
    cr4  = t.read32(0x5802480C)
    csr1 = t.read32(0x58024810)
    csr2 = t.read32(0x58024814)
    d3cr = t.read32(0x58024818)
    wkup = t.read32(0x58024820)

    print(f"PWR_CR1  = 0x{cr1:08X}")
    print(f"  SVOS[15:14]      = {(cr1>>14)&3} ({'SVOS6/VOS0' if (cr1>>14)&3==3 else 'SVOS5/VOS1' if (cr1>>14)&3==2 else 'SVOS4/VOS3' if (cr1>>14)&3==1 else 'reserved'})")
    print(f"  ACTVOSRDY[13]    = {(cr1>>13)&1}")
    print(f"  ACTVOS[12:11]    = {(cr1>>11)&3} ({'VOS3' if (cr1>>11)&3==0 else 'VOS2' if (cr1>>11)&3==1 else 'VOS1' if (cr1>>11)&3==2 else 'VOS0'})")

    print(f"\nPWR_CR2  = 0x{cr2:08X}")
    print(f"PWR_CR3  = 0x{cr3:08X}")
    print(f"  LDOEN[1]         = {(cr3>>1)&1}")
    print(f"  BYPASS[0]        = {(cr3>>0)&1}")
    print(f"PWR_CR4  = 0x{cr4:08X}")
    print(f"PWR_CSR1 = 0x{csr1:08X}")
    print(f"PWR_CSR2 = 0x{csr2:08X}")
    print(f"PWR_D3CR = 0x{d3cr:08X}")
    print(f"PWR_WKUPCR = 0x{wkup:08X}")

    # Test: temporarily set LDO off and on to see if anything changes
    print("\n═══ Trying LDO toggle ═══")
    t.write32(0x58024808, cr3 & ~0x02)  # Disable LDO
    time.sleep(0.01)
    cr1_after = t.read32(0x58024800)
    print(f"After LDO disable: PWR_CR1=0x{cr1_after:08X} ACTVOSRDY={(cr1_after>>13)&1}")

    t.write32(0x58024808, cr3 | 0x02)   # Re-enable LDO
    time.sleep(0.01)
    cr1_after2 = t.read32(0x58024800)
    print(f"After LDO enable:  PWR_CR1=0x{cr1_after2:08X} ACTVOSRDY={(cr1_after2>>13)&1}")

    # Current PC
    pc = t.read_core_register("pc")
    print(f"\nPC = 0x{pc:08X}")

    # RCC
    rcc_cr = t.read32(0x58024400)
    print(f"RCC_CR = 0x{rcc_cr:08X} HSION={rcc_cr&1} HSIRDY={(rcc_cr>>1)&1}")
