#!/usr/bin/env python3
"""Verify with per-cycle logger - FIXED ADDRESSES (0xF000 not 0xE000)."""
import time
import struct
from pyocd.core.helpers import ConnectHelper
from pyocd.flash.file_programmer import FileProgrammer

ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

# DTCM 0xD000 = LOG_BUF, 128 entries × 6 uint32 = 3072B (0xD000-0xDBFF)
# 0xF000 = LOG_COUNT, 0xF004 = DEPLOY_MARK, 0xF008 = DEPLOY_N, 0xF00C = ROUTE49_CHECK
LOG_BUF     = 0x2000D000
LOG_COUNT   = 0x2000F000   # <-- 修正!
DEPLOY_MARK = 0x2000F004   # <-- 修正!
DEPLOY_N    = 0x2000F008   # <-- 修正!
ROUTE49_CHK = 0x2000F00C   # <-- 修正!

N_ROUTES_ADDR  = 0x200000F0
ACT32_ADDR     = 0x20000280
ACT63_ADDR     = 0x200002FC
SHADOW_ADDR    = 0x200000E0
ODR_ADDR       = 0x58021014

# DMA2 / RCC / DMAMUX 寄存器 (用于诊断输出路径)
RCC_AHB1ENR    = 0x580244D8
DMA2_S5CR      = 0x40020488
DMA2_S5PAR     = 0x40020490
DMA2_S5M0AR    = 0x40020494
DMA2_S5NDTR    = 0x4002048C
DMAMUX1_S5CR   = 0x40020814

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    programmer = FileProgrammer(session, chip_erase="sector")
    programmer.program(ELF, file_format="elf")
    print("=== Flashed ===")
    t.reset_and_halt()
    t.resume()
    time.sleep(0.5)
    t.halt()

    n_routes   = t.read32(N_ROUTES_ADDR)
    deploy_mk  = t.read32(DEPLOY_MARK)
    deploy_n   = t.read32(DEPLOY_N)
    route49    = t.read32(ROUTE49_CHK)
    log_count  = t.read32(LOG_COUNT)

    print(f"\n=== Static state (after 500ms) [ADDR FIXED] ===")
    print(f"N_ROUTES       = {n_routes}        (expect 81 if deploy ran)")
    print(f"DEPLOY_MARK    = 0x{deploy_mk:08X} (expect 0xDEADBEEF if deploy finished)")
    print(f"DEPLOY_N       = {deploy_n}        (what deploy tried to set)")
    print(f"ROUTE49 dword0 = 0x{route49:08X}    (expect 0x00030002 = src=2,si=0,dt=3,dc=0)")
    print(f"LOG_COUNT      = {log_count}        (samples logged, expect ~5000)")

    if deploy_mk == 0xDEADBEEF:
        print(f"\n[OK]   deploy function reached its end")
    else:
        print(f"\n[FAIL] DEPLOY_MARK != 0xDEADBEEF  ->  deploy did NOT complete")

    if n_routes == 81:
        print(f"[OK]   N_ROUTES = 81  (init 49 + deploy 32)")
    else:
        print(f"[FAIL] N_ROUTES = {n_routes}  (expected 81)")

    # ── 读日志缓冲 ──
    print(f"\n=== Logger entries (last 8 of ~50) ===")
    print(f"{'idx':>3} {'samp':>7} {'N':>4} {'ACT[32]':>9} {'ACT[63]':>9} {'SHADOW':>10} {'ODR':>10}")
    n_entries = min(log_count // 100, 128)
    if n_entries == 0:
        n_entries = 1
    for k in range(max(0, n_entries - 8), n_entries):
        off = LOG_BUF + k * 24
        samp   = t.read32(off + 0)
        nr     = t.read32(off + 4)
        act32  = t.read32(off + 8)
        act63  = t.read32(off + 12)
        shadow = t.read32(off + 16)
        odr    = t.read32(off + 20)
        a32f = struct.unpack('<f', struct.pack('<I', act32))[0]
        a63f = struct.unpack('<f', struct.pack('<I', act63))[0]
        print(f"{k:3d} {samp:7d} {nr:4d} {a32f:9.3f} {a63f:9.3f} 0x{shadow:08X} 0x{odr:08X}")

    # ── DMA / RCC 寄存器快照 (halt 后读) ──
    rcc_ahb1 = t.read32(RCC_AHB1ENR)
    s5cr   = t.read32(DMA2_S5CR)
    s5par  = t.read32(DMA2_S5PAR)
    s5m0ar = t.read32(DMA2_S5M0AR)
    s5ndtr = t.read32(DMA2_S5NDTR)
    mux5   = t.read32(DMAMUX1_S5CR)
    print(f"\n=== DMA regs (after halt) ===")
    print(f"RCC_AHB1ENR = 0x{rcc_ahb1:08X}  (bit1=DMA2EN, bit2=DMAMUX1EN)")
    print(f"DMA2_S5CR   = 0x{s5cr:08X}    (bit0=EN, bit6=DIR_M2P, bit8=CIRC)")
    print(f"DMA2_S5NDTR = {s5ndtr}        (0=传输完成/未加载)")
    print(f"DMA2_S5PAR  = 0x{s5par:08X} (expect 0x58021014 = GPIOE_ODR)")
    print(f"DMA2_S5M0AR = 0x{s5m0ar:08X} (expect 0x200000E0 = SHADOW)")
    print(f"DMAMUX1_S5CR= 0x{mux5:02X}       (expect 0x0F = TIM1_UP)")
    if s5cr & 1:
        print(f"[OK] DMA Stream5 EN=1")
    else:
        print(f"[!!] DMA Stream5 EN=0 (未启用或已停)")
    # 读取 RCC_CSR (复位标志) — 需要先清除再读
    rcc_csr = t.read32(0x58024440)
    print(f"\n=== Reset flags ===")
    print(f"RCC_CSR     = 0x{rcc_csr:08X}")
    print(f"  bit29=IWDGRSTF={'YES' if (rcc_csr>>29)&1 else 'NO'}")
    print(f"  bit30=WWDGRSTF={'YES' if (rcc_csr>>30)&1 else 'NO'}")
    print(f"  bit31=LPWRRSTF={'YES' if (rcc_csr>>31)&1 else 'NO'}")
    print(f"  bit26=PINRSTF={'YES' if (rcc_csr>>26)&1 else 'NO'}")
    print(f"  bit25=BORRSTF={'YES' if (rcc_csr>>25)&1 else 'NO'}")
    print(f"  bit24=RSTF={'YES' if (rcc_csr>>24)&1 else 'NO'}")

    if s5par == ODR_ADDR and s5m0ar == SHADOW_ADDR:
        print(f"[OK] DMA 地址配置正确")
    elif s5par == 0 and s5m0ar == 0:
        print(f"[!!] DMA 地址全 0 -> 时钟未起效 或 配置被优化掉")
    else:
        print(f"[!!] DMA 地址异常: PAR=0x{s5par:08X} M0AR=0x{s5m0ar:08X}")

    # ── 现在的实时状态 ──
    print(f"\n=== Live state ===")
    cur_n     = t.read32(N_ROUTES_ADDR)
    cur_act32 = t.read32(ACT32_ADDR)
    cur_act63 = t.read32(ACT63_ADDR)
    cur_shdw  = t.read32(SHADOW_ADDR)
    cur_odr   = t.read32(ODR_ADDR)
    a32 = struct.unpack('<f', struct.pack('<I', cur_act32))[0]
    a63 = struct.unpack('<f', struct.pack('<I', cur_act63))[0]
    print(f"N_ROUTES       = {cur_n}")
    print(f"ACTUATOR[32]   = {a32:.4f}  (expect 1.0)")
    print(f"ACTUATOR[63]   = {a63:.4f}  (expect 1.0)")
    print(f"SHADOW_GPIO    = 0x{cur_shdw:08X}  (expect 0xFFFFFFFF)")
    print(f"GPIOE_ODR      = 0x{cur_odr:08X}  (should match SHADOW)")

    if cur_odr == 0xFFFFFFFF:
        print(f"\n[SUCCESS] Full chain working: ISR->ACT->SHADOW->DMA->GPIOE")
    elif cur_shdw == 0xFFFFFFFF and cur_odr != cur_shdw:
        print(f"\n[FAIL-SHADOW-OK-DMA-BAD]  SHADOW correct but DMA Stream5 not transporting")
    elif cur_shdw == 0:
        print(f"\n[FAIL-DEPLOY-NOT-EFFECTIVE]  SHADOW=0 means ACTUATOR[32..63] all 0")
        print(f"    deploy routes not running. Check N_ROUTES = {cur_n} (need 81)")
    else:
        print(f"\n[PARTIAL] SHADOW=0x{cur_shdw:08X} ODR=0x{cur_odr:08X}")
