#!/usr/bin/env python3
"""测试 loadmem 写 ROUTE_TABLE: 写入后立即读回"""
import sys, os, struct, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core'))
from dcl_hardware import Hardware, ADDRESSES

hw = Hardware()
if not hw.connect():
    print("连接失败!"); sys.exit(1)

ROUTE_ADDR = ADDRESSES['ROUTE_TABLE']

# 构造已知数据: Route 0 = DIRECT, src=sensor0, dst=wire0
# 包装成16字节struct (实际写入的是route_bin, 不需要容器格式)
route_data = bytearray(64)  # 4条路由 × 16字节 = 64字节
# Route 0: DIRECT, src_type=0, src_index=0, dst_type=3, dst_channel=0, op=0, flags=1
route_data[0] = 0    # src_type = SRC_SENSOR
route_data[1] = 0    # src_index = 0
route_data[2] = 3    # dst_type = DST_WIRE
route_data[3] = 0    # dst_channel = 0
route_data[4] = 0    # op = DIRECT
route_data[5] = 1    # flags = ENABLED
# 其余补0

print("=" * 60)
print("STEP 1: 写入 ROUTE_TABLE (4 routes, 64 bytes)")
print("=" * 60)
ok = hw.write_block(ROUTE_ADDR, bytes(route_data))
print(f"  write_block: {'成功' if ok else '失败: ' + str(hw.last_error)}")

print("\n" + "=" * 60)
print("STEP 2: 立即读回")
print("=" * 60)
raw = hw.read32(ROUTE_ADDR, 16)  # 16 words = 64 bytes
if raw:
    for i in range(4):
        base = i * 4
        b = struct.pack('<IIII', raw[base], raw[base+1], raw[base+2], raw[base+3])
        src_type, src_index, dst_type, dst_channel = b[0], b[1], b[2], b[3]
        op, flags = b[4], b[5]
        enabled = "✓" if flags & 1 else "✗"
        print(f"  Route {i}: {enabled} src_type={src_type} src={src_index} "
              f"dst_type={dst_type} dst={dst_channel} op={op} flags={flags}")

print("\n" + "=" * 60)
print("STEP 3: 设置 ACTIVE_ROUTES=4, 启动 TIM1, 等 50ms")
print("=" * 60)
hw.write32(ADDRESSES['ACTIVE_ROUTES'], 4)
hw.write32(0x4001000C, 1)  # DIER UIE=1
hw.write32(0x40010000, 1)  # CR1 CEN=1

# 使能 NVIC
nvic_iser1 = 0xE000E104
hw.write32(nvic_iser1, 1 << 11)

time.sleep(0.05)  # 50ms

print("\n" + "=" * 60)
print("STEP 4: 50ms 后再读 ROUTE_TABLE")
print("=" * 60)
raw2 = hw.read32(ROUTE_ADDR, 16)
if raw2:
    for i in range(4):
        base = i * 4
        b = struct.pack('<IIII', raw2[base], raw2[base+1], raw2[base+2], raw2[base+3])
        src_type, src_index, dst_type, dst_channel = b[0], b[1], b[2], b[3]
        op, flags = b[4], b[5]
        enabled = "✓" if flags & 1 else "✗"
        print(f"  Route {i}: {enabled} src_type={src_type} src={src_index} "
              f"dst_type={dst_type} dst={dst_channel} op={op} flags={flags}")

print("\n" + "=" * 60)
print("STEP 5: 读取 WIRE_MAP")
print("=" * 60)
wires = hw.read_wires(0, 4)
if wires:
    for i, v in enumerate(wires):
        print(f"  wire[{i}] = {v:.6f}")

print("\n" + "=" * 60)
print("STEP 6: 读取 SENSOR_MAP")
print("=" * 60)
sensors = hw.read_sensors(0, 4)
if sensors:
    for i, v in enumerate(sensors):
        print(f"  sensor[{i}] = {v:.6f}")

print("\n" + "=" * 60)
print("结论: Route 0 将 sensor[0] 直传到 wire[0]")
print(f"  sensor[0] = {sensors[0] if sensors else '?'}")
print(f"  wire[0]   = {wires[0] if wires else '?'}")
