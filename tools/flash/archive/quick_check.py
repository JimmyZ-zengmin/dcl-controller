#!/usr/bin/env python3
"""Quick check: flash, run 1s, reset_and_halt, check DMA state."""
import time
from pyocd.core.helpers import ConnectHelper
from pyocd.flash.file_programmer import FileProgrammer

ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    # Halt to check current state (from previous run)
    target.halt()
    print("=== Checking state from previous run ===")
    s5m0ar = target.read32(0x40020484)
    s1m0ar = target.read32(0x40020424)
    s5cr = target.read32(0x40020478)
    odr = target.read32(0x58021014)
    shadow = target.read32(0x200000E0)
    cfgr = target.read32(0x58024410)
    print(f"SWS = {(cfgr>>3)&7}")
    print(f"DMA2_S5CR   = 0x{s5cr:08X}  EN={s5cr&1}")
    print(f"DMA2_S1M0AR = 0x{s1m0ar:08X}  (expect 0x200000F0)")
    print(f"DMA2_S5M0AR = 0x{s5m0ar:08X}  (expect 0x200000E0)")
    print(f"GPIOE_ODR   = 0x{odr:08X}")
    print(f"SHADOW_GPIO = 0x{shadow:08X}")
