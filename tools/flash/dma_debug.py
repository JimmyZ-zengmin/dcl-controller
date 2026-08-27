#!/usr/bin/env python3
"""Flash and verify with DMA error flag check."""
import time
from pyocd.core.helpers import ConnectHelper
from pyocd.flash.file_programmer import FileProgrammer

ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target
    programmer = FileProgrammer(session, chip_erase="sector")
    programmer.program(ELF, file_format="elf")

    target.reset_and_halt()
    target.resume()
    time.sleep(1.5)
    target.halt()

    # DMA interrupt status
    lisr = target.read32(0x40020400)  # DMA_LISR
    hisr = target.read32(0x40020404)  # DMA_HISR
    print(f"DMA_LISR = 0x{lisr:08X}")
    print(f"DMA_HISR = 0x{hisr:08X}")

    # Stream 5 detailed
    s5cr = target.read32(0x40020488)
    s5ndtr = target.read32(0x4002048C)
    s5par = target.read32(0x40020490)
    s5m0ar = target.read32(0x40020494)
    s5fcr = target.read32(0x4002049C)
    print(f"\nS5CR   = 0x{s5cr:08X}  EN={s5cr&1}  TCIE={((s5cr>>4)&1)}  DIR={(s5cr>>6)&3}")
    print(f"S5NDTR = {s5ndtr}")
    print(f"S5PAR  = 0x{s5par:08X}")
    print(f"S5M0AR = 0x{s5m0ar:08X}")
    print(f"S5FCR  = 0x{s5fcr:08X}  DMDIS={((s5fcr>>2)&1)}  FEIE={((s5fcr>>7)&1)}")

    # HISR Stream5 flags
    tcif5 = (hisr >> 22) & 1
    htif5 = (hisr >> 23) & 1
    teif5 = (hisr >> 24) & 1
    dmeif5 = (hisr >> 25) & 1
    feif5 = (hisr >> 26) & 1
    print(f"\nStream5 Flags: TCIF={tcif5} HTIF={htif5} TEIF={teif5} DMEIF={dmeif5} FEIF={feif5}")
    if teif5: print("  ❌ Transfer Error!")
    if dmeif5: print("  ❌ Direct Mode Error!")
    if feif5: print("  ❌ FIFO Error!")
    if tcif5: print("  ✅ Transfer Complete")

    # Stream 1
    s1cr = target.read32(0x40020428)
    s1m0ar = target.read32(0x40020434)
    print(f"\nS1CR   = 0x{s1cr:08X}  EN={s1cr&1}")
    print(f"S1M0AR = 0x{s1m0ar:08X}")

    # DMAMUX
    mux_s5 = target.read32(0x40020814)
    print(f"\nDMAMUX_S5CR = 0x{mux_s5:08X}  REQ={mux_s5&0x3F}  EREQ={(mux_s5>>16)&1}")
    mux_s1 = target.read32(0x40020804)
    print(f"DMAMUX_S1CR = 0x{mux_s1:08X}  REQ={mux_s1&0x3F}")

    # GPIOE
    odr = target.read32(0x58021014)
    shadow = target.read32(0x200000E0)
    print(f"\nGPIOE_ODR    = 0x{odr:08X}")
    print(f"SHADOW_GPIO  = 0x{shadow:08X}")

    # TIM1
    tim1_cr1 = target.read32(0x40012C00) & 0xFFFF
    tim1_sr = target.read32(0x40012C10) & 0xFFFF
    tim1_dier = target.read32(0x40012C0C) & 0xFFFF
    print(f"\nTIM1_CR1 = 0x{tim1_cr1:04X}  CEN={tim1_cr1&1}")
    print(f"TIM1_SR  = 0x{tim1_sr:04X}  UIF={tim1_sr&1}")
    print(f"TIM1_DIER = 0x{tim1_dier:04X}  UIE={tim1_dier&1}  UDE={((tim1_dier>>8)&1)}")
