"""Halt CPU, 检查PC/LR/SP, 查看卡在哪里"""
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    target.halt()

    pc = target.read_core_register("pc")
    lr = target.read_core_register("lr")
    sp = target.read_core_register("sp")
    xpsr = target.read_core_register("xpsr")

    print(f"PC = 0x{pc:08X}")
    print(f"LR = 0x{lr:08X}")
    print(f"SP = 0x{sp:08X}")
    print(f"XPSR = 0x{xpsr:08X}")

    # 读取栈顶内容
    stack_data = target.read_memory_block32(sp, 8)
    print(f"\n栈顶 (SP=0x{sp:08X}):")
    for i, val in enumerate(stack_data):
        print(f"  [SP+{i*4:3d}] = 0x{val:08X}")

    # 检查是否在Default_Handler
    if (pc & ~1) >= 0x08000400 and (pc & ~1) < 0x08001000:
        print(f"\n⚠️ PC在向量表区域 (0x{(pc & ~1):08X}) → 可能在Default_Handler中!")

    # 检查RCC
    rcc_cr = target.read32(0x58024400)
    print(f"\nRCC_CR = 0x{rcc_cr:08X}")
    print(f"  PLL1ON = {(rcc_cr >> 24) & 1}")
    print(f"  PLL1RDY = {(rcc_cr >> 25) & 1}")
