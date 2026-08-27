#!/usr/bin/env python3
"""
DCL 部署 v3 — 不停顿, 极小写窗口, 异步观察

思路: 不停留, 写一次(快!), 立刻观察 SAMPLES 5秒看是否在涨。
"""
import os, sys, time, struct, argparse

COMPILER_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'ide', 'compiler')
sys.path.insert(0, COMPILER_DIR)
from dcl_compiler import DCLCompiler

ROUTE_TABLE = 0x20001700
PARAM_TABLE = 0x20005700
N_ROUTES    = 0x200000F0
SAMPLES     = 0x20000010
ACT_BASE    = 0x20000200
WIRE_BASE   = 0x20000300
PERIOD_MAX  = 0x2000000C


def compile_dcl(path):
    with open(path, encoding='utf-8') as f:
        src = f.read()
    c = DCLCompiler()
    c.parse(src)
    c.topological_sort()
    bin = c.generate_binary()
    n_routes = c.routes.__len__()
    n_params = c.params.__len__()
    # 直接构建 route_blob 从 compiler 内部
    rb = bytearray()
    for r in c.routes:
        rb += struct.pack('<BBBBBBBHHH',
            r['src_type'], r['src_index'], r['dst_type'], r['dst_channel'],
            r['op'] & 0xFF, r['flags'],
            r['param_idx'], r['state_offset'],
            r['actuator_idx'], r['wire2_idx'])
    pb = bytearray()
    for a, b, c2, d in c.params:
        pb += struct.pack('<ffff', a, b, c2, d)
    print(f"[OK] {n_routes} routes, {n_params} params")
    return n_routes, bytes(rb), bytes(pb), c.wire_index


def main():
    p = argparse.ArgumentParser()
    p.add_argument('dcl_file')
    p.add_argument('--monitor', type=float, default=5)
    args = p.parse_args()

    n_routes, route_blob, param_blob, wires = compile_dcl(args.dcl_file)

    from pyocd.core.helpers import ConnectHelper
    with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
        t = session.target

        # 先记基线
        s0 = t.read32(SAMPLES)
        print(f"[BASE] SAMPLES={s0}")

        # 写一张全新的表(停掉旧引擎先)
        # 停止 TIM1 (不停顿! 直接写)
        t.write32(0x40010000, 0)   # CR1.CEN=0
        time.sleep(0.001)

        # 整体清表(一次性, 不可中断)
        t.write_memory_block8(ROUTE_TABLE, bytes(26 * 16))   # 只清用到的
        t.write_memory_block8(PARAM_TABLE, bytes(20 * 16))

        # 写 DCL 路由 + 参数
        t.write_memory_block8(ROUTE_TABLE, route_blob)
        t.write_memory_block8(PARAM_TABLE, param_blob)

        # 写 N_ROUTES
        t.write32(N_ROUTES, n_routes)

        # 启 TIM1
        t.write32(0x4001000C, (1 << 0) | (1 << 9))   # UIE + UDE
        t.write32(0x40010000, 1)                     # CEN=1

        print(f"[DEPLOY] DCL routes active, engine restarted")

        # 每 500ms 观察一次
        for i in range(int(args.monitor * 2)):
            time.sleep(0.5)
            s1 = t.read32(SAMPLES)
            n1 = t.read32(N_ROUTES)
            per = t.read32(PERIOD_MAX)
            act = struct.unpack('<32f', bytes(t.read_memory_block8(ACT_BASE + 32 * 4, 32 * 4)))
            nonzero = [(j, v) for j, v in enumerate(act) if abs(v) > 0.01]
            delta = s1 - s0
            rate = delta / 0.5
            print(f"[{i * 0.5:5.1f}s] SAMPLES={s1:8d} ({rate:6.0f}/s) per={per}cyc "
                  f"ACT_nz={len(nonzero):2d} routes={n1}")
            s0 = s1

        # 最终 wires
        print("\nFinal wires:")
        for name in ['level_f', 'temp_f', 'level_ctrl', 'temp_ctrl', 'fault', 'filling']:
            idx = wires.get(name)
            if idx is not None:
                val = struct.unpack('<f', bytes(t.read_memory_block8(WIRE_BASE + idx * 4, 4)))[0]
                print(f"  {name:20s}[{idx:3d}] = {val:.4f}")


if __name__ == "__main__":
    main()
