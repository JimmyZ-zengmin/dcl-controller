#!/usr/bin/env python3
"""
Diagnose DMA Stream5 (SHADOW → GPIOE_ODR) failure.
Reads DMA Stream5 registers and DMAMUX to find why transfer isn't happening.
"""
import time
import struct
from pyocd.core.helpers import ConnectHelper

# DMA2 base (H723)
DMA2_BASE   = 0x40020400
DMAMUX_BASE = 0x40020800

# DMA2 Stream5 register offsets
S5CR    = DMA2_BASE + 0x88
S5NDTR  = DMA2_BASE + 0x8C
S5PAR   = DMA2_BASE + 0x90
S5M0AR  = DMA2_BASE + 0x94
S5FCR   = DMA2_BASE + 0x9C

# DMA2 LISR/HISR/LIFCR/HIFCR
DMA2_LISR = DMA2_BASE + 0x00
DMA2_HISR = DMA2_BASE + 0x04
DMA2_LIFCR = DMA2_BASE + 0x08
DMA2_HIFCR = DMA2_BASE + 0x0C

# DMAMUX Stream5
DMAMUX_S5CR = DMAMUX_BASE + 0x14

# DMA2_SxCR bits
CR_EN   = 1 << 0
CR_DMEIE = 1 << 1
CR_TEIE = 1 << 2
CR_HTIE = 1 << 3
CR_TCIE = 1 << 4
CR_PFCTRL = 1 << 5
CR_DIR_MEM2MEM = 0 << 6
CR_DIR_MEM2PER = 1 << 6
CR_DIR_PER2MEM = 2 << 6
CR_MINC = 1 << 10
CR_PSIZE_8  = 0 << 11
CR_PSIZE_16 = 1 << 11
CR_PSIZE_32 = 2 << 11
CR_MSIZE_8  = 0 << 13
CR_MSIZE_16 = 1 << 13
CR_MSIZE_32 = 2 << 13
CR_PL_LOW  = 0 << 16
CR_PL_MED  = 1 << 16
CR_PL_HIGH = 2 << 16
CR_VERYHIGH = 3 << 16
CR_DBM = 1 << 18
CR_CT  = 1 << 19
CR_PBURST_INC4 = 1 << 21

# LISR Stream5 bits (bit 11 = TCIF5, bit 9 = HTIF5, bit 7 = TEIF5, bit 5 = DMEIF5)
LISR_TCIF5 = 1 << 11
LISR_HTIF5 = 1 << 9
LISR_TEIF5 = 1 << 7
LISR_DMEIF5 = 1 << 5
LISR_FEIF5  = 1 << 1

