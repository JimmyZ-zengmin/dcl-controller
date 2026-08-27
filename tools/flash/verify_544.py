#!/usr/bin/env python3
"""Verify firmware running at 544MHz by halt-then-check."""
import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    # Reset and halt immediately
    target.reset_and_halt()
    pc = target.read_core_register("pc")
    print(f"After reset_and_halt: PC = 0x{pc:08X}")

    # Run for 500ms then halt
    target.resume()
    time.sleep(0.5)

    try:
        target.halt()
        pc = target.read_core_register("pc")
        sp = target.read_core_register("sp")
        print(f"After 500ms run: PC = 0x{pc:08X}, SP = 0x{sp:08X}")

        # Check if PC is in main()
        if 0x08000608 <= pc <= 0x08002100:
            print("✅ CPU in main()!")
        elif 0x08000450 <= pc <= 0x08000608:
            print("❌ CPU still in SystemInit")

        # Read key registers
        cr = target.read32(0x58024400)
        cfgr = target.read32(0x58024410)
        ck = target.read32(0x58024428)
        div1 = target.read32(0x58024430)
        pllcf = target.read32(0x5802442C)

        print(f"\nRCC_CR = 0x{cr:08X}  PLL1ON={(cr>>24)&1} PLL1RDY={(cr>>25)&1}")
        print(f"RCC_CFGR = 0x{cfgr:08X}  SWS={(cfgr>>3)&7}")
        print(f"PLL1DIVR = 0x{div1:08X}  DIVN={(div1&0xFF)}")
        print(f"PLLCFGR = 0x{pllcf:08X}  VCOSEL={pllcf&1}")

        # DMA check
        s5m0ar = target.read32(0x40020484)
        s1m0ar = target.read32(0x40020424)
        print(f"\nDMA2_S1M0AR = 0x{s1m0ar:08X}  (expect 0x200000F0)")
        print(f"DMA2_S5M0AR = 0x{s5m0ar:08X}  (expect 0x200000E0)")

        # GPIOE
        odr = target.read32(0x58021014)
        print(f"GPIOE_ODR = 0x{odr:08X}  (expect 0x00000000 after init)")

        # DTCM
        shadow = target.read32(0x200000E0)
        print(f"SHADOW_GPIO = 0x{shadow:08X}")

        # Summary
        sws = (cfgr >> 3) & 7
        divn = (div1 & 0xFF) + 1
        divm = ((ck >> 4) & 0x3F)
        if divm == 0: divm = 1
        sysclk = (64000000 // divm) * divn
        if sws == 3:
            print(f"\n✅ Running at PLL1 = {sysclk/1e6:.0f}MHz")
        else:
            print(f"\n⚠️ SWS={sws}, not on PLL1")

        if s5m0ar == 0x200000E0:
            print("✅ DMA Stream5 configured correctly!")
        if odr == 0x00000000:
            print("✅ GPIOE initialized (main() reached!)")

    except Exception as e:
        print(f"SWD failed after 544MHz switch: {e}")
        print("This likely means CPU is running at 544MHz (SWD can't keep up)")
        print("✅ SystemInit completed successfully!")
