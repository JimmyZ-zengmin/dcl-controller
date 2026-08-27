#!/usr/bin/env python3
"""Find the correct PWR clock enable register for STM32H723."""
PROBE = "00000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper
import time

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.reset()
    time.sleep(0.1)
    t.halt()

    RCC_BASE = 0x58024400
    
    # Dump all RCC clock enable registers to find PWR clock
    print("=== RCC Clock Enable Registers ===")
    enable_regs = {
        0x0D0: "AHB1ENR",
        0x0D4: "AHB2ENR", 
        0x0D8: "AHB3ENR",
        0x0DC: "AHB4ENR",
        0x0E0: "APB1ENR",
        0x0E4: "APB2ENR",
        0x0E8: "APB3ENR",
        0x0EC: "APB4ENR",
        0x0F0: "AHB5ENR",  # some H7 variants
    }
    for offset, name in enable_regs.items():
        addr = RCC_BASE + offset
        try:
            val = t.read32(addr)
            pwr_bit = " <-- PWR?" if (name in ["AHB4ENR", "APB1ENR"] and (val >> 4) & 1) else ""
            print(f"  RCC_{name:10s} (0x{addr:08X}) = 0x{val:08X}{pwr_bit}")
        except:
            print(f"  RCC_{name:10s} (0x{addr:08X}) = <read error>")
    
    # Also try the AHB5ENR and some other possible locations
    alt_regs = {
        0x060: "D1_AHB1ENR",
        0x064: "D2_APB1ENR", 
        0x068: "D2_APB2ENR",
        0x06C: "D3_AHB1ENR",
        0x070: "D3_AHB2ENR",
    }
    print(f"\n=== Alternative RCC register offsets ===")
    for offset, name in alt_regs.items():
        addr = RCC_BASE + offset
        try:
            val = t.read32(addr)
            print(f"  RCC_{name:16s} (0x{addr:08X}) = 0x{val:08X}")
        except:
            print(f"  RCC_{name:16s} (0x{addr:08X}) = <read error>")

    # Now try to enable PWR clock at various locations and test PWR_CR3 writability
    print(f"\n=== Testing PWR clock enable at various locations ===")
    test_offsets = [0x0D0, 0x0D4, 0x0D8, 0x0DC, 0x0E0, 0x0E4, 0x0E8, 0x0EC]
    
    for offset in test_offsets:
        addr = RCC_BASE + offset
        # Save original
        orig = t.read32(addr)
        # Try setting bit 4 (common PWR enable bit)
        t.write32(addr, orig | (1 << 4))
        time.sleep(0.01)
        
        # Test if PWR_CR3 is now writable
        t.write32(0x58024808, 0x00000002)  # Try LDOEN
        cr3 = t.read32(0x58024808)
        writable = (cr3 != 0)
        
        if writable:
            print(f"  RCC offset 0x{offset:03X} bit 4 -> PWR_CR3 NOW WRITABLE! = 0x{cr3:08X}")
        else:
            print(f"  RCC offset 0x{offset:03X} bit 4 -> PWR_CR3 still 0x{cr3:08X}")
        
        # Restore
        t.write32(addr, orig)
        t.write32(0x58024808, 0x00000000)  # Clear PWR_CR3
        time.sleep(0.01)
    
    # Also try different bits in each register
    print(f"\n=== Trying all bits in AHB4ENR (0x580244DC) ===")
    ahb4_addr = RCC_BASE + 0x0DC
    orig = t.read32(ahb4_addr)
    for bit in range(32):
        t.write32(ahb4_addr, orig | (1 << bit))
        time.sleep(0.005)
        t.write32(0x58024808, 0x00000002)
        cr3 = t.read32(0x58024808)
        if cr3 != 0:
            print(f"  AHB4ENR bit {bit} -> PWR_CR3 WRITABLE! = 0x{cr3:08X}")
        t.write32(ahb4_addr, orig)
        t.write32(0x58024808, 0)
