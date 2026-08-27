from pyocd.core.helpers import ConnectHelper
with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx',
  connect_overwrite_unique_id='000000805059ed5520a4400013dd0702a5a5a5a59796990e') as session:
    core = session.target.selected_core_or_raise
    # RCC_CSR is at 0x580244D4, RESET_FLAGS also
    # FLASH_OPTR at 0x5200201C has IWDG_SW bit
    # FLASH_IWDG1R / IWDG control in option bytes
    r32 = lambda a: core.read_memory(a, 32) if a > 0 else 0
    try:
        # Read core registers
        pc = core.read_core_register('pc')
        lr = core.read_core_register('lr')
        sp = core.read_core_register('sp')
        xpsr = core.read_core_register('xPSR')
        print(f"PC  = 0x{pc:08X}")
        print(f"LR  = 0x{lr:08X}")
        print(f"SP  = 0x{sp:08X}")
        print(f"xPSR= 0x{xpsr:08X}")
        # ACTLR
        actlr = r32(0xE000E008)
        print(f"ACTLR = 0x{actlr:08X}")
        # SCB->SHCSR - faults enabled
        shcsr = r32(0xE000ED24)
        print(f"SCB->SHCSR = 0x{shcsr:08X}")
        # SCB->CFSR
        cfsr = r32(0xE000ED28)
        print(f"SCB->CFSR  = 0x{cfsr:08X}")
        # SCB->HFSR
        hfsr = r32(0xE000ED2C)
        print(f"SCB->HFSR  = 0x{hfsr:08X}")
        # SCB->MMFAR
        mmfar = r32(0xE000ED34)
        print(f"SCB->MMFAR = 0x{mmfar:08X}")
        # SCB->BFAR
        bfar = r32(0xE000ED38)
        print(f"SCB->BFAR  = 0x{bfar:08X}")
    except Exception as e:
        print(f"Error: {e}")
