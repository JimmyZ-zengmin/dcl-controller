#!/usr/bin/env python3
"""
Read reset source: RCC_RSR (0x580244E0) for H723
Also dump all PWR registers to confirm VOS state.
Uses pyocd Python API (not CLI).
"""
import sys
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

PROBE = "000000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper

REGISTERS = {
    "RCC_RSR":   0x580244E0,   # Reset status register (RM0468 Table 380)
    "RCC_CSR":   0x580244E4,
    "PWR_CR1":   0x58024800,   # RM0468 Table 56 — VOS[15:14] + SVOS
    "PWR_CSR1":  0x58024804,
    "PWR_CR2":   0x58024808,
    "PWR_CR3":   0x5802480C,   # NOT VOS on H723
    "PWR_CPUCR": 0x58024810,
    "PWR_D3CR":  0x58024818,   # Domain 3 control
    "RCC_CR":       0x58024400,
    "RCC_PLLCKSELR":0x58024428,
    "RCC_PLLCFGR":  0x5802442C,
    "RCC_CFGR":     0x58024408,
    "RCC_AHB1ENR":  0x580244D8,
    "SCB_CFSR":     0xE000ED28,
    "SCB_HFSR":     0xE000ED2C,
    "SCB_VTOR":     0xE000ED08,
}

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.halt()
    pc = t.read_core_register("pc")
    print(f"═══ H723 Reset Source / Register Dump ═══")
    print(f"  PC = 0x{pc:08X}\n")

    for name, addr in REGISTERS.items():
        v = t.read32(addr)
        print(f"  {name:16s} @ 0x{addr:08X} = 0x{v:08X}")

    # Decode RCC_RSR (RM0468 Table 380)
    rcc_rsr = t.read32(0x580244E0)
    print("\n─── RCC_RSR Decode (reset source) ───")
    bits = {
        31: "LPWRRSTF (low-power reset)",
        30: "WWDGRSTF (window watchdog)",
        29: "IWDGRSTF (independent watchdog *** SUSPECT ***)",
        28: "SFTRSTF (software reset)",
        27: "PINRSTF (NRST pin)",
        26: "OBLRSTF (option byte load)",
        24: "RMVF (reset flag clear)",
    }
    anyflag = False
    for bit, desc in bits.items():
        if rcc_rsr & (1 << bit):
            anyflag = True
            print(f"  -> BIT[{bit}] SET : {desc}")
    if not anyflag:
        print("  (no reset flags set — no prior reset detected)")

    # Decode PWR_CR1 (RM0468 Table 56)
    cr1 = t.read32(0x58024800)
    svos = (cr1 >> 14) & 0x3
    actvosrdy = (cr1 >> 13) & 0x1
    print(f"\n─── PWR_CR1 Decode ───")
    print(f"  SVOS[15:14] = {svos} (0=Scale5, 1=Scale4, 2=Scale3, 3=Scale2)")
    print(f"  ACTVOSRDY[13] = {actvosrdy} (1=voltage valid)")
    print(f"  NOTE: VOS0 (550MHz) requires SVOS=Scale2 → bit[15:14]=11")

    # Decode PWR_D3CR
    d3cr = t.read32(0x58024818)
    print(f"\n─── PWR_D3CR Decode ───")
    print(f"  raw = 0x{d3cr:08X}")
    print(f"  VOS[15:14] = {(d3cr>>14)&3}  (same field if H723 has it here)")
    print(f"  VOS[17:16] = {(d3cr>>16)&3}  (original assumption)")
    print(f"  bit13(VOSRDY) = {(d3cr>>13)&1}")

    # Decode RCC_CR
    rcc_cr = t.read32(0x58024400)
    print(f"\n─── RCC_CR Decode ───")
    print(f"  HSION[0]  = {rcc_cr & 1}")
    print(f"  HSIRDY[1] = {(rcc_cr >> 1) & 1}")
    print(f"  PLL1ON[24]= {(rcc_cr >> 24) & 1}")
    print(f"  PLL1RDY[25]={(rcc_cr >> 25) & 1}")

    # Decode RCC_CFGR
    cfgr = t.read32(0x58024408)
    sw = cfgr & 0x7
    sws = (cfgr >> 3) & 0x7
    print(f"\n─── RCC_CFGR Decode ───")
    print(f"  SW[2:0]   = {sw} (0=HSI,1=HSE,2=PLL1,3=PLL2)")
    print(f"  SWS[5:3]  = {sws} (0=HSI,1=HSE,2=PLL1,3=PLL2)")

    # RCC_AHB1ENR
    ahb1 = t.read32(0x580244D8)
    print(f"\n─── RCC_AHB1ENR Decode ───")
    print(f"  raw = 0x{ahb1:08X}")
    print(f"  DMA2EN[22] = {(ahb1 >> 22) & 1}")

    t.resume()
