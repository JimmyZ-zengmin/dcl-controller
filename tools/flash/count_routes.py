#!/usr/bin/env python3
"""Count init_route calls and figure out the actual initial N_ROUTES."""
import re

with open(r"D:\STM\work\dcl-controller\firmware\h723-core0\Src\main.c", encoding='utf-8') as f:
    code = f.read()

# Find all init_route calls
pattern = re.compile(r'init_route\(ri\+\+')
matches = list(pattern.finditer(code))
print(f"Total init_route(ri++): {len(matches)}")

# Find N_ROUTES = ri assignment
m = re.search(r'\(volatile uint32_t \*\)\(DTCM_BASE \+ 0xF0\)\s*=\s*ri', code)
if m:
    # Find context
    start = max(0, m.start() - 500)
    end = min(len(code), m.end() + 200)
    print("\n=== Context around N_ROUTES = ri ===")
    print(code[start:end])
