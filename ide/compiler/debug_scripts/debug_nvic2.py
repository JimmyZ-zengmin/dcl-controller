#!/usr/bin/env python3
"""深入检查 NVIC 状态"""
import subprocess, struct

PYOCD = r'C:\Espressif\tools\python\v6.0.1\venv\Scripts\pyocd.exe'

def pyocd(cmd, timeout=10):
    r = subprocess.run(
        [PYOCD, 'commander', '-t', 'stm32h723xx', '-c', cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return r.stdout + r.stderr

print("=" * 60)
print("读取 NVIC 寄存器 (4 bytes each)")
print("=" * 60)

# NVIC 寄存器
# ISER0=0xE000E100, ISER1=0xE000E104
# ISPR0=0xE000E200, ISPR1=0xE000E204
# IABR0=0xE000E300, IABR1=0xE000E304
# IPRI0=0xE000E400...

addrs = {
    'NVIC_ISER0': 0xE000E100,
    'NVIC_ISER1': 0xE000E104,
    'NVIC_ISPR0': 0xE000E200,
    'NVIC_ISPR1': 0xE000E204,
    'NVIC_IABR0': 0xE000E300,
    'NVIC_IABR1': 0xE000E304,
    'SCB_VTOR': 0xE000ED08,
    'SCB_SHCSR': 0xE000ED24,
}

for name, addr in addrs.items():
    out = pyocd(f'read32 0x{addr:08X} 4')
    # 解析第一行的 hex 值
    for line in out.split('\n'):
        if ':' in line:
            hex_part = line.split(':')[1].split('|')[0].strip()
            first_word = hex_part.split()[0] if hex_part.split() else '?'
            print(f"  {name:15s} @0x{addr:08X}: {first_word}")
            break

print("\n" + "=" * 60)
print("读取 TIM1 当前状态")
print("=" * 60)

addrs2 = {
    'TIM1_CR1': 0x40010000,
    'TIM1_DIER': 0x4001000C,
    'TIM1_SR': 0x40010010,
    'TIM1_CNT': 0x40010024,
    'TIM1_PSC': 0x40010028,
    'TIM1_ARR': 0x4001002C,
}

for name, addr in addrs2.items():
    out = pyocd(f'read32 0x{addr:08X} 4')
    for line in out.split('\n'):
        if ':' in line:
            hex_part = line.split(':')[1].split('|')[0].strip()
            first_word = hex_part.split()[0] if hex_part.split() else '?'
            print(f"  {name:10s} @0x{addr:08X}: {first_word}")
            break

print("\n" + "=" * 60)
print("尝试写 NVIC_ICPR1 清除挂起 + 读 ISPR1")
print("=" * 60)
out = pyocd('write32 0xE000E284 0x00000800')  # ICPR1 bit 11
print(out)
out = pyocd('read32 0xE000E204 4')  # ISPR1
print(out)

print("=" * 60)
print("检查 CPU PRIMASK (是否能响应中断)")
print("=" * 60)
# PRIMASK 在 CPSR 的 bit 0
# 通过 MRS 读取 - pyocd 不支持
# 但可以通过 SCB 读 SHCSR
out = pyocd('read32 0xE000ED24 4')
print(out)