# GPIOE_ODR
GPIOE_ODR = 0x58021014
# SHADOW_GPIO @ DTCM+0xE0
SHADOW = 0x200000E0
# GPIOE base for checking MODER
GPIOE_MODER = 0x58021000

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    print("=" * 60)
    print("DMA Stream5 (SHADOW → GPIOE_ODR) State")
    print("=" * 60)

    # DMA Stream5 registers
    s5cr = t.read32(S5CR)
    s5ndtr = t.read32(S5NDTR)
    s5par = t.read32(S5PAR)
    s5m0ar = t.read32(S5M0AR)
    s5fcr = t.read32(S5FCR)

    print(f"\nDMA2_S5CR   @ 0x{S5CR:08X} = 0x{s5cr:08X}")
    print(f"  EN     = {(s5cr >> 0) & 1}   (DMA enabled)")
    print(f"  DMEIE  = {(s5cr >> 1) & 1}   (direct mode error int)")
    print(f"  TEIE   = {(s5cr >> 2) & 1}   (transfer error int)")
    print(f"  TCIE   = {(s5cr >> 4) & 1}   (transfer complete int)")
    print(f"  DIR    = {(s5cr >> 6) & 3}   (0=mem2mem, 1=mem2per, 2=per2mem)")
    print(f"  MINC   = {(s5cr >> 10) & 1}  (memory increment)")
    print(f"  PSIZE  = {(s5cr >> 11) & 3}  (peripheral size: 0=8, 1=16, 2=32)")
    print(f"  MSIZE  = {(s5cr >> 13) & 3}  (memory size: 0=8, 1=16, 2=32)")
    print(f"  PL     = {(s5cr >> 16) & 3}  (priority: 0=low, 3=very high)")
    print(f"  CIRC   = {(s5cr >> 8) & 1}   (circular mode)")
    print(f"  PFCTRL = {(s5cr >> 5) & 1}   (peripheral flow ctrl)")
    print(f"  DBM    = {(s5cr >> 18) & 1}  (double buffer mode)")

    print(f"\nDMA2_S5NDTR @ 0x{S5NDTR:08X} = {s5ndtr}    (data length, 0=65536)")
    print(f"DMA2_S5PAR  @ 0x{S5PAR:08X} = 0x{s5par:08X}    (peripheral addr - should be 0x58021014 GPIOE_ODR)")
    print(f"DMA2_S5M0AR @ 0x{S5M0AR:08X} = 0x{s5m0ar:08X}    (memory addr - should be 0x200000E0 SHADOW)")
    print(f"DMA2_S5FCR  @ 0x{S5FCR:08X} = 0x{s5fcr:08X}")

    # DMA status
    lisr = t.read32(DMA2_LISR)
    print(f"\nDMA2_LISR @ 0x{DMA2_LISR:08X} = 0x{lisr:08X}")
    print(f"  Stream5 TCIF  (transfer complete)  = {(lisr >> 11) & 1}")
    print(f"  Stream5 HTIF  (half transfer)      = {(lisr >> 9) & 1}")
    print(f"  Stream5 TEIF  (transfer error)     = {(lisr >> 7) & 1}")
    print(f"  Stream5 DMEIF (direct mode error)  = {(lisr >> 5) & 1}")
    print(f"  Stream5 FEIF  (fifo error)         = {(lisr >> 1) & 1}")

    hisr = t.read32(DMA2_HISR)
    print(f"\nDMA2_HISR @ 0x{DMA2_HISR:08X} = 0x{hisr:08X}")

    # DMAMUX
    mux = t.read32(DMAMUX_S5CR)
    print(f"\nDMAMUX1_S5CR @ 0x{DMAMUX_S5CR:08X} = 0x{mux:08X}")
    print(f"  DMAREQ_ID = {mux & 0x7F}  (request line, 1=default TIM1_UP for Stream5)")

    # SHADOW and GPIOE_ODR
    shadow = t.read32(SHADOW)
    odr = t.read32(GPIOE_ODR)
    print(f"\nSHADOW_GPIO  = 0x{shadow:08X}")
    print(f"GPIOE_ODR    = 0x{odr:08X}")

    # MODER
    moder = t.read32(GPIOE_MODER)
    print(f"\nGPIOE_MODER @ 0x{GPIOE_MODER:08X} = 0x{moder:08X}")
    out_count = 0
    af_count = 0
    for i in range(16):
        mode = (moder >> (i*2)) & 3
        if mode == 1:
            out_count += 1
        elif mode == 2:
            af_count += 1
    print(f"  Pins configured as OUTPUT: {out_count}/16, AF: {af_count}/16")

    print("\n" + "=" * 60)
    print("Diagnosis")
    print("=" * 60)

    if (s5cr & CR_EN) == 0:
        print("[FAIL] DMA Stream5 EN=0 (disabled) - shadow will never reach GPIO")
    else:
        print("[OK] DMA Stream5 EN=1 (enabled)")

    if s5par != GPIOE_ODR:
        print(f"[FAIL] PAR=0x{s5par:08X} != GPIOE_ODR=0x{GPIOE_ODR:08X}")
    else:
        print(f"[OK] PAR = GPIOE_ODR (correct destination)")

    if s5m0ar != SHADOW:
        print(f"[FAIL] M0AR=0x{s5m0ar:08X} != SHADOW=0x{SHADOW:08X}")
    else:
        print(f"[OK] M0AR = SHADOW (correct source)")

    if (s5cr & CR_DIR_PER2MEM) == 0 and (s5cr & CR_DIR_MEM2PER) == 0:
        print(f"[FAIL] DIR=0 (mem2mem), should be 1 (mem2per) for SHADOW→GPIO")
    elif (s5cr & CR_DIR_MEM2PER) == 0:
        print(f"[FAIL] DIR={(s5cr>>6)&3}, should be 1 (mem2per)")

    if (s5cr & CR_CIRC if False else (s5cr >> 8) & 1) == 0:
        print(f"[WARN] CIRC=0, may stop after one transfer")

    # Check if any error is set
    if (lisr & LISR_TEIF5) or (lisr & LISR_DMEIF5):
        print(f"[FAIL] Stream5 has error: TEIF={(lisr>>7)&1} DMEIF={(lisr>>5)&1}")
        print("       This typically means M0AR or PAR is in an inaccessible region")
        print("       DTCM is at 0x20000000 - is DMA allowed to access it?")

    # Check if the request is mapped
    req_id = mux & 0x7F
    if req_id == 0:
        print(f"[INFO] DMAMUX request ID = 0 (no request mapped)")
    else:
        print(f"[INFO] DMAMUX request ID = {req_id} (should be TIM1_UP=15 for shadow DMA trigger)")
