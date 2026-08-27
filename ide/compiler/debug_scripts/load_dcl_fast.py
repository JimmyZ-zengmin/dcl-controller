#!/usr/bin/env python3
"""DCL二进制 → H723 DTCM批量加载 (单次pyocd调用)"""
import struct, subprocess, sys, os

PYOCD = r'C:\Espressif\tools\python\v6.0.1\venv\Scripts\pyocd.exe'

ROUTE_TABLE = 0x20001700
PARAM_TABLE = 0x20005700
ACTIVE_RT   = 0x200000F0

def load(bin_file):
    with open(bin_file, 'rb') as f: data = f.read()
    route_cnt = struct.unpack_from('<I', data, 0)[0]
    param_cnt = struct.unpack_from('<I', data, 4)[0]
    active    = struct.unpack_from('<I', data, 8)[0]
    offset = 16
    route_data = data[offset:offset + route_cnt * 16]
    offset += route_cnt * 16
    param_data = data[offset:offset + param_cnt * 16]

    # 批量pyocd写入: 一条命令写所有路由+参数+active_routes
    cmds = []
    for i in range(0, len(route_data), 4):
        v = struct.unpack_from('<I', route_data, i)[0]
        cmds.append(f'write32 {ROUTE_TABLE+i:08X} {v:08X};')
    for i in range(0, len(param_data), 4):
        v = struct.unpack_from('<I', param_data, i)[0]
        cmds.append(f'write32 {PARAM_TABLE+i:08X} {v:08X};')
    cmds.append(f'write32 {ACTIVE_RT:08X} {active:08X};')
    cmds.append('exit')

    cmd_str = ' '.join(cmds)
    print(f"加载 {route_cnt}条路由 {param_cnt}个参数 ({len(route_data)+len(param_data)}B) ...")
    result = subprocess.run([PYOCD, 'commander', '-t', 'stm32h723xx', '-c', cmd_str],
                           capture_output=True, text=True, timeout=30)
    if result.returncode == 0:
        print(f"✅ 加载成功! ACTIVE_ROUTES={active}")
    else:
        print(f"❌ 失败: {result.stderr[:200]}")

if __name__ == '__main__':
    load(sys.argv[1])
