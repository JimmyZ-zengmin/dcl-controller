#!/usr/bin/env python3
"""测试 write_block 是否正确写入数据"""

import struct
import subprocess
import sys

sys.path.insert(0, '.')
from core.dcl_hardware import Hardware, ADDRESSES

hw = Hardware()
hw.connect()

# 测试1: 写入一个简单的路由条目到 ROUTE_TABLE
print("=== 测试 write_block ===")
test_entry = bytes([0x00, 0x00, 0x03, 0x00, 0x00, 0x01, 0x00, 0x00,
                    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
print(f"写入数据: {test_entry.hex(' ')}")
print(f"目标地址: 0x{ADDRESSES['ROUTE_TABLE']:08X}")

ok = hw.write_block(ADDRESSES['ROUTE_TABLE'], test_entry)
print(f"write_block 返回: {ok}")
if not ok:
    print(f"错误: {hw.last_error}")

# 读取回来验证
print("\n=== 读取验证 ===")
PYOCD = r'C:\Espressif\tools\python\v6.0.1\venv\Scripts\pyocd.exe'
result = subprocess.run(
    [PYOCD, 'commander', '-t', 'stm32h723xx', '-c', f'read32 {ADDRESSES["ROUTE_TABLE"]:08X} 4'],
    capture_output=True, text=True, timeout=5
)
print(f"pyocd stdout: {result.stdout}")
print(f"pyocd stderr: {result.stderr}")
print(f"returncode: {result.returncode}")

# 解析读取结果
raw = []
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
                    raw.append(int(p, 16))
                except:
                    pass

if len(raw) >= 4:
    read_bytes = struct.pack('<IIII', *raw[:4])
    print(f"读取数据: {read_bytes.hex(' ')}")
    if read_bytes == test_entry:
        print("✓ 写入和读取一致!")
    else:
        print("✗ 数据不一致!")
else:
    print(f"✗ 读取失败, 只读到 {len(raw)} 个字")
