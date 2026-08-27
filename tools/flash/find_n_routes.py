#!/usr/bin/env python3
"""Find N_ROUTES writes in main function (store to DTCM+0xF0)."""
import subprocess
r = subprocess.run([r'C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe',
                   '-d', r'D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf'],
                   capture_output=True, text=True)
lines = r.stdout.split('\n')
in_main = False
print('All writes to DTCM+0xF0 (N_ROUTES):')
for line in lines:
    if '<main>:' in line:
        in_main = True
        continue
    if in_main and '<' in line and '>:' in line and 'main' not in line:
        break
    if in_main and 'str' in line and ('0xf0' in line.lower() or '#240' in line):
        print('  ' + line)
print('\nAll writes to DTCM+0x100 (SCRATCH[2]/SENSOR[0]):')
in_main = False
for line in lines:
    if '<main>:' in line:
        in_main = True
        continue
    if in_main and '<' in line and '>:' in line and 'main' not in line:
        break
    if in_main and 'str' in line and ('0x100' in line.lower() or '#256' in line):
        print('  ' + line)
print('\nLast 80 lines of main disassembly:')
in_main = False
all_main = []
for line in lines:
    if '<main>:' in line:
        in_main = True
        continue
    if in_main and '<' in line and '>:' in line and 'main' not in line:
        break
    if in_main:
        all_main.append(line)
for line in all_main[-80:]:
    print(line)
