#!/usr/bin/env python3
"""验证 OUTPUT 路由和 ACTUATOR_STATUS"""
import sys, os, struct, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dcl_compiler import DCLCompiler
from dcl_hardware import Hardware, ADDRESSES, TIM1_BASE

TEST_DCL = """
SENSOR a FROM ADC1_CH0 SCALE 1.0 0.0
SENSOR b FROM ADC1_CH1 SCALE 1.0 0.0
SENSOR c FROM ADC1_CH2 SCALE 1.0 0.0
SENSOR d FROM ADC1_CH3 SCALE 1.0 0.0
LOGIC all_high = a AND b AND c AND d
LOGIC any_high = a OR b OR c OR d
LOGIC not_a_and_b = NOT a AND b
OUTPUT all_high TO GPIO_PE0
OUTPUT any_high TO GPIO_PE1
OUTPUT not_a_and_b TO GPIO_PE2
"""

c = DCLCompiler()
c.parse(TEST_DCL)
c.topological_sort()
c.validate_resources()
binary = c.generate_binary()

n_routes = struct.unpack('<I', binary[0:4])[0]
n_params = struct.unpack('<I', binary[4:8])[0]
route_data = binary[12 : 12 + n_routes * 16]
param_data = binary[12 + n_routes*16 : 12 + n_routes*16 + n_params*16]

print(f"Routes: {n_routes}, Params: {n_params}")

# 打印所有 route 信息（从编译器获取）
print("\n编译器 Routes:")
for i, r in enumerate(c.routes):
    op_name = c._op_name(r['op'])
    print(f"  R{i:2d}: {op_name:8s} src_type={r['src_type']} src={r['src_index']:2d} "
          f"dst_type={r['dst_type']} dst={r['dst_channel']:2d} op={r['op']:2d} flags={r['flags']} "
          f"pi={r['param_idx']} ai={r.get('actuator_idx', 0)}")

# 部署
hw = Hardware()
if not hw.connect():
    print("连接失败!"); sys.exit(1)

hw.write32(TIM1_BASE + 0x0C, 0)
hw.write32(TIM1_BASE, 0)

hw.fill_block(ADDRESSES['ROUTE_TABLE'], n_routes * 16)
hw.fill_block(ADDRESSES['PARAM_TABLE'], n_params * 16)
hw.fill_block(ADDRESSES['WIRE_MAP'], 1024 * 4)
hw.fill_block(ADDRESSES['ACTUATOR_STATUS'], 64 * 4)
hw.write_block(ADDRESSES['ROUTE_TABLE'], route_data)
hw.write_block(ADDRESSES['PARAM_TABLE'], param_data)
hw.write32(ADDRESSES['ACTIVE_ROUTES'], n_routes)
hw.write32(0xE000E104, 1 << 11)

hw.write32(TIM1_BASE + 0x0C, 1)
hw.write32(TIM1_BASE, 1)

time.sleep(0.05)

# 注入 a=1,b=1,c=1,d=1 → all_high=1, any_high=1, not_a_and_b=0
def w_float(addr, val):
    hw.write32(addr, struct.unpack('I', struct.pack('f', val))[0])

def r_float(addr):
    raw = hw.read32(addr, 1)
    if raw:
        return struct.unpack('f', struct.pack('I', raw[0] & 0xFFFFFFFF))[0]
    return None

# 停止 ISR, 注入全1
hw.write32(TIM1_BASE, 0)
w_float(ADDRESSES['SENSOR_MAP'] + 0, 1.0)  # a
w_float(ADDRESSES['SENSOR_MAP'] + 4, 1.0)  # b
w_float(ADDRESSES['SENSOR_MAP'] + 8, 1.0)  # c
w_float(ADDRESSES['SENSOR_MAP'] + 12, 1.0) # d

hw.write32(TIM1_BASE + 0x10, 0x0000FFFF)
hw.write32(TIM1_BASE, 1)
time.sleep(0.002)
hw.write32(TIM1_BASE, 0)

# 读取 ACTUATOR_STATUS
print("\nACTUATOR_STATUS (word 0..5):")
act = hw.read32(ADDRESSES['ACTUATOR_STATUS'], 8)
for i, v in enumerate(act):
    fval = struct.unpack('f', struct.pack('I', v))[0]
    print(f"  act[{i}] = 0x{v:08X} = {fval:.4f}")

# 读 WIRE out_all_high (12), out_any_high (13), out_not_a_and_b (14)
print("\nOUTPUT wires:")
for name, idx in [('out_all_high', 12), ('out_any_high', 13), ('out_not_a_and_b', 14)]:
    v = r_float(ADDRESSES['WIRE_MAP'] + idx * 4)
    print(f"  wire[{idx}] ({name}) = {v}")

# 读 GPIO
GPIOE_ODR = 0x58021014
raw = hw.read32(GPIOE_ODR, 1)
if raw:
    v = raw[0]
    print(f"\nGPIOE_ODR = 0x{v:08X}")
    print(f"  PE0 (all_high)    = {(v>>0)&1} (预期: 1)")
    print(f"  PE1 (any_high)    = {(v>>1)&1} (预期: 1)")
    print(f"  PE2 (not_a_and_b) = {(v>>2)&1} (预期: 0)")

# 读取 ROUTE_TABLE 最后几个 entries
print("\nROUTE_TABLE 实际内容 (后8个):")
ROUTE = ADDRESSES['ROUTE_TABLE']
raw = hw.read32(ROUTE + 8*16, 32)  # route 8..15, 每个4 word
for i in range(8):
    base = i * 4
    b = struct.pack('<IIII', raw[base], raw[base+1], raw[base+2], raw[base+3])
    src_type, src_index, dst_type, dst_channel = b[0], b[1], b[2], b[3]
    op, flags = b[4], b[5]
    enabled = "✓" if flags & 1 else "✗"
    if src_type != 0 or flags != 0:
        op_names = {0:'DIRECT',1:'CMP',14:'SCALE',15:'AND',16:'OR',17:'NOT',30:'LIMIT'}
        op_name = op_names.get(op, f'0x{op:02X}')
        print(f"  R{8+i}: {enabled} {op_name:8s} src_type={src_type} src={src_index} "
              f"dst_type={dst_type} dst={dst_channel} op={op} flags={flags}")
    else:
        print(f"  R{8+i}: 全零")

print("\n完成.")
