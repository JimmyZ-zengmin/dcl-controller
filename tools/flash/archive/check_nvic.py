#!/usr/bin/env python3
"""Check NVIC and TIM1 state to see if ISR is firing."""
import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    t.reset_and_halt()
    t.resume()
    time.sleep(2.0)
    t.halt()

    # NVIC status
    nvic_iser0 = t.read32(0xE000E100)
    nvic_iser1 = t.read32(0xE000E104)
    nvic_iabr0 = t.read32(0xE000E300)  # active bit
    nvic_iabr1 = t.read32(0xE000E304)

    print(f'NVIC_ISER0 = 0x{nvic_iser0:08X}')
    print(f'  TIM1_UP (bit25) = {(nvic_iser0 >> 25) & 1}')
    print(f'  FDCAN1_IT0 (bit19) = {(nvic_iser0 >> 19) & 1}')

    print(f'\nNVIC_IABR0 = 0x{nvic_iabr0:08X}  (active: currently in ISR)')
    print(f'  TIM1_UP active = {(nvic_iabr0 >> 25) & 1}')
    print(f'  FDCAN1_IT0 active = {(nvic_iabr0 >> 19) & 1}')

    # TIM1 status
    cnt1 = t.read32(0x40010024) & 0xFFFF
    sr1  = t.read32(0x40010010) & 0xFFFF
    arr1 = t.read32(0x4001002C) & 0xFFFF
    psc1 = t.read32(0x40010028) & 0xFFFF
    print(f'\nTIM1_CNT = 0x{cnt1:04X}  ({cnt1} / {arr1})')
    print(f'TIM1_SR  = 0x{sr1:04X}  UIF={(sr1>>0)&1}')

    # DTCM state
    shadow = t.read32(0x200000E0)
    adc_raw = t.read32(0x200000F0)
    m0 = t.read32(0x20000000)
    m4 = t.read32(0x20000004)
    print(f'\nDTCM[0]   = 0x{m0:08X}  (Stream1 marker)')
    print(f'DTCM[4]   = 0x{m4:08X}  (Stream5 marker)')
    print(f'SHADOW    = 0x{shadow:08X}  (DTCM 0xE0)')
    print(f'ADC_RAW   = 0x{adc_raw:08X}  (DTCM 0xF0)')

    # GPIOE
    odr = t.read32(0x58021014)
    idr = t.read32(0x58021010)
    print(f'\nGPIOE_ODR = 0x{odr:08X}')
    print(f'GPIOE_IDR = 0x{idr:08X}  (input pin state)')

    # ADC1
    adc1_isr = t.read32(0x40022000)
    adc1_dr  = t.read32(0x40022040)
    print(f'\nADC1_ISR  = 0x{adc1_isr:08X}  EOC={(adc1_isr>>2)&1} OVR={(adc1_isr>>4)&1}')
    print(f'ADC1_DR   = 0x{adc1_dr:08X}')

    # Check if PC is in ISR
    pc = t.read_core_register("pc")
    print(f'\nPC = 0x{pc:08X}')

    # Look up symbols
    try:
        sym_map = t.symbol_map if hasattr(t, 'symbol_map') else None
    except: pass
