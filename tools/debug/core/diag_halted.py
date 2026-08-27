#!/usr/bin/env python3
"""
DCL Controller Halt 状态诊断 - 读取所有关键配置
"""
import struct
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
        
        # 复位并 halt
        core.reset_and_halt()
        
        print("=" * 60)
        print("DCL Controller Halt 状态诊断")
        print("=" * 60)
        
        pc = core.read_core_register('pc')
        print(f"\nPC = 0x{pc:08X}")
        
        # 检查向量表
        vt0 = read32(core, 0x08000000)
        vt1 = read32(core, 0x08000004)
        print(f"向量表: SP=0x{vt0:08X}, Reset=0x{vt1:08X}")
        
        # RCC 寄存器
        print(f"\n[RCC 时钟寄存器]")
        regs = {
            'RCC_CR': 0x58024400,
            'RCC_CFGR': 0x58024410,
            'RCC_D1CFGR': 0x58024418,
            'RCC_D2CFGR': 0x5802441C,
            'RCC_PLLCKSELR': 0x58024428,
            'RCC_PLLCFGR': 0x5802442C,
            'RCC_PLL1DIVR': 0x58024430,
            'RCC_AHB1ENR': 0x580244D8,
            'RCC_AHB4ENR': 0x580244E0,
            'RCC_APB2ENR': 0x580244F0,
        }
        for name, addr in regs.items():
            val = read32(core, addr)
            print(f"  {name:15s} @ 0x{addr:08X} = 0x{val:08X}")
        
        # 解析时钟配置
        rcc_cr = read32(core, 0x58024400)
        rcc_cfgr = read32(core, 0x58024410)
        rcc_d1cfgr = read32(core, 0x58024418)
        rcc_pll1divr = read32(core, 0x58024430)
        
        pll1on = (rcc_cr >> 24) & 1
        pll1rdy = (rcc_cr >> 25) & 1
        sws = (rcc_cfgr >> 3) & 3  # 0=HSI, 1=HSE, 2=PLL1, 3=LOCO
        
        print(f"\n  时钟源: {'HSI' if sws==0 else 'HSE' if sws==1 else 'PLL1' if sws==2 else 'LOCO'}")
        print(f"  PLL1: {'ON' if pll1on else 'OFF'} {'(LOCKED)' if pll1rdy else '(NOT LOCKED)'}")
        
        if pll1rdy:
            # 计算频率
            divn = (rcc_pll1divr >> 0) & 0x1FF  # DIVN
            divp = (rcc_pll1divr >> 9) & 0x7F   # DIVP
            divq = (rcc_pll1divr >> 16) & 0x7F  # DIVQ
            divr = (rcc_pll1divr >> 25) & 0x7F  # DIVR
            
            hsi_freq = 64e6  # HSI = 64MHz
            vco = hsi_freq * (divn + 1) / 1  # HSI/1
            sysclk = vco / (divp + 1)
            
            print(f"  PLL1DIVR: DIVN={divn}, DIVP={divp}, DIVQ={divq}, DIVR={divr}")
            print(f"  VCO = {vco/1e6:.1f}MHz")
            print(f"  SYSCLK = {sysclk/1e6:.1f}MHz")
        
        # TIM1 寄存器
        print(f"\n[TIM1 定时器]")
        tim1_regs = {
            'TIM1_CR1': 0x40010000,
            'TIM1_CR2': 0x40010004,
            'TIM1_SMCR': 0x40010008,
            'TIM1_DIER': 0x4001000C,
            'TIM1_SR': 0x40010010,
            'TIM1_EGR': 0x40010014,
            'TIM1_CNT': 0x40010024,
            'TIM1_PSC': 0x40010028,
            'TIM1_ARR': 0x4001002C,
            'TIM1_RCR': 0x40010030,
            'TIM1_CCR1': 0x40010034,
            'TIM1_BDTR': 0x40010044,
        }
        for name, addr in tim1_regs.items():
            val = read32(core, addr)
            print(f"  {name:15s} @ 0x{addr:08X} = 0x{val:08X}")
        
        # 解析 TIM1 配置
        tim1_cr1 = read32(core, 0x40010000)
        tim1_dier = read32(core, 0x4001000C)
        tim1_psc = read32(core, 0x40010028)
        tim1_arr = read32(core, 0x4001002C)
        
        cen = tim1_cr1 & 1
        uie = (tim1_dier >> 0) & 1
        udis = (tim1_cr1 >> 1) & 1
        
        print(f"\n  TIM1 状态:")
        print(f"    CEN (计数使能): {'ON' if cen else 'OFF'}")
        print(f"    UIE (更新中断): {'ON' if uie else 'OFF'}")
        print(f"    PSC (预分频): {tim1_psc}")
        print(f"    ARR (自动重载): {tim1_arr}")
        
        if tim1_psc > 0 or tim1_arr > 0:
            # 计算周期
            tim1_clk = 136e6  # 假设 136MHz
            period = (tim1_psc + 1) * (tim1_arr + 1) / tim1_clk
            print(f"    周期: {period*1e6:.2f}us")
        
        # NVIC 寄存器
        print(f"\n[NVIC 中断]")
        nvic_regs = {
            'NVIC_ISER0': 0xE000E100,
            'NVIC_ISER1': 0xE000E104,
            'NVIC_ISER2': 0xE000E108,
            'NVIC_ICPR0': 0xE000E180,
            'NVIC_ICPR1': 0xE000E184,
            'NVIC_IABR0': 0xE000E300,
            'NVIC_IABR1': 0xE000E304,
        }
        for name, addr in nvic_regs.items():
            val = read32(core, addr)
            print(f"  {name:15s} @ 0x{addr:08X} = 0x{val:08X}")
        
        # 检查 TIM1_UP 中断
        nvic_iser1 = read32(core, 0xE000E104)
        tim1_up_en = (nvic_iser1 >> 11) & 1
        print(f"\n  TIM1_UP (IRQ 43) 使能: {'YES' if tim1_up_en else 'NO'}")
        
        # 检查 TIM1_UP_TIM10 (IRQ 26)
        nvic_iser0 = read32(core, 0xE000E100)
        tim1_up_tim10_en = (nvic_iser0 >> 26) & 1
        print(f"  TIM1_UP_TIM10 (IRQ 26) 使能: {'YES' if tim1_up_tim10_en else 'NO'}")
        
        # SCB 寄存器
        print(f"\n[SCB 系统控制]")
        scb_regs = {
            'SCB_VTOR': 0xE000ED08,
            'SCB_CCR': 0xE000ED14,
            'SCB_SHCSR': 0xE000ED24,
            'SCB_CFSR': 0xE000ED28,
            'SCB_HFSR': 0xE000ED2C,
            'SCB_MMFAR': 0xE000ED34,
            'SCB_BFAR': 0xE000ED38,
        }
        for name, addr in scb_regs.items():
            val = read32(core, addr)
            print(f"  {name:15s} @ 0x{addr:08X} = 0x{val:08X}")
        
        # DWT
        print(f"\n[DWT 调试]")
        dwt_ctrl = read32(core, 0xE0001000)
        dwt_cyccnt = read32(core, 0xE0001004)
        print(f"  DWT_CTRL  = 0x{dwt_ctrl:08X}")
        print(f"  DWT_CYCCNT = 0x{dwt_cyccnt:08X} ({dwt_cyccnt})")
        print(f"  CYCCNT 使能: {'YES' if dwt_ctrl & 1 else 'NO'}")
        
        # DTCM 变量
        print(f"\n[DTCM 变量]")
        dtcm = {
            'PERIOD_MIN': 0x20000000,
            'PERIOD_MAX': 0x20000004,
            'SAMPLES': 0x2000000C,
            'HEARTBEAT': 0x20000010,
            'ACTIVE_ROUTES': 0x200000F0,
        }
        for name, addr in dtcm.items():
            val = read32(core, addr)
            print(f"  {name:15s} @ 0x{addr:08X} = 0x{val:08X} ({val})")
        
        # WIRE_MAP 前 8 个
        print(f"\n[WIRE_MAP 前 8 个]")
        for i in range(8):
            addr = 0x20000300 + i * 4
            data = core.read_memory_block8(addr, 4)
            val = struct.unpack('<f', bytes(data))[0]
            print(f"  W[{i}] = {val:.6f}")
        
        # SENSOR_MAP 前 4 个
        print(f"\n[SENSOR_MAP 前 4 个]")
        for i in range(4):
            addr = 0x20000100 + i * 4
            data = core.read_memory_block8(addr, 4)
            val = struct.unpack('<f', bytes(data))[0]
            print(f"  S[{i}] = {val:.6f}")
        
        # ROUTE_TABLE 前 4 个
        print(f"\n[ROUTE_TABLE 前 4 个]")
        for i in range(4):
            addr = 0x20001700 + i * 16
            data = core.read_memory_block8(addr, 16)
            src_type, src_idx, dst_type, dst_channel, op, flags = struct.unpack('<BBBBBB', bytes(data[:6]))
            param_idx, state_off, act_idx, wire2_idx = struct.unpack('<HHHH', bytes(data[6:14]))
            print(f"  R[{i}]: src={src_type}:{src_idx} dst={dst_type}:{dst_channel} op=0x{op:02X} flags=0x{flags:02X} param={param_idx} state={state_off}")
        
        # GPIOE
        print(f"\n[GPIOE]")
        gpioe_moder = read32(core, 0x58021000)
        gpioe_odr = read32(core, 0x58021014)
        print(f"  MODER = 0x{gpioe_moder:08X}")
        print(f"  ODR   = 0x{gpioe_odr:08X}")
        
        # 总结
        print(f"\n{'=' * 60}")
        print(f"[诊断总结]")
        print(f"{'=' * 60}")
        
        samples = read32(core, 0x2000000C)
        period_min = read32(core, 0x20000000)
        period_max = read32(core, 0x20000004)
        
        if samples > 0:
            print(f"  ✓ ISR 正在运行 (SAMPLES = {samples})")
            print(f"  抖动: MIN={period_min}, MAX={period_max}, 范围={period_max-period_min} cycles")
        else:
            print(f"  ✗ ISR 未运行 (SAMPLES = 0)")
        
        if tim1_arr > 0:
            print(f"  ✓ TIM1 已配置 (ARR={tim1_arr})")
        else:
            print(f"  ✗ TIM1 未配置 (ARR=0)")
        
        if pll1rdy:
            print(f"  ✓ PLL1 已锁定")
        else:
            print(f"  ✗ PLL1 未锁定 (时钟可能不正确)")

if __name__ == '__main__':
    main()
