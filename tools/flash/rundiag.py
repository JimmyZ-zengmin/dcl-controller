#!/usr/bin/env python3
"""先跑固件 200ms,再 halt 检查 DMA 时钟和寄存器"""
import sys, time
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

PROBE = "000000805059ed5520a4400013dd0702a5a5a5a59796990e"

REG = {
    "RCC_AHB1ENR":  0x580244D8,
    "DMA2_S5CR":    0x40020480,
    "DMA2_S5NDTR":  0x40020484,
    "DMA2_S5PAR":   0x40020488,
    "DMA2_S5M0AR":  0x4002048C,
    "DMA2_S5FCR":   0x40020494,
    "DMAMUX1_S5CR": 0x40020814,
    "TIM1_CR1":     0x40010000,
    "TIM1_SR":      0x40010010,
}

from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target

    # 跑起来 (不复位,让固件自己跑到 main)
    print("=== Resume for 300ms ===")
    t.resume()
    time.sleep(0.3)
    t.halt()

    pc = t.read_core_register("pc")
    print(f"PC  = 0x{pc:08X}")

    ahb1 = t.read32(REG["RCC_AHB1ENR"])
    print(f"RCC_AHB1ENR = 0x{ahb1:08X}  DMA1EN={(ahb1>>0)&1} DMA2EN={(ahb1>>1)&1} DMAMUX={(ahb1>>2)&1}")

    m0ar = t.read32(REG["DMA2_S5M0AR"])
    par  = t.read32(REG["DMA2_S5PAR"])
    cr   = t.read32(REG["DMA2_S5CR"])
    ndtr = t.read32(REG["DMA2_S5NDTR"])
    dmux = t.read32(REG["DMAMUX1_S5CR"])
    tim_cr = t.read16(REG["TIM1_CR1"])
    tim_sr = t.read16(REG["TIM1_SR"])

    print(f"\nDMA2_S5CR    = 0x{cr:08X}   EN={(cr>>0)&1} CIRC={(cr>>8)&1}")
    print(f"DMA2_S5NDTR  = {ndtr}")
    print(f"DMA2_S5PAR   = 0x{par:08X}  (期待 0x58021014)")
    print(f"DMA2_S5M0AR  = 0x{m0ar:08X}  (期待 0x200000E0)")
    print(f"DMAMUX1_S5CR = 0x{dmux:08X}  (bit7: ReqNB; bit6: SOIE)")
    print(f"\nTIM1_CR1 = 0x{tim_cr:04X}  CEN={tim_cr&1}")
    print(f"TIM1_SR  = 0x{tim_sr:04X}")

    # 写测试: 翻转 SHADOW_GPIO 看 ODR 是否跟着翻
    print("\n── SHADOW_GPIO vs ODR 测试 ──")
    shadow_addr = 0x200000E0
    odr_addr    = 0x58021014
    sh0 = t.read32(shadow_addr)
    od0 = t.read32(odr_addr)
    print(f"SHADOW=0x{sh0:08X}  ODR=0x{od0:08X}")
    # 写一个新值到 SHADOW
    t.write32(shadow_addr, 0x0000FFFF)
    time.sleep(0.05)
    od1 = t.read32(odr_addr)
    print(f"写 SHADOW=0x0000FFFF → 50ms后 ODR=0x{od1:08X} (DMA应该搬运)")
    t.write32(shadow_addr, 0x00000000)
    t.resume()
