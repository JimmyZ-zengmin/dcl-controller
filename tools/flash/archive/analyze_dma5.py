#!/usr/bin/env python3
"""Find the DMA Stream5 init code in main()."""
import re
import subprocess

OBJDUMP = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe"
ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

result = subprocess.run([OBJDUMP, '-d', ELF], capture_output=True, text=True)
out = result.stdout

# Find any reference to 0x40020488 (DMA2_S5CR) or 0x40020494 (DMA2_S5M0AR)
# or 0x40020814 (DMAMUX1_S5CR) or 0x58021014 (GPIOE_ODR)
print("=== References to DMA Stream5 registers in disasm ===")
for line in out.splitlines():
    if '0x40020488' in line or '0x4002048c' in line or '0x40020490' in line or \
       '0x40020494' in line or '0x4002049c' in line or '0x40020814' in line or \
       '0x4002040c' in line or '0x40020408' in line:  # LIFCR/HIFCR
        print(f"  {line}")

print("\n=== References to GPIOE_ODR (0x58021014) in disasm ===")
for line in out.splitlines():
    if '0x58021014' in line:
        print(f"  {line}")

print("\n=== References to 0x200000E0 (SHADOW) in disasm ===")
for line in out.splitlines():
    if '0x200000e0' in line:
        print(f"  {line}")

# Find the marker 0xDD000005
print("\n=== 0xDD000005 marker ===")
for line in out.splitlines():
    if '0xdd000005' in line.lower():
        print(f"  {line}")
