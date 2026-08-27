#!/usr/bin/env python3
"""
DMA2 Stream5 最小化验证脚本

目的: 隔离 DMA 输出失败的根因
方法:
  1. 读 RCC_AHB1ENR 确认 DMA2 时钟
  2. 写 SHADOW_GPIO = 0xDEADBEEF (手动触发)
  3. 手动配置 DMA2 Stream5 (绕过固件代码)
  4. 触发一次 DMA 传输 (CNDTR 写 1)
  5. 读 GPIOE_ODR 看是否变成 0xDEADBEEF

如果步骤5成功 -> 硬件通路正常，问题在固件初始化顺序
如果步骤5失败 -> 硬件/MPU/总线矩阵限制，需换方案
"""

import sys, time
try:
    from pyocd.core.target import Target
    from pyocd.core.helpers import ConnectHelper
    from pyocd.debug.elf.symbols import ELFSymbolProvider
except ImportError:
    print("ERROR: pyocd not installed. Run: pip install pyocd")
    sys.exit(1)

# ── 地址表 ──
RCC_BASE       = 0x58024400
RCC_AHB1ENR    = RCC_BASE + 0x0D8
DMA2_BASE      = 0x40020400
DMA2_LIFCR     = DMA2_BASE + 0x08
DMA2_HIFCR     = DMA2_BASE + 0x0C
DMA2_S5CR      = DMA2_BASE + 0x88
DMA2_S5NDTR    = DMA2_BASE + 0x8C
DMA2_S5PAR     = DMA2_BASE + 0x90
DMA2_S5M0AR    = DMA2_BASE + 0x94
DMA2_S5FCR     = DMA2_BASE + 0x9C
DMAMUX1_BASE   = 0x40020800
DMAMUX1_S5CR   = DMAMUX1_BASE + 0x14
GPIOE_BASE     = 0x58021000
GPIOE_ODR      = GPIOE_BASE + 0x14
SHADOW_GPIO    = 0x200000E0

def read32(target, addr):
    return target.read32(addr)

def write32(target, addr, val):
    target.write32(addr, val)

