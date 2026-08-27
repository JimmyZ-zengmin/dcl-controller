#!/usr/bin/env python3
"""Full diagnostic: clock + DMA + GPIO after fresh boot."""
PROBE = "00000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper
import time

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.reset()
    time.sleep(1.0)  # 等 1 秒让代码跑完
    t.halt()

    pc = t.read_core_register("pc")
    lr = t.read_core_register("lr")
    print(f"=== CPU State ===")
    print(f"  PC  = 0x{pc:08X}")
    print(f"  LR  = 0x{lr:08X}")

    # Clock tree
    print(f"\n=== Clock Tree ===")
    cr = t.read32(0x58024400)
    cfgr = t.read32(0x58024410)
    pllcr = t.read32(0x5802442C)
    plldivr = t.read32(0x58024430)
    pllckselr = t.read32(0x58024428)
    d1cfgr = t.read32(0x58024418)
    d2cfgr = t.read32(0x5802441C)
    flash_acr = t.read32(0x52002000)

    sws = (cfgr >> 3) & 7
    sws_str = {0: "HSI 64MHz", 1: "CSI", 3: "PLL1", 4: "HSE"}.get(sws, f"?({sws})")
    hpre = d1cfgr & 0xF
    hpre_div = {0:1, 8:2, 9:4, 10:8, 11:16, 12:32, 13:64, 14:128, 15:256}.get(hpre, 0)
    d2ppre2 = (d2cfgr >> 8) & 7
    d2ppre2_div = {0:1, 4:2, 5:4, 6:8, 7:16}.get(d2ppre2, 0)

    print(f"  RCC_CR      = 0x{cr:08X}  PLLON={1 if cr&(1<<24) else 0} PLLRDY={1 if cr&(1<<25) else 0}")
    print(f"  RCC_CFGR    = 0x{cfgr:08X}  SWS={sws} ({sws_str})")
    print(f"  PLLCKSELR   = 0x{pllckselr:08X}")
    print(f"  PLLCFGR     = 0x{pllcr:08X}  VCOSEL={(pllcr>>1)&1}")
    print(f"  PLL1DIVR    = 0x{plldivr:08X}  DIVN={plldivr&0x1FF} DIVP={(plldivr>>9)&0x7F}")
    print(f"  D1CFGR      = 0x{d1cfgr:08X}  HPRE={hpre} (÷{hpre_div})")
    print(f"  D2CFGR      = 0x{d2cfgr:08X}  D2PPRE2={d2ppre2} (÷{d2ppre2_div})")
    print(f"  FLASH_ACR   = 0x{flash_acr:08X}  LATENCY={flash_acr&0xF}")

    vos = t.read32(0x5802480C)
    print(f"  PWR_VOS     = 0x{vos:08X}  VOS[{((vos>>4)&3)}] VOSRDY={(vos>>6)&1}")

    # Compute actual frequencies
    if sws == 3:  # PLL
        divn = plldivr & 0x1FF
        divp = ((plldivr >> 9) & 0x7F) + 1
        vco_freq = 64_000_000 * divn  # HSI * DIVN (assuming /1 PLLM)
        pll_freq = vco_freq // divp
        sys_freq = pll_freq // hpre_div
        apb2_freq = sys_freq // d2ppre2_div
        print(f"\n  PLL VCO = {vco_freq/1e6:.0f}MHz, PLL out = {pll_freq/1e6:.0f}MHz")
        print(f"  SYSCLK = {sys_freq/1e6:.0f}MHz, APB2 = {apb2_freq/1e6:.0f}MHz")
    else:
        print(f"\n  SYSCLK = HSI 64MHz (PLL not active!)")

    # DMA2 Stream 5 (GPIO output)
    print(f"\n=== DMA2 Stream 5 (SHADOW_GPIO → GPIOE_ODR) ===")
    s5cr   = t.read32(0x40020478)
    s5ndtr = t.read32(0x4002047C)
    s5par  = t.read32(0x40020480)
    s5m0ar = t.read32(0x40020484)
    print(f"  CR   = 0x{s5cr:08X}  EN={s5cr&1} DIR={(s5cr>>6)&3} CIRC={(s5cr>>8)&1}")
    print(f"  NDTR = {s5ndtr}")
    print(f"  PAR  = 0x{s5par:08X}  {'✓ GPIOE_ODR' if s5par == 0x58021014 else '✗ wrong!'}")
    print(f"  M0AR = 0x{s5m0ar:08X}  {'✓ SHADOW_GPIO' if s5m0ar == 0x200000E0 else '✗ wrong!'}")

    # DMA2 Stream 1 (ADC)
    print(f"\n=== DMA2 Stream 1 (ADC1_DR → ADC_RAW) ===")
    s1cr   = t.read32(0x40020418)
    s1m0ar = t.read32(0x40020424)
    print(f"  CR   = 0x{s1cr:08X}  EN={s1cr&1}")
    print(f"  M0AR = 0x{s1m0ar:08X}")

    # GPIOE
    print(f"\n=== GPIOE ===")
    moder = t.read32(0x58021000)
    odr = t.read32(0x58021014)
    shadow = t.read32(0x200000E0)
    print(f"  MODER = 0x{moder:08X}")
    print(f"  ODR   = 0x{odr:08X}")
    print(f"  SHADOW_GPIO = 0x{shadow:08X}")

    # TIM1
    print(f"\n=== TIM1 ===")
    tim1_cr1 = t.read16(0x40010000)
    tim1_dier = t.read16(0x4001000C)
    tim1_arr = t.read16(0x4001002C)
    print(f"  CR1  = 0x{tim1_cr1:04X}  CEN={tim1_cr1&1}")
    print(f"  DIER = 0x{tim1_dier:04X}  UIE={tim1_dier&1} UDE={(tim1_dier>>8)&1}")
    print(f"  ARR  = {tim1_arr}")

    # NVIC
    iser0 = t.read32(0xE000E100)
    print(f"\n=== NVIC ===")
    print(f"  ISER0 = 0x{iser0:08X}  TIM1_UP(bit25)={'enabled' if iser0&(1<<25) else 'DISABLED'}")
