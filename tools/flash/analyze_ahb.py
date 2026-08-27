#!/usr/bin/env python3
"""Find where DMA clock is enabled - look for ldr from 0x580244D8 then orr/str."""
import re
import subprocess

OBJDUMP = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe"
ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

result = subprocess.run([OBJDUMP, '-d', ELF], capture_output=True, text=True)
out = result.stdout
lines = out.splitlines()

# Find all instructions loading from 0x580244D8 (AHB1ENR) or 0x580244E0 (AHB4ENR)
# and the subsequent orr/str pattern
print("=== All AHB1ENR (0x580244D8) and AHB4ENR (0x580244E0) access patterns ===")
for i, line in enumerate(lines):
    # Look for ldr that references these addresses
    m = re.search(r'ldr\s+r(\d+),\s+\[pc,\s+#\d+\]\s*;\s*\(([0-9a-f]+)', line)
    if m:
        reg = m.group(1)
        addr = int(m.group(2), 16)
        if addr in (0x580244D8, 0x580244E0, 0x580244F0):
            # Print this load and the next 5 instructions
            print(f"\n  At 0x{addr:08X} (r{reg}):")
            for j in range(i, min(len(lines), i+6)):
                print(f"    {lines[j].strip()}")
