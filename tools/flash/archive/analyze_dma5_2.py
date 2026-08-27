#!/usr/bin/env python3
"""Disassemble area around 0xDD000005 marker."""
import re
import subprocess

OBJDUMP = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe"
ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

result = subprocess.run([OBJDUMP, '-d', ELF], capture_output=True, text=True)
out = result.stdout

# Find the area with DMA Stream5 init
# Look around 0x8001780-0x80018a0 for the main DMA init
print("=== DMA Stream5 init area (0x8001780-0x8001900) ===")
in_range = False
for line in out.splitlines():
    m = re.match(r'^\s*(8001[7-9][0-9a-f]{2}):\s+(.+)', line)
    if m:
        print(line)

print("\n=== Just the marker area (0x8001a78-0x8001a98) ===")
for line in out.splitlines():
    m = re.match(r'^\s*(8001a[7-9][0-9a-f]):\s+(.+)', line)
    if m:
        print(line)
