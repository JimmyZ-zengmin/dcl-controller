#!/usr/bin/env python3
"""
Comprehensive verify script with correct DTCM addresses.
Reads deployment markers, ACTUATOR_STATUS, SHADOW_GPIO, GPIOE_ODR,
and the new LOG buffer to see ISR-recorded state over time.
"""
import time
import struct
from pyocd.core.helpers import ConnectHelper

ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

# DTCM addresses (matching main.c definitions)
DTCM_BASE = 0x20000000
SCRATCH_ADDR  = DTCM_BASE + 0xF8
N_ROUTES_ADDR = DTCM_BASE + 0xF0
ADC_RAW_ADDR  = DTCM_BASE + 0xF0  # SAME as N_ROUTES! Critical conflict.

# LOG_BASE variables
LOG_BASE       = DTCM_BASE + 0xD000
LOG_COUNT_ADDR = LOG_BASE + 0x2000  # 0x2000F000
DEPLOY_MARK_A  = LOG_BASE + 0x2004  # 0x2000F004
DEPLOY_N_ADDR  = LOG_BASE + 0x2008  # 0x2000F008
ROUTE49_CHK    = LOG_BASE + 0x200C  # 0x2000F00C

# ACTUATOR_STATUS (64 × float @ DTCM+0x200)
ACT_BASE       = DTCM_BASE + 0x200
# SHADOW_GPIO @ DTCM+0xE0
SHADOW_ADDR    = DTCM_BASE + 0xE0
# GPIOE_ODR @ 0x58021014
GPIOE_ODR_ADDR = 0x58021014
# SENSOR_MAP[0] @ DTCM+0x100
SENSOR0_ADDR   = DTCM_BASE + 0x100
# TIMING @ DTCM+0x00
SAMPLES_ADDR   = DTCM_BASE + 0x00

