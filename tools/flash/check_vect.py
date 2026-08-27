#!/usr/bin/env python3
"""Verify vector table: read IRQ25 entry (TIM1_UP) and compare with ITCM ISR."""
import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    t.reset_and_halt()
    t.resume()
    time.sleep(1.5)
    t.halt()

    vtor = t.read32(0xE000ED08)
    print(f'SCB_VTOR = 0x{vtor:08X}')

    # Read vector at IRQ25 (TIM1_UP)
    # In H723, TIM1_UP = position 25 in vector table
    # Note: H723 vector table may differ from H743. Check both 0-based and 16-based.
    print('\n--- Looking for TIM1_UP in vector table ---')

    # Check several positions: ARM standard says IRQ0 starts at offset 0x40 (16*4)
    # But Cortex-M7 has 16 system exceptions + NVIC IRQs.
    for pos in [25, 28, 29, 30, 31]:
        addr = vtor + 16*4 + pos*4
        v = t.read32(addr)
        isr_target = 0x00000018 if v == 0x18 else 0
        print(f'  IRQ{pos:>3} @ 0x{addr:08X} = 0x{v:08X}')

    # The actual ITCM ISR is at 0x00000018
    # If vector table points elsewhere, ISR is wrong
    itcm_isr = t.read32(0x00000018)
    itcm_isr2 = t.read32(0x0000001C)
    print(f'\nITCM[0x18] = 0x{itcm_isr:08X} (first word of TIM1_UP_IRQHandler)')
    print(f'ITCM[0x1C] = 0x{itcm_isr2:08X} (second word)')

    # Check what GDB would disassemble at 0x18 to confirm it's ISR
    print(f'\nFLASH @ 0x08000064 (IRQ25 @ vtor):')
    print(f'  word = 0x{t.read32(0x08000064):08X}')

    # Also check vector table word at index 25 from vtor base
    v25 = t.read32(vtor + 25*4)
    print(f'  vtor+25*4 = 0x{v25:08X}')

    pc = t.read_core_register("pc")
    print(f'\nPC = 0x{pc:08X}')
