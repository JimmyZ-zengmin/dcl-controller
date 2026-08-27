#!/usr/bin/env python3
"""检查编译后的二进制文件内容"""
import struct

with open(r'D:\STM\work\dcl-controller\ide\compiler\test_logic.bin', 'rb') as f:
    data = f.read()

print(f'Binary size: {len(data)} bytes')
n_routes = struct.unpack_from('<I', data, 0)[0]
n_params = struct.unpack_from('<I', data, 4)[0]
active = struct.unpack_from('<I', data, 8)[0]
print(f'Header: n_routes={n_routes}, n_params={n_params}, active={active}')

# 读取前 14 条路由 (offset 12, 每条 16 bytes)
offset = 12
route_data = data[offset:offset + n_routes * 16]
print(f'\nRoute data: {len(route_data)} bytes ({len(route_data)//16} routes)')
for i in range(n_routes):
    route = route_data[i*16:i*16+16]
    src_type, src_index, dst_type, dst_channel, op, flags = route[0], route[1], route[2], route[3], route[4], route[5]
    param_idx = struct.unpack_from('<H', route, 6)[0]
    state_offset = struct.unpack_from('<H', route, 8)[0]
    actuator_idx = struct.unpack_from('<H', route, 10)[0]
    enabled = 'EN' if (flags & 1) else '  '
    print(f'Route[{i:2d}]: {enabled} src:{src_type}:{src_index} dst:{dst_type}:{dst_channel} op:{op:02X} fl:{flags:02X} pi:{param_idx} so:{state_offset} ai:{actuator_idx}')

# 检查 route_data 是否全为零
if all(b == 0 for b in route_data):
    print('\n*** WARNING: route_data is all zeros! ***')
else:
    print(f'\nroute_data has {sum(1 for b in route_data if b != 0)} non-zero bytes')

# 读取 param 数据
param_offset = offset + n_routes * 16
param_data = data[param_offset:param_offset + n_params * 16]
print(f'\nParam data: {len(param_data)} bytes ({len(param_data)//16} params)')
for i in range(n_params):
    p = param_data[i*16:i*16+16]
    values = struct.unpack('<ffff', p)
    print(f'Param[{i}]: a={values[0]:.6f} b={values[1]:.6f} c={values[2]:.6f} d={values[3]:.6f}')
