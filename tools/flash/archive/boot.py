from pyocd.core.helpers import ConnectHelper
with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx',
  connect_overwrite_unique_id='000000805059ed5520a4400013dd0702a5a5a5a59796990e') as session:
    core = session.target.selected_core_or_raise
    core.halt()
    r = lambda a: core.read_memory(a, 32)
    # RCC_RSR - reset status (new in H723 aka RCC_RSR)
    print(f"RCC_CSR       = 0x{r(0x580244D4):08X}")
    # BOOT 状态寄存器(SYSCFG。BOOTCR or similar)
    # FLASH_OPTR = 0x52002018 - has nBOOT0 bit
    optr = r(0x52002018)
    print(f"FLASH_OPTR    = 0x{optr:08X} (bit0=nBOOT0)")
    # Check current PC
    print(f"PC            = 0x{core.read_core_register('pc'):08X}")
    # 读 Flash 0x1FF0xxxx alias (system ROM 0)
    # alias 0: 0x00000000 should be Flash content (vector table)
    print(f"Alias 0x00000 vector[0] = 0x{r(0x00000000):08X}")
    print(f"Alias 0x00004 vector[1] = 0x{r(0x00000004):08X}")
