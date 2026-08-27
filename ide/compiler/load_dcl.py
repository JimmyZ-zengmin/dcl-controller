#!/usr/bin/env python3
"""
DCL二进制 → H723 DTCM加载工具
用法: python load_dcl.py program.bin
功能: 解析编译器生成的二进制, 通过pyocd写入H723的DTCM,
      然后设置ACTIVE_ROUTES, ISR自动加载新程序
"""
import struct, subprocess, sys

PYOCD = r'C:\Espressif\tools\python\v6.0.1\venv\Scripts\pyocd.exe'

# DTCM地址 (与H723固件一致)
ROUTE_TABLE_ADDR = 0x20001700
PARAM_TABLE_ADDR = 0x20005700
ACTIVE_ROUTES_ADDR = 0x200000F0

def load_binary(bin_file: str):
    with open(bin_file, 'rb') as f:
        data = f.read()

    # Header: route_count(4B) + param_count(4B) + active_routes(4B) + CRC32(4B)
    route_count = struct.unpack_from('<I', data, 0)[0]
    param_count = struct.unpack_from('<I', data, 4)[0]
    active_routes = struct.unpack_from('<I', data, 8)[0]

    offset = 16  # skip header
    route_size = route_count * 16
    param_size = param_count * 16

    route_data = data[offset:offset + route_size]
    offset += route_size
    param_data = data[offset:offset + param_size]

    print(f"路由: {route_count} 条, 参数: {param_count} 条")
    print(f"写入 ROUTE_TABLE @ 0x{ROUTE_TABLE_ADDR:08X} ...")

    # 通过pyocd写入DTCM
    # Step 1: 写入路由表
    for i in range(0, len(route_data), 16):
        chunk = route_data[i:i+16]
        vals = struct.unpack('<IIII', chunk)
        addr = ROUTE_TABLE_ADDR + i
        cmd = f'write32 {addr:08X} {vals[0]:08X}; write32 {addr+4:08X} {vals[1]:08X}; write32 {addr+8:08X} {vals[2]:08X}; write32 {addr+12:08X} {vals[3]:08X}'
        subprocess.run([PYOCD, 'commander', '-t', 'stm32h723xx', '-c', cmd],
                       capture_output=True)

    print(f"写入 PARAM_TABLE @ 0x{PARAM_TABLE_ADDR:08X} ...")
    for i in range(0, len(param_data), 16):
        chunk = param_data[i:i+16]
        vals = struct.unpack('<ffff', chunk)
        addr = PARAM_TABLE_ADDR + i
        cmd = f'write32 {addr:08X} {struct.unpack("<I", struct.pack("<f", vals[0]))[0]:08X}; write32 {addr+4:08X} {struct.unpack("<I", struct.pack("<f", vals[1]))[0]:08X}; write32 {addr+8:08X} {struct.unpack("<I", struct.pack("<f", vals[2]))[0]:08X}; write32 {addr+12:08X} {struct.unpack("<I", struct.pack("<f", vals[3]))[0]:08X}'
        subprocess.run([PYOCD, 'commander', '-t', 'stm32h723xx', '-c', cmd],
                       capture_output=True)

    # Step 3: 设置ACTIVE_ROUTES
    cmd = f'write32 {ACTIVE_ROUTES_ADDR:08X} {active_routes:08X}'
    subprocess.run([PYOCD, 'commander', '-t', 'stm32h723xx', '-c', cmd],
                   capture_output=True)

    print(f"✅ 加载完成! {active_routes}条路由已激活")

if __name__ == '__main__':
    load_binary(sys.argv[1])
