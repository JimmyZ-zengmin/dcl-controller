#!/usr/bin/env python3
"""Verify flash content matches ELF at SystemInit."""
import sys, struct
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

PROBE = "00000805059ed5520a4400013dd0702a5a5a5a59796990e"
ELF = r"d:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

from pyocd.core.helpers import ConnectHelper
import subprocess

# Get SystemInit address from ELF
result = subprocess.run(
    [r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe",
     "-t", ELF],
    capture_output=True, text=True
)
sysinit_addr = None
for line in result.stdout.split('\n'):
    if "SystemInit" in line:
        sysinit_addr = int(line.split()[0], 16)
        break

print(f"SystemInit @ 0x{sysinit_addr:08X}")

# Read flash at that address
with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.halt()

    # Read 64 bytes from flash
    flash_data = t.read_memory_block8(sysinit_addr, 64)

    # Decode Thumb-2 instructions (little-endian, 16-bit halfwords)
    print(f"\nFlash @ 0x{sysinit_addr:08X}:")
    for i in range(0, min(64, len(flash_data)), 4):
        # Try 32-bit instruction (2 halfwords)
        if i+3 < len(flash_data):
            hw1 = flash_data[i] | (flash_data[i+1] << 8)
            hw2 = flash_data[i+2] | (flash_data[i+3] << 8)
            instr = (hw2 << 16) | hw1
            print(f"  {sysinit_addr+i:08X}: {instr:08X}  ({hw1:04X} {hw2:04X})")
        elif i+1 < len(flash_data):
            hw1 = flash_data[i] | (flash_data[i+1] << 8)
            print(f"  {sysinit_addr+i:08X}: {hw1:04X}")

    # Check PWR registers after reset (fresh)
    print(f"\nPWR_CR1 = 0x{t.read32(0x58024800):08X}")
    print(f"PWR_CR3 = 0x{t.read32(0x58024808):08X}")
    pc = t.read_core_register("pc")
    print(f"PC = 0x{pc:08X}")
