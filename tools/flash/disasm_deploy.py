#!/usr/bin/env python3
"""Find any reference to 0x08000451 (deploy+1) in literal pools and what's at blx r3 sites."""
import re

ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"
OBJDUMP = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe"

import subprocess
result = subprocess.run([OBJDUMP, "-d", ELF], capture_output=True, text=True, encoding='utf-8', errors='replace')
out = result.stdout

# Find references to 0x08000451 in literal pools
print("=== Any reference to 0x08000451 in binary ===")
for line in out.splitlines():
    if '0x08000451' in line or '08000451' in line:
        print(f"  {line}")

# Find context around blx r3
for target in ['80019de', '8002250', '800225a']:
    print(f"\n=== Around blx r3 at {target} ===")
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if target in line and ('blx' in line or 'r3' in line):
            for j in range(max(0, i-15), min(len(lines), i+5)):
                print(f"  {lines[j]}")
            break
