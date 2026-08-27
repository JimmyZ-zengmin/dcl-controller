#!/usr/bin/env python3
"""完整部署验证：编译 → 部署 → 读取 WIRE 值"""
import sys, os, struct, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dcl_compiler import DCLCompiler
from dcl_hardware import Hardware

test_dcl = """
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

print("=" * 60)
print("STEP 1: 编译")
c = DCLCompiler()
c.parse(test_dcl)
c.topological_sort()
c.validate_resources()
binary = c.generate_binary()

n_routes = struct.unpack('<I', binary[0:4])[0]
n_params = struct.unpack('<I', binary[4:8])[0]
print(f"  路由: {n_routes}, 参数: {n_params}, 二进制大小: {len(binary)} bytes")

# 打印 wire 分配
print("\n  Wire 分配:")
for name, idx in sorted(c.wire_index.items(), key=lambda x: x[1]):
    print(f"    wire[{idx:2d}] = {name}")

# 打印 route 详情
print("\n  Route 详情:")
for i, r in enumerate(c.routes):
    op = c._op_name(r['op'])
    print(f"    R{i:2d}: {op:8s} src={r['src_index']:2d} dst={r['dst_channel']:2d} pi={r['param_idx']}")

print("\n" + "=" * 60)
print("STEP 2: 部署到硬件")

hw = Hardware()
if not hw.connect():
    print("❌ 连接失败!")
    sys.exit(1)
print("  ✓ 已连接")

ok = hw.deploy(binary)
if not ok:
    print(f"❌ 部署失败: {hw.last_error}")
    sys.exit(1)
print("  ✓ 部署成功")

print("\n" + "=" * 60)
print("STEP 3: 读取结果")
time.sleep(0.1)  # 等待 ISR 运行几轮

# 读取 sensors
sensors = hw.read_sensors(0, 4)
if sensors:
    print(f"\n  SENSORS:")
    for i, v in enumerate(sensors):
        print(f"    sensor[{i}] = {v:.6f}")

# 读取 wires
wires = hw.read_wires(0, 16)
if wires:
    print(f"\n  WIRES:")
    for i, v in enumerate(wires):
        name = ''
        for n, idx in c.wire_index.items():
            if idx == i:
                name = n
                break
        label = f" ({name})" if name else ""
        print(f"    wire[{i:2d}]{label:25s} = {v:.6f}")

# 读取 active routes
active = hw.get_active_routes()
print(f"\n  ACTIVE_ROUTES: {active}")

print("\n" + "=" * 60)
print("✅ 部署验证完成")
