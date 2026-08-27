#!/usr/bin/env python3
"""
DCL ISR周期极限测试 v4 - 使用固件性能计数器

固件性能计数器地址（DTCM_BASE = 0x20000000）:
  EXEC_MIN    = 0x20000000 (ISR执行时间最小值，DWT_CYCCNT周期)
  EXEC_MAX    = 0x20000004 (ISR执行时间最大值)
  PERIOD_MIN  = 0x20000008 (ISR周期最小值)
  PERIOD_MAX  = 0x2000000C (ISR周期最大值)
  SAMPLES     = 0x20000010 (采样数)
  CLOCK_HZ    = 0x2000001C (CPU频率，应为136000000)
  TIMER_HZ    = 0x20000020 (定时器频率)

DWT_CYCCNT频率 = 136MHz → 1周期 = 7.4ns
"""

import subprocess
import time
import sys

PYOCD = r'C:\Espressif\tools\python\v6.0.1\venv\Scripts\pyocd.exe'

# 性能计数器地址
PERF_BASE  = 0x20000000
EXEC_MIN   = 0x20000000
EXEC_MAX   = 0x20000004
PERIOD_MIN = 0x20000008
PERIOD_MAX = 0x2000000C
SAMPLES    = 0x20000010
CLOCK_HZ   = 0x2000001C
TIMER_HZ   = 0x20000020

TIM1_ARR = 0x4001002C
TIM1_PSC = 0x40010028
WIRE_MAP = 0x20000300