def main():
    with ConnectHelper.session_with_chosen_probe() as session:
        target = session.board.target
        target.reset_and_halt()
        print(f"[OK] Connected: {target.part_number}")

        # ── Step 1: RCC 时钟检查 ──
        ahb1 = read32(target, RCC_AHB1ENR)
        dma2_en = (ahb1 >> 1) & 1
        dmamux_en = (ahb1 >> 2) & 1
        print(f"[1] RCC_AHB1ENR = 0x{ahb1:08X}")
        print(f"    DMA2EN={'YES' if dma2_en else 'NO'}  DMAMUX1EN={'YES' if dmamux_en else 'NO'}")

        # ── Step2: 写 SHADOW_GPIO ──
        write32(target, SHADOW_GPIO, 0xDEADBEEF)
        shadow = read32(target, SHADOW_GPIO)
        print(f"[2] SHADOW_GPIO = 0x{shadow:08X} (expected 0xDEADBEEF)")

        # ── Step3: DMA Stream5 手动配置 ──
        print("[3] Configuring DMA Stream5 manually...")

        # 禁用 Stream5
        write32(target, DMA2_S5CR, 0)
        time.sleep(0.001)
        cr = read32(target, DMA2_S5CR)
        print(f"    DMA2_S5CR (after disable) = 0x{cr:08X}")

        # 清标志
        write32(target, DMA2_HIFCR, 0x0F000000)

        # DMAMUX: TIM1_UP = ch15
        write32(target, DMAMUX1_S5CR, 15)
        mux = read32(target, DMAMUX1_S5CR)
        print(f"    DMAMUX1_S5CR = 0x{mux:02X} (expected 0F)")

        # 设 NDTR=0 解锁 M0AR
        write32(target, DMA2_S5NDTR, 0)
        write32(target, DMA2_S5PAR, GPIOE_ODR)
        write32(target, DMA2_S5M0AR, SHADOW_GPIO)
        write32(target, DMA2_S5NDTR, 1)
        write32(target, DMA2_S5FCR, 0)

        par  = read32(target, DMA2_S5PAR)
        m0ar = read32(target, DMA2_S5M0AR)
        ndtr = read32(target, DMA2_S5NDTR)
        print(f"    PAR  = 0x{par:08X} (expected 0x{GPIOE_ODR:08X})")
        print(f"    M0AR = 0x{m0ar:08X} (expected 0x{SHADOW_GPIO:08X})")
        print(f"    NDTR = {ndtr} (expected 1)")

        # 如果 PAR/M0AR 回读 0 -> 寄存器写不进去 -> 时钟或地址问题
        if par == 0 or m0ar == 0:
            print("[!!] DMA 寄存器写入失败 -> DMA2 时钟未起效 或 寄存器地址错")
            print("    检查 RCC_AHB1ENR 的 DMA2EN 位")
            return

        # ── Step4: 启动单次.transfer (非循环，手动触发) ──
        # 内存→外设, 单次, P=32bit, M=32bit, PL=高, EN=1
        # 触发器: 直接写 CNDTR 重新加载 (先关 EN, 再设 NDTR=1, 再开 EN)
        cr_val = (1 << 6)   # DIR=01 (M2P)
        cr_val |= (0 << 8)   # CIRC=0 (单次)
        cr_val |= (2 << 11)  # PSIZE=32
        cr_val |= (2 << 13)  # MSIZE=32
        cr_val |= (3 << 16)  # PL=高
        cr_val |= 1          # EN=1

        write32(target, DMA2_S5CR, cr_val)
        cr_rb = read32(target, DMA2_S5CR)
        print(f"[4] DMA2_S5CR = 0x{cr_rb:08X} (set 0x{cr_val:08X})")

        # 因为无外部触发，需要手动写 NDTR=1 重新加载以启动一次传输
        write32(target, DMA2_S5CR, 0)  # 关 EN
        time.sleep(0.001)
        write32(target, DMA2_S5NDTR, 0)  # 解锁
        write32(target, DMA2_S5NDTR, 1)  # 设 NDTR
        write32(target, DMA2_S5CR, cr_val)  # EN

        time.sleep(0.01)  # 等传输完成
        ndtr_after = read32(target, DMA2_S5NDTR)
        print(f"    NDTR after transfer = {ndtr_after} (0=传输完成)")

        # ── Step5: 读 GPIOE_ODR ──
        odr = read32(target, GPIOE_ODR)
        print(f"[5] GPIOE_ODR = 0x{odr:08X} (expected 0xDEADBEEF)")

        if (odr & 0xDEADBEEF) == 0xDEADBEEF:
            print("[PASS] DMA 硬件通路正常! 固件初始化逻辑有问题 (时序/优化)")
        elif odr != 0:
            print(f"[????] GPIOE 有值但不对, 部分位生效 -> 检查 GPIO 配置")
        else:
            print("[FAIL] GPIOE_ODR 仍为 0")
            print("  可能原因:")
            print("  a) MPU/AHB 矩阵阻止 DMA 访问 DTCM (ITCM/DTCM 的 DMA 限制)")
            print("  b) 单次模式无硬件触发源时传输不启动 (需 SW 触发)")
            print("  c) GPIOE 未配置为输出")
            # 额外测: 直接写 GPIOE_ODR 看 CPU 是否可写
            write32(target, GPIOE_ODR, 0xAAAAAAAA)
            odr2 = read32(target, GPIOE_ODR)
            print(f"  CPU写GPIOE=0xAAAAAAAA -> 回读=0x{odr2:08X}")
            if odr2 == 0xAAAAAAAA:
                print("  -> CPU 可写 GPIO 正常，纯 DMA 通路问题")

        target.reset()

if __name__ == "__main__":
    main()
