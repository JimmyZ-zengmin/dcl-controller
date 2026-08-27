#!/usr/bin/env python3
"""
DCL 编译 → 部署 → 运行 (不停留读 → 用 ring buffer)

正确流程:
  1. 编译 .dcl → 二进制
  2. 通过 pyocd SWD 不停留写入 ROUTE_TABLE + PARAM_TABLE
     (在 while(1) 循环中写入,IWDG 每 100μs 自动 feed,不会被 reset)
  3. 引擎跑 N 秒
  4. halt → 读 ring buffer → 统计抖动

关键:不停留写入,避免 IWDG reset 芯片
"""
import os, sys, time, struct, numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'ide', 'compiler'))
from dcl_compiler import DCLCompiler
from pyocd.core.helpers import ConnectHelper

BASE = os.path.dirname(__file__)
sys.path.insert(0, BASE)

# 地址
ROUTE_TABLE  = 0x20001700
PARAM_TABLE  = 0x20005700
N_ROUTES     = 0x200000F0
SAMPLES      = 0x20000010
ACT_BASE     = 0x20000200
REC_ENABLE   = 0x20000040
REC_IDX      = 0x20000044
REC_BUF      = 0x2000E000
REC_BUF_SIZE = 8192
GPIOE_BASE   = 0x58021000
GPIOE_ODR    = 0x58021014

DWT_HZ = 240_000_000  # TRACECLK 实测


def compile_dcl(dcl_path):
    src = open(dcl_path, encoding='utf-8').read()
    c = DCLCompiler(); c.parse(src); c.topological_sort()
    bin = c.generate_binary()
    n_routes = bin[0] | (bin[1] << 8)
    n_params = bin[2] | (bin[3] << 8)
    off = 4
    rb = bin[off:off + n_routes * 16]; off += n_routes * 16
    pb = bin[off:off + n_params * 16]
    return n_routes, rb, pb, c.wire_index


def read_ring_buffer(t, duration=2.0):
    """运行 duration 秒后读 ring buffer (不停留)"""
    # 在写入 routes 前启用记录
    t.write32(REC_ENABLE, 0)    # 清
    t.write32(REC_IDX, 0)       # 清
    t.write32(REC_ENABLE, 1)    # 启用
    time.sleep(duration)
    # 读 ring buffer (不停留,引擎继续跑)
    idx = t.read32(REC_IDX)
    if idx < 2:
        return None
    raw = t.read_memory_block8(REC_BUF, min(idx, REC_BUF_SIZE) * 4)
    return np.array(struct.unpack(f'<{len(raw)//4}I', bytes(raw)), dtype=np.uint64)


def deploy_no_halt(t, t1, t2, n, t_record, tbuf, wire_index):
    """在引擎运行中不间断写入 routes"""
    # 清旧表
    t.write_memory_block8(ROUTE_TABLE, bytes(1024 * 16))
    t.write_memory_block8(PARAM_TABLE, bytes(52 * 16))
    t.write_memory_block8(ROUTE_TABLE, bytes(t1))
    t.write_memory_block8(PARAM_TABLE, bytes(t2))
    t.write32(N_ROUTES, n)


def main():
    import argparse
    arg = argparse.ArgumentParser()
    arg.add_argument('--dcl', default='ide/compiler/samples/tank_control.dcl')
    arg.add_argument('--duration', type=float, default=3.0)
    args = arg.parse_args()

    n, rb, pb, wires = compile_dcl(args.dcl)
    print(f'[OK] {n} routes')

    with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
        board = session.target
        # 部署 routes,引擎正常跑
        deploy_no_halt(board, rb, pb, n, args.duration, None, wires)
        time.sleep(0.1)
        s = board.read32(SAMPLES)
        print(f'  已部署, SAMPLES={s}')
        # 记录
        dt_ns = []
        t0 = time.time()
        n_log = 0
        while time.time() - t0 < args.duration and n_log < 200:
            time.sleep(0.1)
            s2 = board.read32(SAMPLES)
            if s2 > s:
                rate = (s2 - s) / 0.1
                # print(f'  SAMPLES={s2}  rate={rate:.0f}/s')
                s = s2
        # 最后读 buffer
        raw = board.read_memory_block8(REC_IDX, 4)
        idx = struct.unpack('<I', bytes(raw))[0]
        if idx >= 2:
            raw = board.read_memory_block8(REC_BUF, min(idx, REC_BUF_SIZE) * 4)
            ts = np.array(struct.unpack(f'<{len(raw)//4}I', bytes(raw)), dtype=np.uint64)
            dt = np.diff(ts.astype(np.int64))
            # 过滤异常
            dt = dt[(dt > 0) & (dt < 100000)]
            us_ns = dt / DWT_HZ * 1e9
            print(f'=== Ring buffer n={len(dt)} ===')
            print(f'周期 us   = {np.mean(dt) / DWT_HZ * 1e6:.3f}')
            print(f'周期 ns   = {np.mean(us_ns):.0f}')
            print(f'sigma ns  = {np.std(us_ns):.0f}')
            print(f'3sigma ns = {3 * np.std(us_ns):.0f}')


if __name__ == '__main__':
    main()
