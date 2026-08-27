#!/usr/bin/env python3
"""Read DMA2 Stream 5 registers from correct addresses."""
import sys

from pyocd.core.helpers import ConnectHelper

DMA2_BASE = 0x40020400
# Stream 5 registers (spacing 0x18 per stream)
S5CR   = DMA2_BASE + 0x78
S5NDTR = DMA2_BASE + 0x7C
S5PAR  = DMA2_BASE + 0x80
S5M0AR = DMA2_BASE + 0x84
S5FCR  = DMA2_BASE + 0x8C

# Also read Stream 1 for comparison
S1CR   = DMA2_BASE + 0x18
S1NDTR = DMA2_BASE + 0x1C
S1PAR  = DMA2_BASE + 0x20
S1M0AR = DMA2_BASE + 0x24
S1FCR  = DMA2_BASE + 0x2C

# GPIOE_ODR
GPIOE_ODR = 0x58021014

# SHADOW_GPIO in DTCM
SHADOW_GPIO = 0x200000E0
ADC_RAW     = 0x200000F0

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    print("=== DMA2 Stream 5 (SHADOW_GPIO -> GPIOE_ODR) ===")
    cr   = target.read32(S5CR)
    ndtr = target.read32(S5NDTR)
    par  = target.read32(S5PAR)
    m0ar = target.read32(S5M0AR)
    fcr  = target.read32(S5FCR)
    print(f"  S5CR   (0x{S5CR:08X}) = 0x{cr:08X}  EN={cr&1}  CIRC={(cr>>8)&1}  DIR={(cr>>6)&3}")
    print(f"  S5NDTR (0x{S5NDTR:08X}) = 0x{ndtr:08X}  ({ndtr})")
    print(f"  S5PAR  (0x{S5PAR:08X}) = 0x{par:08X}  (expect 0x{GPIOE_ODR:08X})")
    print(f"  S5M0AR (0x{S5M0AR:08X}) = 0x{m0ar:08X}  (expect 0x{SHADOW_GPIO:08X})")
    print(f"  S5FCR  (0x{S5FCR:08X}) = 0x{fcr:08X}")

    print("\n=== DMA2 Stream 1 (ADC1_DR -> ADC_RAW) ===")
    cr   = target.read32(S1CR)
    ndtr = target.read32(S1NDTR)
    par  = target.read32(S1PAR)
    m0ar = target.read32(S1M0AR)
    fcr  = target.read32(S1FCR)
    print(f"  S1CR   = 0x{cr:08X}  EN={cr&1}")
    print(f"  S1NDTR = 0x{ndtr:08X}")
    print(f"  S1PAR  = 0x{par:08X}  (expect 0x58021040)")
    print(f"  S1M0AR = 0x{m0ar:08X}  (expect 0x{ADC_RAW:08X})")
    print(f"  S1FCR  = 0x{fcr:08X}")

    print("\n=== Memory ===")
    shadow = target.read32(SHADOW_GPIO)
    adc   = target.read32(ADC_RAW)
    odr   = target.read32(GPIOE_ODR)
    print(f"  SHADOW_GPIO (0x{SHADOW_GPIO:08X}) = 0x{shadow:08X}")
    print(f"  ADC_RAW     (0x{ADC_RAW:08X}) = 0x{adc:08X}")
    print(f"  GPIOE_ODR   (0x{GPIOE_ODR:08X}) = 0x{odr:08X}")

    if m0ar == SHADOW_GPIO:
        print("\n✅ S5M0AR 正确!")
    else:
        print(f"\n❌ S5M0AR 错误: 读到 0x{m0ar:08X}, 期望 0x{SHADOW_GPIO:08X}")
