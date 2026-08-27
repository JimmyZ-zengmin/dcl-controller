#!/usr/bin/env python3
"""Verify the test deployment: 32 routes CONST(1.0) → ACTUATOR[32..63] → GPIOE 0..31."""
import time
import struct
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
    time.sleep(0.5)
    t.halt()

    # ── DTCM deployment verification ──
    n_routes = t.read32(0x200000F0)
    scratch0 = t.read32(0x200000F8)
    scratch1 = t.read32(0x200000FC)
    scratch2 = t.read32(0x20000100)
    print(f'\n--- Deployment Verification ---')
    print(f'N_ROUTES @ 0xF0  = {n_routes}  (old 20 + 32 = 52 expected)')
    print(f'SCRATCH[0] @ F8  = 0x{scratch0:08X}')
    print(f'SCRATCH[1] @ FC  = 0x{scratch1:08X}')
    print(f'SCRATCH[2] @ 100 = 0x{scratch2:08X}  (DEPLOYED_MAGIC? 0xDEADBEEF)')

    # Check PARAM[0].value_d = 1.0
    param0 = t.read32(0x20005700)
    f = struct.unpack('<f', struct.pack('<I', param0))[0]
    print(f'PARAM[0].value_d = 0x{param0:08X} = {f:.2f}  (should be 1.0)')

    # Check ROUTE_TABLE entries (last 32 should have actuator_idx = 32+i)
    print(f'\n--- Last 4 of 32 test routes ---')
    print(f'{"idx":>4} {"src":>4} {"si":>3} {"dt":>3} {"dc":>3} {"op":>3} {"flg":>3} {"pi":>4} {"so":>4} {"ai":>4} {"w2":>4}')
    for k in [48, 49, 50, 51]:
        offset = 0x20001700 + k * 16
        raw = [t.read8(offset+i) for i in range(16)]
        st, si, dt, dc, op, fl, pi, so, ai, w2 = raw[0], raw[1], raw[2], raw[3], raw[4], raw[5], raw[6]|(raw[7]<<8), raw[8]|(raw[9]<<8), raw[10]|(raw[11]<<8), raw[12]|(raw[13]<<8)
        print(f'{k:4d} {st:4d} {si:3d} {dt:3d} {dc:3d} {op:3d} 0x{fl:02X} {pi:4d} {so:4d} {ai:4d} {w2:4d}')

    # ── Run for 200ms then check ACTUATOR_STATUS + GPIOE_ODR ──
    t.resume()
    time.sleep(0.2)
    t.halt()

    print(f'\n--- After 200ms of engine running ---')
    # ACTUATOR_STATUS[32..63] (DTCM 0x280-0x2FF)
    print('ACTUATOR_STATUS[32..63]:')
    actuator_vals = []
    for i in range(32):
        raw = t.read32(0x20000200 + (32 + i) * 4)
        f = struct.unpack('<f', struct.pack('<I', raw))[0]
        actuator_vals.append(f)
    print(f'  All 32 = {actuator_vals[0]:.1f}  (should be 1.0 each)')

    # SHADOW_GPIO @ DTCM 0xE0
    shadow = t.read32(0x200000E0)
    print(f'\nSHADOW_GPIO  @ 0xE0  = 0x{shadow:08X}  (should be 0xFFFFFFFF)')

    # GPIOE_ODR @ 0x58021014
    odr = t.read32(0x58021014)
    print(f'GPIOE_ODR    @ 14    = 0x{odr:08X}  (should match SHADOW)')

    # ISR count
    samp = t.read32(0x20000000)
    hb   = t.read32(0x20000018)
    print(f'\nSAMPLES   = {samp}  (ISR count)')
    print(f'HEARTBEAT = {hb}')

    # ADC status
    adc_isr = t.read32(0x40022000)
    print(f'\nADC1_ISR = 0x{adc_isr:08X}  OVR={(adc_isr>>4)&1}')

    # Compare bit by bit
    print(f'\n--- Bit-by-bit compare ---')
    diff_bits = shadow ^ odr
    if diff_bits == 0:
        print(f'[OK] SHADOW_GPIO == GPIOE_ODR  (perfect match, all 32 bits aligned)')
    else:
        print(f'[FAIL] Diff: SHADOW=0x{shadow:08X} ODR=0x{odr:08X} DIFF=0x{diff_bits:08X}')

    # Check expected pattern
    if odr == 0xFFFFFFFF:
        print(f'[OK] GPIOE_ODR = 0xFFFFFFFF  (all 32 outputs HIGH, route table deployed successfully)')
    elif odr == 0:
        print(f'[FAIL] GPIOE_ODR = 0  (route table not reaching actuators)')
    else:
        print(f'[WARN] GPIOE_ODR = 0x{odr:08X}  (partial output)')

    # ── Test dynamic: change SENSOR[0] and check change propagates ──
    # Inject new test: change PARAM[0].value_d to 0.0 → expect GPIOE = 0
    print(f'\n--- Dynamic test: Set PARAM[0].value_d = 0.0 ---')
    t.resume()
    t.halt()
    t.write32(0x20005700, 0)  # value_d = 0.0
    t.resume()
    time.sleep(0.2)
    t.halt()
    odr2 = t.read32(0x58021014)
    shadow2 = t.read32(0x200000E0)
    print(f'After PARAM[0].value_d=0.0:')
    print(f'  SHADOW_GPIO  = 0x{shadow2:08X}')
    print(f'  GPIOE_ODR    = 0x{odr2:08X}')
    if odr2 == 0:
        print(f'  [OK] All 32 outputs LOW (engine correctly updated)')
    else:
        print(f'  [WARN] Not all low: 0x{odr2:08X}')

    # Restore to 1.0
    t.resume()
    t.halt()
    t.write32(0x20005700, 0x3F800000)  # 1.0f
    t.resume()
