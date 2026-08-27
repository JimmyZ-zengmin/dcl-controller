#!/usr/bin/env python3
"""检查编译器生成的二进制格式"""

import struct
import sys
sys.path.insert(0, '.')

from dcl_compiler import DCLCompiler, ROUTE_FMT
print(f"ROUTE_FMT size: {struct.calcsize(ROUTE_FMT)} bytes (expected 16)")

with open('test_logic.dcl') as f:
    src = f.read()

c = DCLCompiler()
c.parse(src)
c.topological_sort()
c.validate_resources()

print(f"\n编译结果: {len(c.routes)} routes, {c.next_wire} wires")

for i, r in enumerate(c.routes):
    print(f"  [{i:2d}] src_t={r['src_type']} src_i={r['src_index']:3d} "
          f"dst_t={r['dst_type']} dst_ch={r['dst_channel']:3d} "
          f"op=0x{r['op']:02x} flags={r['flags']} pi={r['param_idx']} "
          f"so={r['state_offset']} act={r.get('actuator_idx',0)} w2={r.get('wire2_idx',0)}")

# Generate binary
binary = c.generate_binary()
print(f"\nBinary: {len(binary)} bytes")
print(f"Header: routes={struct.unpack_from('<I', binary, 0)[0]}, "
      f"params={struct.unpack_from('<I', binary, 4)[0]}, "
      f"active={struct.unpack_from('<I', binary, 8)[0]}")

# Show first 5 route entries (16 bytes each)
print(f"\n前5个路由条目 (每个16字节, 从字节偏移16开始):")
for i in range(min(5, len(c.routes))):
    entry = binary[16 + i*16 : 16 + (i+1)*16]
    print(f"  [{i}] hex: {entry.hex(' ')}")
    fields = struct.unpack(ROUTE_FMT, entry)
    print(f"       解码: src_t={fields[0]} src_i={fields[1]} dst_t={fields[2]} "
              f"dst_ch={fields[3]} op={fields[4]} flags={fields[5]} "
              f"pi={fields[6]} so={fields[7]} act={fields[8]} w2={fields[9]}")
