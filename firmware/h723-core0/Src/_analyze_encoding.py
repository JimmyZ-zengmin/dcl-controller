#!/usr/bin/env python3
"""Analyze and fix broken UTF-8 characters in main.c"""

SRC = r'd:\STM\work\dcl-controller\firmware\h723-core0\Src\main.c'

with open(SRC, 'rb') as f:
    data = f.read()

# Find all occurrences of the replacement character
BROKEN = b'\xef\xbf\xbd'
pos = 0
results = []

while True:
    idx = data.find(BROKEN, pos)
    if idx < 0:
        break
    line_num = data[:idx].count(b'\n') + 1
    ls = data.rfind(b'\n', 0, idx) + 1
    le = data.find(b'\n', idx)
    if le < 0: le = len(data)
    line_text = data[ls:le]
    # Replace broken chars with [?] for display
    safe = line_text.replace(BROKEN, b'[?]')
    after = data[idx+3:idx+4]
    after_hex = f'0x{after[0]:02X}' if after else 'EOF'
    results.append(f"Line {line_num:4d}: after={after_hex} | {safe.decode('ascii', errors='replace')[:100]}")
    pos = idx + 3

with open(SRC + '_encoding_report.txt', 'w', encoding='utf-8') as f:
    f.write(f"Found {len(results)} broken character occurrences\n\n")
    for r in results:
        f.write(r + '\n')

print(f"Report written: {len(results)} occurrences found")
