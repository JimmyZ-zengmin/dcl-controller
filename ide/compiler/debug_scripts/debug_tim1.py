#!/usr/bin/env python3
"""读取 TIM1 寄存器状态，诊断 ISR 引擎是否运行"""
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core'))
from dcl_hardware import Hardware, TIM1_BASE

import os
hw = Hardware()
if not hw.connect():
    print("连接失败!"); sys.exit(1)

# 手动读取 TIM1 所有相关寄存器
regs = {
    'CR1  (0x00)': TIM1_BASE + 0x00,
    'CR2  (0x04)': TIM1_BASE + 0x04,
    'SMCR (0x08)': TIM1_BASE + 0x08,
    'DIER (0x0C)': TIM1_BASE + 0x0C,
    'SR   (0x10)': TIM1_BASE + 0x10,
    'EGR  (0x14)': TIM1_BASE + 0x14,
    'CNT  (0x24)': TIM1_BASE + 0x24,
    'PSC  (0x28)': TIM1_BASE + 0x28,
    'ARR  (0x2C)': TIM1_BASE + 0x2C,
    'RCR  (0x30)': TIM1_BASE + 0x30,
}

print("=" * 60)
print("TIM1 寄存器状态 (deploy 后)")
print("=" * 60)

# 批量读取
raw = hw.read32(TIM1_BASE, 14)  # 0x00 ~ 0x34, 14 words
if not raw:
    print("读取失败!")
    sys.exit(1)

names = ['CR1', 'CR2', 'SMCR', 'DIER', 'SR', 'EGR', 'CCMR1', 'CCMR2',
         'CCER', 'CNT', 'PSC', 'ARR', 'RCR', 'CCR1']
for i, v in enumerate(raw):
    name = names[i] if i < len(names) else f'?0x{i*4:02X}'
    addr = TIM1_BASE + i * 4
    bits = ''
    if i == 0:  # CR1
        bits = f" CEN={v&1} URS={v>>1&1} UDIS={v>>2&1}"
    elif i == 3:  # DIER
        bits = f" UIE={v&1}"
    elif i == 4:  # SR
        bits = f" UIF={v&1}"
    elif i == 9:  # CNT
        bits = f" counter={v}"
    elif i == 10:  # PSC
        bits = f" prescale={v}"
    elif i == 11:  # ARR
        bits = f" period={v} ({(v+1)/136.0:.1f}us @136MHz)"
    print(f"  {name:6s} @0x{addr:08X}: 0x{v:08X}{bits}")

# 检查 NVIC 中 TIM1_UP 是否使能
print("\n" + "=" * 60)
print("检查 NVIC 中断使能")
print("=" * 60)

# NVIC_ISER1 address for TIM1_UP_IRQn = 43
# TIM1_UP is IRQ 43, which is in NVIC_ISER1 (bits 32-63)
# NVIC_ISER0 = 0xE000E100, NVIC_ISER1 = 0xE000E104
nvic_iser1 = 0xE000E104
raw = hw.read32(nvic_iser1, 1)
if raw:
    # IRQ 43 is bit (43-32) = 11 in ISER1
    tim1_enabled = (raw[0] >> 11) & 1
    print(f"  NVIC_ISER1: 0x{raw[0]:08X}")
    print(f"  TIM1_UP_IRQ (bit 11): {'✓ 已使能' if tim1_enabled else '✗ 未使能'}")

# 读一次 CNT, 等一下再读, 看是否变化
print("\n" + "=" * 60)
print("检查 TIM1 CNT 是否计数")
print("=" * 60)
cnt1 = hw.read32(TIM1_BASE + 0x24, 1)[0]
import time
time.sleep(0.01)  # 10ms
cnt2 = hw.read32(TIM1_BASE + 0x24, 1)[0]
print(f"  CNT @ t0: {cnt1}")
print(f"  CNT @ t0+10ms: {cnt2}")
print(f"  Δ = {cnt2 - cnt1} (定时器{'运行中 ✓' if cnt2 != cnt1 else '已停止 ✗'})")
