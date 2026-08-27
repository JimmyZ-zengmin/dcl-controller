#!/usr/bin/env python3
"""Flash 272MHz version, run, halt, check ALL state."""
import time
from pyocd.core.helpers import ConnectHelper
from pyocd.flash.file_programmer import FileProgrammer

ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    # Flash
    programmer = FileProgrammer(session, chip_erase="sector")
    programmer.program(ELF, file_format="elf")
    print("=== Flashed ===")

    # Reset and run
    target.reset_and_halt()
    target.resume()
    print("Running at 272MHz...")
    time.sleep(1.5)

    # Halt and check
    target.halt()
    pc = target.read_core_register("pc")
    sp = target.read_core_register("sp")
    print(f"\nPC = 0x{pc:08X}  SP = 0x{sp:08X}")

    # RCC
    cr = target.read32(0x58024400)
    cfgr = target.read32(0x58024410)
    ck = target.read32(0x58024428)
    div1 = target.read32(0x58024430)
    pllcf = target.read32(0x5802442C)

    print(f"\n=== RCC ===")
    print(f"RCC_CR = 0x{cr:08X}  PLL1ON={(cr>>24)&1}  PLL1RDY={(cr>>25)&1}")
    print(f"RCC_CFGR = 0x{cfgr:08X}  SWS={(cfgr>>3)&7}  (0=HSI, 3=PLL1)")
    print(f"PLLCKSELR = 0x{ck:08X}  DIVM1={((ck>>4)&0x3F)}")
    print(f"PLL1DIVR = 0x{div1:08X}  DIVN={(div1&0xFF)}  DIVP={((div1>>9)&0x7F)}")
    print(f"PLLCFGR = 0x{pllcf:08X}  VCOSEL={pllcf&1}")

    # Compute frequency
    sws = (cfgr >> 3) & 7
    if sws == 3:
        divm = ((ck >> 4) & 0x3F)
        if divm == 0: divm = 1
        mul = (div1 & 0xFF)
        p = ((div1 >> 9) & 0x7F)
        if p == 0: p = 1
        freq = 64000000 // divm * mul // p
        print(f"SYSCLK = {freq/1e6:.0f}MHz")

    # DTCM markers
    marker = target.read32(0x20000000)
    shadow = target.read32(0x200000E0)
    adc_raw = target.read32(0x200000F0)
    sensor0 = target.read32(0x20000100)
    print(f"\n=== DTCM ===")
    print(f"MARKER (0x20000000) = 0x{marker:08X}")
    if (marker >> 24) == 0xAA: print("  → SystemInit done")
    elif (marker >> 24) == 0xBB: print("  → GPIO init done")
    elif (marker >> 24) == 0xCC: print("  ✅ ALL init done!")
    print(f"SHADOW_GPIO = 0x{shadow:08X}")
    print(f"ADC_RAW     = 0x{adc_raw:08X}")
    print(f"SENSOR_MAP[0]= 0x{sensor0:08X}")

    # DMA (corrected offsets for STM32H7)
    s1cr = target.read32(0x40020428)
    s1ndtr = target.read32(0x4002042C)
    s1par = target.read32(0x40020430)
    s1m0ar = target.read32(0x40020434)
    s5cr = target.read32(0x40020488)
    s5ndtr = target.read32(0x4002048C)
    s5par = target.read32(0x40020490)
    s5m0ar = target.read32(0x40020494)

    print(f"\n=== DMA2 Stream1 (ADC→DTCM) ===")
    print(f"S1CR   = 0x{s1cr:08X}  EN={s1cr&1}  DIR={(s1cr>>6)&3}  CIRC={((s1cr>>8)&1)}")
    print(f"S1NDTR = {s1ndtr}")
    print(f"S1PAR  = 0x{s1par:08X}  (expect 0x40022040=ADC1_DR)")
    print(f"S1M0AR = 0x{s1m0ar:08X}  (expect 0x200000F0=ADC_RAW)")

    print(f"\n=== DMA2 Stream5 (SHADOW→GPIOE_ODR) ===")
    print(f"S5CR   = 0x{s5cr:08X}  EN={s5cr&1}  DIR={(s5cr>>6)&3}  CIRC={((s5cr>>8)&1)}")
    print(f"S5NDTR = {s5ndtr}")
    print(f"S5PAR  = 0x{s5par:08X}  (expect 0x58021014=GPIOE_ODR)")
    print(f"S5M0AR = 0x{s5m0ar:08X}  (expect 0x200000E0=SHADOW_GPIO)")

    # GPIOE
    odr = target.read32(0x58021014)
    moder = target.read32(0x58021000)
    print(f"\n=== GPIOE ===")
    print(f"MODER = 0x{moder:08X}")
    print(f"ODR   = 0x{odr:08X}")

    # Check if SHADOW changes (ISR running)
    target.resume()
    time.sleep(0.05)
    target.halt()
    shadow2 = target.read32(0x200000E0)
    odr2 = target.read32(0x58021014)
    print(f"\n=== After 50ms ===")
    print(f"SHADOW_GPIO = 0x{shadow2:08X}  {'✅ CHANGING!' if shadow != shadow2 else 'unchanged'}")
    print(f"GPIOE_ODR   = 0x{odr2:08X}  {'✅ CHANGING!' if odr != odr2 else 'unchanged'}")
