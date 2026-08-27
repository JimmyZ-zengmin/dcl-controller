#!/usr/bin/env python3
"""Full diagnostic of DMA and TIM1 status."""
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
    time.sleep(2.0)
    target.halt()

    # DTCM markers
    m0 = target.read32(0x20000000)
    m4 = target.read32(0x20000004)
    print(f"DTCM[0]   = 0x{m0:08X}  (Stream1 done marker)")
    print(f"DTCM[4]   = 0x{m4:08X}  (Stream5 done marker)")

    # ADC_RAW and SHADOW_GPIO in DTCM
    adc_raw = target.read32(0x200000F0)
    shadow  = target.read32(0x200000E0)
    print(f"ADC_RAW   = 0x{adc_raw:08X}  (DTCM 0xF0)")
    print(f"SHADOW    = 0x{shadow:08X}  (DTCM 0xE0)")

    # GPIOE ODR
    gpioe_odr = target.read32(0x58021014)
    print(f"GPIOE_ODR = 0x{gpioe_odr:08X}  (should match SHADOW after Stream5)")

    # DMA Stream1 (full)
    s1cr   = target.read32(0x40020428)
    s1ndtr = target.read32(0x4002042C)
    s1par  = target.read32(0x40020430)
    s1m0ar = target.read32(0x40020434)
    s1fcr  = target.read32(0x4002043C)
    print(f"\nDMA2_S1CR  =0x{s1cr:08X}  EN={s1cr&1} DIR={(s1cr>>6)&3} CIRC={(s1cr>>8)&1}")
    print(f"DMA2_S1NDTR=0x{s1ndtr:08X}")
    print(f"DMA2_S1PAR =0x{s1par:08X}  (ADC1_DR expect 0x40022040)")
    print(f"DMA2_S1M0AR=0x{s1m0ar:08X}  (expect 0x200000F0)")
    print(f"DMA2_S1FCR =0x{s1fcr:08X}  DMDIS={(s1fcr>>2)&1}")

    # DMA Stream5 (full)
    s5cr   = target.read32(0x40020488)
    s5ndtr = target.read32(0x4002048C)
    s5par  = target.read32(0x40020490)
    s5m0ar = target.read32(0x40020494)
    s5fcr  = target.read32(0x4002049C)
    print(f"\nDMA2_S5CR  =0x{s5cr:08X}  EN={s5cr&1} DIR={(s5cr>>6)&3} CIRC={(s5cr>>8)&1}")
    print(f"DMA2_S5NDTR=0x{s5ndtr:08X}")
    print(f"DMA2_S5PAR =0x{s5par:08X}  (GPIOE_ODR expect 0x58021014)")
    print(f"DMA2_S5M0AR=0x{s5m0ar:08X}  (expect 0x200000E0)")
    print(f"DMA2_S5FCR =0x{s5fcr:08X}  DMDIS={(s5fcr>>2)&1}")

    # DMA interrupt flags
    dma_lisr = target.read32(0x40020400)
    dma_hisr = target.read32(0x40020404)
    dma_lifcr = target.read32(0x40020408)
    dma_hifcr = target.read32(0x4002040C)
    print(f"\nDMA2_LISR  =0x{dma_lisr:08X}  (Stream1-4 flags)")
    print(f"DMA2_HISR  =0x{dma_hisr:08X}  (Stream5-7 flags)")
    print(f"DMA2_LIFCR =0x{dma_lifcr:08X}")
    print(f"DMA2_HIFCR =0x{dma_hifcr:08X}")

    # DMAMUX
    mux_s1cr = target.read32(0x40020804)
    mux_s5cr = target.read32(0x40020814)
    print(f"\nDMAMUX1_S1CR=0x{mux_s1cr:08X}  (expect ADC1=9, EN bit 0)")
    print(f"DMAMUX1_S5CR=0x{mux_s5cr:08X}  (expect TIM1_UP=15, EN bit 0)")

    # TIM1
    tim1_cr1  = target.read32(0x40010000) & 0xFFFF
    tim1_cr2  = target.read32(0x40010004) & 0xFFFF
    tim1_dier = target.read32(0x4001000C) & 0xFFFF
    tim1_sr   = target.read32(0x40010010) & 0xFFFF
    tim1_arr  = target.read32(0x4001002C) & 0xFFFF
    tim1_cnt  = target.read32(0x40010024) & 0xFFFF
    print(f"\nTIM1_CR1   =0x{tim1_cr1:04X}  CEN={tim1_cr1&1}")
    print(f"TIM1_CR2   =0x{tim1_cr2:04X}  MMS={(tim1_cr2>>4)&7}")
    print(f"TIM1_DIER  =0x{tim1_dier:04X}  UIE={(tim1_dier>>0)&1} UDE={(tim1_dier>>8)&1}")
    print(f"TIM1_SR    =0x{tim1_sr:04X}  UIF={(tim1_sr>>0)&1}")
    print(f"TIM1_CNT   =0x{tim1_cnt:04X}  (timer counter)")
    print(f"TIM1_ARR   =0x{tim1_arr:04X}  (13599=0x351F)")

    # ADC1
    adc1_isr = target.read32(0x40022000)
    adc1_cr  = target.read32(0x40022008)
    print(f"\nADC1_ISR   =0x{adc1_isr:08X}  ADRDY={(adc1_isr>>0)&1} EOC={(adc1_isr>>2)&1}")
    print(f"ADC1_CR    =0x{adc1_cr:08X}  ADEN={(adc1_cr>>0)&1} ADSTART={(adc1_cr>>2)&1}")

    # PC
    pc = target.read_core_register("pc")
    print(f"\nPC         =0x{pc:08X}")
