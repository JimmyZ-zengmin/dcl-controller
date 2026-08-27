#!/usr/bin/env python3
"""Look at the area around blx r3 to verify it's the deploy call."""
import re
import subprocess

OBJDUMP = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe"
ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

result = subprocess.run([OBJDUMP, '-d', ELF], capture_output=True, text=True)
out = result.stdout

# Find area around 0x080019de
print("=== Context around blx r3 at 0x080019de ===")
lines = out.splitlines()
for i, line in enumerate(lines):
    if '80019d' in line or '80019e' in line or '80019c' in line or '80019f' in line or '8001a0' in line:
        # Print this and 20 lines around
        for j in range(max(0, i-30), min(len(lines), i+10)):
            print(lines[j])
        print("---")
        if i > 0 and '80019de' in line:
            break

# Show main() in order
print("\n=== main() lines 0x80019c0 - 0x8001a10 ===")
for line in lines:
    m = re.match(r'^\s*(80019[a-f][0-9a-f]|8001a0[0-9a-f]):\s+(.+)', line)
    if m:
        print(line)
