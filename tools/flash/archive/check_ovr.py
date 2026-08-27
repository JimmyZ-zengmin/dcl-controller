#!/usr/bin/env python3
"""Check ADC OVR and verify SHADOW toggle (PE2 via SHADOW+DMA)."""
import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    t.reset_and_halt()
    t.resume()
    time.sleep(2.0)
    t.halt()

    # ADC status
    adc1_isr = t.read32(0x40022000)
    adc1_dr  = t.read32(0x40022040)
    adc_raw  = t.read32(0x200000F0)
    print(f'ADC1_ISR = 0x{adc1_isr:08X}')
    print(f'  ADRDY = {(adc1_isr>>0)&1}')
    print(f'  EOC   = {(adc1_isr>>2)&1}')
    print(f'  OVR   = {(adc1_isr>>4)&1}  ← should be 0 now')
    print(f'ADC1_DR  = 0x{adc1_dr:04X}')
    print(f'ADC_RAW @ DTCM = 0x{adc_raw:08X}')

    # SHADOW and ODR (after Stream5搬运)
    shadow = t.read32(0x200000E0)
    odr    = t.read32(0x58021014)
    print(f'\nSHADOW     = 0x{shadow:08X} (PE2 = {(shadow>>2)&1})')
    print(f'GPIOE_ODR  = 0x{odr:08X}    (PE2 = {(odr>>2)&1})')
    if shadow == odr:
        print('  ✓ Stream5 正确搬运了 SHADOW → ODR')
    else:
        print('  ✗ Stream5 未同步')

    # DMA Stream1 (ADC->DTCM)
    s1ndtr = t.read32(0x4002042C)
    s1cr   = t.read32(0x40020428)
    lisr   = t.read32(0x40020400)
    print(f'\nDMA2_S1NDTR = 0x{s1ndtr:08X}')
    print(f'DMA2_S1CR   = 0x{s1cr:08X} EN={s1cr&1}')
    print(f'DMA2_LISR   = 0x{lisr:08X}  (Stream1-4 flags)')
    print(f'  TCIF1 = {(lisr>>11)&1}  (Stream1 transfer complete)')

    # DMA Stream5 (DTCM->GPIOE_ODR)
    s5ndtr = t.read32(0x4002048C)
    s5cr   = t.read32(0x40020488)
    hisr   = t.read32(0x40020404)
    print(f'\nDMA2_S5NDTR = 0x{s5ndtr:08X}')
    print(f'DMA2_S5CR   = 0x{s5cr:08X} EN={s5cr&1}')
    print(f'DMA2_HISR   = 0x{hisr:08X}')
    print(f'  TCIF5 = {(hisr>>9)&1}  (Stream5 transfer complete)')
    print(f'  TEIF5 = {(hisr>>11)&1}')

    # TIM1 status
    cnt = t.read32(0x40010024) & 0xFFFF
    sr  = t.read32(0x40010010) & 0xFFFF
    print(f'\nTIM1_CNT = 0x{cnt:04X}  SR=0x{sr:04X}  UIF={(sr>>0)&1}')

    # SENSOR_MAP[0] (float)
    s0 = t.read32(0x20000100)
    import struct
    if s0:
        f = struct.unpack('<f', struct.pack('<I', s0))[0]
        print(f'\nSENSOR[0] = 0x{s0:08X} = {f:.4f} V (from ADC_RAW)')

    # DTCM markers
    m0 = t.read32(0x20000000)
    m4 = t.read32(0x20000004)
    print(f'\nDTCM[0] = 0x{m0:08X}')
    print(f'DTCM[4] = 0x{m4:08X}')
