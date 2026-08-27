#!/usr/bin/env python3
"""Flash core0_h723.elf via pyocd (CMSIS-DAP) and verify."""
import sys
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"
PROBE = "000000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper
from pyocd.flash.file_programmer import FileProgrammer

print(f"=== Flashing {ELF}")
print(f"=== Probe: {PROBE[:16]}...")

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    target = session.target
    target.halt()

    # Flash
    programmer = FileProgrammer(session, chip_erase="sector")
    programmer.program(ELF, file_format="elf")

    # Reset & halt to check
    target.reset_and_halt()
    pc = target.read_core_register("pc")
    sp = target.read_core_register("sp")
    print(f"\n=== After reset ===")
    print(f"SP = 0x{sp:08X}")
    print(f"PC = 0x{pc:08X}")

    # Verify vector table
    vt0 = target.read32(0x08000000)
    vt1 = target.read32(0x08000004)
    print(f"Vector[0] (initial SP) = 0x{vt0:08X}")
    print(f"Vector[1] (Reset_Handler) = 0x{vt1:08X}")

    # Check DMA Stream 5 M0AR (if DMA was enabled in fw)
    dma5_m0ar = target.read32(0x400204B8)  # DMA2_Stream5_M0AR
    print(f"\nDMA2_Stream5_M0AR = 0x{dma5_m0ar:08X}  (expect 0x200000E0 if fw enabled DMA)")

    # Resume
    target.resume()
    print("\n=== Running ===")
