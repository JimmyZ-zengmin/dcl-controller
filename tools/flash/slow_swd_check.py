#!/usr/bin/env python3
"""Connect at lower SWD frequency to read state at 544MHz."""
import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    frequency=1000000  # 1MHz SWD (default is ~10MHz)
) as session:
    target = session.target

    # Don't halt — just try to read
    print("=== Reading at 1MHz SWD ===")
    try:
        # Read RCC first
        cr = target.read32(0x58024400)
        cfgr = target.read32(0x58024410)
        print(f"RCC_CR   = 0x{cr:08X}  PLL1ON={(cr>>24)&1}  PLL1RDY={(cr>>25)&1}")
        print(f"RCC_CFGR = 0x{cfgr:08X}  SWS={(cfgr>>3)&7}")

        # DTCM
        shadow  = target.read32(0x200000E0)
        adc_raw = target.read32(0x200000F0)
        sensor0 = target.read32(0x20000100)
        print(f"\nSHADOW_GPIO  = 0x{shadow:08X}")
        print(f"ADC_RAW      = 0x{adc_raw:08X}")
        print(f"SENSOR_MAP[0]= 0x{sensor0:08X}")

        # Check ISR alive
        time.sleep(0.01)
        shadow2 = target.read32(0x200000E0)
        time.sleep(0.01)
        shadow3 = target.read32(0x200000E0)
        print(f"SHADOW +10ms = 0x{shadow2:08X}")
        print(f"SHADOW +20ms = 0x{shadow3:08X}")

        if shadow != shadow2 or shadow2 != shadow3:
            print("✅ SHADOW changing — ISR running!")
        elif shadow == 0:
            print("❌ SHADOW=0 — ISR not writing")
        else:
            print("⚠️ SHADOW static — ISR may not be running")

        # DMA
        s5cr = target.read32(0x40020478)
        s5m0ar = target.read32(0x40020484)
        s1cr = target.read32(0x40020418)
        s1m0ar = target.read32(0x40020424)
        print(f"\nDMA2_S1CR   = 0x{s1cr:08X}  EN={s1cr&1}")
        print(f"DMA2_S1M0AR = 0x{s1m0ar:08X}")
        print(f"DMA2_S5CR   = 0x{s5cr:08X}  EN={s5cr&1}")
        print(f"DMA2_S5M0AR = 0x{s5m0ar:08X}")

        # GPIOE
        odr = target.read32(0x58021014)
        print(f"GPIOE_ODR = 0x{odr:08X}")

    except Exception as e:
        print(f"Failed: {e}")
