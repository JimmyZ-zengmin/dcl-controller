#!/usr/bin/env python3
"""Check if TIM1 + DMA + ADC are all running after new CFGR."""
import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    t.reset_and_halt()
    t.resume()
    time.sleep(0.5)
    t.halt()

    # TIM1
    cr1  = t.read32(0x40010000) & 0xFFFF
    dier = t.read32(0x4001000C) & 0xFFFF
    sr   = t.read32(0x40010010) & 0xFFFF
    cnt  = t.read32(0x40010024) & 0xFFFF
    arr  = t.read32(0x4001002C) & 0xFFFF
    print(f'TIM1_CR1  = 0x{cr1:04X}  CEN={cr1&1}')
    print(f'TIM1_DIER = 0x{dier:04X}  UIE={dier&1} UDE={(dier>>8)&1}')
    print(f'TIM1_SR   = 0x{sr:04X}  UIF={sr&1}')
    print(f'TIM1_CNT  = 0x{cnt:04X}  (running 0~{arr})')

    # RCC APB2
    apb2 = t.read32(0x580244F0)
    print(f'\nRCC_APB2ENR = 0x{apb2:08X}  TIM1EN={apb2&1}')

    # ADC
    cr = t.read32(0x40022008)
    isr = t.read32(0x40022000)
    print(f'\nADC1_CR   = 0x{cr:08X}  ADEN={cr&1} ADSTART={(cr>>2)&1}')
    print(f'ADC1_ISR  = 0x{isr:08X}  ADRDY={isr&1} EOC={(isr>>2)&1} OVR={(isr>>4)&1}')

    # DMA
    s1cr = t.read32(0x40020428)
    s1ndtr = t.read32(0x4002042C)
    s1m0ar = t.read32(0x40020434)
    s5cr = t.read32(0x40020488)
    print(f'\nDMA2_S1CR   = 0x{s1cr:08X}  EN={s1cr&1}')
    print(f'DMA2_S1NDTR = 0x{s1ndtr:08X}')
    print(f'DMA2_S1M0AR = 0x{s1m0ar:08X}')
    print(f'DMA2_S5CR   = 0x{s5cr:08X}  EN={s5cr&1}')

    # DMAMUX
    print(f'\nDMAMUX1_S1CR = 0x{t.read32(0x40020804):08X} (ADC1 expect 9)')
    print(f'DMAMUX1_S5CR = 0x{t.read32(0x40020814):08X} (TIM1_UP expect 15)')

    # DTCM
    print(f'\nDTCM[0]   = 0x{t.read32(0x20000000):08X}')
    print(f'DTCM[4]   = 0x{t.read32(0x20000004):08X}')
    print(f'SHADOW    = 0x{t.read32(0x200000E0):08X}')
    print(f'ADC_RAW   = 0x{t.read32(0x200000F0):08X}')
    print(f'SENSOR[0] = 0x{t.read32(0x20000100):08X}')
    print(f'SAMPLES   = {t.read32(0x20000018)}')

    # PC
    print(f'\nPC = 0x{t.read_core_register("pc"):08X}')

    # Watch SENSOR[0] for 200ms without halt
    print('\n--- Watching SENSOR[0] for 200ms (no halt) ---')
    t.resume()
    start = time.time()
    last = 0
    while time.time() - start < 0.2:
        time.sleep(0.02)
        t.halt()
        s = t.read32(0x20000100)
        if s != last:
            elapsed = (time.time() - start) * 1000
            import struct
            f = struct.unpack('<f', struct.pack('<I', s))[0]
            print(f't={elapsed:5.1f}ms SENSOR[0]=0x{s:08X} = {f:.4f}')
            last = s
        t.resume()
