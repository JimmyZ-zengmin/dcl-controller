#!/usr/bin/env python3
"""
DCL ISR周期极限测试 v5 - 精细扫描2μs~10μs范围
"""

import subprocess
import time
import sys

PYOCD = r'C:\Espressif\tools\python\v6.0.1\venv\Scripts\pyocd.exe'

EXEC_MIN   = 0x20000000
EXEC_MAX   = 0x20000004
PERIOD_MIN = 0x20000008
PERIOD_MAX = 0x2000000C
SAMPLES    = 0x20000010
CLOCK_HZ   = 0x2000001C
TIMER_HZ   = 0x20000020
TIM1_ARR   = 0x4001002C
TIM1_PSC   = 0x40010028
WIRE_MAP   = 0x20000300

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

def reset_counters():
    write32(EXEC_MIN, 0xFFFFFFFF)
    write32(EXEC_MAX, 0)
    write32(PERIOD_MIN, 0xFFFFFFFF)
    write32(PERIOD_MAX, 0)
    write32(SAMPLES, 0)

def main():
    print("=" * 70)
    print("DCL ISR周期极限测试 v5 - 精细扫描")
    print("=" * 70)
    
    clock_hz = read32(CLOCK_HZ) or 544000000
    timer_hz = read32(TIMER_HZ) or 136000000
    clock_ns = 1e9 / clock_hz
    timer_us = 1e6 / timer_hz
    
    print(f"CPU: {clock_hz/1e6:.0f}MHz, TIMER: {timer_hz/1e6:.0f}MHz")
    print()
    
    # 精细扫描: 从10μs降到1μs，步长更细
    # ARR = period_us / timer_us = period_us * 136
    test_periods_us = [
        10.0, 9.0, 8.0, 7.0, 6.0, 5.5, 5.0, 4.5, 4.0, 3.5, 3.0, 2.5, 2.0, 1.5, 1.0
    ]
    
    print(f"{'周期(μs)':>8} | {'ARR':>6} | {'ISR_min(μs)':>11} | {'ISR_max(μs)':>11} | {'CPU%':>7} | {'状态':>8}")
    print("-" * 70)
    
    results = []
    
    for period_us in test_periods_us:
        arr = int(period_us * 136) - 1  # ARR = period * 136 - 1
        if arr < 0:
            arr = 0
        
        # 复位计数器
        reset_counters()
        
        # 写入ARR
        write32(TIM1_ARR, arr)
        time.sleep(0.2)
        
        # 读取
        exec_min = read32(EXEC_MIN)
        exec_max = read32(EXEC_MAX)
        samples = read32(SAMPLES)
        
        isr_min = exec_min * clock_ns / 1000 if exec_min and exec_min != 0xFFFFFFFF else 0
        isr_max = exec_max * clock_ns / 1000 if exec_max else 0
        
        actual_period = (arr + 1) * timer_us
        cpu_pct = (isr_max / actual_period * 100) if actual_period > 0 else 0
        
        alive = read32(WIRE_MAP) is not None
        
        if not alive:
            status = "✗ 崩溃"
        elif cpu_pct > 100:
            status = "✗ 过载"
        elif cpu_pct > 95:
            status = "~ 临界"
        elif cpu_pct > 80:
            status = "~ 警告"
        else:
            status = "✓ 正常"
        
        print(f"{actual_period:>8.2f} | {arr:>6} | {isr_min:>10.2f} | {isr_max:>10.2f} | {cpu_pct:>6.1f} | {status:>8}")
        
        results.append({
            'period_us': actual_period,
            'arr': arr,
            'isr_min': isr_min,
            'isr_max': isr_max,
            'cpu_pct': cpu_pct,
            'alive': alive,
        })
        
        if not alive or cpu_pct > 100:
            break
    
    # 恢复
    write32(TIM1_ARR, 13599)
    write32(TIM1_PSC, 0)
    
    print()
    print("=" * 70)
    print("分析结果")
    print("=" * 70)
    
    # 找到最小稳定周期
    min_stable = None
    for r in results:
        if r['alive'] and r['cpu_pct'] <= 100:
            min_stable = r
    
    if min_stable:
        print(f"最小稳定周期: {min_stable['period_us']:.2f}μs (ARR={min_stable['arr']})")
        print(f"  ISR执行时间: {min_stable['isr_min']:.2f}μs ~ {min_stable['isr_max']:.2f}μs")
        print(f"  CPU占用率: {min_stable['cpu_pct']:.1f}%")
    
    # 找到崩溃点
    for r in results:
        if not r['alive'] or r['cpu_pct'] > 100:
            print(f"崩溃点: {r['period_us']:.2f}μs (ARR={r['arr']})")
            break
    
    # 计算抖动
    if len(results) >= 2:
        isr_times = [r['isr_max'] for r in results if r['isr_max'] > 0]
        if isr_times:
            jitter = max(isr_times) - min(isr_times)
            print(f"ISR执行时间抖动: {jitter:.2f}μs (max-min)")
            print(f"平均ISR执行时间: {sum(isr_times)/len(isr_times):.2f}μs")
    
    print()
    print("理论最大频率:")
    if min_stable:
        max_freq = 1e6 / min_stable['period_us']
        print(f"  {max_freq/1000:.1f} kHz ({min_stable['period_us']:.2f}μs周期)")

if __name__ == '__main__':
    main()
