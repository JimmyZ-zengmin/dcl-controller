#!/usr/bin/env python3
"""
DCL 部署 v2 — 不停留写入 + 实时验证

问题:
  reset_and_halt → 写路由 → resume 后 startup 清 BSS, 擦掉我们的路由。
  解决:
  1. 先跑固件 (不 reset), 让 MCU 部署自己的 49 条硬编码路由
  2. MCU 跑 ISR 的过程中(不停留!), 我们写新的路由到 ROUTE_TABLE+0x400
     (偏移 49*16B = 784B), 避开硬编码路由区
  3. 同时写 N_ROUTES = 26 + 49 = 75 (或 26 如果只想跑 DCL 路由)
  等等, 如果 N_ROUTES 改了, ISR 下一次循环用新 N 值。

除非: 在 MCU 运行中直接改为 26 路由。ISR 在读 N 后用 26, 但原 49 路由的 N_ROUTES 改成 26 后, ROUTE_TABLE[0..25] 被 DCL 路由覆盖, ROUTE_TABLE[26..48] 还是旧数据。所以:
 方案A: 写全部 26 条 DCL 路由到 ROUTE_TABLE[0..25], N_ROUTES=26, 不停留
 方案B: UART DEPLOY 协议 (最干净)

方案A 可行原因: ISR 读 N_ROUTES 是 volatile 的, 写一次生效。
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


def compile_dcl(path):
    with open(path, encoding='utf-8') as f:
        src = f.read()
    c = DCLCompiler()
    c.parse(src)
    c.topological_sort()
    bin = c.generate_binary()
    n_routes = bin[0] | (bin[1] << 8)
    n_params = bin[2] | (bin[3] << 8)
    off = 4
    rb = bin[off:off + n_routes * 16]; off += n_routes * 16
    pb = bin[off:off + n_params * 16]
    print(f"[OK] {n_routes} routes, {n_params} params, {len(bin)} B")
    return n_routes, rb, pb, c.wire_index


def main():
    p = argparse.ArgumentParser()
    p.add_argument('dcl_file')
    p.add_argument('--monitor', type=float, default=0)
    args = p.parse_args()

    n_routes, route_blob, param_blob, wires = compile_dcl(args.dcl_file)

    from pyocd.core.helpers import ConnectHelper
    with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
        t = session.target

        # 不停留! MCU 在跑, 我们直接覆盖路由表
        print(f"[DEPLOY] Overwriting ROUTE_TABLE + PARAM_TABLE (no halt)...")

        # 清旧表 (ROUTE_TABLE 整体清零)
        t.write_memory_block8(ROUTE_TABLE, bytes(1024 * 16))
        t.write_memory_block8(PARAM_TABLE, bytes(512 * 16))
        time.sleep(0.05)

        # 写 DCL 路由 + 参数
        t.write_memory_block8(ROUTE_TABLE, route_blob)
        t.write_memory_block8(PARAM_TABLE, param_blob)

        # 写 N_ROUTES (关键: 下一次 ISR 循环生效)
        t.write32(N_ROUTES, n_routes)
        print(f"[DEPLOY] N_ROUTES set to {n_routes}")

        # 验证
        time.sleep(0.5)
        s = t.read32(SAMPLES)
        act = struct.unpack('<32f', bytes(t.read_memory_block8(ACT_BASE + 32 * 4, 32 * 4)))
        nonzero = sum(1 for v in act if abs(v) > 0.01)
        print(f"SAMPLES={s}  ACT[32..63]_nonzero={nonzero}/32")

        for name in ['level_f', 'temp_f', 'level_ctrl', 'temp_ctrl', 'fault', 'filling']:
            idx = wires.get(name)
            if idx is not None:
                val = struct.unpack('<f', bytes(t.read_memory_block8(WIRE_BASE + idx * 4, 4)))[0]
                print(f"  {name:20s}[{idx:3d}] = {val:.4f}")

        if args.monitor > 0:
            print(f"\n[MONITOR {args.monitor}s]")
            tb = time.time()
            while time.time() < tb + args.monitor:
                s = t.read32(SAMPLES)
                print(f"  t={time.time() - tb:5.2f}s  SAMPLES={s}")
                time.sleep(0.2)


if __name__ == "__main__":
    main()
