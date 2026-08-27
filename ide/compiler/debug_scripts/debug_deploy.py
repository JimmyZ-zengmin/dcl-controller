#!/usr/bin/env python3
"""Debug: 检查 deploy 后 ROUTE_TABLE 实际内容"""
import sys
sys.path.insert(0, r'D:\STM\work\dcl-controller\ide\compiler')
from core.dcl_hardware import get_hardware, ADDRESSES, TIM1_CR1, TIM1_DIER

hw = get_hardware()
hw.connect()

# 1. 检查 TIM1 状态 (ISR 引擎是否运行)
print("=== TIM1 STATUS ===")
cr1 = hw.read32(TIM1_CR1, 1)
dier = hw.read32(TIM1_DIER, 1)
print(f"CR1:  {cr1[0]:08X} (CEN bit: {cr1[0] & 1})")
print(f"DIER: {dier[0]:08X} (UIE bit: {dier[0] & 1})")

# 2. 检查 ACTIVE_ROUTES
print("\n=== ACTIVE_ROUTES ===")
active = hw.get_active_routes()
print(f"ACTIVE_ROUTES: {active}")

# 3. 读取 ROUTE_TABLE 前 20 个条目 (320 bytes)
print("\n=== ROUTE_TABLE (first 20 entries) ===")
raw = hw.read32(ADDRESSES['ROUTE_TABLE'], 80)  # 80 words = 320 bytes = 20 routes
if raw:
    for i in range(min(20, len(raw) // 4)):
        addr = ADDRESSES['ROUTE_TABLE'] + i * 16
        w0, w1, w2, w3 = raw[i*4], raw[i*4+1], raw[i*4+2], raw[i*4+3]
        # 解析路由条目
        b = w0 & 0xFF
        src_type = w0 & 0xFF
        src_index = (w0 >> 8) & 0xFF
        dst_type = (w0 >> 16) & 0xFF
        dst_channel = (w0 >> 24) & 0xFF
        op = w1 & 0xFF
        flags = (w1 >> 8) & 0xFF
        param_idx = (w1 >> 16) & 0xFFFF
        state_offset = w2 & 0xFFFF
        actuator_idx = (w2 >> 16) & 0xFFFF
        wire2_idx = w3 & 0xFFFF
        enabled = "EN" if (flags & 1) else "  "
        print(f"  Route[{i:2d}]: {enabled} src:{src_type}:{src_index} dst:{dst_type}:{dst_channel} op:{op:02X} fl:{flags:02X} pi:{param_idx} so:{state_offset} ai:{actuator_idx}")
else:
    print("  READ FAILED")

# 4. 读取 PARAM_TABLE 前 5 个条目
print("\n=== PARAM_TABLE (first 5 entries) ===")
import struct
raw_p = hw.read32(ADDRESSES['PARAM_TABLE'], 20)  # 20 words = 5 entries
if raw_p:
    for i in range(min(5, len(raw_p) // 4)):
        vals = raw_p[i*4:i*4+4]
        floats = [struct.unpack('f', struct.pack('I', v))[0] for v in vals]
        print(f"  Param[{i}]: a={floats[0]:.4f} b={floats[1]:.4f} c={floats[2]:.4f} d={floats[3]:.4f}")
else:
    print("  READ FAILED")

# 5. 读取 WIRE 值
print("\n=== WIRE VALUES (0-15) ===")
wires = hw.read_wires(0, 16)
if wires:
    for i, v in enumerate(wires):
        print(f"  WIRE[{i:2d}] = {v}")
