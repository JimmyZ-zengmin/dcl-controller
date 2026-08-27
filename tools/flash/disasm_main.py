#!/usr/bin/env python3
"""Disassemble main to see if my added code is in there."""
import subprocess
r = subprocess.run([r'C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe',
                   '-d', r'D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf'],
                   capture_output=True, text=True)
lines = r.stdout.split('\n')

# Find main function
in_main = False
main_lines = []
for i, line in enumerate(lines):
    if '<main>:' in line:
        in_main = True
    if in_main:
        main_lines.append(line)
        if in_main and len(main_lines) > 800:
            break

# Print first 50 and last 100 lines of main
print('--- main first 50 lines ---')
for line in main_lines[:50]:
    print(line)
print('\n--- main last 100 lines (looking for test code) ---')
for line in main_lines[-100:]:
    print(line)
