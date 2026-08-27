#!/usr/bin/env python3
"""Check DMA2 clock enable and ADC init order."""
import re
import subprocess

OBJDUMP = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe"
ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

result = subprocess.run([OBJDUMP, '-d', ELF], capture_output=True, text=True)
out = result.stdout

# Find code that writes to RCC_AHB1ENR (0x580244D8) and RCC_AHB4ENR (0x580244E0)
print("=== Writes to RCC_AHB1ENR (0x580244D8) ===")
for i, line in enumerate(out.splitlines()):
    if '0x580244d8' in line.lower() or '0x580244e0' in line.lower():
        # Print context
        for j in range(max(0, i-3), min(len(out.splitlines()), i+3)):
            print(f"  {out.splitlines()[j]}")
        print()

# Find specific DMA init code
print("=== ADC1 init (writes to ADC1_CR @ 0x40022008) ===")
for i, line in enumerate(out.splitlines()):
    if '0x40022008' in line.lower() or '0x40022000' in line.lower():
        for j in range(max(0, i-3), min(len(out.splitlines()), i+3)):
            print(f"  {out.splitlines()[j]}")
        print()

# Check the timing of writes
print("=== All writes to DMA/DMAMUX registers with nearby code ===")
dma_addrs = ['0x40020408', '0x40020428', '0x40020488', '0x4002048c',
             '0x40020490', '0x40020494', '0x4002049c', '0x40020804', '0x40020814']
for i, line in enumerate(out.splitlines()):
    for addr in dma_addrs:
        if addr in line.lower():
            print(f"  {line.strip()}")
            break
