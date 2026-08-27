#!/usr/bin/env python3
"""Read DTCM without halting - just read memory while CPU runs."""
import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    # Don't halt! Just read memory directly
    print("=== Reading DTCM without halting ===")
    try:
        # Read multiple DTCM locations to see what the firmware wrote
        timing  = target.read32(0x20000000)  # TIMING area
        shadow  = target.read32(0x200000E0)  # SHADOW_GPIO
        adc_raw = target.read32(0x200000F0)  # ADC_RAW
        sensor0 = target.read32(0x20000100)  # SENSOR_MAP[0]

        print(f"TIMING[0]   (0x20000000) = 0x{timing:08X}")
        print(f"SHADOW_GPIO (0x200000E0) = 0x{shadow:08X}")
        print(f"ADC_RAW     (0x200000F0) = 0x{adc_raw:08X}")
        print(f"SENSOR_MAP[0](0x20000100)= 0x{sensor0:08X}")

        # Try reading DMA registers
        s1cr = target.read32(0x40020418)
        s1m0ar = target.read32(0x40020424)
        s5cr = target.read32(0x40020478)
        s5m0ar = target.read32(0x40020484)

        print(f"\nDMA2_S1CR   = 0x{s1cr:08X}  EN={s1cr&1}")
        print(f"DMA2_S1M0AR = 0x{s1m0ar:08X}  (expect 0x200000F0)")
        print(f"DMA2_S5CR   = 0x{s5cr:08X}  EN={s5cr&1}")
        print(f"DMA2_S5M0AR = 0x{s5m0ar:08X}  (expect 0x200000E0)")

        # RCC
        cfgr = target.read32(0x58024410)
        cr = target.read32(0x58024400)
        print(f"\nRCC_CR = 0x{cr:08X}  PLL1ON={(cr>>24)&1}  PLL1RDY={(cr>>25)&1}")
        print(f"RCC_CFGR = 0x{cfgr:08X}  SWS={(cfgr>>3)&7}")

        # GPIOE
        odr = target.read32(0x58021014)
        print(f"GPIOE_ODR = 0x{odr:08X}")

        # Read SHADOW again after a short delay to see if it changes (ISR running)
        time.sleep(0.01)
        shadow2 = target.read32(0x200000E0)
        print(f"\nSHADOW_GPIO 10ms later = 0x{shadow2:08X}")
        if shadow != shadow2:
            print("✅ SHADOW_GPIO is changing — ISR is running!")
        else:
            print("SHADOW_GPIO unchanged")

    except Exception as e:
        print(f"Read failed: {e}")
        print("CPU likely running at 544MHz (SWD can't keep up)")
