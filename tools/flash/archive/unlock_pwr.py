#!/usr/bin/env python3
"""Exhaustive search for PWR_CR3 unlock mechanism on STM32H723."""
PROBE = "00000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper
import time

def test_pwr_cr3_writable(t):
    """Test if PWR_CR3 is currently writable."""
    t.write32(0x58024808, 0x00000080)  # Try SCUEN (bit 7)
    val = t.read32(0x58024808)
    return val != 0

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.reset()
    time.sleep(0.05)
    t.halt()

    print(f"PWR_CR3 after reset = 0x{t.read32(0x58024808):08X}")
    print(f"PWR_CR1 after reset = 0x{t.read32(0x58024800):08X}")

    # Test 1: Try DBP bit in PWR_CR1 (bit 8) - might unlock backup/PWR domain
    print(f"\n=== Test 1: DBP bit (PWR_CR1[8]) ===")
    cr1 = t.read32(0x58024800)
    t.write32(0x58024800, cr1 | (1 << 8))  # Set DBP
    time.sleep(0.01)
    print(f"  PWR_CR1 after DBP=1: 0x{t.read32(0x58024800):08X}")
    print(f"  PWR_CR3 writable? {test_pwr_cr3_writable(t)}")
    t.write32(0x58024800, cr1)  # restore

    # Test 2: Try setting PWR_CR1 bit 16 (AVDEN) or bit 17 (ALVREN)
    print(f"\n=== Test 2: Try PWR_CR1 higher bits ===")
    for bit in [16, 17, 18, 19]:
        cr1 = t.read32(0x58024800)
        t.write32(0x58024800, cr1 | (1 << bit))
        time.sleep(0.005)
        val = t.read32(0x58024800)
        if val & (1 << bit):
            print(f"  PWR_CR1 bit {bit} set, checking CR3...")
            print(f"  PWR_CR3 writable? {test_pwr_cr3_writable(t)}")
        t.write32(0x58024800, cr1)

    # Test 3: Exhaustive scan of ALL RCC registers - try every bit
    print(f"\n=== Test 3: Exhaustive RCC register scan ===")
    RCC_BASE = 0x58024400
    
    # Try all reasonable RCC offsets (0x000 to 0x1FF)
    found = False
    for reg_offset in range(0x000, 0x200, 4):
        addr = RCC_BASE + reg_offset
        try:
            orig = t.read32(addr)
        except:
            continue
        if orig == 0xFFFFFFFF:  # invalid/unmapped
            continue
        
        # Try each bit in the register
        for bit in range(32):
            if orig & (1 << bit):
                continue  # Already set, skip
            t.write32(addr, orig | (1 << bit))
            time.sleep(0.001)
            
            # Quick check - write and read PWR_CR3
            t.write32(0x58024808, 0x00000080)
            cr3 = t.read32(0x58024808)
            
            if cr3 != 0:
                print(f"  FOUND! RCC+0x{reg_offset:03X} bit {bit} unlocks PWR_CR3!")
                print(f"  Register was 0x{orig:08X}, now 0x{t.read32(addr):08X}")
                print(f"  PWR_CR3 = 0x{cr3:08X}")
                found = True
                # Don't restore - leave enabled
            
            t.write32(addr, orig)  # Restore
        
        if found:
            break

    if not found:
        print("  No RCC register/bit found that unlocks PWR_CR3")
    
    # Test 4: Check if PWR_CR3 is just ALWAYS zero on this chip
    # Maybe the supply is determined entirely by option bytes
    print(f"\n=== Test 4: Option bytes analysis ===")
    optcr = t.read32(0x52002020)  # FLASH_OPTR
    print(f"  FLASH_OPTR = 0x{optcr:08X}")
    # Decode relevant bits for supply config
    print(f"  Bit 4 (IWDG_SW)     = {(optcr >> 4) & 1}")  # Maybe wrong bit positions
    print(f"  Bit 6 (nRST_STOP)   = {(optcr >> 6) & 1}")
    print(f"  Bit 7 (nRST_STDBY)  = {(optcr >> 7) & 1}")
    print(f"  Bit 12 (BOR_EN)     = {(optcr >> 12) & 1}")
    print(f"  Bits[15:13] (BOR_LEV) = {(optcr >> 13) & 7}")
    
    # Try reading OPTR2 or other option byte registers
    for addr, name in [(0x52002024, "OPTR2"), (0x52002028, "OPTR3"),
                        (0x52002014, "PCROP1SR"), (0x52002018, "PCROP1ER"),
                        (0x5200202C, "WRP1AR"), (0x52002030, "WRP1BR")]:
        try:
            val = t.read32(addr)
            print(f"  FLASH_{name} = 0x{val:08X}")
        except:
            pass
    
    # Test 5: Check if we can read PWR_CR3 content via different access width or method
    print(f"\n=== Test 5: Different access methods ===")
    print(f"  Read32 PWR_CR3: 0x{t.read32(0x58024808):08X}")
    try:
        print(f"  Read16 PWR_CR3: 0x{t.read16(0x58024808):04X}")
    except:
        print(f"  Read16 PWR_CR3: error")
    try:
        print(f"  Read8  PWR_CR3: 0x{t.read8(0x58024808):02X}")
    except:
        print(f"  Read8  PWR_CR3: error")
    
    # Check if there's a PWR unlock register we missed
    print(f"\n=== Test 6: Full PWR register space scan ===")
    for offset in range(0x00, 0x40, 4):
        addr = 0x58024800 + offset
        val = t.read32(addr)
        print(f"  PWR+0x{offset:02X} (0x{addr:08X}) = 0x{val:08X}")
