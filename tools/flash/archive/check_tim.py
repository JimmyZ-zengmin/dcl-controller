"""检查TIM1寄存器, 看中断是否使能"""
from pyocd.core.helpers import ConnectHelper

TIM1_BASE = 0x40010000

# TIM1寄存器偏移
TIM_CR1    = 0x00  # 控制寄存器1
TIM_CR2    = 0x04  # 控制寄存器2
TIM_DIER   = 0x0C  # 中断使能
TIM_SR     = 0x10  # 状态
TIM_CNT    = 0x24  # 计数器
TIM_PSC    = 0x28  # 预分频
TIM_ARR    = 0x2C  # 自动重载
TIM_CCR1   = 0x34  # 比较1
TIM_CCR4   = 0x40  # 比较4

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    dier = target.read32(TIM1_BASE + TIM_DIER)
    cr1  = target.read32(TIM1_BASE + TIM_CR1)
    sr   = target.read32(TIM1_BASE + TIM_SR)
    cnt  = target.read32(TIM1_BASE + TIM_CNT)
    psc  = target.read32(TIM1_BASE + TIM_PSC)
    arr  = target.read32(TIM1_BASE + TIM_ARR)

    print("=== TIM1 寄存器 ===")
    print(f"CR1  = 0x{cr1:08X}  (CEN={(cr1)&1}, URS={(cr1>>1)&1})")
    print(f"DIER = 0x{dier:08X}  (UIE={(dier)&1}, CC1IE={(dier>>1)&1}, CC4IE={(dier>>4)&1})")
    print(f"SR   = 0x{sr:08X}   (UIF={(sr)&1})")
    print(f"CNT  = 0x{cnt:08X}  (计数器)")
    print(f"PSC  = 0x{psc:08X}  (预分频)")
    print(f"ARR  = 0x{arr:08X}  (自动重载, 周期)")

    # 检查NVIC
    # TIM1_UP_IRQn = 25 (从向量表)
    # NVIC_ISER0 = 0xE000E100
    nvic_iser = target.read32(0xE000E100)
    tim1_up_bit = 1 << 25
    nvic_enabled = (nvic_iser & tim1_up_bit) != 0

    print(f"\nNVIC_ISER0 = 0x{nvic_iser:08X}")
    print(f"TIM1_UP (bit25) = {'✓ 已使能' if nvic_enabled else '✗ 未使能'}")

    if not nvic_enabled:
        print("\n❌ TIM1_UP中断未在NVIC中使能!")
    if not (dier & 1):
        print("❌ TIM1 UIE (更新中断使能) 未设置!")
    if not (cr1 & 1):
        print("❌ TIM1 CEN (计数器使能) 未设置!")
