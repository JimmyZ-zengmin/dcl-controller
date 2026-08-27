#!/usr/bin/env python3
"""Find all writes to AHB1ENR (RCC clock for DMA) and trace init sequence."""
import re
import subprocess

OBJDUMP = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe"
ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

result = subprocess.run([OBJDUMP, '-d', ELF], capture_output=True, text=True)
out = result.stdout

# Find str instructions where the target is computed from 0x580244D8 (AHB1ENR)
# or from addresses in the AHB1ENR/AHB4ENR literal pool
lines = out.splitlines()
for i, line in enumerate(lines):
    if '0x580244d8' in line.lower() and '.word' in line.lower():
        addr = re.match(r'^\s*([0-9a-f]+):', line).group(1)
        # Now look back for instructions that load from this address
        for j in range(max(0, i-50), i):
            l = lines[j]
            m = re.match(r'^\s*([0-9a-f]+):\s+[0-9a-f]+\s+ldr\s+r(\d+),\s+\[pc,\s+#\d+\]\s*;\s*\(' + addr, l)
            if m:
                # Found the load - now find the str that uses this register
                reg = m.group(2)
                # Look forward for a str with this register as base
                for k in range(j, min(len(lines), j+30)):
                    if re.search(rf'\bstr\b.*\s+r\w+,\s+\[r{reg},', lines[k]) or \
                       re.search(rf'\bstr\w+\s+r\w+,\s+\[r{reg}', lines[k]) or \
                       re.search(rf'\borr\w*\s+r(\w+),\s+r(\w+),\s+#', lines[k]):
                        print(f"  LDR at {l.strip()}")
                        print(f"  USE: {lines[k].strip()}")
                        print()
                        break
                break

# Find the actual write of 0x20000004 = 0xDD000005 marker
# Look for ldr r0, [pc, #N] where the literal pool contains 0xdd000005
print("=== Trace: where 0xDD000005 marker gets written ===")
for i, line in enumerate(lines):
    if '0xdd000005' in line.lower() and '.word' in line.lower():
        addr = re.match(r'^\s*([0-9a-f]+):', line).group(1)
        # Find the ldr that loads this
        for j in range(max(0, i-30), i):
            l = lines[j]
            m = re.search(r'^\s*([0-9a-f]+):\s+[0-9a-f]+\s+ldr\s+r(\d+),\s+\[pc,\s+#(\d+)\]\s*;\s*\(' + addr, l)
            if m:
                load_addr = m.group(1)
                reg = m.group(2)
                # Find the str that uses this register (write marker to DTCM+4)
                for k in range(j, min(len(lines), j+20)):
                    if re.search(rf'\bstr\b.*r{reg}\b.*\[r(\d+),', lines[k]):
                        target = re.search(rf'r{reg}\b.*\[r(\d+)', lines[k])
                        if target:
                            treg = target.group(1)
                            # Find what address this register holds
                            print(f"  LDR r{reg} at 0x{load_addr} from 0x{addr}")
                            print(f"  STR at {lines[k].strip()}")
                            # Backtrack to find what r{treg} holds
                            for m2 in range(max(0, k-15), k):
                                m3 = re.search(rf'ldr\s+r{treg},\s+\[pc,\s+#\d+\]\s*;\s*\(([0-9a-f]+)', lines[m2])
                                if m3:
                                    target_addr = int(m3.group(1), 16)
                                    print(f"  → r{treg} = 0x{target_addr:08X}")
                                    break
                            print()
                        break
                break
