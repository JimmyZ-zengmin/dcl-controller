#!/usr/bin/env python3
"""Flash DMA version, verify ISR + DMA work at 272MHz."""
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
    print("Running at 272MHz (DMA input + output)...")
    time.sleep(1.5)

    target.halt()
    pc = target.read_core_register("pc")
    print(f"\nPC = 0x{pc:08X}")

    # RCC
    cr = target.read32(0x58024400)
    cfgr = target.read32(0x58024410)
    apb2enr = target.read32(0x58024490)
    print(f"RCC_CR     = 0x{cr:08X}  PLL1ON={(cr>>24)&1} PLL1RDY={(cr>>25)&1}")
    print(f"RCC_CFGR   = 0x{cfgr:08X}  SWS={(cfgr>>3)&7} (3=PLL)")
    print(f"RCC_APB2ENR= 0x{apb2enr:08X}  TIM1EN={apb2enr & 1}")

    # DMA
    dma_s1cr = target.read32(0x40020428)
    dma_s1m0ar = target.read32(0x40020434)
    dma_s5cr = target.read32(0x40020488)
    dma_s5m0ar = target.read32(0x40020494)
    dma_s5par = target.read32(0x40020490)
    print(f"\nDMA2_S1CR   = 0x{dma_s1cr:08X}  EN={dma_s1cr & 1}")
    print(f"DMA2_S1M0AR = 0x{dma_s1m0ar:08X}  (expect 0x200000F0)")
    print(f"DMA2_S5CR   = 0x{dma_s5cr:08X}  EN={dma_s5cr & 1}")
    print(f"DMA2_S5M0AR = 0x{dma_s5m0ar:08X}  (expect 0x200000E0)")
    print(f"DMA2_S5PAR  = 0x{dma_s5par:08X}  (expect 0x58021014 GPIOE_ODR)")

    # TIM1
    tim1_cr1 = target.read32(0x40012C00) & 0xFFFF
    tim1_sr = target.read32(0x40012C10) & 0xFFFF
    tim1_cnt = target.read32(0x40012C24) & 0xFFFF
    tim1_ccr4 = target.read32(0x40012C40) & 0xFFFF
    print(f"\nTIM1_CR1 = 0x{tim1_cr1:04X}  CEN={tim1_cr1&1}")
    print(f"TIM1_SR  = 0x{tim1_sr:04X}  UIF={tim1_sr&1}")
    print(f"TIM1_CNT = {tim1_cnt}  CCR4={tim1_ccr4}")

    # ADC + DTCM
    adc_dr = target.read32(0x40022040)
    adc_isr = target.read32(0x40022000)
    adc_raw = target.read32(0x200000F0)
    print(f"\nADC1_DR   = 0x{adc_dr:08X}  raw={adc_dr & 0xFFF}")
    print(f"ADC1_ISR  = 0x{adc_isr:08X}  ADRDY={(adc_isr>>0)&1} EOC={(adc_isr>>2)&1}")
    print(f"ADC_RAW   = 0x{adc_raw:08X}  (DMA should write here)")

    # GPIOE + SHADOW
    odr = target.read32(0x58021014)
    shadow = target.read32(0x200000E0)
    print(f"\nGPIOE_ODR    = 0x{odr:08X}  (DMA Stream5 output)")
    print(f"SHADOW_GPIO  = 0x{shadow:08X}  (ISR writes this)")
    print(f"Match: {'✅' if odr == shadow else '❌'}")

    # Check ISR alive: read ODR twice
    target.resume()
    time.sleep(0.05)
    target.halt()
    odr2 = target.read32(0x58021014)
    print(f"\nGPIOE_ODR after 50ms = 0x{odr2:08X}")
    if odr != odr2:
        print("✅ GPIOE_ODR changing — ISR+DMA alive!")
    elif odr != 0:
        print("⚠️ GPIOE_ODR static but non-zero")
    else:
        print("❌ GPIOE_ODR = 0 — ISR/DMA may not be running")

    # DTCM marker
    marker = target.read32(0x20000000)
    print(f"\nDTCM marker = 0x{marker:08X}")
    if (marker >> 24) == 0xCC:
        print("✅ All init completed!")
    elif (marker >> 24) == 0xBB:
        print("→ GPIO done, stuck before TIM1/DMA")
    elif (marker >> 24) == 0xAA:
        print("→ SystemInit done, stuck before GPIO")
