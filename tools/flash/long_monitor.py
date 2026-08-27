#!/usr/bin/env python3
"""Long-term ADC + DMA + ISR monitoring (no OVR expected)."""
import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    t.reset_and_halt()
    t.resume()

    print('--- Watching ADC + DMA + ISR over 2s ---')
    print(f'{"t(ms)":>7} {"ADC_ISR":>10} {"OVR":>3} {"EOC":>3} {"DR":>5} {"RAW":>5} {"SEN0":>9} {"SHADOW":>7} {"SAMP":>5}')
    start = time.time()
    n = 0
    while time.time() - start < 2.0:
        t.halt()
        isr = t.read32(0x40022000)
        dr  = t.read32(0x40022040) & 0xFFFF
        raw = t.read32(0x200000F0) & 0xFFF
        sen = t.read32(0x20000100)
        sh  = t.read32(0x200000E0)
        samp = t.read32(0x20000010)
        elapsed = (time.time() - start) * 1000
        # Always show OVR change
        if (isr >> 4) & 1 or n % 10 == 0:
            print(f'{elapsed:7.1f} 0x{isr:08X} {(isr>>4)&1:3d} {(isr>>2)&1:3d} 0x{dr:04X} {raw:5d} 0x{sen:08X} 0x{sh:07X} {samp:5d}')
        n += 1
        t.resume()
        time.sleep(0.020)

    # Final summary
    t.halt()
    isr = t.read32(0x40022000)
    print(f'\n--- Final State ---')
    print(f'ADC_ISR  = 0x{isr:08X}')
    print(f'  ADRDY = {isr & 1}')
    print(f'  EOC   = {(isr>>2) & 1}')
    print(f'  EOSMP = {(isr>>3) & 1}')
    print(f'  OVR   = {(isr>>4) & 1}  (should stay 0)')
    print(f'  LDORDY= {(isr>>12) & 1}')
    print(f'  CCRDY = {(isr>>13) & 1}')

    # DMA Stream1 TCIF count (read, clear, read again)
    lisr = t.read32(0x40020400)
    print(f'\nDMA2_LISR = 0x{lisr:08X}  (Stream1-4 IRQs)')
    print(f'  TCIF1 = {(lisr>>11) & 1}')
    print(f'  HTIF1 = {(lisr>>10) & 1}')
    print(f'  TEIF1 = {(lisr>>3) & 1}')

    # Total samples processed
    samples = t.read32(0x20000018)  # SAMPLES counter
    print(f'\nSAMPLES counter @ DTCM 0x18 = {samples}  (ISR count)')
