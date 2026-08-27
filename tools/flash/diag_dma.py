#!/usr/bin/env python3
"""Deep DMA + TIM1 diagnostic."""
import time
from pyocd.core.helpers import ConnectHelper
from pyocd.flash.file_programmer import FileProgrammer

ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target
    programmer = FileProgrammer(session, chip_erase="sector")
    programmer.program(ELF, file_format="elf")
    print("=== Flashed ===")

    target.reset_and_halt()
    target.resume()
    time.sleep(1.5)
    target.halt()

    pc = target.read_core_register("pc")
    lr = target.read_core_register("lr")
    print(f"PC=0x{pc:08X}  LR=0x{lr:08X}")

    # DMA Stream1
    print(f"\n=== DMA2 Stream1 (ADC→DTCM) ===")
    for name, off in [("CR",0x28),("NDTR",0x2C),("PAR",0x30),("M0AR",0x34),("FCR",0x3C)]:
        val = target.read32(0x40020400 + off)
        print(f"  S1{name:5s} = 0x{val:08X}")

    # DMA Stream5
    print(f"\n=== DMA2 Stream5 (SHADOW→ODR) ===")
    for name, off in [("CR",0x88),("NDTR",0x8C),("PAR",0x90),("M0AR",0x94),("FCR",0x9C)]:
        val = target.read32(0x40020400 + off)
        print(f"  S5{name:5s} = 0x{val:08X}")

    # DMA interrupt flags
    lisr = target.read32(0x40020408)  # LISR
    hisr = target.read32(0x4002040C)  # HISR
    print(f"\nDMA2_LISR = 0x{lisr:08X}")
    print(f"  Stream1 TEIF={(lisr>>23)&1} DMEIF={(lisr>>22)&1} FEIF={(lisr>>21)&1} TCIF={(lisr>>19)&1}")
    print(f"  Stream0 TEIF={(lisr>>5)&1}  DMEIF={(lisr>>4)&1}  FEIF={(lisr>>3)&1}")
    print(f"\nDMA2_HISR = 0x{hisr:08X}")
    print(f"  Stream5 TEIF={(hisr>>23)&1} DMEIF={(hisr>>22)&1} FEIF={(hisr>>21)&1} TCIF={(hisr>>19)&1}")
    print(f"  Stream5 HTIF={(hisr>>20)&1}")

    # Fault status
    cfsr = target.read32(0xE000ED28)
    hfsr = target.read32(0xE000ED2C)
    bfar = target.read32(0xE000ED38)
    print(f"\nCFSR=0x{cfsr:08X}  HFSR=0x{hfsr:08X}  BFAR=0x{bfar:08X}")

    # TIM1
    print(f"\n=== TIM1 ===")
    for name, off in [("CR1",0x00),("CR2",0x04),("DIER",0x0C),("SR",0x10),
                       ("CNT",0x24),("PSC",0x28),("ARR",0x2C),("CCR4",0x40),("BDTR",0x44)]:
        val = target.read32(0x40012C00 + off)
        print(f"  TIM1_{name:5s} = 0x{val:08X}")

    # RCC
    rcc_apb2 = target.read32(0x58024490)
    print(f"\nRCC_APB2ENR=0x{rcc_apb2:08X}  TIM1EN={rcc_apb2&1}")
