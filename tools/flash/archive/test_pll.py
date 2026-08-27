#!/usr/bin/env python3
"""Test PLL configuration step by step."""
from pyocd.core.helpers import ConnectHelper

RCC_BASE = 0x58024400
RCC_CR       = RCC_BASE + 0x00
RCC_ICSCR    = RCC_BASE + 0x04
RCC_CRRCR    = RCC_BASE + 0x08
RCC_CFGR     = RCC_BASE + 0x10
RCC_D1CFGR   = RCC_BASE + 0x18
RCC_D2CFGR   = RCC_BASE + 0x1C
RCC_D3CFGR   = RCC_BASE + 0x20
RCC_PLLCKSELR= RCC_BASE + 0x28
RCC_PLLCFGR  = RCC_BASE + 0x2C
RCC_PLL1DIVR = RCC_BASE + 0x30
RCC_PLL1FRACR= RCC_BASE + 0x34
RCC_PLL2DIVR = RCC_BASE + 0x38

PWR_BASE = 0x58024800
PWR_CR3  = PWR_BASE + 0x0C

FLASH_BASE = 0x52002000
FLASH_ACR  = FLASH_BASE + 0x00

def read(target, addr):
    return target.read32(addr)

def write(target, addr, val):
    target.write32(addr, val)

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target
    target.halt()

    print("=== Step 0: Read current state ===")
    cr = read(target, RCC_CR)
    ck = read(target, RCC_PLLCKSELR)
    div1 = read(target, RCC_PLL1DIVR)
    pllcf = read(target, RCC_PLLCFGR)
    print(f"  RCC_CR       = 0x{cr:08X}")
    print(f"  RCC_PLLCKSELR= 0x{ck:08X}")
    print(f"  RCC_PLL1DIVR = 0x{div1:08X}")
    print(f"  RCC_PLLCFGR  = 0x{pllcf:08X}")

    print("\n=== Step 1: Set PWR_CR3 ===")
    pwr_cr3 = read(target, PWR_CR3)
    print(f"  PWR_CR3 before: 0x{pwr_cr3:08X}")
    write(target, PWR_CR3, (pwr_cr3 & ~(3 << 4)) | (0 << 4))
    pwr_cr3 = read(target, PWR_CR3)
    print(f"  PWR_CR3 after:  0x{pwr_cr3:08X}")

    print("\n=== Step 2: Disable PLL1 ===")
    cr = read(target, RCC_CR)
    cr &= ~(1 << 24)
    write(target, RCC_CR, cr)
    cr = read(target, RCC_CR)
    print(f"  RCC_CR: 0x{cr:08X}  PLL1ON={(cr>>24)&1}  PLL1RDY={(cr>>25)&1}")

    print("\n=== Step 3: Configure PLLCKSELR (DIVM=4, HSI) ===")
    write(target, RCC_PLLCKSELR, (0 << 0) | (4 << 4))
    ck = read(target, RCC_PLLCKSELR)
    print(f"  RCC_PLLCKSELR = 0x{ck:08X}  SRC={ck&3}  DIVM1={(ck>>4)&0x3F}")

    print("\n=== Step 4: Configure PLL1DIVR (DIVN=34) ===")
    write(target, RCC_PLL1DIVR, (0 << 24) | (0 << 16) | (0 << 9) | (34 << 0))
    div1 = read(target, RCC_PLL1DIVR)
    print(f"  RCC_PLL1DIVR = 0x{div1:08X}")
    print(f"    DIVN={(div1&0x1FF)+1}  DIVP={((div1>>9)&0x7F)+1}  DIVQ={((div1>>16)&0x7F)+1}")

    print("\n=== Step 5: Configure PLLCFGR (VCOSEL=0 wide 192-836MHz, DIVP1EN=1) ===")
    write(target, RCC_PLLCFGR, (1 << 16))
    pllcf = read(target, RCC_PLLCFGR)
    print(f"  RCC_PLLCFGR = 0x{pllcf:08X}  VCOSEL={pllcf&1}  DIVP1EN={(pllcf>>16)&1}")

    print("\n=== Step 6: Enable PLL1 ===")
    cr = read(target, RCC_CR)
    cr |= (1 << 24)
    write(target, RCC_CR, cr)
    import time
    for i in range(100):
        cr = read(target, RCC_CR)
        rdy = (cr >> 25) & 1
        if rdy:
            print(f"  PLL locked after {(i+1)*0.001:.3f}ms!")
            break
        time.sleep(0.001)
    else:
        print(f"  PLL NOT locked!")
    print(f"  RCC_CR: 0x{cr:08X}  PLL1ON={(cr>>24)&1}  PLL1RDY={(cr>>25)&1}")

    print("\n=== Step 7: Flash wait states ===")
    flash = read(target, FLASH_ACR)
    print(f"  FLASH_ACR before: 0x{flash:08X}")
    write(target, FLASH_ACR, 0x0504)  # 5 WS, 4 for 544MHz
    flash = read(target, FLASH_ACR)
    print(f"  FLASH_ACR after:  0x{flash:08X}")

    print("\n=== Step 8: Prescalers ===")
    d1 = read(target, RCC_D1CFGR)
    write(target, RCC_D1CFGR, (d1 & ~0xF) | (8 << 0))  # D1CPRE=/8 AXI=68MHz
    d1 = read(target, RCC_D1CFGR)
    print(f"  D1CFGR: 0x{d1:08X}")

    print("\n=== Step 9: Switch to PLL ===")
    write(target, RCC_CFGR, 0x03)
    cfgr = read(target, RCC_CFGR)
    for i in range(100):
        cfgr = read(target, RCC_CFGR)
        sws = (cfgr >> 3) & 7
        if sws == 3:
            print(f"  Switched to PLL after {(i+1)*0.001:.3f}ms!")
            break
        time.sleep(0.001)
    else:
        print(f"  Switch FAILED! SWS={sws}")
    print(f"  RCC_CFGR: 0x{cfgr:08X}  SW={cfgr&7}  SWS={(cfgr>>3)&7}")

    cr = read(target, RCC_CR)
    print(f"\n=== Final ===")
    print(f"  Clock source: {'PLL' if (cfgr>>3)&3==3 else 'HSI'}")
    print(f"  PLL1RDY: {(cr>>25)&1}")
