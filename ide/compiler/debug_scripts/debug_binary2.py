#!/usr/bin/env python3
"""检查磁盘二进制文件 vs 内存中的路由表"""

import struct
import sys

sys.path.insert(0, '.')
from dcl_compiler import DCLCompiler, ROUTE_FMT
from core.dcl_hardware import ADDRESSES

print(f"ROUTE_FMT = {ROUTE_FMT}")
print(f"ROUTE_FMT size = {struct.calcsize(ROUTE_FMT)} bytes")

# 编译 test_logic.dcl
with open('test_logic.dcl') as f:
    src = f.read()

c = DCLCompiler()
c.parse(src)
c.topological_sort()
c.validate_resources()

print(f"\n编译器输出: {len(c.routes)} routes, {c.next_wire} wires")

# 生成二进制
binary = c.generate_binary()

print(f"\n=== 二进制文件前5个路由条目 (从偏移12开始) ===")
print(f"Header: routes={struct.unpack_from('<I', binary, 0)[0]}, "
      f"params={struct.unpack_from('<I', binary, 4)[0]}")

for i in range(min(5, len(c.routes))):
    entry = binary[12 + i*16 : 12 + (i+1)*16]
    print(f"  [{i}] file: {entry.hex(' ')}")
    fields = struct.unpack(ROUTE_FMT, entry)
    print(f"       解码: src_t={fields[0]} src_i={fields[1]} dst_t={fields[2]} "
          f"dst_ch={fields[3]} op=0x{fields[4]:02x} flags={fields[5]}")

# 读取磁盘上的二进制文件
print(f"\n=== 磁盘 test_logic.bin 内容 ===")
with open('test_logic.bin', 'rb') as f:
    disk_data = f.read()
print(f"文件大小: {len(disk_data)} bytes")
print(f"Header: routes={struct.unpack_from('<I', disk_data, 0)[0]}, "
      f"params={struct.unpack_from('<I', disk_data, 4)[0]}")

for i in range(min(5, struct.unpack_from('<I', disk_data, 0)[0])):
    entry = disk_data[12 + i*16 : 12 + (i+1)*16]
    print(f"  [{i}] disk: {entry.hex(' ')}")
    fields = struct.unpack(ROUTE_FMT, entry)
    print(f"       解码: src_t={fields[0]} src_i={fields[1]} dst_t={fields[2]} "
          f"dst_ch={fields[3]} op=0x{fields[4]:02x} flags={fields[5]}")

# 对比内存中的 ROUTE_TABLE
import subprocess
PYOCD = r'C:\Espressif\tools\python\v6.0.1\venv\Scripts\pyocd.exe'

print(f"\n=== 硬件 ROUTE_TABLE 前5个条目 ===")
result = subprocess.run(
    [PYOCD, 'commander', '-t', 'stm32h723xx', '-c', f'read32 {ADDRESSES["ROUTE_TABLE"]:08X} 20'],
    capture_output=True, text=True, timeout=5
)
raw_values = []
for line in result.stdout.split('\n'):
    line = line.strip()
    if not line or ':' not in line:
        continue
    parts = line.split(':', 1)
    if len(parts) >= 2:
        hex_part = parts[1].split('|')[0].strip()
        for p in hex_part.split():
            if len(p) == 8:
                try:
                    raw_values.append(int(p, 16))
                except:
                    pass

for i in range(min(5, len(raw_values)//4)):
    entry_bytes = struct.pack('<IIII', *raw_values[i*4:i*4+4])
    print(f"  [{i}] mem:  {entry_bytes.hex(' ')}")
    fields = struct.unpack(ROUTE_FMT, entry_bytes)
    print(f"       解码: src_t={fields[0]} src_i={fields[1]} dst_t={fields[2]} "
          f"dst_ch={fields[3]} op=0x{fields[4]:02x} flags={fields[5]}")
