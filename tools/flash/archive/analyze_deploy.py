#!/usr/bin/env python3
"""Analyze disassembly to verify deploy_test_routes is called."""
import re
import subprocess

OBJDUMP = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe"
ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

result = subprocess.run([OBJDUMP, '-d', ELF], capture_output=True, text=True)
out = result.stdout

# Find deploy_test_routes address
m = re.search(r'^([0-9a-f]+) <deploy_test_routes>:', out, re.MULTILINE)
if m:
    deploy_addr = m.group(1)
    print(f"deploy_test_routes @ 0x{deploy_addr}")
else:
    print("ERROR: deploy_test_routes not found!")
    deploy_addr = None

# Find main function
m2 = re.search(r'^([0-9a-f]+) <main>:', out, re.MULTILINE)
if m2:
    main_addr = m2.group(1)
    print(f"main @ 0x{main_addr}")

# Find calls to deploy_test_routes
if deploy_addr:
    print(f"\n=== Looking for calls to 0x{deploy_addr} ===")
    call_count = 0
    for line in out.splitlines():
        if re.search(rf'\b(bl|blx)\s+.*?0x{deploy_addr}\b', line) or \
           re.search(rf'\b(bl|blx)\s+<deploy_test_routes>', line):
            print(f"  {line}")
            call_count += 1
    if call_count == 0:
        print("  NO CALLS FOUND!")

# Find blx r3 or blx r7 near deploy function call (function pointer calls)
print(f"\n=== Looking for blx/bl with function pointer (r3, r7) near deploy region ===")
# In main function, look for blx r3, blx r7 (function pointer calls)
main_lines = []
in_main = False
for line in out.splitlines():
    if '<main>:' in line:
        in_main = True
    elif in_main and re.match(r'^[0-9a-f]+ <[^>]+>:', line) and '<main>:' not in line:
        in_main = False
    if in_main:
        main_lines.append(line)

for line in main_lines:
    if re.search(r'\b(blx|bl)\s+r[0-9]+\b', line):
        print(f"  {line}")

# Find ITCM section
print(f"\n=== deploy_test_routes disassembly ===")
if deploy_addr:
    lines = out.splitlines()
    in_func = False
    for i, line in enumerate(lines):
        if f'<deploy_test_routes>:' in line:
            in_func = True
            print(line)
            continue
        if in_func:
            # Stop at next function
            if re.match(r'^[0-9a-f]+ <[^>]+>:', line) and '<deploy_test_routes>:' not in line:
                break
            # Only print first 50 lines
            if i < len(lines) and lines.index(line) - lines.index(lines[lines.index(line) - 1]) < 60:
                print(line)
