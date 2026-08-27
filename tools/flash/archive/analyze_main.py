#!/usr/bin/env python3
"""Look at main() around the deploy call site."""
import re
import subprocess

OBJDUMP = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe"
ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

result = subprocess.run([OBJDUMP, '-d', ELF], capture_output=True, text=True)
out = result.stdout

# Find the blx r3 call
print("=== Lines containing 'blx r3' ===")
for line in out.splitlines():
    if re.search(r'\bblx\s+r3\b', line):
        print(f"  {line}")

print("\n=== Last 80 lines of main() ===")
in_main = False
main_lines = []
for line in out.splitlines():
    if '<main>:' in line:
        in_main = True
    elif in_main and re.match(r'^[0-9a-f]+ <[^>]+>:', line) and '<main>:' not in line:
        break
    if in_main:
        main_lines.append(line)

# Show last 80 lines
for line in main_lines[-80:]:
    print(line)

# Find any references to 0x200000f0 in main
print("\n=== main() references to 0x200000F0 (N_ROUTES) ===")
for line in main_lines:
    if '0x200000f0' in line.lower():
        print(f"  {line}")

print("\n=== main() references to 0x20005700 (PARAM_TABLE) ===")
for line in main_lines:
    if '0x20005700' in line.lower():
        print(f"  {line}")

print("\n=== main() references to 0x20001700 (ROUTE_TABLE) ===")
for line in main_lines:
    if '0x20001700' in line.lower():
        print(f"  {line}")
