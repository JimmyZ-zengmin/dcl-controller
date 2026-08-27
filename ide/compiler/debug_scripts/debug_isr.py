#!/usr/bin/env python3
"""读取 ROUTE_TABLE 和 PARAM_TABLE 内存，确认部署数据正确"""
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core'))
from dcl_hardware import Hardware, ADDRESSES

hw = Hardware()
if not hw.connect():
    print("连接失败!")
    sys.exit(1)

ROUTE_TABLE_ADDR = ADDRESSES['ROUTE_TABLE']
PARAM_TABLE_ADDR = ADDRESSES['PARAM_TABLE']
ACTIVE_ROUTES_ADDR = ADDRESSES['ACTIVE_ROUTES']

print("=" * 60)
print("读取 ACTIVE_ROUTES")
raw = hw.read32(ACTIVE_ROUTES_ADDR, 1)
if raw:
    print(f"  ACTIVE_ROUTES = {raw[0]}")
else:
    print("  读取失败")

print("\n" + "=" * 60)
print("读取 ROUTE_TABLE 前 16 条路由 (256 bytes / 64 words)")
raw = hw.read32(ROUTE_TABLE_ADDR, 64)
if not raw:
    print("  读取失败!")
    sys.exit(1)

# 按 RouteEntry_t 解析
for i in range(min(len(raw) // 4, 16)):
    base = i * 4
    b = [struct.pack('<I', raw[base+j]) for j in range(4)]
    entry = b''.join(b)
    src_type, src_index, dst_type, dst_channel = entry[0], entry[1], entry[2], entry[3]
    op, flags = entry[4], entry[5]
    param_idx, state_offset, actuator_idx, wire2_idx = struct.unpack('<HHHH', entry[6:14])
    
    enabled = "✓" if (flags & 0x01) else "✗"
    op_names = {0:'DIRECT',1:'CMP',2:'HYST',4:'LPF',5:'PID',15:'AND',16:'OR',17:'NOT',14:'SCALE',30:'LIMIT'}
    op_name = op_names.get(op, f'0x{op:02X}')
    
    print(f"  R{i:2d}: {enabled} {op_name:8s} src_type={src_type} src={src_index:2d} "
          f"dst={dst_channel:2d} op={op:2d} flags={flags:2d} pi={param_idx:2d} "
          f"so={state_offset:2d} ai={actuator_idx:2d}")

print("\n" + "=" * 60)
print("读取 PARAM_TABLE 前 8 个参数")
raw = hw.read32(PARAM_TABLE_ADDR, 32)
if not raw:
    print("  读取失败!")
    sys.exit(1)

for i in range(min(len(raw) // 4, 8)):
    base = i * 4
    vals = struct.unpack('<ffff', struct.pack('<IIII', raw[base], raw[base+1], raw[base+2], raw[base+3]))
    print(f"  P{i}: a={vals[0]:.4f} b={vals[1]:.4f} c={vals[2]:.4f} d={vals[3]:.4f}")

print("\n" + "=" * 60)
print("读取 WIRE_MAP 前 16 个")
raw = hw.read32(ADDRESSES['WIRE_MAP'], 16)
if raw:
    for i, v in enumerate(raw):
        fval = struct.unpack('f', struct.pack('I', v))[0]
        print(f"  wire[{i:2d}] = {fval:.6f}")

print("\n" + "=" * 60)
print("读取 SENSOR_MAP 前 8 个")
raw = hw.read32(ADDRESSES['SENSOR_MAP'], 8)
if raw:
    for i, v in enumerate(raw):
        fval = struct.unpack('f', struct.pack('I', v))[0]
        print(f"  sensor[{i}] = {fval:.6f}")
