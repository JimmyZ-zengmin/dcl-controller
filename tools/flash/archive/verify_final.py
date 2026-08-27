#!/usr/bin/env python3
"""
Final comprehensive diagnostic:
1. Read DTCM init flow markers (0xCC000001=Stream1 done, 0xDD000005=Stream5 done)
2. Check ACTUATOR_STATUS, SHADOW_GPIO, GPIOE_ODR
3. Check DMA Stream5 register state
4. Print all LOG_BUF entries (the new "in-flight recorder")
"""
import time
import struct
from pyocd.core.helpers import ConnectHelper

# Markers written by main.c during init
DTCM_M0      = 0x20000000   # 0xCC000003 after TIM1 started
DTCM_M4      = 0x20000004   # 0xDD000005 after DMA Stream5 done
DTCM_M8      = 0x20000008   # 0xCC000001 after DMA Stream1 done (but is it?)

# DMA2 base
DMA2_BASE   = 0x40020400
DMAMUX_BASE = 0x40020800

S5CR    = DMA2_BASE + 0x88
S5NDTR  = DMA2_BASE + 0x8C
S5PAR   = DMA2_BASE + 0x90
S5M0AR  = DMA2_BASE + 0x94
S5FCR   = DMA2_BASE + 0x9C

S1CR    = DMA2_BASE + 0x28
S1NDTR  = DMA2_BASE + 0x2C
S1PAR   = DMA2_BASE + 0x30
S1M0AR  = DMA2_BASE + 0x34
S1FCR   = DMA2_BASE + 0x3C

DMA2_LISR = DMA2_BASE + 0x00
DMA2_LIFCR = DMA2_BASE + 0x08
DMA2_HIFCR = DMA2_BASE + 0x0C

DMAMUX_S5CR = DMAMUX_BASE + 0x14
DMAMUX_S1CR = DMAMUX_BASE + 0x04

# LOG_BUF @ DTCM 0xD000, 128 entries × 24B
LOG_BASE = 0x2000D000
LOG_COUNT_ADDR = 0x2000F000
DEPLOY_MARK_A  = 0x2000F004
DEPLOY_N_ADDR  = 0x2000F008
ROUTE49_CHK    = 0x2000F00C

SHADOW = 0x200000E0
GPIOE_ODR = 0x58021014
GPIOE_MODER = 0x58021000
GPIOE_BASE = 0x58021000

# ACTUATOR_STATUS[32..63] @ DTCM 0x280-0x2F8
ACT32_BASE = 0x20000280
N_ROUTES_ADDR = 0x200000F0

