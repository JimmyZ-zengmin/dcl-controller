#!/usr/bin/env python3
"""Flash and read DTCM marker to find where code gets stuck."""
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

    marker = target.read32(0x20000000)
    pc = target.read_core_register("pc")
    print(f"DTCM marker = 0x{marker:08X}")
    print(f"PC = 0x{pc:08X}")

    if marker == 0xAA000000:
        print("→ Stuck in SystemInit (after PWR/VOS)")
    elif marker == 0xBB000000:
        print("→ Stuck after GPIO init, before DMA")
    elif marker == 0xCC000001:
        print("→ DMA Stream1 done, stuck in Stream5 config")
    elif marker == 0xCC000002:
        print("→ DMA Stream5 done, stuck in TIM1 config")
    elif marker == 0xCC000003:
        print("→ TIM1 started! Stuck in NVIC or later")
    elif marker == 0x00000BA8 or marker == 0x00000000:
        print("→ Marker not written, code stuck before GPIO init")
    else:
        print(f"→ Unknown marker value")

    # Also check DMA and TIM1 status
    dma_s1cr = target.read32(0x40020428)   # DMA2_S1CR
    dma_s5cr = target.read32(0x40020488)   # DMA2_S5CR
    dma_s1m0ar = target.read32(0x40020434) # DMA2_S1M0AR (should be 0x200000F0)
    tim1_cr1 = target.read32(0x40010000) & 0xFFFF  # TIM1_CR1 (correct addr)
    tim1_arr = target.read32(0x4001002C) & 0xFFFF  # TIM1_ARR
    rcc_apb2 = target.read32(0x580244F0)   # RCC_APB2ENR (correct addr)
    rcc_cfgr = target.read32(0x58024410)   # RCC_CFGR (SWS)
    print(f"\nDMA2_S1CR  =0x{dma_s1cr:08X} EN={dma_s1cr&1}")
    print(f"DMA2_S5CR  =0x{dma_s5cr:08X} EN={dma_s5cr&1}")
    print(f"DMA2_S1M0AR=0x{dma_s1m0ar:08X} (expect 0x200000F0)")
    print(f"TIM1_CR1   =0x{tim1_cr1:04X} CEN={tim1_cr1&1}")
    print(f"TIM1_ARR   =0x{tim1_arr:04X} (expect 13599=0x351F)")
    print(f"RCC_APB2ENR=0x{rcc_apb2:08X} TIM1EN={rcc_apb2&1}")
    print(f"RCC_CFGR   =0x{rcc_cfgr:08X} SWS={(rcc_cfgr>>3)&3}")
