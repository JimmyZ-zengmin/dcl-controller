"""深入诊断: 检查VTOR, SCB, 复位状态等"""
import time
from pyocd.core.helpers import ConnectHelper

# 寄存器地址
VTOR = 0xE000ED08
SCB_SHCSR = 0xE000ED24
SCB_CFSR = 0xE000ED28
SCB_HFSR = 0xE000ED2C
RCC_CSR = 0x58024474  # RCC_CSR (复位状态)
RCC_CR = 0x58024400
RCC_CFGR = 0x58024410
TIMING_0 = 0x20000000

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    vtor = target.read32(VTOR)
    shcsr = target.read32(SCB_SHCSR)
    cfsr = target.read32(SCB_CFSR)
    hfsr = target.read32(SCB_HFSR)
    rcc_csr = target.read32(RCC_CSR)
    rcc_cr = target.read32(RCC_CR)
    rcc_cfgr = target.read32(RCC_CFGR)
    timing0 = target.read32(TIMING_0)

    print("=== SCB/向量表 ===")
    print(f"VTOR = 0x{vtor:08X}  (中断向量表地址，应为 0x08000000)")
    print(f"SCB_SHCSR = 0x{shcsr:08X}  (fault使能)")
    print(f"SCB_CFSR = 0x{cfsr:08X}  (fault状态)")
    print(f"SCB_HFSR = 0x{hfsr:08X}  (hardfault)")

    print("\n=== RCC/时钟 ===")
    print(f"RCC_CSR = 0x{rcc_csr:08X}")
    print(f"  IWDGRSTF = {(rcc_csr >> 29) & 1}  (IWDG复位)")
    print(f"  LPWRRSTF = {(rcc_csr >> 30) & 1}  (低功耗复位)")
    print(f"  WWDGRSTF = {(rcc_csr >> 31) & 1}  (窗口看门狗复位)")
    print(f"RCC_CR = 0x{rcc_cr:08X}")
    print(f"  HSIRDY = {(rcc_cr >> 2) & 1}")
    print(f"  PLL1ON = {(rcc_cr >> 24) & 1}")
    print(f"  PLL1RDY = {(rcc_cr >> 25) & 1}")
    print(f"RCC_CFGR = 0x{rcc_cfgr:08X}")
    print(f"  SWS = {(rcc_cfgr >> 3) & 7}  (0=HSI, 3=PLL)")

    print("\n=== 内存 ===")
    print(f"TIMING[0] (DTCM+0x0000) = 0x{timing0:08X}")
    print(f"  应为 0xAA000000 (代码首行设置) 或 0xCC000000 (初始化完成)")
    print(f"  若=0x00000000则代码未运行!")

    if timing0 == 0 and vtor != 0x08000000:
        print("\n❌ VTOR错误 + TIMING未设置 → 可能卡在启动文件或SCB配置错误")
    elif rcc_csr & (1 << 29):
        print("\n⚠️ IWDG复位! 之前复位是因为看门狗")
