#!/usr/bin/env python3
"""Check fault status and CPU state."""
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target
    target.halt()

    # Fault status registers
    SCB_SHCSR  = 0xE000ED24  # System Handler Control
    SCB_CFSR   = 0xE000ED28  # Configurable Fault Status
    SCB_HFSR   = 0xE000ED2C  # HardFault Status
    SCB_MMFAR  = 0xE000ED34  # MemManage Fault Address
    SCB_BFAR   = 0xE000ED38  # BusFault Address
    SCB_AFSR   = 0xE000ED3C  # Aux Fault Status

    # DMA LISR/HISR
    DMA2_LISR = 0x40020400
    DMA2_HISR = 0x40020404

    # GPIOE
    GPIOE_ODR = 0x58021014
    GPIOE_IDR = 0x58021010

    regs = {
        "CPSR": None,
        "PC": None,
        "SP": None,
        "LR": None,
    }

    pc = target.read_core_register("pc")
    sp = target.read_core_register("sp")
    lr = target.read_core_register("lr")

    shcsr = target.read32(SCB_SHCSR)
    cfsr  = target.read32(SCB_CFSR)
    hfsr  = target.read32(SCB_HFSR)
    mmfar = target.read32(SCB_MMFAR)
    bfar  = target.read32(SCB_BFAR)

    dma_lisr = target.read32(DMA2_LISR)
    dma_hisr = target.read32(DMA2_HISR)

    print(f"=== CPU State ===")
    print(f"PC  = 0x{pc:08X}")
    print(f"SP  = 0x{sp:08X}")
    print(f"LR  = 0x{lr:08X}")

    print(f"\n=== Fault Status ===")
    print(f"SCB_SHCSR = 0x{shcsr:08X}")
    print(f"  MEMFAULTENA = {(shcsr>>16)&1}")
    print(f"  BUSFAULTENA = {(shcsr>>17)&1}")
    print(f"  USGFAULTENA = {(shcsr>>18)&1}")
    print(f"SCB_CFSR  = 0x{cfsr:08X}  (MMFSR=0x{(cfsr>>0)&0xFF:02X}, BFSR=0x{(cfsr>>8)&0xFF:02X}, UFSR=0x{(cfsr>>16)&0xFFFF:04X})")
    if cfsr & 0xFF:
        print(f"  MemManage: IACCVIOL={cfsr&1}, DACCVIOL={(cfsr>>1)&1}", end="")
        print(f", MMFVALID={(cfsr>>7)&1}")
    if (cfsr >> 8) & 0xFF:
        bf = (cfsr >> 8) & 0xFF
        print(f"  BusFault: IBUSERR={bf&1}, PRECISERR={(bf>>1)&1}", end="")
        print(f", BFARVALID={(bf>>7)&1}")
    if cfsr & (1 << 15):
        print(f"    MMFAR = 0x{mmfar:08X}")
    if cfsr & (1 << 15) == 0 and (cfsr >> 8) & (1 << 7):
        print(f"    BFAR = 0x{bfar:08X}")

    print(f"SCB_HFSR  = 0x{hfsr:08X}")
    if hfsr & (1 << 1): print(f"  VECTTBL: vector table read fault")
    if hfsr & (1 << 30): print(f"  FORCED: escalated to HardFault")

    # Check if PC points to Default_Handler (0x08000xxx typically)
    print(f"\n=== DMA Status ===")
    print(f"DMA2_LISR = 0x{dma_lisr:08X}")
    print(f"DMA2_HISR = 0x{dma_hisr:08X}")

    # GPIOE
    odr = target.read32(GPIOE_ODR)
    idr = target.read32(GPIOE_IDR)
    print(f"\nGPIOE_ODR = 0x{odr:08X}")
    print(f"GPIOE_IDR = 0x{idr:08X}")
