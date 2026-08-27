#!/usr/bin/env python3
"""Check DMA registers after fresh boot with new VOS fix."""
PROBE = "00000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper
import time

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.reset()
    time.sleep(0.5)
    t.halt()

    pc = t.read_core_register("pc")
    print(f"PC = 0x{pc:08X}")

    # VOS status
    vos = t.read32(0x5802480C)
    print(f"PWR_VOS (0x5802480C) = 0x{vos:08X}  VOS={4 - ((vos>>4)&3)} VOSRDY={(vos>>6)&1}")

    # Clock status
    cfgr = t.read32(0x58024410)
    sw = cfgr & 7
    sws = (cfgr >> 3) & 7
    print(f"RCC_CFGR = 0x{cfgr:08X}  SW={sw} SWS={sws} ({'HSI' if sws==0 else 'PLL' if sws==3 else '?'})")

    # DMA2 Stream 5 (GPIO output)
    print(f"\n=== DMA2 Stream 5 (SHADOW_GPIO → GPIOE_ODR) ===")
    s5cr   = t.read32(0x40020478)
    s5ndtr = t.read32(0x4002047C)
    s5par  = t.read32(0x40020480)
    s5m0ar = t.read32(0x40020484)
    print(f"  CR   = 0x{s5cr:08X}  EN={s5cr&1} DIR={(s5cr>>6)&3} CIRC={(s5cr>>8)&1}")
    print(f"  NDTR = 0x{s5ndtr:08X}")
    print(f"  PAR  = 0x{s5par:08X}  (expect GPIOE_ODR = 0x58021014)")
    print(f"  M0AR = 0x{s5m0ar:08X}  (expect SHADOW_GPIO = 0x200000E0)")

    # DMA2 Stream 1 (ADC, in main.c)
    print(f"\n=== DMA2 Stream 1 (ADC1_DR → ADC_RAW) ===")
    s1cr   = t.read32(0x40020418)
    s1m0ar = t.read32(0x40020424)
    print(f"  CR   = 0x{s1cr:08X}  EN={s1cr&1}")
    print(f"  M0AR = 0x{s1m0ar:08X}  (expect 0x20000290)")

    # SHADOW_GPIO value
    shadow = t.read32(0x200000E0)
    print(f"\n  SHADOW_GPIO (0x200000E0) = 0x{shadow:08X}")

    # GPIOE ODR
    gpioe = t.read32(0x58021014)
    print(f"  GPIOE_ODR   (0x58021014) = 0x{gpioe:08X}")

    # Check if main.c's inline DMA or dma2.c's function was used
    print(f"\n=== Analysis ===")
    if s5m0ar == 0x200000E0:
        print("  DMA2 Stream 5 M0AR: CORRECT (0x200000E0)")
    elif s5m0ar == 0x000000A0:
        print("  DMA2 Stream 5 M0AR: WRONG (0x000000A0) - old bug!")
    else:
        print(f"  DMA2 Stream 5 M0AR: UNEXPECTED (0x{s5m0ar:08X})")
    
    if s5cr & 1:
        print("  DMA2 Stream 5: ENABLED")
    else:
        print("  DMA2 Stream 5: DISABLED")
