#!/usr/bin/env python3
"""
验证 H7223 时钟配置和 TIM1 寄存器
"""
import time
from pyocd.core.helpers import ConnectHelper

SERIAL = '000000805059ed5520a4400013dd0702a5a5a5a59796990e'

def read32(core, addr):
    return core.read_memory(addr, 32)

def main():
    with ConnectHelper.session_with_chosen_probe(
        target_override='stm32h723xx',
        connect_overwrite_unique_id=SERIAL
    ) as session:
        core = session.target.selected_core_or_raise
        
        print("=" * 60)
        print("Step 1: Halt 并检查复位后状态")
        print("=" * 60)
        
        core.reset_and_halt()
        time.sleep(0.05)
        
        # 向量表
        vt0 = read32(core, 0x08000000)
        vt1 = read32(core, 0x08000004)
        print(f"向量表: SP=0x{vt0:08X}, Reset=0x{vt1:08X}")
        
        # RCC 复位后默认值
        rcc_cr = read32(core, 0x58024400)
        rcc_cfgr = read32(core, 0x58024410)
        rcc_d1cfgr = read32(core, 0x58024418)
        rcc_d2cfgr = read32(core, 0x5802441C)
        rcc_apb2enr = read32(core, 0x580244F0)
        
        print(f"\nRCC_CR    = 0x{rcc_cr:08X} (HSION={rcc_cr&1})")
        print(f"RCC_CFGR  = 0x{rcc_cfgr:08X} (SWS={((rcc_cfgr>>3)&3)})")
        print(f"RCC_D1CFGR= 0x{rcc_d1cfgr:08X}")
        print(f"RCC_D2CFGR= 0x{rcc_d2cfgr:08X}")
        print(f"RCC_APB2ENR=0x{rcc_apb2enr:08X}")
        
        # 解析 HPRE
        hpre = rcc_d1cfgr & 0xF
        hpre_div = [1,1,1,1,1,1,1,1,2,4,8,16,64,128,256,512][hpre] if hpre < 16 else 1
        print(f"HPRE = {hpre} (/{hpre_div})")
        
        # 解析 D2PPRE2
        d2ppre2 = (rcc_d2cfgr >> 4) & 0x7
        d2ppre2_div = [1,1,1,1,2,4,8,16][d2ppre2] if d2ppre2 < 8 else 1
        print(f"D2PPRE2 = {d2ppre2} (/{d2ppre2_div})")
        
        print("\n" + "=" * 60)
        print("Step 2: 运行 2 秒后 halt，检查配置后状态")
        print("=" * 60)
        
        core.resume()
        time.sleep(2)
        core.halt()
        time.sleep(0.05)
        
        # 读取时钟寄存器
        rcc_cr = read32(core, 0x58024400)
        rcc_cfgr = read32(core, 0x58024410)
        rcc_d1cfgr = read32(core, 0x58024418)
        rcc_d2cfgr = read32(core, 0x5802441C)
        rcc_pllckselr = read32(core, 0x58024428)
        rcc_pllcfgr = read32(core, 0x5802442C)
        rcc_pll1divr = read32(core, 0x58024430)
        rcc_apb2enr = read32(core, 0x580244F0)
        rcc_ahb4enr = read32(core, 0x580244E0)
        
        # TIM1 寄存器
        tim1_cr1 = read32(core, 0x40010000)
        tim1_dier = read32(core, 0x4001000C)
        tim1_psc = read32(core, 0x40010028)
        tim1_arr = read32(core, 0x4001002C)
        tim1_cnt = read32(core, 0x40010024)
        tim1_sr = read32(core, 0x40010010)
        tim1_ccr1 = read32(core, 0x40010034)
        tim1_ccr4 = read32(core, 0x40010040)
        tim1_bdtr = read32(core, 0x40010044)
        
        # NVIC
        nvic_iser0 = read32(core, 0xE000E100)
        nvic_iser1 = read32(core, 0xE000E104)
        
        # DWT
        dwt_ctrl = read32(core, 0xE0001000)
        dwt_cyccnt = read32(core, 0xE0001004)
        
        # DTCM 变量
        samples = read32(core, 0x20000010)
        heartbeat = read32(core, 0x20000018)
        period_min = read32(core, 0x20000008)
        period_max = read32(core, 0x2000000C)
        clock_hz = read32(core, 0x2000001C)
        timer_hz = read32(core, 0x20000020)
        
        print(f"\n[RCC 时钟寄存器]")
        print(f"RCC_CR     = 0x{rcc_cr:08X} (PLL1ON={(rcc_cr>>24)&1}, PLL1RDY={(rcc_cr>>25)&1})")
        print(f"RCC_CFGR   = 0x{rcc_cfgr:08X} (SWS={((rcc_cfgr>>3)&3)})")
        print(f"RCC_D1CFGR = 0x{rcc_d1cfgr:08X} (HPRE={rcc_d1cfgr&0xF})")
        print(f"RCC_D2CFGR = 0x{rcc_d2cfgr:08X} (D2PPRE2={(rcc_d2cfgr>>4)&0x7})")
        print(f"RCC_PLLCKSELR = 0x{rcc_pllckselr:08X}")
        print(f"RCC_PLLCFGR   = 0x{rcc_pllcfgr:08X}")
        print(f"RCC_PLL1DIVR  = 0x{rcc_pll1divr:08X}")
        print(f"RCC_APB2ENR= 0x{rcc_apb2enr:08X} (TIM1EN={((rcc_apb2enr>>0)&1)})")
        print(f"RCC_AHB4ENR=0x{rcc_ahb4enr:08X}")
        
        # 计算 PLL
        divm1 = (rcc_pllckselr >> 4) & 0x1F
        divn = (rcc_pll1divr >> 0) & 0x1FF
        divp = (rcc_pll1divr >> 9) & 0x7F
        vcosel = (rcc_pllcfgr >> 1) & 1
        
        hsi_freq = 64e6
        pll_in = hsi_freq / (divm1 + 1) if divm1 > 0 else hsi_freq
        vco = pll_in * (divn + 1)
        sysclk = vco / (divp + 1) if divp > 0 else vco
        
        print(f"\n[PLL 计算]")
        print(f"DIVM1={divm1}, DIVN={divn}, DIVP={divp}, VCOSEL={vcosel}")
        print(f"PLL 输入 = {pll_in/1e6:.1f}MHz")
        print(f"VCO = {vco/1e6:.1f}MHz")
        print(f"SYSCLK = {sysclk/1e6:.1f}MHz")
        
        # 计算 TIM1 时钟
        hpre = rcc_d1cfgr & 0xF
        hpre_div = [1,1,1,1,1,1,1,1,2,4,8,16,64,128,256,512][hpre] if hpre < 16 else 1
        d2ppre2 = (rcc_d2cfgr >> 4) & 0x7
        d2ppre2_div = [1,1,1,1,2,4,8,16][d2ppre2] if d2ppre2 < 8 else 1
        
        ahb_clk = sysclk / hpre_div
        apb2_clk = ahb_clk / d2ppre2_div
        # TIM1 时钟 = APB2 × 2 (当 APB2 预分频不为 1 时)
        tim1_clk = apb2_clk * 2 if d2ppre2_div > 1 else apb2_clk
        
        print(f"\n[TIM1 时钟树]")
        print(f"AHB = SYSCLK/{hpre_div} = {ahb_clk/1e6:.1f}MHz")
        print(f"APB2 = AHB/{d2ppre2_div} = {apb2_clk/1e6:.1f}MHz")
        print(f"TIM1 = APB2 x {'2' if d2ppre2_div > 1 else '1'} = {tim1_clk/1e6:.1f}MHz")
        
        print(f"\n[TIM1 寄存器]")
        print(f"TIM1_CR1 = 0x{tim1_cr1:08X} (CEN={tim1_cr1&1})")
        print(f"TIM1_DIER= 0x{tim1_dier:08X} (UIE={tim1_dier&1})")
        print(f"TIM1_PSC = {tim1_psc}")
        print(f"TIM1_ARR = {tim1_arr}")
        print(f"TIM1_CNT = {tim1_cnt}")
        print(f"TIM1_SR  = 0x{tim1_sr:08X}")
        print(f"TIM1_CCR4= {tim1_ccr4}")
        print(f"TIM1_BDTR= 0x{tim1_bdtr:08X}")
        
        # 计算 TIM1 周期
        if tim1_arr > 0:
            period = (tim1_psc + 1) * (tim1_arr + 1) / tim1_clk
            print(f"\nTIM1 周期 = {period*1e6:.2f}μs")
        
        print(f"\n[NVIC]")
        print(f"NVIC_ISER0 = 0x{nvic_iser0:08X}")  # TIM1_UP_TIM10 @ bit 26
        print(f"NVIC_ISER1 = 0x{nvic_iser1:08X}")  # 其他中断
        
        # TIM1_UP 中断号
        if nvic_iser0 & (1 << 26):
            print("TIM1_UP_TIM10 (IRQ 26) 使能: YES")
        if nvic_iser1 & (1 << 11):
            print("TIM1_UP (IRQ 43) 使能: YES")
        
        print(f"\n[DTCM 变量]")
        print(f"SAMPLES    = {samples}")
        print(f"HEARTBEAT  = {heartbeat}")
        print(f"PERIOD_MIN = {period_min}")
        print(f"PERIOD_MAX = {period_max}")
        print(f"CLOCK_HZ   = {clock_hz}")
        print(f"TIMER_HZ   = {timer_hz}")
        
        if period_min > 0 and period_max > period_min:
            meas_period = (period_min + period_max) // 2
            calc_freq = meas_period / 0.4  # 假设周期 400us
            print(f"\n实测平均周期: {meas_period} cycles = {meas_period/tim1_clk*1e6:.1f}μs (@{tim1_clk/1e6:.0f}MHz)")
        
        print(f"\n[DWT]")
        print(f"DWT_CTRL  = 0x{dwt_ctrl:08X} (CYCCNTEN={dwt_ctrl&1})")
        print(f"DWT_CYCCNT= {dwt_cyccnt}")

if __name__ == '__main__':
    main()
