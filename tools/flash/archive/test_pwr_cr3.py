#!/usr/bin/env python3
"""Test PWR_CR3 bit writability to find the correct LDOEN/SMPSEN position."""
PROBE = "00000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper
import time

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    
    # Reset first
    t.reset()
    time.sleep(0.1)
    t.halt()
    
    print("=== PWR_CR3 bit writability test ===")
    cr3_init = t.read32(0x58024808)
    print(f"PWR_CR3 initial = 0x{cr3_init:08X}")
    
    # Try writing all 1s
    t.write32(0x58024808, 0xFFFFFFFF)
    cr3_all = t.read32(0x58024808)
    print(f"PWR_CR3 after 0xFFFFFFFF write = 0x{cr3_all:08X}")
    print(f"  Writable bits = 0x{cr3_all:08X}")
    
    # Reset writable bits back to 0
    t.write32(0x58024808, 0x00000000)
    cr3_zero = t.read32(0x58024808)
    print(f"PWR_CR3 after clear = 0x{cr3_zero:08X}")
    
    # Try individual bits
    print(f"\n=== Individual bit tests ===")
    for bit in range(10):
        addr = 0x58024808
        t.write32(addr, 1 << bit)
        val = t.read32(addr)
        accepted = "YES" if (val & (1 << bit)) else "NO"
        print(f"  Bit {bit}: write (1<<{bit})=0x{1<<bit:08X} -> read=0x{val:08X} -> {accepted}")
        t.write32(addr, 0)  # clear
    
    # Also check option bytes for supply config
    print(f"\n=== Flash Option Bytes ===")
    # FLASH_OPTR at 0x52002020 (for H723)
    optr = t.read32(0x52002020)
    print(f"FLASH_OPTR = 0x{optr:08X}")
    print(f"  Supply config bits:")
    print(f"    nRST_STOP   = {(optr >> 6) & 1}")
    print(f"    nRST_STDBY  = {(optr >> 7) & 1}")
    
    # Check bank 1 OPTR
    optr1 = t.read32(0x52002000 + 0x00)
    print(f"FLASH_OPTR (bank1) @ 0x52002000 = 0x{optr1:08X}")
    
    # Check PWR_CR1, PWR_CR2, PWR_CR3, PWR_CPUCR
    print(f"\n=== Full PWR register dump ===")
    pwr_base = 0x58024800
    regs = {
        0x00: "CR1", 0x04: "CR2", 0x08: "CR3", 0x0C: "CSR1",
        0x10: "CSR2", 0x14: "CR4", 0x18: "D3CR/SR",
    }
    for offset, name in regs.items():
        val = t.read32(pwr_base + offset)
        print(f"  PWR_{name:8s} (0x{pwr_base+offset:08X}) = 0x{val:08X}")
    
    # Check RCC_AHB4ENR or similar for PWR clock enable
    print(f"\n=== RCC APB clock enable for PWR ===")
    # RCC_AHB4ENR at 0x580244E0
    ahb4enr = t.read32(0x580244E0)
    print(f"RCC_AHB4ENR = 0x{ahb4enr:08X}")
    print(f"  PWR clock enable (bit 4) = {(ahb4enr >> 4) & 1}")
    
    # RCC_D3CFGR or RCC_BOOTCR for boot config
    print(f"\n=== RCC additional registers ===")
    for addr, name in [(0x58024400, "CR"), (0x58024410, "CFGR"), 
                        (0x58024428, "PLLCKSELR"), (0x58024444, "D3CFGR"),
                        (0x58024448, "D1CFGR"), (0x5802444C, "D2CFGR"),
                        (0x58024450, "D3CFGR_alt")]:
        val = t.read32(addr)
        print(f"  RCC_{name:12s} (0x{addr:08X}) = 0x{val:08X}")
