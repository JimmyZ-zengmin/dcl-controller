#!/usr/bin/env python3
"""Resume CPU, wait, then check state."""
import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    target.resume()
    print("Resumed. Waiting 500ms...")
    time.sleep(0.5)

    target.halt()

    pc = target.read_core_register("pc")
    sp = target.read_core_register("sp")
    lr = target.read_core_register("lr")

    # Fault registers
    cfsr = target.read32(0xE000ED28)
    hfsr = target.read32(0xE000ED2C)

    # DMA Stream 5
    s5cr   = target.read32(0x40020478)
    s5ndtr = target.read32(0x4002047C)
    s5par  = target.read32(0x40020480)
    s5m0ar = target.read32(0x40020484)

    # GPIOE
    odr = target.read32(0x58021014)

    # DTCM
    shadow = target.read32(0x200000E0)

    print(f"\nAfter running 500ms:")
    print(f"PC = 0x{pc:08X}, SP = 0x{sp:08X}, LR = 0x{lr:08X}")
    print(f"CFSR = 0x{cfsr:08X}, HFSR = 0x{hfsr:08X}")
    print(f"S5CR = 0x{s5cr:08X}, S5NDTR = 0x{s5ndtr:08X}, S5PAR = 0x{s5par:08X}, S5M0AR = 0x{s5m0ar:08X}")
    print(f"GPIOE_ODR = 0x{odr:08X}")
    print(f"SHADOW_GPIO = 0x{shadow:08X}")

    if pc == 0x080002DC or pc == 0x080002DD:
        print(f"\n⚠️ CPU 还在 Reset_Handler - 启动代码循环或卡住")
    elif hfsr != 0 or cfsr != 0:
        print(f"\n⚠️ CPU 故障!")
    elif s5m0ar == 0x200000E0:
        print(f"\n✅ DMA Stream 5 配置正确!")
    elif s5m0ar == 0:
        print(f"\n❌ DMA 未配置 - main() 未到达 DMA init")
