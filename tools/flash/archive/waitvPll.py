#!/usr/bin/env python3
"""Resume 5秒期间,每50ms 记录 PC,看 SystemInit 进展 + VOS 转换状态"""
import sys, time
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

PROBE = "000000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.resume()
    t0 = time.time()
    while time.time() - t0 < 6:
        time.sleep(0.05)
        t.halt()
        pc = t.read_core_register("pc")
        vosh = (t.read32(0x58024818) >> 16) & 3   # VOS bits
        vos_rdy = (t.read32(0x58024818) >> 13) & 1  # VOSRDY
        pll_on = (t.read32(0x58024400) >> 24) & 1
        pll_rdy = (t.read32(0x58024400) >> 25) & 1
        sw_status = (t.read32(0x58024410) >> 2) & 3
        ahb1en = t.read32(0x580244D8)
        t.resume()
        print(f"t={time.time()-t0:5.2f}s  PC=0x{pc:08X}  VOS={vosh}(RDY={vos_rdy})  PLLON={pll_on} PLLRDY={pll_rdy}  SW={sw_status}  RCC_AHB1ENR=0x{ahb1en:08X}")
        if pc < 0x08001E98 or pc > 0x08002938:
            print("  → PC 已跳出 SystemInit 范围!")
            break
        if pc >= 0x08003000:
            print("  → PC 进入 main 区域 (0x08003x)!")
            break
