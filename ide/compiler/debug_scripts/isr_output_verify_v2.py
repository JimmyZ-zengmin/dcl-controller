#!/usr/bin/env python3
"""
DCL ISR测试输出正确性验证 v2 - 读取全部60个Wire
"""
import subprocess
import struct

PYOCD = r'C:\Espressif\tools\python\v6.0.1\venv\Scripts\pyocd.exe'

WIRE_MAP  = 0x20000300
ACTUATOR  = 0x20000200
SENSOR    = 0x20000100
EXEC_MIN  = 0x20000000
EXEC_MAX  = 0x20000004
SAMPLES   = 0x20000010
CLOCK_HZ  = 0x2000001C
TIMER_HZ  = 0x20000020
PE_ODR    = 0x58020014

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

def main():
    print("=" * 70)
    print("DCL ISR测试 - 输出正确性验证 v2 (60 Wires)")
    print("=" * 70)
    
    clock_hz = read32(CLOCK_HZ) or 544000000
    clock_ns = 1e9 / clock_hz
    
    exec_min = read32(EXEC_MIN)
    exec_max = read32(EXEC_MAX)
    samples = read32(SAMPLES)
    
    print(f"\n[性能数据]")
    print(f"  EXEC_MIN: {exec_min} ({exec_min * clock_ns / 1000:.2f}μs)")
    print(f"  EXEC_MAX: {exec_max} ({exec_max * clock_ns / 1000:.2f}μs)")
    print(f"  SAMPLES:  {samples}")
    
    # 读取全部60个Wire
    print(f"\n[WIRE_MAP] 全部60个")
    wires = []
    for i in range(60):
        raw = read32(WIRE_MAP + i*4)
        if raw is not None:
            fval = struct.unpack('<f', struct.pack('<I', raw))[0]
            wires.append(fval)
            if fval != 0.0:
                print(f"  WIRE[{i:2d}] = {fval:.4f} (raw: 0x{raw:08X}) ***")
        else:
            wires.append(None)
    
    # 读取ACTUATOR (64个, 包含GPIO 32-63)
    print(f"\n[ACTUATOR_STATUS] 全部64个")
    for i in range(64):
        raw = read32(ACTUATOR + i*4)
        if raw is not None:
            fval = struct.unpack('<f', struct.pack('<I', raw))[0]
            if fval != 0.0:
                print(f"  ACT[{i:2d}] = {fval:.4f} (raw: 0x{raw:08X}) ***")
    
    # 读取GPIOE ODR
    pe_odr = read32(PE_ODR)
    print(f"\n[GPIOE_ODR] = 0x{pe_odr:08X}")
    for i in range(4):
        bit = (pe_odr >> i) & 1
        print(f"  PE{i} = {'HIGH' if bit else 'LOW'}")
    
    # 分析
    print(f"\n{'='*70}")
    print("分析")
    print(f"{'='*70}")
    
    # 查找逻辑输出 (应该在wire 48-59范围)
    print(f"\n[逻辑输出搜索 - Wire 48-59]")
    for i in range(48, 60):
        if i < len(wires) and wires[i] is not None:
            print(f"  WIRE[{i}] = {wires[i]:.4f}")
    
    # 预期:
    # system_ready = 1 (所有Timer Q=1)
    # any_alarm = 1 (level_low=1)
    # fault = 1 (any_alarm=1)
    # production_ok = 0 (any_alarm=1)
    
    print(f"\n[预期逻辑输出]")
    print("  system_ready  = 1.0 (所有Timer已超时)")
    print("  any_alarm     = 1.0 (level_low: 0<20)")
    print("  fault         = 1.0 (any_alarm OR NOT system_ready)")
    print("  production_ok = 0.0 (system_ready AND NOT any_alarm AND flow_full AND ...)")

if __name__ == '__main__':
    main()
