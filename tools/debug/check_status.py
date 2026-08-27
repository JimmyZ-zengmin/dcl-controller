#!/usr/bin/env python3
"""
DCL Controller 状态检查脚本 - 老方法 pyocd 直读
读取关键寄存器判断机器运行状态
"""
from pyocd.core.helpers import ConnectHelper

# CMSIS-DAP serial
SERIAL = '000000805059ed5520a4400013dd0702a5a5a5a59796990e'

# 关键寄存器地址
REGISTERS = {
    # 时钟相关
    'RCC_CR':       0x58024400,
    'RCC_CFGR':     0x58024410,
    'RCC_D1CFGR':   0x58024418,
    'RCC_D2CFGR':   0x5802441C,
    'RCC_PLLCKSELR':0x58024428,
    'RCC_PLLCFGR':  0x5802442C,
    'RCC_PLL1DIVR': 0x58024430,
    'RCC_AHB1ENR':  0x580244D8,  # H723 offset
    'RCC_AHB4ENR':  0x580244E0,
    'RCC_APB2ENR':  0x580244F0,
    
    # TIM1
    'TIM1_CR1':     0x40010000,
    'TIM1_CR2':     0x40010004,
    'TIM1_DIER':    0x4001000C,
    'TIM1_SR':      0x40010010,
    'TIM1_CNT':     0x40010024,
    'TIM1_PSC':     0x40010028,
    'TIM1_ARR':     0x4001002C,
    'TIM1_CCR1':    0x40010034,
    
    # NVIC
    'NVIC_ISER0':   0xE000E100,
    'NVIC_ISER1':   0xE000E104,
    'NVIC_ICPR0':   0xE000E180,
    'NVIC_ICPR1':   0xE000E184,
    'NVIC_IABR0':   0xE000E300,
    'NVIC_IABR1':   0xE000E304,
    
    # SCB
    'SCB_VTOR':     0xE000ED08,
    'SCB_CCR':      0xE000ED14,
    'SCB_SHCSR':    0xE000ED24,
    
    # DWT
    'DWT_CTRL':     0xE0001000,
    'DWT_CYCCNT':   0xE0001004,
    
    # GPIOE
    'GPIOE_MODER':  0x58021000,
    'GPIOE_ODR':    0x58021014,
    'GPIOE_AFRL':   0x58021020,
    
    # PWR
    'PWR_CR3':      0x5802480C,
}

# DTCM 关键变量 (基址 0x20000000)
DTCM_VARS = {
    'PERIOD_MIN':   0x20000000,  # uint32
    'PERIOD_MAX':   0x20000004,  # uint32
    'PERIOD_AVG':   0x20000008,  # float32
    'SAMPLES':      0x2000000C,  # uint32
    'HEARTBEAT':    0x20000010,  # uint32
    'ACTIVE_ROUTES':0x200000F0,  # uint32
    'ROUTE_COUNT':  0x200000F0,  # 别名
}

# 寄存器空间
REG_SPACES = {
    'SENSOR_MAP':      0x20000100,  # 64×float32
    'ACTUATOR_STATUS': 0x20000200,  # 32×float32
    'WIRE_MAP':        0x20000300,  # 1024×float32
    'LUT_DATA':        0x20001300,  # 256×float32
    'ROUTE_TABLE':     0x20001700,  # 1024×16B
    'PARAM_TABLE':     0x20005700,  # 512×16B
    'STATE_TABLE':     0x20007700,  # 256×16B
    'SHADOW_GPIO':     0x200000E0,  # 4B
}

def read32(core, addr):
    return core.read_memory(addr, 32)

def read_block(core, addr, count):
    """读取 count 个 uint32"""
    data = core.read_memory_block8(addr, count * 4)
    import struct
    return struct.unpack(f'<{count}I', bytes(data))

def read_float_block(core, addr, count):
    """读取 count 个 float32"""
    data = core.read_memory_block8(addr, count * 4)
    import struct
    return struct.unpack(f'<{count}f', bytes(data))

