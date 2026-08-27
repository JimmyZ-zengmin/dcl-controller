#!/usr/bin/env python3
"""Watch ISR by patching ITCM to write a magic value when ISR runs.

This script:
1. Reads original word at 0x18 (ITCM ISR start)
2. Patches with: STR r0, [r1]; ... ; original code
3. Resets CPU
4. Waits
5. Checks if magic value was written to DTCM[0x08]
"""
import time
import struct
from pyocd.core.helpers import ConnectHelper
from pyocd.flash.file_programmer import FileProgrammer

ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

# We will patch 0x00 to 0x10 of DTCM with a known marker
# Each ISR entry writes DTCM[0x08] = ++counter (so we can count hits)

# Approach: just poll SHADOW_GPIO. If ISR runs, eventually SHADOW will be non-zero
# (only if the engine writes it). Otherwise, ISR isn't running.

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    programmer = FileProgrammer(session, chip_erase="sector")
    programmer.program(ELF, file_format="elf")
    print("=== Flashed ===")
    t.reset_and_halt()

    # Check vector table IRQ25 entry
    vtor = t.read32(0xE000ED08)
    irq25_ptr = t.read32(vtor + 25*4)
    print(f'VTOR=0x{vtor:08X}  IRQ25 vector=0x{irq25_ptr:08X}')

    # Verify ITCM[0x18] has actual ISR code (not zero)
    isr_word0 = t.read32(0x00000018)
    print(f'ITCM[0x18]=0x{isr_word0:08X} (ISR first instruction)')

    # Read TIM1 status before resume
    sr = t.read32(0x40010010) & 0xFFFF
    print(f'TIM1_SR before resume: 0x{sr:04X} UIF={(sr>>0)&1}')

    t.resume()
    time.sleep(2.0)  # Let CPU run for 2s = 20000 ISR cycles
    t.halt()

    # Check TIM1 status now
    sr = t.read32(0x40010010) & 0xFFFF
    print(f'\nTIM1_SR after 2s: 0x{sr:04X} UIF={(sr>>0)&1}')

    # Check ODR (PE2 should be toggled many times)
    odr = t.read32(0x58021014)
    print(f'GPIOE_ODR = 0x{odr:08X} (PE2 bit = {(odr>>2)&1})')

    # Check SHADOW and other DTCM
    shadow = t.read32(0x200000E0)
    sensor0 = t.read32(0x20000100)
    m0 = t.read32(0x20000000)
    m4 = t.read32(0x20000004)
    print(f'DTCM[0]   = 0x{m0:08X}')
    print(f'DTCM[4]   = 0x{m4:08X}')
    print(f'SHADOW    = 0x{shadow:08X}')
    print(f'SENSOR[0] = 0x{sensor0:08X}')

    # Read TIM1 CNT
    cnt = t.read32(0x40010024) & 0xFFFF
    arr = t.read32(0x4001002C) & 0xFFFF
    psc = t.read32(0x40010028) & 0xFFFF
    print(f'\nTIM1_CNT=0x{cnt:04X} ARR=0x{arr:04X} PSC=0x{psc:04X}')

    # PC
    pc = t.read_core_register("pc")
    print(f'PC = 0x{pc:08X}')

    # UDE bit
    dier = t.read32(0x4001000C) & 0xFFFF
    print(f'TIM1_DIER = 0x{dier:04X} UDE={(dier>>8)&1} UIE={(dier>>0)&1}')

    # Was the GPIO toggled? Check TIM1_SR after we know ISR runs
    # Actually PE2 toggle only happens if ISR runs
    # If ODR bit 2 = 0 after 20000 cycles of toggle, that's 0.005% chance, meaning ISR not running