def read_float(t, addr):
    raw = t.read32(addr)
    return struct.unpack('<f', struct.pack('<I', raw))[0]

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target

    print("=" * 60)
    print("Step 1: Flash + Reset")
    print("=" * 60)
    from pyocd.flash.file_programmer import FileProgrammer
    programmer = FileProgrammer(session, chip_erase="sector")
    programmer.program(ELF, file_format="elf")
    t.reset_and_halt()
    t.resume()

    print("\nWaiting 2 seconds for engine to run...")
    time.sleep(2.0)
    t.halt()

    print("\n" + "=" * 60)
    print("Step 2: Static deployment markers (read from DTCM)")
    print("=" * 60)

    n_routes = t.read32(N_ROUTES_ADDR)
    deploy_mark = t.read32(DEPLOY_MARK_A)
    deploy_n = t.read32(DEPLOY_N_ADDR)
    route49_chk = t.read32(ROUTE49_CHK)
    log_count = t.read32(LOG_COUNT_ADDR)
    samples = t.read32(SAMPLES_ADDR)
    scratch0 = t.read32(SCRATCH_ADDR)
    scratch2 = t.read32(SCRATCH_ADDR + 8)  # = SENSOR_MAP[0]

    print(f"N_ROUTES  @ 0x{N_ROUTES_ADDR:08X} = {n_routes}    (should be 81 if deploy ran)")
    print(f"DEPLOY_MARK@0x{DEPLOY_MARK_A:08X} = 0x{deploy_mark:08X} (0xDEADBEEF = deploy completed)")
    print(f"DEPLOY_N  @ 0x{DEPLOY_N_ADDR:08X} = {deploy_n}    (should be 81)")
    print(f"ROUTE49_CHK@0x{ROUTE49_CHK:08X} = 0x{route49_chk:08X} (0x00030002 = CONST/0/DST_WIRE/0)")
    print(f"LOG_COUNT @ 0x{LOG_COUNT_ADDR:08X} = {log_count}    (should be > 1000 = ISR logging)")
    print(f"SAMPLES   @ 0x{SAMPLES_ADDR:08X} = {samples}    (ISR count, should be > 10000)")
    print(f"SCRATCH[0]@ 0x{SCRATCH_ADDR:08X} = 0x{scratch0:08X}")
    print(f"SCRATCH[2]@ 0x{SCRATCH_ADDR+8:08X} = 0x{scratch2:08X} (= SENSOR_MAP[0])")

    # Check N_ROUTES = ADC_RAW conflict
    print(f"\n*** N_ROUTES = ADC_RAW = 0x{N_ROUTES_ADDR:08X} (overlap! DMA writes ADC ch0 here)")
    print(f"    ADC_RAW interpretation: 0x{n_routes:08X} = {(n_routes & 0xFFF)} LSB = ADC value")
    print(f"    Actual N_ROUTES may have been overwritten by DMA")

    print("\n" + "=" * 60)
    print("Step 3: ACTUATOR_STATUS[32..63] (DTCM 0x280..0x2F8)")
    print("=" * 60)
    actuator_vals = []
    for i in range(32):
        addr = ACT_BASE + (32 + i) * 4
        f = read_float(t, addr)
        actuator_vals.append(f)

    one_count = sum(1 for v in actuator_vals if v > 0.5)
    zero_count = sum(1 for v in actuator_vals if v < 0.5)
    print(f"ACTUATOR_STATUS[32..63]:")
    print(f"  >0.5 (1.0): {one_count}/32")
    print(f"  <0.5 (0.0): {zero_count}/32")
    print(f"  First 8: {[f'{v:.2f}' for v in actuator_vals[:8]]}")
    print(f"  Last  8: {[f'{v:.2f}' for v in actuator_vals[-8:]]}")

    # Determine which bits are set in SHADOW
    print("\n" + "=" * 60)
    print("Step 4: SHADOW_GPIO and GPIOE_ODR")
    print("=" * 60)
    shadow = t.read32(SHADOW_ADDR)
    odr = t.read32(GPIOE_ODR_ADDR)
    print(f"SHADOW_GPIO  @ 0x{SHADOW_ADDR:08X} = 0x{shadow:08X}  (32-bit digital output map)")
    print(f"GPIOE_ODR    @ 0x{GPIOE_ODR_ADDR:08X} = 0x{odr:08X}")

    if shadow == odr:
        print(f"  [OK] SHADOW == ODR (DMA Stream5 is correctly copying SHADOW to GPIOE)")
    else:
        print(f"  [FAIL] SHADOW 0x{shadow:08X} != ODR 0x{odr:08X}, diff=0x{shadow^odr:08X}")

    if odr == 0xFFFFFFFF:
        print(f"  [OK] All 32 GPIOE bits HIGH (deploy routes produced 1.0 → bit set)")
    elif odr == 0:
        print(f"  [FAIL] All 32 GPIOE bits LOW (no high outputs)")
    else:
        print(f"  [INFO] Partial: {bin(odr).count('1')}/32 bits high")

    print("\n" + "=" * 60)
    print("Step 5: LOG buffer entries (24B each, last 10)")
    print("=" * 60)

    # Each entry is 6 uint32 = 24B
    # LOG_BUF @ 0x2000D000, LOG_WRAP=128
    n_entries = min(log_count // 100, 128)
    if n_entries == 0:
        n_entries = 0  # no entries logged
    print(f"Log entries written: ~{log_count // 100} (LOG_COUNT={log_count})")
    print(f"Stored in LOG_BUF: {n_entries} entries (LOG_WRAP=128)")
    print()
    print(f"{'idx':>4} {'samp':>9} {'N_ROUTE':>8} {'ACT32':>8} {'ACT63':>8} {'SHADOW':>10} {'ODR':>10}")

    start = max(0, n_entries - 10)
    for k in range(start, n_entries):
        off = LOG_BASE + k * 24
        samp = t.read32(off + 0)
        nr = t.read32(off + 4)
        act32 = t.read32(off + 8)
        act63 = t.read32(off + 12)
        shadow_e = t.read32(off + 16)
        odr_e = t.read32(off + 20)

        # ACT32/63 are stored as float bit-pattern in log
        a32f = struct.unpack('<f', struct.pack('<I', act32))[0]
        a63f = struct.unpack('<f', struct.pack('<I', act63))[0]

        print(f"{k:4d} {samp:9d} {nr:8d} {a32f:8.2f} {a63f:8.2f} 0x{shadow_e:08X} 0x{odr_e:08X}")

    print("\n" + "=" * 60)
    print("Step 6: Diagnosis")
    print("=" * 60)

    if deploy_mark == 0xDEADBEEF:
        print("[OK] deploy_test_routes() REACHED END (DEPLOY_MARK = 0xDEADBEEF)")
    else:
        print(f"[FAIL] deploy_test_routes() DID NOT REACH END (DEPLOY_MARK = 0x{deploy_mark:08X})")
        print("       Possible causes:")
        print("       - Function not called (compiler optimized out)")
        print("       - Function crashed mid-execution (hard fault)")
        print("       - Memory layout collision prevented writes")

    if n_routes == 81:
        print("[OK] N_ROUTES = 81 (deploy correctly incremented)")
    else:
        print(f"[INFO] N_ROUTES = {n_routes}")
        print("       The DMA-ADC_RAW-N_ROUTES collision means the value")
        print("       read here is the current DMA destination, not N_ROUTES.")
        print("       DEPLOY_MARK is the trustworthy indicator.")
