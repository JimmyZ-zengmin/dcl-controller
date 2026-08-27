#!/usr/bin/env python3
"""Flash, run 2s, then reset+halt and read DTCM markers (preserved across reset)."""
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

    # Clear marker
    target.write32(0x20000000, 0x00000000)
    print("Cleared marker")

    # Resume — firmware runs
    target.resume()
    print("=== Running at 544MHz... ===")
    time.sleep(2.0)

    # Now do a pin reset (keeps DTCM alive)
    target.reset_and_halt()
    print("=== Reset and halted ===")

    # Read markers from DTCM (preserved across reset!)
    marker = target.read32(0x20000000)
    shadow = target.read32(0x200000E0)
    adc_raw = target.read32(0x200000F0)
    sensor0 = target.read32(0x20000100)

    print(f"\n=== DTCM Markers ===")
    print(f"PROGRESS (0x20000000) = 0x{marker:08X}")

    if (marker >> 24) == 0xAA:
        print("  → 0xAA: SystemInit completed, stuck before GPIO init")
    elif (marker >> 24) == 0xBB:
        print("  → 0xBB: GPIO init done, stuck before DMA/TIM1")
    elif (marker >> 24) == 0xCC:
        print("  ✅ 0xCC: ALL initialization completed!")
    elif (marker >> 24) == 0x00:
        print("  → 0x00: SystemInit not reached or marker not written")
    else:
        print(f"  → Unknown marker: 0x{(marker>>24):02X}")

    print(f"SHADOW_GPIO  = 0x{shadow:08X}")
    print(f"ADC_RAW      = 0x{adc_raw:08X}")
    print(f"SENSOR_MAP[0]= 0x{sensor0:08X}")

    # Also check DMA registers (these reset with system, but let's try)
    s1cr = target.read32(0x40020418)
    s1m0ar = target.read32(0x40020424)
    s5cr = target.read32(0x40020478)
    s5m0ar = target.read32(0x40020484)
    print(f"\nDMA2_S1CR   = 0x{s1cr:08X}  EN={s1cr&1}")
    print(f"DMA2_S1M0AR = 0x{s1m0ar:08X}  (expect 0x200000F0)")
    print(f"DMA2_S5CR   = 0x{s5cr:08X}  EN={s5cr&1}")
    print(f"DMA2_S5M0AR = 0x{s5m0ar:08X}  (expect 0x200000E0)")
