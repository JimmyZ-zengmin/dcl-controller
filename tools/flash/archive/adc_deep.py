#!/usr/bin/env python3
"""Deep ADC diagnosis: CFGR bits, ADRDY, OVR behavior, NDTR dynamics."""
import time
from pyocd.core.helpers import ConnectHelper
from pyocd.flash.file_programmer import FileProgrammer

ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    programmer = FileProgrammer(session, chip_erase="sector")
    programmer.program(ELF, file_format="elf")
    print("=== Flashed ===")
    t.reset_and_halt()
    t.resume()
    time.sleep(1.0)
    t.halt()

    # 1. ADC CFGR bits
    cfgr = t.read32(0x4002200C)
    print(f'\nADC1_CFGR = 0x{cfgr:08X}')
    print(f'  DMAEN     = {cfgr & 1}')
    print(f'  DMACFG    = {(cfgr>>1) & 1}  (0=one-shot, 1=circular)')
    print(f'  RES[1:0]  = {(cfgr>>3) & 3}  (00=12bit, 01=10bit, 10=8bit, 11=6bit)')
    print(f'  ALIGN     = {(cfgr>>5) & 1}')
    print(f'  EXTSEL[4:0] = {(cfgr>>10) & 0x1F}  (10=TIM1_TRGO, 15=HRTIM_ADC1)')
    print(f'  EXTEN[1:0]  = {(cfgr>>13) & 3}  (00=disabled, 01=rising, 10=falling, 11=both)')

    # 2. ADC CR
    cr = t.read32(0x40022008)
    print(f'\nADC1_CR = 0x{cr:08X}')
    print(f'  ADEN     = {cr & 1}')
    print(f'  ADSTART  = {(cr>>2) & 1}  (active conversion)')
    print(f'  ADCAL    = {(cr>>31) & 1}  (calibration in progress)')
    print(f'  ADCALDIF = {(cr>>30) & 1}')
    print(f'  DEEPPWD  = {(cr>>29) & 1}  (deep power-down)')

    # 3. ADC ISR
    isr = t.read32(0x40022000)
    print(f'\nADC1_ISR = 0x{isr:08X}')
    print(f'  ADRDY     = {isr & 1}')
    print(f'  EOC       = {(isr>>2) & 1}')
    print(f'  EOSMP     = {(isr>>3) & 1}')
    print(f'  OVR       = {(isr>>4) & 1}  ← key diagnostic')
    print(f'  EOSEQ     = {(isr>>1) & 1}')
    print(f'  AWD1      = {(isr>>7) & 1}')
    print(f'  LDORDY    = {(isr>>12) & 1}')
    print(f'  CCRDY    = {(isr>>13) & 1}')

    # 4. ADC CCR (Common)
    ccr = t.read32(0x40022308)
    print(f'\nADC12_CCR = 0x{ccr:08X}')
    print(f'  CKMODE[1:0]  = {ccr & 3}  (00=async, 01=AHB/1, 10=AHB/2, 11=AHB/4)')
    print(f'  ADCPRE       = {(ccr>>18) & 3}  (prescaler when async)')
    print(f'  VREFEN       = {(ccr>>22) & 1}')
    print(f'  TSEN         = {(ccr>>23) & 1}')

    # 5. ADCCLK check
    rcc_d3ccipr = t.read32(0x58024538)
    print(f'\nRCC_D3CCIPR = 0x{rcc_d3ccipr:08X}')
    print(f'  ADCSEL[1:0] = {(rcc_d3ccipr>>18) & 3}  (00=sys_ck, 01=pll2_p, 10=pll3_r, 11=HSI)')

    # 6. Watch NDTR over time (should oscillate in CIRC mode)
    print(f'\n--- Watching DMA S1NDTR + OVR over 200ms ---')
    print(f'{"t(ms)":>6} {"S1NDTR":>8} {"S1CR":>9} {"EN":>3} {"ADC_ISR":>10} {"OVR":>3} {"ADRDY":>6} {"DR":>5}')
    start = time.time()
    while time.time() - start < 0.5:
        t.resume()
        time.sleep(0.020)  # 20ms between samples
        t.halt()
        elapsed = (time.time() - start) * 1000
        ndtr = t.read32(0x4002042C)
        s1cr = t.read32(0x40020428)
        isr2 = t.read32(0x40022000)
        dr = t.read32(0x40022040) & 0xFFFF
        print(f'{elapsed:6.1f}  0x{ndtr:08X} 0x{s1cr:08X} {(s1cr&1):3d} 0x{isr2:08X} {(isr2>>4)&1:3d} {(isr2&1):6d} 0x{dr:04X}')
