#!/usr/bin/env python3
"""DMA2 全面诊断: 时钟、 Stream 5 寄存器、 DMAMUX 1"""
import sys
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

PROBE = "000000805059ed5520a4400013dd0702a5a5a5a59796990e"

# 正确地址 (RM0468 验证)
REG = {
    "RCC_AHB1ENR":  0x580244D8,   # DMA1EN=bit0, DMA2EN=bit1, DMAMUX1EN=bit2
    "DMA2_LISR":    0x40020400,
    "DMA2_HISR":    0x40020404,
    "DMA2_S5CR":    0x40020480,   # 实际 (TRM)
    "DMA2_S5NDTR":  0x40020484,
    "DMA2_S5PAR":   0x40020488,
    "DMA2_S5M0AR":  0x4002048C,
    "DMA2_S5FCR":   0x40020494,
    "DMAMUX1_S5CR": 0x40020814,
}

from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.halt()

    ahb1 = t.read32(REG["RCC_AHB1ENR"])
    print(f"RCC_AHB1ENR       = 0x{ahb1:08X}")
    print(f"  DMA1EN={(ahb1>>0)&1}  DMA2EN={(ahb1>>1)&1}  DMAMUX1EN={(ahb1>>2)&1}")

    lisr = t.read32(REG["DMA2_LISR"])
    hisr = t.read32(REG["DMA2_HISR"])
    print(f"DMA2_LISR = 0x{lisr:08X}  HISR=0x{hisr:08X}")

    for name in ["DMA2_S5CR","DMA2_S5NDTR","DMA2_S5PAR","DMA2_S5M0AR","DMA2_S5FCR","DMAMUX1_S5CR"]:
        v = t.read32(REG[name])
        print(f"{name:14s} @ 0x{REG[name]:08X} = 0x{v:08X}")

    # Stream 5 状态解码
    cr = t.read32(REG["DMA2_S5CR"])
    m0ar = t.read32(REG["DMA2_S5M0AR"])
    par = t.read32(REG["DMA2_S5PAR"])
    ndtr = t.read32(REG["DMA2_S5NDTR"])
    print(f"\n--- Stream 5 解码 ---")
    print(f"EN={(cr>>0)&1}  DMEIE={(cr>>1)&1}  TEIE={(cr>>2)&1}  HTIE={(cr>>3)&1}  TCIE={(cr>>4)&1}")
    print(f"DIR={(cr>>6)&3}  CIRC={(cr>>8)&1}  PINC={(cr>>9)&1}  MINC={(cr>>10)&1}")
    print(f"PSIZE={(cr>>11)&3}  MSIZE={(cr>>13)&3}  PL={(cr>>16)&3}")
    print(f"M0AR = 0x{m0ar:08X}  (期待 0x200000E0)")
    print(f"PAR  = 0x{par:08X}   (期待 0x58021014 = GPIOE_ODR)")
    print(f"NDTR = {ndtr}")

    t.resume()
