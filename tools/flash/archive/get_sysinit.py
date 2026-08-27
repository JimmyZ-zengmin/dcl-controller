#!/usr/bin/env python3
"""Extract SystemInit disassembly from ELF"""
import subprocess
import sys

OBJDUMP = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe"
ELF = r"d:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

result = subprocess.run(
    [OBJDUMP, "-d", "-j", ".text", ELF],
    capture_output=True, text=True
)

lines = result.stdout.split('\n')
in_sysinit = False
for line in lines:
    if "<SystemInit>:" in line:
        in_sysinit = True
    elif in_sysinit and (line.strip() == "" or (":" in line and not line.startswith(" "))):
        # Next function starts
        if "<" in line and ">:" in line:
            break
    if in_sysinit:
        print(line)
