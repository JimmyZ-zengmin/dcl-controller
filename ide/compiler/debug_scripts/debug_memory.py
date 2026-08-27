#!/usr/bin/env python3
"""直接读取硬件内存，诊断部署问题"""

import struct
import sys
import subprocess

sys.path.insert(0, '.')
from core.dcl_hardware import ADDRESSES

PYOCD = r'C:\Espressif\tools\python\v6.0.1\venv\Scripts\pyocd.exe'

def read32(addr, count):
    """读取32位数据"""
    cmd = f'read32 0x{addr:08X} {count * 4}'
    result = subprocess.run(
        [PYOCD, 'commander', '-t', 'stm32h723xx', '-c', cmd],
        capture_output=True, text=True, timeout=5
    )
    values = []
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
                        values.append(int(p, 16))
                    except:
                        pass
    return values


def main():
    print("=== 读取 ACTIVE_ROUTES (0x200000F0) ===")
    v = read32(0x200000F0, 1)
    print(f"  ACTIVE_ROUTES = {v[0] if v else 'READ FAILED'}")

    print("\n=== 读取 ROUTE_TABLE 前5个条目 ===")
    # 每个条目16字节 = 4个32位字, 5个条目 = 20个字
    raw = read32(ADDRESSES['ROUTE_TABLE'], 20)
    for i in range(5):
        if i*4 < len(raw):
            # 4个32位字 = 一个16字节条目
            w0 = raw[i*4]
            w1 = raw[i*4+1] if i*4+1 < len(raw) else 0
            w2 = raw[i*4+2] if i*4+2 < len(raw) else 0
            w3 = raw[i*4+3] if i*4+3 < len(raw) else 0

            # 解码16字节为路由条目
            entry_bytes = struct.pack('<IIII', w0, w1, w2, w3)
            fields = struct.unpack('<BBBBBBHHHHxx', entry_bytes)

            print(f"  [{i}] hex: {entry_bytes.hex(' ')}")
            print(f"       src_t={fields[0]} src_i={fields[1]} dst_t={fields[2]} "
                  f"dst_ch={fields[3]} op={fields[4]} flags={fields[5]} "
                  f"pi={fields[6]} so={fields[7]} act={fields[8]} w2={fields[9]}")

    print("\n=== 读取 WIRE_MAP 前16个值 ===")
    raw_wires = read32(ADDRESSES['WIRE_MAP'], 16)
    for i, v in enumerate(raw_wires):
        fval = struct.unpack('f', struct.pack('I', v))[0]
        print(f"  WIRE[{i}] = {fval}")

    print("\n=== 读取 PARAM_TABLE 前3个条目 ===")
    raw_params = read32(ADDRESSES['PARAM_TABLE'], 12)  # 3 entries × 4 words
    for i in range(3):
        if i*4 < len(raw_params):
            entry_bytes = struct.pack('<IIII', *raw_params[i*4:i*4+4])
            vals = struct.unpack('<ffff', entry_bytes)
            print(f"  PARAM[{i}] = {vals}")


if __name__ == '__main__':
    main()
