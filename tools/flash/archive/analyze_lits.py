#!/usr/bin/env python3
"""Check literal pool addresses around main()."""
import re
import subprocess

OBJDUMP = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe"
ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

result = subprocess.run([OBJDUMP, '-d', ELF], capture_output=True, text=True)
out = result.stdout

# Find all literal pool entries near 0x8001aec, 0x8001af0, 0x8001af4
print("=== Literal pool entries in main() ===")
for line in out.splitlines():
    # Match literal pool format
    m = re.match(r'^\s*8001a[e-f][0-9a-f]:\s+([0-9a-f]+)\s+\.word\s+(0x[0-9a-f]+)', line)
    if m:
        addr = int(m.group(1), 16)
        val = int(m.group(2), 16)
        # Compute which PC-relative offset it was loaded from
        # PC-relative load: ldr rN, [pc, #imm]; PC is addr+4
        # The literal is at (load_addr + 4) & ~3 + imm
        print(f"  {line.strip()}")

# Look for address 0x08000450 (deploy_test_routes)
print("\n=== Looking for 0x08000450 (deploy_test_routes) in literal pool ===")
for line in out.splitlines():
    if '0x08000450' in line or '8000450' in line:
        print(f"  {line.strip()}")
