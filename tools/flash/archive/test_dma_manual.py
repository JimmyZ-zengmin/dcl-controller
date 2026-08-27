#!/usr/bin/env python3
"""
Test: Manually configure DMA2_S5 from pyocd and see if the values stick.
This will reveal whether the DMA controller is actually accessible.
"""
import time
from pyocd.core.helpers import ConnectHelper

DMA2_BASE = 0x40020400
DMAMUX_BASE = 0x40020800
GPIOE_ODR = 0x58021014
SHADOW = 0x200000E0

# First, halt the CPU
with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    t.halt()

    # Read current state
    print("=== Before manual config ===")
    s5cr_before = t.read32(DMA2_BASE + 0x88)
    s5m0ar_before = t.read32(DMA2_BASE + 0x94)
    s5par_before = t.read32(DMA2_BASE + 0x90)
    print(f"DMA2_S5CR  = 0x{s5cr_before:08X}")
    print(f"DMA2_S5M0AR= 0x{s5m0ar_before:08X}")
    print(f"DMA2_S5PAR = 0x{s5par_before:08X}")
    print(f"DMAMUX_S5CR= 0x{t.read32(DMAMUX_BASE + 0x14):08X}")

    # Now write manually
    print("\n=== Writing manually ===")
    # 1. Disable stream
    t.write32(DMA2_BASE + 0x88, 0)
    # Wait for EN=0
    for i in range(1000000):
        if not (t.read32(DMA2_BASE + 0x88) & 1):
            break
    print(f"  After disable, EN=0: {(t.read32(DMA2_BASE+0x88) & 1) == 0}")

    # 2. Clear flags
    t.write32(DMA2_BASE + 0x0C, 0x00000F7C)  # DMA2_HIFCR

    # 3. Set NDTR=0 to unlock
    t.write32(DMA2_BASE + 0x8C, 0)

    # 4. Set PAR
    t.write32(DMA2_BASE + 0x90, GPIOE_ODR)
    print(f"  After PAR write:  0x{t.read32(DMA2_BASE+0x90):08X} (expect {GPIOE_ODR:08X})")

    # 5. Set M0AR
    t.write32(DMA2_BASE + 0x94, SHADOW)
    print(f"  After M0AR write: 0x{t.read32(DMA2_BASE+0x94):08X} (expect {SHADOW:08X})")

    # 6. Set NDTR=1
    t.write32(DMA2_BASE + 0x8C, 1)
    print(f"  After NDTR write: {t.read32(DMA2_BASE+0x8C)} (expect 1)")

    # 7. Set FCR=0
    t.write32(DMA2_BASE + 0x9C, 0)

    # 8. Set CR with EN=1, DIR=mem2per, CIRC, P=32, M=32, PL=3
    cr = (1 << 0) | (1 << 6) | (1 << 8) | (2 << 11) | (2 << 13) | (3 << 16)
    t.write32(DMA2_BASE + 0x88, cr)
    print(f"  After CR write:   0x{t.read32(DMA2_BASE+0x88):08X} (expect {cr:08X})")

    # 9. Set DMAMUX to TIM1_UP
    t.write32(DMAMUX_BASE + 0x14, 15)
    print(f"  After DMAMUX write: 0x{t.read32(DMAMUX_BASE+0x14):08X} (expect 15)")

    print("\n=== After manual config (CPU still halted) ===")
    print(f"DMA2_S5CR  = 0x{t.read32(DMA2_BASE+0x88):08X}")
    print(f"DMA2_S5M0AR= 0x{t.read32(DMA2_BASE+0x94):08X}")
    print(f"DMA2_S5PAR = 0x{t.read32(DMA2_BASE+0x90):08X}")
    print(f"DMA2_S5NDTR= 0x{t.read32(DMA2_BASE+0x8C):08X}")
    print(f"DMAMUX_S5CR= 0x{t.read32(DMAMUX_BASE+0x14):08X}")

    # Now resume the CPU
    print("\n=== Resuming CPU for 1s ===")
    t.resume()
    time.sleep(1.0)
    t.halt()

    print("=== After 1s of CPU running ===")
    print(f"DMA2_S5CR  = 0x{t.read32(DMA2_BASE+0x88):08X}")
    print(f"DMA2_S5M0AR= 0x{t.read32(DMA2_BASE+0x94):08X}")
    print(f"DMA2_S5PAR = 0x{t.read32(DMA2_BASE+0x90):08X}")
    print(f"DMA2_S5NDTR= 0x{t.read32(DMA2_BASE+0x8C):08X}")
    print(f"DMAMUX_S5CR= 0x{t.read32(DMAMUX_BASE+0x14):08X}")
    shadow = t.read32(SHADOW)
    odr = t.read32(GPIOE_ODR)
    print(f"SHADOW_GPIO= 0x{shadow:08X}")
    print(f"GPIOE_ODR  = 0x{odr:08X}")
    if shadow == odr:
        print("  [OK] SHADOW == ODR")
    else:
        print(f"  [FAIL] SHADOW 0x{shadow:08X} != ODR 0x{odr:08X}")
