#!/usr/bin/env python3
"""Find DMA clock enable (RCC_AHB1ENR |= 6) and Stream1/5 init code."""
import re
import subprocess

OBJDUMP = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe"
ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

result = subprocess.run([OBJDUMP, '-d', ELF], capture_output=True, text=True)
out = result.stdout
lines = out.splitlines()

# Show main from 0x80016b0 to 0x8001800 - covering DMA clock enable, ADC init, DMA Stream1/5 init
print("=== Main: 0x80016b0 - 0x8001800 (ADC + DMA init) ===")
for line in lines:
    m = re.match(r'^\s*(8001[6-7][0-9a-f]{2}):\s+(.+)', line)
    if m:
        addr = m.group(1)
        if 0x80016b0 <= int(addr, 16) <= 0x8001800:
            print(line)