def pyocd_cmd(cmd, timeout=5):
    result = subprocess.run(
        [PYOCD, 'commander', '-t', 'stm32h723xx', '-c', cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return result.returncode == 0, result.stdout, result.stderr

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

def write32(addr, val):
    ok, out, err = pyocd_cmd(f'write32 {addr} {val}')
    return ok

def read_performance_counters():
    """读取所有性能计数器"""
    exec_min = read32(EXEC_MIN)
    exec_max = read32(EXEC_MAX)
    period_min = read32(PERIOD_MIN)
    period_max = read32(PERIOD_MAX)
    samples = read32(SAMPLES)
    clock_hz = read32(CLOCK_HZ)
    timer_hz = read32(TIMER_HZ)
    
    return {
        'exec_min': exec_min,
        'exec_max': exec_max,
        'period_min': period_min,
        'period_max': period_max,
        'samples': samples,
        'clock_hz': clock_hz,
        'timer_hz': timer_hz,
    }

def reset_performance_counters():
    """复位性能计数器"""
    # 写入0xFFFFFFFF到EXEC_MIN和PERIOD_MIN
    # 写入0到EXEC_MAX和PERIOD_MAX
    write32(EXEC_MIN, 0xFFFFFFFF)
    write32(EXEC_MAX, 0)
    write32(PERIOD_MIN, 0xFFFFFFFF)
    write32(PERIOD_MAX, 0)
    write32(SAMPLES, 0)

def main():
    print("=" * 70)
    print("DCL ISR周期极限测试 v4 - 使用固件性能计数器")
    print("程序: stress_test.dcl (43 routes, 39 params, 48 wires)")
    print("=" * 70)
    
    # 读取当前配置
    arr = read32(TIM1_ARR)
    psc = read32(TIM1_PSC)
    
    # 读取性能计数器初始值
    perf = read_performance_counters()
    clock_hz = perf['clock_hz'] if perf['clock_hz'] else 136000000
    timer_hz = perf['timer_hz'] if perf['timer_hz'] else 136000000
    
    clock_ns = 1e9 / clock_hz  # 每个时钟周期的纳秒数
    timer_us = 1e6 / timer_hz  # 每个定时器周期的微秒数
    
    print(f"当前配置: ARR={arr}, PSC={psc}")
    print(f"CPU频率: {clock_hz/1e6:.0f} MHz ({clock_ns:.1f}ns/周期)")
    print(f"定时器频率: {timer_hz/1e6:.0f} MHz")
    print(f"当前ISR周期: {(arr+1) * timer_us:.1f} μs")
    print()
    
    # 读取初始性能
    print("初始性能计数器:")
    print(f"  EXEC_MIN: {perf['exec_min']} ({perf['exec_min'] * clock_ns / 1000:.1f}μs)")
    print(f"  EXEC_MAX: {perf['exec_max']} ({perf['exec_max'] * clock_ns / 1000:.1f}μs)")
    print(f"  PERIOD_MIN: {perf['period_min']}")
    print(f"  PERIOD_MAX: {perf['period_max']}")
    print(f"  SAMPLES: {perf['samples']}")
    print()
    
    # 复位计数器
    reset_performance_counters()
    time.sleep(0.5)  # 等待采样
    
    # 读取稳定后的性能
    perf = read_performance_counters()
    print("稳定后性能计数器:")
    print(f"  EXEC_MIN: {perf['exec_min']} ({perf['exec_min'] * clock_ns / 1000:.1f}μs)")
    print(f"  EXEC_MAX: {perf['exec_max']} ({perf['exec_max'] * clock_ns / 1000:.1f}μs)")
    print(f"  PERIOD_MIN: {perf['period_min']}")
    print(f"  PERIOD_MAX: {perf['period_max']}")
    print(f"  SAMPLES: {perf['samples']}")
    print()
    
    # 计算ISR执行时间
    isr_time_min = perf['exec_min'] * clock_ns / 1000  # μs
    isr_time_max = perf['exec_max'] * clock_ns / 1000  # μs
    
    print(f"ISR执行时间: {isr_time_min:.1f}μs ~ {isr_time_max:.1f}μs")
    print(f"当前周期: {(arr+1) * timer_us:.1f}μs")
    print(f"CPU占用率: {isr_time_max / ((arr+1) * timer_us) * 100:.1f}%")
    print()
    
    # 开始降低周期测试
    print("=" * 70)
    print("开始降低ISR周期测试...")
    print("=" * 70)
    print()
    print(f"{'ARR':>6} | {'周期(μs)':>10} | {'CPU%':>8} | {'状态':>10} | {'备注'}")
    print("-" * 70)
    
    # 测试周期列表（从100μs降到1μs）
    test_arr_values = [
        13600,  # 100μs
        6800,   # 50μs
        2720,   # 20μs
        1360,   # 10μs
        680,    # 5μs
        272,    # 2μs
        136,    # 1μs
        68,     # 0.5μs
    ]
    
    results = []
    
    for arr_val in test_arr_values:
        period_us = arr_val * timer_us
        
        # 复位计数器
        reset_performance_counters()
        
        # 写入新的ARR
        write32(TIM1_ARR, arr_val)
        time.sleep(0.3)  # 等待稳定
        
        # 读取性能
        perf = read_performance_counters()
        
        isr_min = perf['exec_min'] * clock_ns / 1000 if perf['exec_min'] else 0
        isr_max = perf['exec_max'] * clock_ns / 1000 if perf['exec_max'] else 0
        
        # 计算CPU占用率
        cpu_pct = (isr_max / period_us * 100) if period_us > 0 else 0
        
        # 判断状态
        alive = read32(WIRE_MAP) is not None
        
        if not alive:
            status = "✗ 崩溃"
        elif cpu_pct > 100:
            status = "✗ 过载"
        elif cpu_pct > 80:
            status = "~ 警告"
        else:
            status = "✓ 正常"
        
        note = f"ISR={isr_max:.1f}μs"
        
        print(f"{arr_val:>6} | {period_us:>10.1f} | {cpu_pct:>7.1f}% | {status:>10} | {note}")
        
        results.append({
            'arr': arr_val,
            'period_us': period_us,
            'cpu_pct': cpu_pct,
            'isr_max': isr_max,
            'alive': alive,
        })
        
        if not alive or cpu_pct > 100:
            break
    
    # 恢复
    print()
    print("恢复原始配置...")
    write32(TIM1_ARR, arr)
    write32(TIM1_PSC, psc)
    
    # 汇总
    print()
    print("=" * 70)
    print("测试结果汇总")
    print("=" * 70)
    
    # 找到最小稳定周期
    min_stable = None
    for r in results:
        if r['alive'] and r['cpu_pct'] <= 100:
            min_stable = r
    
    if min_stable:
        print(f"最小稳定周期: {min_stable['period_us']:.1f}μs (ARR={min_stable['arr']})")
        print(f"此时CPU占用率: {min_stable['cpu_pct']:.1f}%")
        print(f"ISR执行时间: {min_stable['isr_max']:.1f}μs")
    
    # 找到崩溃点
    crash = None
    for r in results:
        if not r['alive'] or r['cpu_pct'] > 100:
            crash = r
            break
    
    if crash:
        print(f"崩溃/过载点: {crash['period_us']:.1f}μs (ARR={crash['arr']})")
    
    print()
    print(f"总测试点数: {len(results)}")
    print(f"正常点数: {sum(1 for r in results if r['alive'] and r['cpu_pct'] <= 100)}")
    print(f"异常点数: {sum(1 for r in results if not r['alive'] or r['cpu_pct'] > 100)}")

if __name__ == '__main__':
    main()
