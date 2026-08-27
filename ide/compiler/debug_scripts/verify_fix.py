#!/usr/bin/env python3
"""验证编译器修复：NOT 运算符 + 二进制格式"""
import sys, os, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dcl_compiler import DCLCompiler

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

c = DCLCompiler()
c.parse(test_dcl)
c.topological_sort()
c.validate_resources()

print("=" * 60)
print("ROUTES:")
for i, r in enumerate(c.routes):
    op_name = c._op_name(r['op'])
    print(f"  R{i}: {op_name:8s} src_type={r['src_type']} src={r['src_index']} "
          f"dst={r['dst_channel']} pi={r['param_idx']}")

print(f"\nPARAMS ({c.next_param}):")
for i, p in enumerate(c.params):
    print(f"  P{i}: a={p[0]:.4f} b={p[1]:.4f} c={p[2]:.4f} d={p[3]:.4f}")

# 检查 NOT route 是否存在
has_not = any(r['op'] == 0x11 for r in c.routes)
print(f"\n✓ NOT route 存在: {has_not}")

# 生成二进制并验证
binary = c.generate_binary()

# 解析二进制
n_routes = struct.unpack('<I', binary[0:4])[0]
n_params = struct.unpack('<I', binary[4:8])[0]
print(f"\n二进制: n_routes={n_routes}, n_params={n_params}, total={len(binary)} bytes")
print(f"  路由数据偏移: 12 ~ {12 + n_routes*16} ({n_routes*16} bytes)")
print(f"  参数数据偏移: {12 + n_routes*16} ~ {12 + n_routes*16 + n_params*16} ({n_params*16} bytes)")

# 验证 param 区域不是全零
param_offset = 12 + n_routes * 16
param_size = n_params * 16
param_data = binary[param_offset:param_offset + param_size]
param_nonzero = sum(1 for b in param_data if b != 0)
print(f"\n参数区域非零字节: {param_nonzero}/{len(param_data)}")

if param_nonzero > 0:
    print("✓ 参数区域包含有效数据")
    # 解码前几个参数
    for i in range(min(n_params, 4)):
        off = param_offset + i * 16
        vals = struct.unpack('<ffff', binary[off:off+16])
        print(f"  P{i}: a={vals[0]:.4f} b={vals[1]:.4f} c={vals[2]:.4f} d={vals[3]:.4f}")
else:
    print("✗ 参数区域全为零 — 格式仍然错误!")

# 验证 NOT route 在二进制中
print("\n" + "=" * 60)
if has_not and param_nonzero > 0:
    print("✅ 两项修复验证通过")
else:
    print("❌ 修复未通过")
