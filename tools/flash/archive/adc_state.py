#!/usr/bin/env python3
"""Verify CFGR2 + ADC state in detail."""
import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    t.reset_and_halt()
    t.resume()
    time.sleep(1.5)
    t.halt()

    # ADC1 registers
    print('--- ADC1 Registers ---')
    print(f'ADC1_CR   = 0x{t.read32(0x40022008):08X}  (ADEN bit 0, ADSTART bit 2, ADCAL bit 31)')
    print(f'ADC1_CFGR = 0x{t.read32(0x4002200C):08X}')
    print(f'ADC1_CFGR2= 0x{t.read32(0x40022010):08X}  (EXTEN in bit 0:1, others are sample time bits)')
    print(f'ADC1_SMPR1= 0x{t.read32(0x40022014):08X}  (SMP17 in bit 21:23)')
    print(f'ADC1_SQR1 = 0x{t.read32(0x40022030):08X}')
    print(f'ADC1_ISR  = 0x{t.read32(0x40022000):08X}  (ADRDY bit 0, EOC bit 2, OVR bit 4)')
    print(f'ADC1_IER  = 0x{t.read32(0x40022004):08X}')
    print(f'ADC1_DR   = 0x{t.read32(0x40022040):08X}')

    # ADC12_CCR
    print(f'\nADC12_CCR = 0x{t.read32(0x40022308):08X}')
    ccr = t.read32(0x40022308)
    print(f'  CKMODE[1:0] = {ccr & 3}  (0=async, 1=AHB/1, 2=AHB/2, 3=AHB/4)')
    print(f'  bit[16:17] = {(ccr>>16) & 3} (raw CKMODE)')

    # CFGR2 bit 0:1 is EXTEN
    cfgr2 = t.read32(0x40022010)
    print(f'\nCFGR2 bit[0:1] (EXTEN) = {cfgr2 & 3}  (0=disabled, 1=rising, 2=falling, 3=both)')
    print(f'CFGR2 bit[2:4] (SMP)   = {(cfgr2>>2) & 7}  (sample time for some channels)')

    # CR details
    cr = t.read32(0x40022008)
    print(f'\nADC1_CR bits:')
    print(f'  ADEN    = {cr & 1}        (1=ENABLED)')
    print(f'  ADDIS   = {(cr>>1) & 1}')
    print(f'  ADSTART = {(cr>>2) & 1}        (1=CONVERSION IN PROGRESS)')
    print(f'  ADSTP   = {(cr>>4) & 1}')
    print(f'  ADVREGEN= {(cr<<28>>28) & 3}')

    # TIM1 state
    print(f'\nTIM1_CNT = 0x{t.read32(0x40010024) & 0xFFFF:04X}')
    print(f'TIM1_SR  = 0x{t.read32(0x40010010) & 0xFFFF:04X}  UIF={(t.read32(0x40010010)>>0)&1}')

    # DTCM
    print(f'\nADC_RAW @ DTCM 0xF0 = 0x{t.read32(0x200000F0):08X}')
    print(f'SHADOW  @ DTCM 0xE0 = 0x{t.read32(0x200000E0):08X}')
    print(f'SENSOR[0]          = 0x{t.read32(0x20000100):08X}')
