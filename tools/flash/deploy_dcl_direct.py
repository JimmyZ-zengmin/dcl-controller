#!/usr/bin/env python3
"""
DCL compile → direct pyocd write → engine start → verify.

Strategy:
  1. Compile .cl → route_blob + param_blob + n_routes
  2. reset_and_halt chip
  3. Write route_blob → ROUTE_TABLE @ 0x20001700
     Write param_blob → PARAM_TABLE @ 0x20005700
     Write n_routes    → N_ROUTES    @ 0x200000F0
     Write DEPLOYED_MAGIC → SCRATCH[2] (skips firmware hard-coded routes)
  4. Start TIM1 (UIE + CEN)
  5. resume + verify via SAMPLES / PERIOD / ACTUATOR
"""
import os, sys, time, struct, argparse

COMPILER_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'ide', 'compiler')
sys.path.insert(0, COMPILER_DIR)
from dcl_compiler import DCLCompiler

ROUTE_TABLE  = 0x20001700
PARAM_TABLE  = 0x20005700
N_ROUTES     = 0x200000F0
SAMPLES      = 0x20000010
ACT_BASE     = 0x20000200
WIRE_BASE    = 0x20000300
TIM1_CR1     = 0x40010000
TIM1_DIER    = 0x4001000C
PERIOD_MIN   = 0x20000008
PERIOD_MAX   = 0x2000000C
EXEC_MIN     = 0x20000000
EXEC_MAX     = 0x20000004
SCRATCH2     = 0x20001000   # align to firmware: SCRATCH = DTCM+F8, [2] = +8
DEPLOYED_MAGIC = 0xDEADBEEF


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
        t.reset_and_halt()

        # 清表
        t.write_memory_block8(ROUTE_TABLE, bytes(1024 * 16))
        t.write_memory_block8(PARAM_TABLE, bytes(512 * 16))
        t.write_memory_block8(N_ROUTES, struct.pack('<I', 0))
        t.write_memory_block8(ACT_BASE, b'\x00' * (64 * 4))

        # 写路由 + 参数 + N_ROUTES
        t.write_memory_block8(ROUTE_TABLE, route_blob)
        t.write_memory_block8(PARAM_TABLE, param_blob)
        t.write_memory_block8(N_ROUTES, struct.pack('<I', n_routes))

        # DEPLOYED_MAGIC — 让固件跳过 hard-coded 路由初始化
        t.write32(SCRATCH2, DEPLOYED_MAGIC)

        # 启动 TIM1 引擎
        t.write32(TIM1_DIER, (1 << 0) | (1 << 9))   # UIE + UDE
        t.write32(TIM1_CR1, 1)                       # CEN=1
        t.resume()

        # 验证
        time.sleep(0.5)
        s = t.read32(SAMPLES)
        n = t.read32(N_ROUTES)
        pmin = t.read32(PERIOD_MIN)
        pmax = t.read32(PERIOD_MAX)
        emin = t.read32(EXEC_MIN)
        emax = t.read32(EXEC_MAX)

        print(f"\nSAMPLES     = {s}")
        print(f"N_ROUTES    = {n} (wrote {n_routes})")
        print(f"PERIOD_MIN  = {pmin} cyc ({pmin / 136:.1f} us @136MHz)")
        print(f"PERIOD_MAX  = {pmax} cyc ({pmax / 136:.1f} us)")
        print(f"EXEC_MIN    = {emin} cyc ({emin / 136:.1f} us)")
        print(f"EXEC_MAX    = {emax} cyc ({emax / 136:.1f} us)")

        if s > 1000:
            print(f"[PASS] ISR engine running")
        elif s > 0:
            print(f"[WARN] SAMPLES={s}: ran briefly then stopped")
        else:
            print(f"[FAIL] SAMPLES=0: engine not running")

        if 0 < pmax < 20000:
            print(f"[PASS] 100us period jitter: {pmax - pmin} cyc = {(pmax - pmin) / 136:.2f} us")

        # ACTUATOR_STATUS[32..63]
        act = struct.unpack('<32f', bytes(t.read_memory_block8(ACT_BASE + 32 * 4, 32 * 4)))
        nonzero = [(i, v) for i, v in enumerate(act) if abs(v) > 0.01]
        print(f"\nACT[32..63] nonzero: {len(nonzero)}/32")

        # key wires
        print("\nKey wires:")
        for name in ['level_f', 'temp_f', 'level_ctrl', 'temp_ctrl', 'fault', 'filling']:
            idx = wires.get(name)
            if idx is not None:
                val = struct.unpack('<f', bytes(t.read_memory_block8(WIRE_BASE + idx * 4, 4)))[0]
                print(f"  {name:20s}[{idx:3d}] = {val:.4f}")

        if args.monitor > 0:
            print(f"\n[MONITOR {args.monitor}s]")
            tb = time.time()
            last = s
            step = 0
            while time.time() < tb + args.monitor:
                s = t.read32(SAMPLES)
                per = t.read32(PERIOD_MAX)
                if step % 10 == 0:
                    print(f"  t={time.time() - tb:5.2f}s  SAMPLES={s:8d}  "
                          f"rate={(s - last) / 0.1:.0f}/s  PERIOD_MAX={per}")
                    last = s
                step += 1
                time.sleep(0.1)


if __name__ == "__main__":
    main()
