#!/usr/bin/env python3
"""Check current PLL state without modifying clocks (avoid SWD lock)."""
from pyocd.core.helpers import ConnectHelper

RCC_BASE = 0x58024400
RCC_CR       = RCC_BASE + 0x00
RCC_CFGR     = RCC_BASE + 0x10
RCC_PLLCKSELR= RCC_BASE + 0x28
RCC_PLLCFGR  = RCC_BASE + 0x2C
RCC_PLL1DIVR = RCC_BASE + 0x30

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    cr = target.read32(RCC_CR)
    cfgr = target.read32(RCC_CFGR)
    ck = target.read32(RCC_PLLCKSELR)
    div1 = target.read32(RCC_PLL1DIVR)
    pllcf = target.read32(RCC_PLLCFGR)

    print(f"RCC_CR        = 0x{cr:08X}")
    print(f"  HSION={cr&1} PLL1ON={(cr>>24)&1} PLL1RDY={(cr>>25)&1}")
    print(f"RCC_CFGR      = 0x{cfgr:08X}")
    print(f"  SW={cfgr&7} SWS={(cfgr>>3)&7}  (0=HSI 1=HSE 2=PLL1)")
    print(f"RCC_PLLCKSELR = 0x{ck:08X}")
    print(f"  SRC={ck&3}  DIVM1={(ck>>4)&0x3F}  DIVM2={(ck>>12)&0x3F}")
    print(f"RCC_PLL1DIVR  = 0x{div1:08X}")
    print(f"  DIVN_reg={(div1&0xFF)}  actual_mul={(div1&0xFF)+1}")
    print(f"  DIVP_reg={((div1>>9)&0x7F)}  DIVQ_reg={((div1>>16)&0x7F)}")
    print(f"RCC_PLLCFGR   = 0x{pllcf:08X}")
    print(f"  VCOSEL={pllcf&1}  DIVP1EN={(pllcf>>16)&1}")

    # Compute VCO and sysclk
    src_freq = 64000000  # HSI
    divm = (ck >> 4) & 0x3F
    if divm == 0: divm = 1
    pll_in = src_freq // divm
    divn_reg = div1 & 0xFF
    actual_mul = divn_reg + 1
    vco = pll_in * actual_mul
    divp_reg = (div1 >> 9) & 0x7F
    actual_divp = divp_reg + 1 if divp_reg > 0 else 1
    sysclk = vco // actual_divp
    print(f"\n=== Computed clocks ===")
    print(f"  PLL input: {pll_in/1e6:.1f} MHz (HSI/{divm})")
    print(f"  VCO: {vco/1e6:.1f} MHz (×{actual_mul})")
    print(f"  SYSCLK: {sysclk/1e6:.1f} MHz (÷{actual_divp})")
