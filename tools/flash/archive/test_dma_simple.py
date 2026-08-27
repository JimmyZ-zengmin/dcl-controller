#!/usr/bin/env python3
"""
纯软件触发 DMA 搬运测试 (不用引擎,TIM1 也没启动)
目的: 隔离验证 DMA2 Stream5 硬件是否能搬运 SHADOW → GPIOE_ODR

步骤:
  1. reset + halt
  2. 手动配 DMA Stream5 (不依赖固件 main)
  3. 手动触发 (写 EN = 1 + 等 TCIF)
  4. 读 GPIOE_ODR 是否变成 SHADOW 的值
"""
import os, sys, time

os.chdir(r'D:\STM\work\dcl-controller')

# 地址
DMA2_BASE    = 0x40020400
DMAMUX_BASE  = 0x40020800
GPIOE_ODR    = 0x58021014
SHADOW       = 0x200000E0
DWT_CTRL     = 0xE0001000
DWT_CYCCNT   = 0xE0001004

# DMA2 Stream5 寄存器偏移
S5CR   = DMA2_BASE + 0x88
S5NDTR = DMA2_BASE + 0x8C
S5PAR  = DMA2_BASE + 0x90
S5M0AR = DMA2_BASE + 0x94
S5FCR  = DMA2_BASE + 0x9C
HIFCR  = DMA2_BASE + 0x0C    # 中断标志清除
LIFCR  = DMA2_BASE + 0x08

# DMAMUX1 channel 13 (DMA2 Stream5 → CH13)
CH13CR = DMAMUX_BASE + 0x34

# TIM1_CH4 compare event ID = 14 (Table 118)
# 纯软件测试不用 TIM1,直接用 DMAMUX sync mode? 不...纯软件用 "always-on" trigger?

from pyocd.core.helpers import ConnectHelper

def write32(t, addr, val): t.write32(addr, val)
def read32(t, addr): return t.read32(addr)

def main():
    with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
        t = session.target
        t.reset_and_halt()
        time.sleep(0.1)

        # 1. 写已知值到 SHADOW
        write32(t, SHADOW, 0xDEADBEEF)
        shdw = read32(t, SHADOW)
        odr_before = read32(t, GPIOE_ODR)
        print(f'SHADOW (before) = 0x{shdw:08X}')
        print(f'ODR    (before) = 0x{odr_before:08X}')

        # 2. 配 DMA Stream5 (MEM2PER, SHADOW → GPIOE_ODR)
        # 2a. Disable stream
        write32(t, S5CR, 0)
        time.sleep(0.001)
        # 2b. Clear flags
        write32(t, HIFCR, 0x00000F7C)  # clear all stream 5 flags
        write32(t, LIFCR, 0x00000F7C)  # just in case
        # 2c. Config
        write32(t, S5NDTR, 0)  # unlock
        write32(t, S5PAR, GPIOE_ODR)  # peripheral
        write32(t, S5M0AR, SHADOW)    # memory
        write32(t, S5NDTR, 1)  # 1 word
        write32(t, S5FCR, 0)   # direct mode
        # 2d. Enable + start
        # DIR=01(M2P) | CIRC=0 | PL=3 | MINC=1
        cr = (1 << 6) | (3 << 16) | (1 << 10)
        write32(t, S5CR, cr | 1)  # EN = 1

        # 3. 等 TCIF (Transfer Complete flag) 或超时
        for i in range(1000):
            s5cr_val = read32(t, S5CR)
            hifcr_val = read32(t, HIFCR)
            # Stream5 TCIF = HIFCR bit 10 (CTCIF5)
            if (hifcr_val >> 10) & 1:
                print(f'[OK] TCIF set after {i} polls')
                break
            time.sleep(0.001)
        else:
            print(f'[!!] TIMEOUT waiting TCIF. S5CR=0x{s5cr_val:08X} HIFCR=0x{hifcr_val:08X}')

        # 4. 读结果
        dma_result = read32(t, S5M0AR)  # memory address after transfer
        s5ndtr_after = read32(t, S5NDTR)
        odr_after = read32(t, GPIOE_ODR)

        print(f'SHADOW (after)  = 0x{read32(t, SHADOW):08X}')
        print(f'S5NDTR (after)   = {s5ndtr_after}')
        print(f'ODR    (after)   = 0x{odr_after:08X}')

        if odr_after == 0xDEADBEEF:
            print()
            print('[SUCCESS] DMA 搬运工作正常!GPIOE_ODR = SHADOW 值')
        elif odr_after == odr_before:
            print()
            print('[FAIL] DMA 搬运失败!GPIOE_ODR 未变化')
        else:
            print()
            print(f'[UNEXPECTED] ODR 变成 0x{odr_after:08X}')

        # 清标志 + 关闭 stream
        write32(t, HIFCR, 0x00000F7C)
        write32(t, S5CR, 0)

if __name__ == "__main__":
    main()
