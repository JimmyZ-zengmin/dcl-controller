#!/usr/bin/env python3
"""Diagnose SENSOR[0] = 0x41C80000 mystery."""
import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    t.reset_and_halt()
    t.resume()
    time.sleep(1.0)
    t.halt()

    # Dump DTCM region 0x100-0x140
    print('DTCM 0x0E0-0x130 (SENSOR_MAP area):')
    for offset in range(0xE0, 0x130, 4):
        v = t.read32(0x20000000 + offset)
        if v != 0:
            print(f'  0x{0x20000000+offset:08X} (0x{offset:03X}): 0x{v:08X}')

    # Dump DTCM TIMING area
    print('\nDTCM 0x000-0x020 (TIMING area):')
    for offset in range(0, 0x20, 4):
        v = t.read32(0x20000000 + offset)
        print(f'  0x{0x20000000+offset:08X} (0x{offset:03X}): 0x{v:08X}')

    # PC
    pc = t.read_core_register("pc")
    print(f'\nPC = 0x{pc:08X}')

    # Check if PC is in FDCAN init
    import subprocess
    r = subprocess.run([r'C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin\arm-none-eabi-addr2line.exe',
                       '-e', r'D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf',
                       f'0x{pc:08X}'],
                       capture_output=True, text=True)
    print(f'  → {r.stdout.strip()}')

    # FDCAN state
    print(f'\nFDCAN1_CCCR = 0x{t.read32(0x4000AC18):08X}')
    cccr = t.read32(0x4000AC18)
    print(f'  INIT = {cccr & 1}')
    print(f'  CCE  = {(cccr>>1) & 1}')
    print(f'  TEST = {(cccr>>7) & 1}')
    print(f'  DAR  = {(cccr>>6) & 1}')

    # RCC
    print(f'\nRCC_APB1HENR = 0x{t.read32(0x580244EC):08X}  FDCANEN bit 8 = {(t.read32(0x580244EC)>>8) & 1}')