def main():
    print("=" * 60)
    print("DCL Controller 状态检查")
    print("=" * 60)
    
    with ConnectHelper.session_with_chosen_probe(
        target_override='stm32h723xx',
        connect_overwrite_unique_id=SERIAL
    ) as session:
        core = session.target.selected_core_or_raise
        
        # 读取 PC 和 xPSR
        pc = core.read_core_register('pc')
        xpsr = core.read_core_register('xpsr')
        sp = core.read_core_register('sp')
        lr = core.read_core_register('lr')
        
        print(f"\n[Core State]")
        print(f"  PC     = 0x{pc:08X}")
        print(f"  SP     = 0x{sp:08X}")
        print(f"  LR     = 0x{lr:08X}")
        print(f"  xPSR   = 0x{xpsr:08X}")
        
        # 检查是否在 Flash 区域 (0x08000000-0x0807FFFF)
        if 0x08000000 <= pc <= 0x0807FFFF:
            print(f"  → PC 在 Flash 区域 (正常)")
        elif 0x20000000 <= pc <= 0x2001FFFF:
            print(f"  → PC 在 DTCM 区域 (可能在 ITCM 执行)")
        elif pc == 0 or pc == 0xFFFFFFFF:
            print(f"  → PC 异常 (可能未运行)")
        
        # 读取关键寄存器
        print(f"\n[Clock Registers]")
        for name in ['RCC_CR', 'RCC_CFGR', 'RCC_D1CFGR', 'RCC_D2CFGR', 
                     'RCC_PLLCKSELR', 'RCC_PLLCFGR', 'RCC_PLL1DIVR',
                     'RCC_AHB1ENR', 'RCC_AHB4ENR', 'RCC_APB2ENR']:
            val = read32(core, REGISTERS[name])
            print(f"  {name:15s} = 0x{val:08X}")
        
        # 检查 PLL 锁定
        rcc_cr = read32(core, REGISTERS['RCC_CR'])
        pll1rdy = (rcc_cr >> 25) & 1
        print(f"\n  PLL1 {'LOCKED' if pll1rdy else 'NOT LOCKED'}")
        
        # TIM1 状态
        print(f"\n[TIM1 Registers]")
        for name in ['TIM1_CR1', 'TIM1_CR2', 'TIM1_DIER', 'TIM1_SR', 
                     'TIM1_CNT', 'TIM1_PSC', 'TIM1_ARR', 'TIM1_CCR1']:
            val = read32(core, REGISTERS[name])
            print(f"  {name:15s} = 0x{val:08X}")
        
        # 解析 TIM1 状态
        tim1_cr1 = read32(core, REGISTERS['TIM1_CR1'])
        tim1_dier = read32(core, REGISTERS['TIM1_DIER'])
        tim1_sr = read32(core, REGISTERS['TIM1_SR'])
        tim1_arr = read32(core, REGISTERS['TIM1_ARR'])
        
        cen = tim1_cr1 & 1
        uie = (tim1_dier >> 0) & 1
        uif = (tim1_sr >> 0) & 1
        
        print(f"\n  TIM1 CEN (计数使能): {'ON' if cen else 'OFF'}")
        print(f"  TIM1 UIE (更新中断): {'ON' if uie else 'OFF'}")
        print(f"  TIM1 UIF (更新标志): {'PENDING' if uif else 'CLEAR'}")
        print(f"  TIM1 ARR (自动重载): {tim1_arr} ({(tim1_arr+1)*7.4:.1f}ns = {(tim1_arr+1)*7.4/1000:.2f}us)")
        
        # NVIC 状态
        print(f"\n[NVIC Registers]")
        for name in ['NVIC_ISER0', 'NVIC_ISER1', 'NVIC_ICPR0', 'NVIC_ICPR1',
                     'NVIC_IABR0', 'NVIC_IABR1']:
            val = read32(core, REGISTERS[name])
            print(f"  {name:15s} = 0x{val:08X}")
        
        # 检查 TIM1_UP 中断使能
        # TIM1_UP_IRQn = 43, 在 NVIC_ISER1 的 bit (43-32)=11
        nvic_iser1 = read32(core, REGISTERS['NVIC_ISER1'])
        tim1_up_enable = (nvic_iser1 >> 11) & 1
        print(f"\n  TIM1_UP (IRQ 43) NVIC 使能: {'YES' if tim1_up_enable else 'NO'}")
        
        # 检查中断挂起
        nvic_icpr1 = read32(core, REGISTERS['NVIC_ICPR1'])
        tim1_up_pending = (nvic_icpr1 >> 11) & 1
        print(f"  TIM1_UP 中断挂起: {'YES' if tim1_up_pending else 'NO'}")
        
        # 检查中断活跃
        nvic_iabr1 = read32(core, REGISTERS['NVIC_IABR1'])
        tim1_up_active = (nvic_iabr1 >> 11) & 1
        print(f"  TIM1_UP 中断活跃: {'YES' if tim1_up_active else 'NO'}")
        
        # SCB 状态
        print(f"\n[SCB Registers]")
        for name in ['SCB_VTOR', 'SCB_CCR', 'SCB_SHCSR']:
            val = read32(core, REGISTERS[name])
            print(f"  {name:15s} = 0x{val:08X}")
        
        # DWT 状态
        print(f"\n[DWT Registers]")
        for name in ['DWT_CTRL', 'DWT_CYCCNT']:
            val = read32(core, REGISTERS[name])
            print(f"  {name:15s} = 0x{val:08X}")
        
        dwt_ctrl = read32(core, REGISTERS['DWT_CTRL'])
        cyccnt_en = (dwt_ctrl >> 0) & 1
        print(f"\n  DWT CYCCNT 使能: {'YES' if cyccnt_en else 'NO'}")
        
        # DTCM 变量
        print(f"\n[DTCM Variables]")
        for name, addr in DTCM_VARS.items():
            val = read32(core, addr)
            print(f"  {name:15s} @ 0x{addr:08X} = 0x{val:08X} ({val})")
        
        # 读取 WIRE_MAP 前 16 个值
        print(f"\n[WIRE_MAP - 前 16 个]")
        wires = read_float_block(core, REG_SPACES['WIRE_MAP'], 16)
        for i, v in enumerate(wires):
            print(f"  W[{i:3d}] = {v:.6f}")
        
        # 读取 SENSOR_MAP 前 8 个值
        print(f"\n[SENSOR_MAP - 前 8 个]")
        sensors = read_float_block(core, REG_SPACES['SENSOR_MAP'], 8)
        for i, v in enumerate(sensors):
            print(f"  S[{i:3d}] = {v:.6f}")
        
        # 读取 ACTUATOR_STATUS 前 8 个值
        print(f"\n[ACTUATOR_STATUS - 前 8 个]")
        actuators = read_float_block(core, REG_SPACES['ACTUATOR_STATUS'], 8)
        for i, v in enumerate(actuators):
            print(f"  A[{i:3d}] = {v:.6f}")
        
        # 读取 ROUTE_TABLE 前 4 个条目 (每个 16 字节)
        print(f"\n[ROUTE_TABLE - 前 4 个条目]")
        for i in range(4):
            addr = REG_SPACES['ROUTE_TABLE'] + i * 16
            data = core.read_memory_block8(addr, 16)
            import struct
            # RouteEntry: src_type(1) src_index(1) dst_type(1) dst_channel(1) op(1) flags(1) param_idx(2) state_offset(2) actuator_idx(2) wire2_idx(2) reserved(2)
            src_type, src_idx, dst_type, dst_channel, op, flags = struct.unpack('<BBBBBB', bytes(data[:6]))
            param_idx, state_off, act_idx, wire2_idx = struct.unpack('<HHHH', bytes(data[6:14]))
            print(f"  R[{i:2d}]: src={src_type}:{src_idx}  dst={dst_type}:{dst_channel}  op=0x{op:02X}  flags=0x{flags:02X}  param={param_idx}  state={state_off}  act={act_idx}")
        
        # 读取 PARAM_TABLE 前 4 个条目
        print(f"\n[PARAM_TABLE - 前 4 个条目]")
        for i in range(4):
            addr = REG_SPACES['PARAM_TABLE'] + i * 16
            data = core.read_memory_block8(addr, 16)
            import struct
            vals = struct.unpack('<ffff', bytes(data[:16]))
            print(f"  P[{i:2d}]: a={vals[0]:.4f}  b={vals[1]:.4f}  c={vals[2]:.4f}  d={vals[3]:.4f}")
        
        # GPIOE 状态
        print(f"\n[GPIOE]")
        gpioe_moder = read32(core, REGISTERS['GPIOE_MODER'])
        gpioe_odr = read32(core, REGISTERS['GPIOE_ODR'])
        print(f"  MODER = 0x{gpioe_moder:08X}")
        print(f"  ODR   = 0x{gpioe_odr:08X}")
        
        # 检查 PE2 配置
        pe2_moder = (gpioe_moder >> 4) & 3
        pe2_odr = (gpioe_odr >> 2) & 1
        print(f"  PE2 MODER = {pe2_moder} ({'Output' if pe2_moder == 1 else 'Other'})")
        print(f"  PE2 ODR   = {pe2_odr}")
        
        # 总结
        print(f"\n{'=' * 60}")
        print(f"[状态总结]")
        print(f"{'=' * 60}")
        
        # 判断运行状态
        samples = read32(core, DTCM_VARS['SAMPLES'])
        period_min = read32(core, DTCM_VARS['PERIOD_MIN'])
        period_max = read32(core, DTCM_VARS['PERIOD_MAX'])
        
        if samples > 0:
            print(f"  ✓ ISR 正在运行 (SAMPLES = {samples})")
            print(f"  抖动: MIN={period_min} cycles, MAX={period_max} cycles")
            print(f"  抖动范围: {(period_max - period_min) * 7.4:.1f}ns")
        else:
            print(f"  ✗ ISR 未运行 (SAMPLES = 0)")
            if not cen:
                print(f"    - TIM1 未使能")
            if not uie:
                print(f"    - TIM1 中断未使能")
            if not tim1_up_enable:
                print(f"    - NVIC TIM1_UP 未使能")
        
        # 检查 WIRE 是否有值
        wire_sum = sum(abs(w) for w in wires)
        if wire_sum > 0:
            print(f"  ✓ WIRE_MAP 有数据 (sum={wire_sum:.4f})")
        else:
            print(f"  ✗ WIRE_MAP 全零 (路由引擎未产生输出)")

if __name__ == '__main__':
    main()
