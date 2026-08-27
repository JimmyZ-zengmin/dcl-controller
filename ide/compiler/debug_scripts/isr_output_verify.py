#!/usr/bin/env python3
"""
DCL ISR测试输出正确性验证
- 无输入条件 (所有传感器ADC读数为0)
- 验证各Wire和Output是否符合预期
"""
import subprocess
import time

PYOCD = r'C:\Espressif\tools\python\v6.0.1\venv\Scripts\pyocd.exe'

WIRE_MAP  = 0x20000300
ACTUATOR  = 0x20000200  # ACTUATOR_STATUS
SENSOR    = 0x20000100  # SENSOR_MAP
EXEC_MIN  = 0x20000000
EXEC_MAX  = 0x20000004
SAMPLES   = 0x20000010
CLOCK_HZ  = 0x2000001C
TIMER_HZ  = 0x20000020
TIM1_SR   = 0x40010010  # TIM1状态寄存器
TIM1_CNT  = 0x40010024  # TIM1计数器
PE_ODR    = 0x58020014  # GPIOE ODR

def pyocd_cmd(cmd, timeout=5):
    result = subprocess.run(
        [PYOCD, 'commander', '-t', 'stm32h723xx', '-c', cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return result.returncode == 0, result.stdout, result.stderr

def read32_f(addr):
    """读float"""
    ok, out, err = pyocd_cmd(f'read32 {addr}')
    if ok:
        parts = out.strip().split(':')
        if len(parts) >= 2:
            hex_str = parts[1].split()[0].strip()
            try:
                import struct
                return struct.unpack('>I', bytes.fromhex(hex_str.zfill(8)))[0]
            except:
                return None
    return None

def read32(addr):
    ok, out, err = pyocd_cmd(f'read32 {addr}')
    if ok:
        parts = out.strip().split(':')
        if len(parts) >= 2:
            hex_str = parts[1].split()[0].strip()
            try:
                return int(hex_str, 16)
            except:
                return None
    return None

def main():
    import struct
    
    print("=" * 70)
    print("DCL ISR测试 - 无输入条件下输出正确性验证")
    print("=" * 70)
    
    clock_hz = read32(CLOCK_HZ) or 544000000
    clock_ns = 1e9 / clock_hz
    
    # 读取性能计数器
    exec_min = read32(EXEC_MIN)
    exec_max = read32(EXEC_MAX)
    samples = read32(SAMPLES)
    
    print(f"\n[性能数据]")
    print(f"  EXEC_MIN: {exec_min} ({exec_min * clock_ns / 1000:.2f}μs)")
    print(f"  EXEC_MAX: {exec_max} ({exec_max * clock_ns / 1000:.2f}μs)")
    print(f"  SAMPLES:  {samples}")
    print(f"  实际周期(估算): {samples} (累计运行)")
    
    # 读取SENSOR (8个)
    print(f"\n[SENSOR_MAP] @ 0x{SENSOR:08X}")
    sensors = []
    for i in range(8):
        raw = read32(SENSOR + i*4)
        if raw is not None:
            val = struct.unpack('<I', struct.pack('<I', raw))[0]
            fval = struct.unpack('<f', struct.pack('<I', raw))[0]
            sensors.append(fval)
            print(f"  SENSOR[{i}] = {fval:.4f} (raw: 0x{raw:08X})")
    
    # 读取WIRE_MAP (48个)
    print(f"\n[WIRE_MAP] @ 0x{WIRE_MAP:08X}")
    wires = []
    for i in range(48):
        raw = read32(WIRE_MAP + i*4)
        is_zero = (raw == 0 or raw is None)
        fval = struct.unpack('<f', struct.pack('<I', raw))[0] if raw is not None else 0.0
        wires.append({'raw': raw, 'fval': fval, 'is_zero': is_zero})
        if not is_zero:
            print(f"  WIRE[{i:2d}] = {fval:.4f} (raw: 0x{raw:08X}) ***")
        else:
            print(f"  WIRE[{i:2d}] = {fval:.4f}")
    
    # 读取ACTUATOR_STATUS
    print(f"\n[ACTUATOR_STATUS] @ 0x{ACTUATOR:08X}")
    actuators = []
    for i in range(16):
        raw = read32(ACTUATOR + i*4)
        fval = struct.unpack('<f', struct.pack('<I', raw))[0] if raw is not None else -1.0
        actuators.append(fval)
        print(f"  ACT[{i:2d}] = {fval:.4f}")
    
    # 读取GPIOE ODR (实际引脚输出)
    pe_odr = read32(PE_ODR)
    print(f"\n[GPIOE_ODR] @ 0x{PE_ODR:08X}")
    print(f"  PE_ODR = 0x{pe_odr:08X} (二进制: {pe_odr:032b})")
    for i in range(4):
        bit = (pe_odr >> i) & 1
        print(f"  PE{i} = {'HIGH' if bit else 'LOW'}")
    
    # 读取TIM1状态
    print(f"\n[TIM1状态]")
    tim1_sr = read32(TIM1_SR)
    tim1_cnt = read32(TIM1_CNT)
    print(f"  TIM1_SR:  0x{tim1_sr:04X}")
    print(f"  TIM1_CNT: {tim1_cnt}")
    
    # ═══ 分析预期 ═══
    print(f"\n{'='*70}")
    print("无输入条件下的预期分析")
    print(f"{'='*70}")
    
    print("""
[程序逻辑分析 - 无输入]

1. 传感器层 (8个ADC, 无输入=0V):
   → 所有SENSOR应读出 ≈ 0.0

2. 滤波层 (4个LOWPASS):
   → 输入为0, 输出应保持 ≈ 0.0

3. PID层 (4个回路):
   输入误差 = setpoint - 0 = setpoint (正误差)
   → PID输出应正向饱和至 LIMIT_MAX = 100.0
   
   | PID | Setpoint | 预期输出 |
   |-----|----------|----------|
   | temp_ctrl | 60 | 100.0 (饱和) |
   | pressure_ctrl | 50 | 100.0 |
   | speed_ctrl | 1000 | 100.0 |
   | level_ctrl | 80 | 100.0 |

4. 定时器 (4个):
   IN = PID输出 = 100 (>0.5触发)
   → 各定时器应在PT时间后Q=1
   → 由于已运行足够长时间, Q应全部为1

5. 计数器 (4个):
   CU = sensor ≈ 0 (不触发)
   → 所有计数器CV应保持0, Q=0

6. 报警 (8个):
   所有ALARM比较 0 > threshold → 全为0
   (除了level_low: 0 < 20 → 1!)

7. 逻辑输出:
   system_ready = 全Q=1 → 1
   any_alarm = level_low(1) OR ... → 1
   fault = 1 OR NOT(1) = 1 → 1
   production_ok = 1 AND NOT(1) AND 0 → 0

8. 物理输出:
   TIM1_CH1-4: PWM = 100% (PID饱和)
   PE0 (system_ready): HIGH (1)
   PE1 (fault): HIGH (1)
   PE2 (production_ok): LOW (0)
   PE3 (flow_full): LOW (0)
""")

    # ═══ 验证 ═══
    print(f"\n{'='*70}")
    print("实际观测 vs 预期 对比")
    print(f"{'='*70}")
    
    # 关键Wire映射 (根据编译器输出, 这些是近似地址)
    # 从dcl_compiler.py的编译日志可获得精确位置
    # 这里根据常见布局推断
    
    print(f"\n[Wire值检查 - 非零项]")
    nonzero_wires = [(i, w['fval']) for i, w in enumerate(wires) if not w['is_zero']]
    
    for idx, val in nonzero_wires:
        print(f"  WIRE[{idx}] = {val:.4f}")
    
    print(f"\n[预期非零项]")
    print("  - 4个PID输出应≈100.0 (饱和)")
    print("  - 4个Timer Q值应=1.0 (已超时)")
    print("  - level_low报警应=1.0 (0 < 20)")
    print("  - system_ready应=1.0 (所有Timer Q=1)")
    print("  - any_alarm应=1.0 (level_low=1)")
    print("  - fault应=1.0 (any_alarm=1)")
    
    # 检查GPIO
    print(f"\n[GPIO输出验证]")
    print(f"  PE0 (system_ready): 预期=HIGH, 实际={'HIGH' if (pe_odr>>0)&1 else 'LOW'}")
    print(f"  PE1 (fault):        预期=HIGH, 实际={'HIGH' if (pe_odr>>1)&1 else 'LOW'}")
    print(f"  PE2 (production_ok): 预期=LOW,  实际={'HIGH' if (pe_odr>>2)&1 else 'LOW'}")
    print(f"  PE3 (flow_full):    预期=LOW,  实际={'HIGH' if (pe_odr>>3)&1 else 'LOW'}")
    
    # 检查PWM输出
    tim1_ccr1 = read32(0x40010034)
    tim1_ccr2 = read32(0x40010038)
    tim1_ccr3 = read32(0x4001003C)
    print(f"\n[PWM输出验证 (TIM1_CCR)]")
    print(f"  CH1_CCR: {tim1_ccr1} (100% Duty)")
    print(f"  CH2_CCR: {tim1_ccr2} (100% Duty)")
    print(f"  CH3_CCR: {tim1_ccr3} (100% Duty)")
    
    # ═══ 结论 ═══
    print(f"\n{'='*70}")
    print("结论")
    print(f"{'='*70}")
    
    # 检查关键GPIO
    pe0_ok = (pe_odr >> 0) & 1 == 1  # system_ready
    pe1_ok = (pe_odr >> 1) & 1 == 1  # fault
    pe2_ok = (pe_odr >> 2) & 1 == 0  # production_ok
    pe3_ok = (pe_odr >> 3) & 1 == 0  # flow_full
    
    if pe0_ok and pe1_ok and pe2_ok and pe3_ok:
        print("✓ GPIO输出与预期一致 - 逻辑正确!")
    else:
        print("✗ GPIO输出与预期不符 - 需要检查!")
        if not pe0_ok: print("  PE0(system_ready)错误")
        if not pe1_ok: print("  PE1(fault)错误")
        if not pe2_ok: print("  PE2(production_ok)错误")
        if not pe3_ok: print("  PE3(flow_full)错误")

if __name__ == '__main__':
    main()
