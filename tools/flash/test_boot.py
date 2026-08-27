#!/usr/bin/env python3
"""Run firmware and check if PLL configured correctly."""
import time
from pyocd.core.helpers import ConnectHelper

RCC_BASE = 0x58024400
RCC_CR       = RCC_BASE + 0x00
RCC_CFGR     = RCC_BASE + 0x10
RCC_PLLCKSELR= RCC_BASE + 0x28
RCC_PLLCFGR  = RCC_BASE + 0x2C
RCC_PLL1DIVR = RCC_BASE + 0x30

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    target.resume()
    print("Resumed. Waiting 200ms...")
    time.sleep(0.2)

    # Read registers (don't halt - avoid SWD issues at high speed)
    cr = target.read32(RCC_CR)
    cfgr = target.read32(RCC_CFGR)
    ck = target.read32(RCC_PLLCKSELR)
    pllcf = target.read32(RCC_PLLCFGR)
    div1 = target.read32(RCC_PLL1DIVR)

    print(f"\nRCC_CR        = 0x{cr:08X}")
    print(f"  PLL1ON={((cr>>24)&1)}  PLL1RDY={((cr>>25)&1)}")
    print(f"RCC_CFGR      = 0x{cfgr:08X}")
    print(f"  SW={cfgr&7}  SWS={((cfgr>>3)&7)}  (0=HSI, 2=PLL1)")
    print(f"RCC_PLLCKSELR = 0x{ck:08X}")
    print(f"  SRC={ck&3}  DIVM1={((ck>>4)&0x3F)}")
    print(f"RCC_PLLCFGR   = 0x{pllcf:08X}")
    print(f"  VCOSEL={pllcf&1}  DIVP1EN={((pllcf>>16)&1)}")
    print(f"RCC_PLL1DIVR  = 0x{div1:08X}")
    print(f"  DIVN_reg={(div1&0xFF)}  actual_mul={(div1&0xFF)+1}")

    # Compute clocks
    src = 64000000
    divm = ((ck >> 4) & 0x3F)
    if divm == 0: divm = 1
    pll_in = src // divm
    mul = (div1 & 0xFF) + 1
    vco = pll_in * mul
    divp = ((div1 >> 9) & 0x7F) + 1
    sysclk = vco // divp

    print(f"\nComputed: PLLin={pll_in/1e6:.0f}MHz, VCO={vco/1e6:.0f}MHz, SYSCLK={sysclk/1e6:.0f}MHz")

    if (cr >> 25) & 1:
        print("\n✅ PLL LOCKED!")
    else:
        print("\n❌ PLL NOT LOCKED")

    if ((cfgr >> 3) & 7) == 3:
        print("✅ Switched to PLL (SWS=3)")
    elif ((cfgr >> 3) & 7) == 0:
        print("❌ Still on HSI (SWS=0) - SystemInit stuck before PLL switch")
    else:
        print(f"⚠️ SWS={((cfgr>>3)&7)} - intermediate state")

    # Check GPIOE_ODR
    odr = target.read32(0x58021014)
    print(f"\nGPIOE_ODR = 0x{odr:08X}")
    if odr != 0 and odr != 0xFFFFFFFF:
        print("✅ GPIO has been written (main() reached GPIO init)")
