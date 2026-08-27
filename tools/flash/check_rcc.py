#!/usr/bin/env python3
"""Check RCC registers for PLL configuration and lock status."""
from pyocd.core.helpers import ConnectHelper

RCC_BASE = 0x58024400
RCC_CR      = RCC_BASE + 0x00   # RCC clock control
RCC_CFGR    = RCC_BASE + 0x10   # Clock config
RCC_D1CCIPR = RCC_BASE + 0x138
RCC_D2CCIP1R= RCC_BASE + 0x13C
RCC_D2CCIP2R= RCC_BASE + 0x140
RCC_D1CFGR  = RCC_BASE + 0x18   # Domain 1 (AXI)
RCC_D2CFGR  = RCC_BASE + 0x1C   # Domain 2 (APB1/2)
RCC_D3CFGR  = RCC_BASE + 0x20   # Domain 3
RCC_PLLCKSELR = RCC_BASE + 0x28 # PLL source
RCC_PLLCFGR = RCC_BASE + 0x2C   # PLL config
RCC_PLL1DIVR= RCC_BASE + 0x30   # PLL1 dividers
RCC_PLL2DIVR= RCC_BASE + 0x38   # PLL2 dividers
RCC_PLL3DIVR= RCC_BASE + 0x40   # PLL3 dividers

# Offset 0x04 = RCC_PLL1FRACR (RCC_CFGR is 0x10)
RCC_PLL1FRACR = RCC_BASE + 0x34

# AHB, APB clocks
RCC_AHB1ENR = RCC_BASE + 0xD8
RCC_AHB4ENR = RCC_BASE + 0xE0
RCC_APB1LENR = RCC_BASE + 0xE8
RCC_APB1HENR = RCC_BASE + 0xEC
RCC_APB2ENR = RCC_BASE + 0xF0
RCC_APB4ENR = RCC_BASE + 0xF4

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    cr    = target.read32(RCC_CR)
    cfgr  = target.read32(RCC_CFGR)
    ck    = target.read32(RCC_PLLCKSELR)
    pllcf = target.read32(RCC_PLLCFGR)
    div1  = target.read32(RCC_PLL1DIVR)
    div2  = target.read32(RCC_PLL2DIVR)
    d1    = target.read32(RCC_D1CFGR)
    d2    = target.read32(RCC_D2CFGR)

    print("=== RCC Status ===")
    print(f"RCC_CR       = 0x{cr:08X}")
    print(f"  HSION     = {(cr>>0)&1}")
    print(f"  HSEON     = {(cr>>16)&1}")
    print(f"  PLL1ON    = {(cr>>24)&1}")
    print(f"  PLL1RDY   = {(cr>>25)&1}")
    print(f"  PLL2ON    = {(cr>>26)&1}")
    print(f"  PLL2RDY   = {(cr>>27)&1}")

    print(f"\nRCC_CFGR     = 0x{cfgr:08X}")
    print(f"  SWS (clock source) = {(cfgr>>0)&3}  (0=HSI, 1=HSE, 2=PLL1, 3=PLL2)")

    print(f"\nRCC_PLLCKSELR = 0x{ck:08X}")
    src = ck & 3
    src_names = {0: "HSI", 1: "CSI", 2: "HSE", 3: "no-clock"}
    print(f"  SRC       = {src} ({src_names.get(src, '?')})")
    print(f"  DIVM1     = {(ck>>4)&0x3F}")
    print(f"  DIVM2     = {(ck>>12)&0x3F}")

    print(f"\nRCC_PLLCFGR  = 0x{pllcf:08X}")
    print(f"  PLL1 VCOSEL = {(pllcf>>0)&1}  (0=wide 192-836MHz, 1=medium 150MHz)")
    print(f"  PLL1 FRACEN = {(pllcf>>2)&1}")
    print(f"  PLL1 VCOWID = {(pllcf>>4)&3}  (0=range1 192-432MHz, 1=range2)")

    print(f"\nRCC_PLL1DIVR = 0x{div1:08X}")
    n1 = ((div1 >> 0) & 0x1FF) + 1  # DIVN1 mul
    p1 = ((div1 >> 9) & 0x7F) + 1   # DIVP1
    q1 = ((div1 >> 16) & 0x7F) + 1  # DIVQ1
    r1 = ((div1 >> 24) & 0x7F) + 1  # DIVR1
    print(f"  PLL1 DIVN (mul) = {n1}  (Feedback)")
    print(f"  PLL1 DIVP = {p1}  (System clock out)")
    print(f"  PLL1 DIVQ = {q1}")
    print(f"  PLL1 DIVR = {r1}")

    print(f"\nRCC_PLL2DIVR = 0x{div2:08X}")

    print(f"\nRCC_D1CFGR   = 0x{d1:08X}")
    d1_hpre = (d1 >> 0) & 0xF
    d1_ppre = (d1 >> 8) & 0x7
    print(f"  D1 HPRE  (AXI) = {d1_hpre}  (prescaler)")
    print(f"  D1 PPRE  = {d1_ppre}")

    print(f"\nRCC_D2CFGR   = 0x{d2:08X}")
    d2_ppre1 = (d2 >> 4) & 0x7
    d2_ppre2 = (d2 >> 8) & 0x7
    print(f"  D2 PPRE1 (APB1) = {d2_ppre1}")
    print(f"  D2 PPRE2 (APB2) = {d2_ppre2}")

    ahb1 = target.read32(RCC_AHB1ENR)
    apb2 = target.read32(RCC_APB2ENR)
    ahb4 = target.read32(RCC_AHB4ENR)
    print(f"\nClocks:")
    print(f"  AHB1 = 0x{ahb1:08X} (DMA2EN={(ahb1>>1)&1}, DMAMUX1EN={(ahb1>>2)&1})")
    print(f"  AHB4 = 0x{ahb4:08X} (GPIOEEN={(ahb4>>4)&1})")
    print(f"  APB2 = 0x{apb2:08X} (TIM1EN={(apb2>>0)&1})")
