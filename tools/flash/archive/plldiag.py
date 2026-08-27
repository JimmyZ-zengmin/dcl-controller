#!/usr/bin/env python3
"""PLL 死锁诊断: 读 RCC 相关寄存器,查卡在哪一步"""
import sys, time
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

PROBE = "000000805059ed5520a4400013dd0702a5a5a5a59796990e"

RCC = {
    "CR":         0x58024400,
    "CFGR":       0x58024410,
    "CK_D1CFGR":  0x58024418,   # was RCC_BASE+0x18
    "CK_D2CFGR":  0x5802441C,   # was RCC_BASE+0x1C
    "PLLCKSELR":  0x5802442C,
    "PLLCFGR":    0x58024430,
    "PLL1DIVR":   0x58024434,
}
PWR = {
    "CR1": 0x58024800,
    "CR3": 0x5802480C,
    "CSR1": 0x58024814,
}
FLASH = {
    "ACR": 0x52002000,
}
SCB = {
    "CPACR": 0xE000ED88,
    "VTOR":  0xE000ED08,
    "CFSR":  0xE000ED28,
    "HFSR":  0xE000ED2C,
}

from pyocd.core.helpers import ConnectHelper

def dump(dev):
    for name, addr in dev.items():
        v = t.read32(addr)
        print(f"  {name:12s} @ 0x{addr:08X} = 0x{v:08X}")

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.halt()
    pc = t.read_core_register("pc")
    print(f"PC = 0x{pc:08X}")

    print("\n── RCC Registers ──")
    dump(RCC)
    cr = t.read32(RCC["CR"])
    print(f"\n  RCC_CR  bit24(PLL1ON)={(cr>>24)&1}  bit25(PLL1RDY)={(cr>>25)&1}")
    cfgr = t.read32(RCC["CFGR"])
    print(f"  RCC_CFGR SW[1:0]={cfgr&3}  SWS[3:2]={(cfgr>>2)&3}")

    print("\n── PWR Registers ──")
    dump(PWR)
    pwr3 = t.read32(PWR["CR3"])
    print(f"  PWR_CR3 VOS[17:16]={(pwr3>>16)&3}  bit6(AVDOSRDY)={(pwr3>>6)&1}")

    print("\n── FLASH Registers ──")
    dump(FLASH)

    print("\n── SCB Fault Registers ──")
    dump(SCB)
    cfsr = t.read32(SCB["CFSR"])
    print(f"  CFSR = 0x{cfsr:08X}  (如果非0,表明发生fault)")
    hfsr = t.read32(SCB["HFSR"])
    print(f"  HFSR = 0x{hfsr:08X}")

    t.resume()
