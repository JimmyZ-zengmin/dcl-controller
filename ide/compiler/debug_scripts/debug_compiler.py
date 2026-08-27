#!/usr/bin/env python3
"""Debug: 检查编译器参数生成"""
import sys
sys.path.insert(0, r'D:\STM\work\dcl-controller\ide\compiler')
from core.dcl_compiler import DCLCompiler

with open(r'D:\STM\work\dcl-controller\ide\compiler\test_logic.dcl', 'r') as f:
    source = f.read()

compiler = DCLCompiler()
compiler.parse(source)
compiler.topological_sort()

print("=== ROUTES ===")
for i, r in enumerate(compiler.routes):
    op_name = compiler._op_name(r['op'])
    print(f"  R{i}: {op_name} src:{r['src_type']}:{r['src_index']} dst:{r['dst_channel']} pi={r['param_idx']} fl={r['flags']}")

print("\n=== PARAMS ===")
for i, p in enumerate(compiler.params):
    print(f"  P{i}: a={p[0]:.6f} b={p[1]:.6f} c={p[2]:.6f} d={p[3]:.6f}")

print("\n=== WIRES ===")
for name, idx in sorted(compiler.wire_index.items(), key=lambda x: x[1]):
    print(f"  wire[{idx}] = {name}")

# 生成二进制并检查
print("\n=== GENERATE BINARY ===")
binary = compiler.generate_binary()
n_routes = compiler.routes.__len__()
n_params = len(compiler.params)

# 检查 param_data
param_offset = 12 + n_routes * 16
param_data = binary[param_offset:param_offset + n_params * 16]
for i in range(n_params):
    p = param_data[i*16:i*16+16]
    import struct
    values = struct.unpack('<ffff', p)
    print(f"  Param[{i}] in binary: a={values[0]:.6f} b={values[1]:.6f} c={values[2]:.6f} d={values[3]:.6f}")
