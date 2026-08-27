#!/usr/bin/env python3
"""Disassemble .elf around addresses of interest to understand code."""
import subprocess, sys

ELF = r"d:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"
OBJDUMP = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-objdump.exe"

print(f"Using objdump: {OBJDUMP}")
print(f"ELF: {ELF}")

# Run full disassembly, capture to file
out_file = r"d:\STM\work\dcl-controller\tools\flash\fw_disasm.txt"
with open(out_file, "w") as f:
    r = subprocess.run([OBJDUMP, "-d", ELF], stdout=f, stderr=subprocess.STDOUT)

print(f"Disassembly saved to {out_file} (exit code {r.returncode})")

with open(out_file) as f:
    lines = f.readlines()

# Show first few lines to verify content
print("── First 5 lines of disasm ──")
for l in lines[:5]:
    print(f"  {l.rstrip()}")

print(f"── Total lines: {len(lines)} ──")

# Find symbols of interest
print("\n── Symbols (function names and addresses) ──")
sym_lines = set()
for line in lines:
    lt = line.lower()
    for sym in ["systeminit", "reset_handler", "main>", "tim1_up", "systemclock", "clock_init"]:
        if sym in lt and "<" in lt:
            sym_lines.add(line.rstrip())

for s in sorted(sym_lines):
    print(f"  {s}")

# Now extract address ranges of interest
target_addrs = [0x08001F78, 0x08002938, 0x08001F72]
for target in target_addrs:
    print(f"\n── Code around 0x{target:08X} ──")
    found = 0
    for line in lines:
        if line.strip() and len(line) > 8:
            try:
                parts = line.split(":")
                if len(parts) >= 1:
                    addr_str = parts[0].strip()
                    if addr_str.startswith("0") or addr_str.startswith("8"):
                        addr_val = int(addr_str, 16)
                        if abs(addr_val - target) < 0x60:
                            prefix = " >>> " if addr_val == target else "     "
                            print(f"{prefix}{line.rstrip()}")
                            found += 1
            except:
                pass
    if found == 0:
        print("  (not found — address not in disassembly)")
