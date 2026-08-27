#!/usr/bin/env python3
"""Deep diagnostic: why TIM1 not running."""
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
    sp = target.read_core_register("sp")
    print(f"PC=0x{pc:08X}  LR=0x{lr:08X}  SP=0x{sp:08X}")

    # Fault status
    cfsr = target.read32(0xE000ED28)
    hfsr = target.read32(0xE000ED2C)
    print(f"CFSR=0x{cfsr:08X}  HFSR=0x{hfsr:08X}")

    # RCC
    cr = target.read32(0x58024400)
    rcc_cfgr = target.read32(0x58024410)
    d2cfgr = target.read32(0x5802441C)  # RCC_D2CFGR (HCLK, APB1, APB2 prescalers)
    apb2enr = target.read32(0x58024490)  # RCC_APB2ENR
    ahb1enr = target.read32(0x580244D8)  # RCC_AHB1ENR
    print(f"\nRCC_CR     = 0x{cr:08X}")
    print(f"RCC_CFGR   = 0x{rcc_cfgr:08X}  SWS={(rcc_cfgr>>3)&7}")
    print(f"RCC_D2CFGR = 0x{d2cfgr:08X}")
    print(f"  D2CPRE={d2cfgr & 0x1F}  D2PPRE1={(d2cfgr>>4)&7}  D2PPRE2={(d2cfgr>>8)&7}")
    print(f"RCC_APB2ENR= 0x{apb2enr:08X}  TIM1EN={apb2enr & 1}")
    print(f"RCC_AHB1ENR= 0x{ahb1enr:08X}  DMA2EN={(ahb1enr>>1)&1}")

    # PWR
    pwr_cr3 = target.read32(0x5802480C)
    pwr_d3cr = target.read32(0x58024818)
    print(f"\nPWR_CR3 = 0x{pwr_cr3:08X}  SCUEN={(pwr_cr3>>2)&1}")
    print(f"PWR_D3CR= 0x{pwr_d3cr:08X}  VOS={(pwr_d3cr>>14)&3} VOSRDY={(pwr_d3cr>>13)&1}")

    # Flash
    flash_acr = target.read32(0x52002000)
    print(f"\nFLASH_ACR = 0x{flash_acr:08X}  LATENCY={flash_acr & 0xF} WRHIGHFREQ={(flash_acr>>4)&3}")

    # TIM1 detailed
    print(f"\n=== TIM1 @ 0x40012C00 ===")
    for name, off in [("CR1",0x00),("CR2",0x04),("SMCR",0x08),("DIER",0x0C),
                       ("SR",0x10),("EGR",0x14),("CCMR1",0x18),("CCMR2",0x1C),
                       ("CCER",0x20),("CNT",0x24),("PSC",0x28),("ARR",0x2C),
                       ("CCR1",0x34),("CCR2",0x38),("CCR3",0x3C),("CCR4",0x40),
                       ("BDTR",0x44)]:
        val = target.read32(0x40012C00 + off)
        print(f"  TIM1_{name:5s} ({off:#04x}) = 0x{val:08X}")

    # NVIC
    iser0 = target.read32(0xE000E100)
    ispr0 = target.read32(0xE000E200)
    iabr0 = target.read32(0xE000E300)
    print(f"\nNVIC_ISER0=0x{iser0:08X}  TIM1_UP(25)={iser0>>25&1}")
    print(f"NVIC_ISPR0=0x{ispr0:08X}  TIM1_UP(25)={ispr0>>25&1}")
    print(f"NVIC_IABR0=0x{iabr0:08X}  TIM1_UP(25)={iabr0>>25&1}")

    # Stack content (check for stuck loops)
    print(f"\nStack content at SP=0x{sp:08X}:")
    for i in range(8):
        val = target.read32(sp + i*4)
        print(f"  [SP+{i*4:2d}] = 0x{val:08X}")

    # ADC detailed
    print(f"\n=== ADC1 ===")
    for name, off in [("ISR",0x00),("CR",0x08),("CFGR",0x0C),("SMPR1",0x14),
                       ("SQR1",0x30),("DR",0x40)]:
        val = target.read32(0x40022000 + off)
        print(f"  ADC1_{name:6s} ({off:#04x}) = 0x{val:08X}")

    # GPIOE
    odr = target.read32(0x58021014)
    print(f"\nGPIOE_ODR = 0x{odr:08X}")
