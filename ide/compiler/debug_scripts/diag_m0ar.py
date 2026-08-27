#!/usr/bin/env python3
"""
专门排查 DMA M0AR 问题
"""
import sys, os, struct, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dcl_compiler import DCLCompiler
from dcl_hardware import Hardware, ADDRESSES, TIM1_BASE

TEST_DCL = """
SENSOR a FROM ADC1_CH0 SCALE 1.0 0.0
SENSOR b FROM ADC1_CH1 SCALE 1.0 0.0
SENSOR c FROM ADC1_CH2 SCALE 1.0 0.0
SENSOR d FROM ADC1_CH3 SCALE 1.0 0.0
LOGIC all_high = a AND b AND c AND d
LOGIC any_high = a OR b OR c OR d
LOGIC not_a_and_b = NOT a AND b
OUTPUT all_high TO GPIO_PE0
OUTPUT any_high TO GPIO_PE1
OUTPUT not_a_and_b TO GPIO_PE2
"""

DMA2_S5CR    = 0x40020478
DMA2_S5NDTR  = 0x4002047C
DMA2_S5PAR   = 0x40020480
DMA2_S5M0AR  = 0x40020484
DMA2_S5FCR   = 0x4002048C
DMAMUX1_S5CR = 0x40020888

def main():
    print("═══ DMA M0AR 专项诊断 ═══\n")

    c = DCLCompiler()
    c.parse(TEST_DCL)
    c.topological_sort()
    c.validate_resources()
    binary = c.generate_binary()

    hw = Hardware()
    if not hw.connect():
        print("连接失败!"); return
    ok = hw.deploy(binary)
    if not ok:
        print(f"部署失败: {hw.last_error}")
        return

    print("部署完成\n")

    # 读 DMA2 Stream 5 所有寄存器
    print("── DMA2 Stream 5 寄存器 ──")
    for name, addr in [
        ("S5CR",   0x40020478),
        ("S5NDTR", 0x4002047C),
        ("S5PAR",  0x40020480),
        ("S5M0AR", 0x40020484),
        ("S5M1AR", 0x40020488),
        ("S5FCR",  0x4002048C),
    ]:
        val = hw.read32(addr, 1)[0]
        print(f"  {name} (0x{addr:08X}) = 0x{val:08X}")

    # 对比: 读 SHADOW_GPIO 实际存储的地址和内容
    print("\n── SHADOW_GPIO 区域 (DTCM 0x20000280) ──")
    for offset in [0x0280, 0x0284, 0x0288, 0x0290]:
        addr = 0x20000000 + offset
        val = hw.read32(addr, 1)[0]
        print(f"  [0x{addr:08X}] = 0x{val:08X}")

    # 读 startup 后未使用寄存器 (如果是 0 表示 DMA 完全未初始化)
    print("\n── DMA2 Stream 5 周边 (验证时钟/总线) ──")
    for offset in [0x70, 0x74, 0x78, 0x7C, 0x80, 0x84, 0x88, 0x8C, 0x90, 0x94, 0x98]:
        addr = 0x40020400 + offset
        val = hw.read32(addr, 1)[0]
        print(f"  DMA2+0x{offset:02X} (0x{addr:08X}) = 0x{val:08X}")

    print("\n完成.")
    hw.disconnect()

if __name__ == "__main__":
    main()
