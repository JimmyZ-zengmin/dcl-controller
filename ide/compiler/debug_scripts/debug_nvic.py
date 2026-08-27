#!/usr/bin/env python3
"""通过 pyocd commander 直接读取 — 第二参数是字节数"""
import subprocess

PYOCD = r'C:\Espressif\tools\python\v6.0.1\venv\Scripts\pyocd.exe'

def pyocd(cmd, timeout=10):
    r = subprocess.run(
        [PYOCD, 'commander', '-t', 'stm32h723xx', '-c', cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return r.stdout + r.stderr

print("=" * 60)
print("TIM1_SR (4 bytes)")
print(pyocd('read32 0x40010010 4'))

print("=" * 60)
print("WIRE_MAP 前8个 (32 bytes)")
print(pyocd('read32 0x20000300 32'))

print("=" * 60)
print("SENSOR_MAP 前4个 (16 bytes)")
print(pyocd('read32 0x20000100 16'))

print("=" * 60)
print("ROUTE_TABLE Route 0 (16 bytes)")
print(pyocd('read32 0x20001700 16'))

print("=" * 60)
print("ACTIVE_ROUTES (4 bytes)")
print(pyocd('read32 0x200000F0 4'))

print("=" * 60)
print("NVIC_ISER1 (4 bytes)")
print(pyocd('read32 0xE000E104 4'))
