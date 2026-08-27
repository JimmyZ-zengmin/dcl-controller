#!/usr/bin/env python3
"""Flash, run, then read state — all in one session."""
import time
from pyocd.core.helpers import ConnectHelper
from pyocd.flash.file_programmer import FileProgrammer

ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target
    target.halt()

    # Flash
    programmer = FileProgrammer(session, chip_erase="sector")
    programmer.program(ELF, file_format="elf")
    print("=== Flashed ===")

    # Reset and halt
    target.reset_and_halt()
    pc = target.read_core_register("pc")
    print(f"PC after reset = 0x{pc:08X}")

    # Resume — firmware starts running
    target.resume()
    print("=== Running... ===")
    time.sleep(1.0)  # Wait 1 second for full initialization

    # Now try to read DTCM directly (no halt needed)
    try:
        shadow  = target.read32(0x200000E0)
        adc_raw = target.read32(0x200000F0)
        sensor0 = target.read32(0x20000100)

        print(f"\n=== After 1s run ===")
        print(f"SHADOW_GPIO  = 0x{shadow:08X}")
        print(f"ADC_RAW      = 0x{adc_raw:08X}")
        print(f"SENSOR_MAP[0]= 0x{sensor0:08X}")

        # Check if SHADOW changes (ISR running)
        time.sleep(0.01)
        shadow2 = target.read32(0x200000E0)
        print(f"SHADOW 10ms  = 0x{shadow2:08X}")

        if shadow != shadow2:
            print("✅ SHADOW_GPIO changing — ISR running at 544MHz!")
        elif shadow != 0:
            print("⚠️ SHADOW has value but not changing — may be stuck after init")

        # Try DMA registers
        s5cr = target.read32(0x40020478)
        s5m0ar = target.read32(0x40020484)
        s1cr = target.read32(0x40020418)
        s1m0ar = target.read32(0x40020424)
        print(f"\nDMA2_S1CR   = 0x{s1cr:08X}  EN={s1cr&1}")
        print(f"DMA2_S1M0AR = 0x{s1m0ar:08X}")
        print(f"DMA2_S5CR   = 0x{s5cr:08X}  EN={s5cr&1}")
        print(f"DMA2_S5M0AR = 0x{s5m0ar:08X}")

        # RCC
        cr = target.read32(0x58024400)
        cfgr = target.read32(0x58024410)
        print(f"\nRCC_CR   = 0x{cr:08X}  PLL1ON={(cr>>24)&1}  PLL1RDY={(cr>>25)&1}")
        print(f"RCC_CFGR = 0x{cfgr:08X}  SWS={(cfgr>>3)&7}")

        # GPIOE
        odr = target.read32(0x58021014)
        print(f"GPIOE_ODR = 0x{odr:08X}")

    except Exception as e:
        print(f"Read failed: {e}")
        print("✅ This likely means CPU is at 544MHz — SWD can't keep up")
