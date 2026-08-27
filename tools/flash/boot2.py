from pyocd.core.helpers import ConnectHelper
with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx',
  connect_overwrite_unique_id='000000805059ed5520a4400013dd0702a5a5a5a59796990e') as session:
    core = session.target.selected_core_or_raise
    core.halt()
    r = lambda a: core.read_memory(a, 32)
    # SYSCFG register in D2
    for addr, name in [
        (0x58000400, "SYSCFG_PMCR"),
        (0x58000408, "SYSCFG_UR0"),
    ]:
        try:
            print(f"{name} @ 0x{addr:08X} = 0x{r(addr):08X}")
        except Exception as e:
            print(f"{name} read fail: {e}")

    # Read PC, prints
    print(f"PC         = 0x{core.read_core_register('pc'):08X}")
    lr = core.read_core_register('lr')
    print(f"LR         = 0x{lr:08X}")

    # check vector table at ITCM 0x0000_alias
    # alias 0: 0x00000000 should alias which memory?
    # dependent on H7 boot mode
    # mode 0 (Flash): should alias Flash 0x08000000
    # mode 1 (System ROM): should alias 0x1FF00000
    # mode 2 ( Embedded SRAM): alias 0x20000000
    print(f"\nAlias probing:")
    print(f"Alias 0x00000 (vector[0]) = 0x{r(0x00000000):08X}")
    print(f"Alias 0x00004 (vector[1]) = 0x{r(0x00000004):08X}")
    print(f"Flash  0x80000 = 0x{r(0x08000000):08X}")
    print(f"Flash  0x80004 = 0x{r(0x08000004):08X}")
    print(f"ROM    0x1FF00000 = ???")
    # read H7 BOOT_ADD options in FLASH
    try:
        print(f"FLASH_BOOTCR = 0x{r(0x52002020):08X}")
    except Exception as e:
        print(f"BOOTCR fail: {e}")
