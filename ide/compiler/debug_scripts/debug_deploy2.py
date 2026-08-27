#!/usr/bin/env python3
"""Debug: 逐步执行 deploy 并检查每一步"""
import sys, struct, os, time
sys.path.insert(0, r'D:\STM\work\dcl-controller\ide\compiler')
from core.dcl_hardware import get_hardware, ADDRESSES, TIM1_CR1, TIM1_DIER, TIM1_BASE

hw = get_hardware()
hw.connect()

# 先停止 TIM1 (确保 ISR 不运行)
hw.write32(TIM1_DIER, 0)
hw.write32(TIM1_CR1, 0)
time.sleep(0.1)

# 读取编译后的二进制
with open(r'D:\STM\work\dcl-controller\ide\compiler\test_logic.bin', 'rb') as f:
    data = f.read()

route_count = struct.unpack_from('<I', data, 0)[0]
param_count = struct.unpack_from('<I', data, 4)[0]
active_routes = struct.unpack_from('<I', data, 8)[0]
route_data = data[12:12+route_count*16]
param_data = data[12+route_count*16:12+route_count*16+param_count*16]

print(f"Binary: {route_count} routes, {param_count} params, {len(route_data)} bytes route_data, {len(param_data)} bytes param_data")
print(f"route_data first 16 bytes: {route_data[:16].hex()}")
print(f"param_data first 16 bytes: {param_data[:16].hex()}")

# Step 1: 清零 ROUTE_TABLE
print("\n=== Step 1: fill ROUTE_TABLE ===")
ok = hw.fill_block(ADDRESSES['ROUTE_TABLE'], 16384)
print(f"fill_block result: {ok}")
if not ok:
    print(f"Error: {hw.last_error}")

# 验证清零
raw = hw.read32(ADDRESSES['ROUTE_TABLE'], 4)
print(f"After fill: {raw}")

# Step 2: 写入 route_data
print("\n=== Step 2: write ROUTE_TABLE ===")
print(f"Writing {len(route_data)} bytes to 0x{ADDRESSES['ROUTE_TABLE']:08X}")
ok = hw.write_block(ADDRESSES['ROUTE_TABLE'], route_data)
print(f"write_block result: {ok}")
if not ok:
    print(f"Error: {hw.last_error}")

# 验证写入
raw = hw.read32(ADDRESSES['ROUTE_TABLE'], 8)
print(f"After write: {[f'{v:08X}' for v in raw]}")

# 对比期望值
expected_words = [struct.unpack_from('<I', route_data, i*4)[0] for i in range(8)]
print(f"Expected:     {[f'{v:08X}' for v in expected_words]}")

match = raw == expected_words
print(f"Match: {match}")

# Step 3: 写入 PARAM_TABLE
print("\n=== Step 3: fill+write PARAM_TABLE ===")
hw.fill_block(ADDRESSES['PARAM_TABLE'], 8192)
ok = hw.write_block(ADDRESSES['PARAM_TABLE'], param_data)
print(f"write_block PARAM_TABLE result: {ok}")

raw_p = hw.read32(ADDRESSES['PARAM_TABLE'], 8)
print(f"After write: {[f'{v:08X}' for v in raw_p]}")

# Step 4: 设置 ACTIVE_ROUTES
print("\n=== Step 4: set ACTIVE_ROUTES ===")
ok = hw.write32(ADDRESSES['ACTIVE_ROUTES'], active_routes)
print(f"write32 ACTIVE_ROUTES result: {ok}")

rawActive = hw.read32(ADDRESSES['ACTIVE_ROUTES'], 1)
print(f"ACTIVE_ROUTES: {rawActive}")

# Step 5: 启动 TIM1
print("\n=== Step 5: start TIM1 ===")
hw.write32(TIM1_DIER, 1)
hw.write32(TIM1_CR1, 1)
time.sleep(0.1)

# Step 6: 读取 WIRE
print("\n=== Step 6: read WIRES ===")
wires = hw.read_wires(0, 16)
for i, v in enumerate(wires):
    print(f"  WIRE[{i}] = {v}")