def f32(t, addr):
    raw = t.read32(addr)
    return struct.unpack('<f', struct.pack('<I', raw))[0]

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target

    print("=" * 60)
    print("DTCM Init Flow Markers")
    print("=" * 60)
    m0 = t.read32(DTCM_M0)
    m4 = t.read32(DTCM_M4)
    print(f"DTCM+0x0 (should be 0xCC000003 after TIM1 start): 0x{m0:08X}")
    print(f"DTCM+0x4 (should be 0xDD000005 after DMA S5 done): 0x{m4:08X}")

    # Decode marker
    def marker_state(v, expected):
        if v == expected:
            return f"[OK] = 0x{expected:08X}"
        # Check if it was once set
        hi = (v >> 24) & 0xFF
        if hi in (0xAA, 0xBB, 0xCC, 0xDD, 0xEE):
            return f"[FAIL] was 0x{hi:02X}xxxx marker, now ISR/TIMING overwrote it"
        return f"[FAIL] = 0x{v:08X}"

    print(f"  DTCM+0x0: {marker_state(m0, 0xCC000003)}")
    print(f"  DTCM+0x4: {marker_state(m4, 0xDD000005)}")

    # If DTCM+0x4 is 0xDD000005, Stream5 init completed
    # If not, Stream5 init was never called

    print("\n" + "=" * 60)
    print("Engine Output State")
    print("=" * 60)
    n_routes = t.read32(N_ROUTES_ADDR)
    deploy_mark = t.read32(DEPLOY_MARK_A)
    deploy_n = t.read32(DEPLOY_N_ADDR)
    route49 = t.read32(ROUTE49_CHK)
    log_count = t.read32(LOG_COUNT_ADDR)

    print(f"N_ROUTES   = {n_routes}    (DMA-RAW overlap, read may be wrong)")
    print(f"DEPLOY_MARK= 0x{deploy_mark:08X}  ({'deploy completed' if deploy_mark==0xDEADBEEF else 'deploy NOT completed'})")
    print(f"DEPLOY_N   = {deploy_n}")
    print(f"ROUTE49[0..3] = 0x{route49:08X}  (should be 0x00030002 = CONST/0/DST_WIRE/0)")
    print(f"LOG_COUNT  = {log_count}    (ISR samples since boot)")

    # ACTUATOR 32-63
    print("\nACTUATOR_STATUS[32..63]:")
    one_count = 0
    for i in range(32):
        v = f32(t, ACT32_BASE + i*4)
        if v > 0.5: one_count += 1
    print(f"  32/32 = 1.0: {one_count}/32 {'[OK]' if one_count==32 else '[FAIL]'}")

    # SHADOW and ODR
    shadow = t.read32(SHADOW)
    odr = t.read32(GPIOE_ODR)
    print(f"\nSHADOW_GPIO = 0x{shadow:08X}")
    print(f"GPIOE_ODR   = 0x{odr:08X}")

    if shadow == 0xFFFFFFFF or shadow == 0xFFFFFFFE or shadow == 0xFFFFFFFB:
        print(f"  [OK] SHADOW is all-1s (with possible ISR toggling bit 2)")
    else:
        print(f"  [INFO] SHADOW pattern 0x{shadow:08X}")

    if odr == shadow:
        print(f"  [OK] ODR == SHADOW (DMA Stream5 IS working)")
    elif odr == 0xFFFFFFFF:
        print(f"  [INFO] ODR = 0xFFFFFFFF (might be initial BSRR write or stuck)")
    else:
        print(f"  [FAIL] ODR 0x{odr:08X} != SHADOW 0x{shadow:08X} (DMA Stream5 not transferring)")

    # DMA Stream5
    print("\n" + "=" * 60)
    print("DMA Stream5 (SHADOW → GPIOE_ODR)")
    print("=" * 60)
    s5cr = t.read32(S5CR)
    s5ndtr = t.read32(S5NDTR)
    s5par = t.read32(S5PAR)
    s5m0ar = t.read32(S5M0AR)
    s5fcr = t.read32(S5FCR)
    mux = t.read32(DMAMUX_S5CR)

    print(f"DMA2_S5CR   = 0x{s5cr:08X}")
    print(f"  EN={s5cr&1} DIR={(s5cr>>6)&3} MINC={(s5cr>>10)&1} CIRC={(s5cr>>8)&1}")
    print(f"  PSIZE={(s5cr>>11)&3} MSIZE={(s5cr>>13)&3} PL={(s5cr>>16)&3}")
    print(f"DMA2_S5NDTR = {s5ndtr}")
    print(f"DMA2_S5PAR  = 0x{s5par:08X}  (should be 0x{GPIOE_ODR:08X})")
    print(f"DMA2_S5M0AR = 0x{s5m0ar:08X}  (should be 0x{SHADOW:08X})")
    print(f"DMA2_S5FCR  = 0x{s5fcr:08X}")
    print(f"DMAMUX_S5CR = 0x{mux:08X}  (req_id={mux&0x7F}, should be 15=TIM1_UP)")

    lisr = t.read32(DMA2_LISR)
    print(f"DMA2_LISR   = 0x{lisr:08X}")
    print(f"  S5 TCIF={(lisr>>11)&1} HTIF={(lisr>>9)&1} TEIF={(lisr>>7)&1} DMEIF={(lisr>>5)&1}")

    # Stream1 (for comparison)
    print("\n--- DMA Stream1 (for comparison) ---")
    s1cr = t.read32(S1CR)
    s1par = t.read32(S1PAR)
    s1m0ar = t.read32(S1M0AR)
    print(f"DMA2_S1CR   = 0x{s1cr:08X}  EN={s1cr&1} DIR={(s1cr>>6)&3} CIRC={(s1cr>>8)&1}")
    print(f"DMA2_S1PAR  = 0x{s1par:08X}")
    print(f"DMA2_S1M0AR = 0x{s1m0ar:08X}")

    # LOG buffer (the in-flight recorder)
    print("\n" + "=" * 60)
    print("LOG_BUF (in-flight recorder, last 10 of ~{})".format(log_count // 100))
    print("=" * 60)
    n_entries = min(log_count // 100, 128)
    if n_entries > 0:
        print(f"{'idx':>4} {'samp':>9} {'N':>5} {'ACT32':>6} {'ACT63':>6} {'SHADOW':>10} {'ODR':>10}")
        start = max(0, n_entries - 10)
        for k in range(start, n_entries):
            off = LOG_BASE + k * 24
            samp = t.read32(off + 0)
            nr = t.read32(off + 4)
            a32 = f32(t, off + 8)
            a63 = f32(t, off + 12)
            s = t.read32(off + 16)
            o = t.read32(off + 20)
            print(f"{k:4d} {samp:9d} {nr:5d} {a32:6.2f} {a63:6.2f} 0x{s:08X} 0x{o:08X}")
    else:
        print("(no log entries)")

    print("\n" + "=" * 60)
    print("Final Verdict")
    print("=" * 60)
    if deploy_mark == 0xDEADBEEF:
        print("[OK] deploy_test_routes() ran to completion")
    else:
        print(f"[FAIL] deploy_test_routes() did not complete (mark=0x{deploy_mark:08X})")

    if m4 == 0xDD000005:
        print("[OK] DMA Stream5 init completed (marker 0xDD000005 at DTCM+4)")
    else:
        print(f"[FAIL] DMA Stream5 init marker missing (DTCM+4=0x{m4:08X})")
        print("       → Stream5 was never configured, SHADOW never reaches GPIOE")
