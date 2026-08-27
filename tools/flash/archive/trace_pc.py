#!/usr/bin/env python3
"""Trace PC to see where CPU is stuck."""
import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    # First run for 200ms
    target.resume()
    time.sleep(0.2)
    target.halt()

    pcs = []
    for i in range(20):
        target.resume()
        time.sleep(0.05)
        target.halt()
        pc = target.read_core_register("pc")
        pcs.append(pc)
        cfsr = target.read32(0xE000ED28)
        print(f"PC = 0x{pc:08X}  CFSR = 0x{cfsr:08X}")

    # Check if PC is stuck
    unique_pcs = set(pcs)
    if len(unique_pcs) == 1:
        print(f"\n⚠️ PC 固定在 0x{pcs[0]:08X} - 死循环或卡住")
    elif len(unique_pcs) <= 3:
        print(f"\n⚠️ PC 在少量地址间循环:")
        for pc in unique_pcs:
            count = pcs.count(pc)
            print(f"  0x{pc:08X}: {count}次")
    else:
        print(f"\n✅ PC 在变化 ({len(unique_pcs)} 个不同地址)")

    # Check DMA at end
    s5par = target.read32(0x40020480)
    s5m0ar = target.read32(0x40020484)
    print(f"\nFinal: S5PAR=0x{s5par:08X}, S5M0AR=0x{s5m0ar:08X}")
