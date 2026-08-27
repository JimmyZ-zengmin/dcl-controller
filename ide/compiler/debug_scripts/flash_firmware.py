#!/usr/bin/env python3
"""烧录 core0_h723.elf 到 Flash"""
import time

ELF_PATH = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

from pyocd.core.helpers import ConnectHelper
from pyocd.flash.file_programmer import FileProgrammer

print(f"烧录: {ELF_PATH}")

with ConnectHelper.session_with_chosen_probe(
    target_override='stm32h723xx',
    connect_overwrite_unique_id='000000805059ed5520a4400013dd0702a5a5a5a59796990e'
) as session:
    core = session.target.selected_core_or_raise
    board = session.board

    core.halt()
    print(f"CPU halted, PC = 0x{core.read_core_register('pc'):08X}")

    # 编程 Flash
    print("编程 Flash ...")
    FileProgrammer(session).program(ELF_PATH, file_format='elf')
    print("Flash 编程完成 ✓")

    # 复位
    print("复位芯片 ...")
    core.reset()
    time.sleep(0.5)

    pc = core.read_core_register('pc')
    print(f"复位后 PC = 0x{pc:08X}")

    # 全速运行
    core.resume()
    print("✓ 芯片已开始运行新固件")
    print("等待 1 秒让固件初始化 ...")
    time.sleep(1)

    # 验证: 读取 ACTIVE_ROUTES 和 SENSOR_MAP
    r = lambda a, n=4: core.read_memory(a, n)
    active = r(0x200000F0)
    sensor0 = r(0x20000100)
    print(f"  ACTIVE_ROUTES = 0x{active:08X}")
    print(f"  SENSOR_MAP[0] = 0x{sensor0:08X}")

print("✓ 烧录成功")
