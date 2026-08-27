"""Step 10次, 看Reset_Handler卡在哪"""
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    print("初始状态:")
    for i in range(20):
        pc = target.read_core_register("pc")
        target.step()
        pc2 = target.read_core_register("pc")
        print(f"  Step {i}: PC 0x{pc:08X} → 0x{pc2:08X}")

        # 如果到达main (0x08001xxx) 则停止
        if pc2 > 0x08001000:
            print(f"✓ 到达 main!")
            break

    # 检查SP
    sp_before = 0x24050000  # 从前面
    print(f"\n注意: SP默认应该是 _estack (通常是RAM末尾)")
