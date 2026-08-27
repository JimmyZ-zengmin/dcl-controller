#!/usr/bin/env python3
"""
DMA 输出路径最小验证 (运行时直接写 EN=1)

假设:
  1. MCU 正在运行,SAMPLES 持续增长(ISR 在跑)
  2. DMA2_S5 已经配置好(历史数据证明 PAR/NDTR/MUX 都是对的)
  3. 唯一的问题是 EN=0

做法:
  A. 读 DMA2_S5CR 确认 EN=0
  B. 直接写 EN=1 (不 halt)
  C. 等 10ms (100 周期)
  D. 读 GPIOE_ODR 看是否 = 0xFFFFFFFF

如果 ODR 变 0xFFFFFFFF -> 问题: 固件没启动 DMA (非硬件故障)
如果 ODR 仍然是 0 -> 问题: 硬件/DMA 通路 (进一步诊断)
"""
import time
from pyocd.core.helpers import ConnectHelper

DMA2_S5CR  = 0x40020488
DMA2_S5NDTR= 0x4002048C
SHADOW     = 0x200000E0
GPIOE_ODR  = 0x58021014

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target

    print("=== Before EN=1 (running) ===")
    cr_before = t.read32(DMA2_S5CR)
    m0ar      = t.read32(DMA2_S5CR + 0x0C)  # DMA2_S5M0AR = 0x40020494
    par       = t.read32(DMA2_S5CR + 0x08)  # DMA2_S5PAR  = 0x40020490
    print(f"DMA2_S5CR   = 0x{cr_before:08X} (EN={'YES' if cr_before&1 else 'NO'})")
    print(f"DMA2_S5M0AR = 0x{m0ar:08X}   (expect 0x200000E0)")
    print(f"DMA2_S5PAR  = 0x{par:08X}   (expect 0x58021014)")
    print(f"DMA2_S5NDTR = {t.read32(DMA2_S5NDTR)}")
    print(f"SHADOW      = 0x{t.read32(SHADOW):08X}")

    # 写完整 cr 配置 (CIRC=1, DIR=M2P, EN=1, PL=3, 32-bit)
    new_cr = 0x00035140 | 1
    t.write32(DMA2_S5CR, new_cr)
    print(f"\n>>> Wrote DMA2_S5CR = 0x{new_cr:08X}")

    time.sleep(0.02)  # 等待 2ms

    print("\n=== After EN=1, 2ms later ===")
    cr_after = t.read32(DMA2_S5CR)
    print(f"DMA2_S5CR   = 0x{cr_after:08X} (EN={'YES' if cr_after&1 else 'NO'})")
    odr = t.read32(GPIOE_ODR)
    shadow = t.read32(SHADOW)
    print(f"SHADOW      = 0x{shadow:08X}")
    print(f"GPIOE_ODR   = 0x{odr:08X}")

    if odr == 0xFFFFFFFF:
        print("\n[PASS] DMA 输出工作! 地址和硬件都没问题,启动即可")
        print("  固件需在 DMA 配置完成后置 EN=1,并在 while(1) 中保持")
    elif odr != 0:
        print(f"\n[PARTIAL] GPIOE_ODR = 0x{odr:08X}")
    else:
        print("\n[FAIL] GPIOE_ODR 仍为 0, EN=1 后无输出")
        print("  可能硬件 MPU 阻碍 DMA 访问 GPIO,或 TIM1_UP 未真实触发")
        # 诊断: 直接写 NDTR=1 触发一次
        t.write32(DMA2_S5NDTR, 0)
        t.write32(DMA2_S5NDTR, 1)
        time.sleep(0.005)
        odr2 = t.read32(GPIOE_ODR)
        print(f"  二次触发后 GPIOE_ODR = 0x{odr2:08X}")
