#!/usr/bin/env python3
"""
DMA 输出验证 — 多次快速读 ODR 确认 DMA 是否搬运
"""
import os, sys, time

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, '..', '..', 'ide', 'compiler'))
from dcl_compiler import DCLCompiler
from pyocd.core.helpers import ConnectHelper

ROUTE_TABLE  = 0x20001700
PARAM_TABLE  = 0x20005700
N_ROUTES     = 0x200000F0
SAMPLES      = 0x20000010
ACTUATOR_BASE = 0x20000200
GPIOE_ODR    = 0x58021014
SHADOW       = 0x200000E0


def compile_dcl(path):
    src = open(path, encoding='utf-8').read()
    c = DCLCompiler(); c.parse(src); c.topological_sort()
    bin = c.generate_binary()
    n_routes = bin[0] | (bin[1] << 8)
    n_params = bin[2] | (bin[3] << 8)
    off = 4
    rb = bin[off:off + n_routes * 16]; off += n_routes * 16
    pb = bin[off:off + n_params * 16]
    return n_routes, rb, pb


def main():
    n, rb, pb = compile_dcl(os.path.join(HERE, '..', '..', 'ide', 'compiler', 'samples', 'medium_test.dcl'))
    print(f'[OK] {n} routes')

    with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
        t = session.target

        # 1. 部署 routes
        t.reset_and_halt()
        t.write_memory_block8(ROUTE_TABLE, bytes(1024 * 16))
        t.write_memory_block8(PARAM_TABLE, bytes(512 * 16))
        t.write_memory_block8(ROUTE_TABLE, bytes(rb))
        t.write_memory_block8(PARAM_TABLE, bytes(pb))
        t.write32(N_ROUTES, n)
        t.resume()

        time.sleep(2.0)

        # 2. 一次 halt,读全状态
        t.halt()
        odr   = t.read32(GPIOE_ODR)
        shdw  = t.read32(SHADOW)
        s     = t.read32(SAMPLES)
        n     = t.read32(N_ROUTES)
        s5cr  = t.read32(0x40020488)
        s5ndtr= t.read32(0x4002048C)
        ch13  = t.read32(0x40020834)
        dier  = t.read32(0x4001000C) & 0xFFFF
        arr   = t.read32(0x4001002C) & 0xFFFF
        ccr4  = t.read32(0x40010040) & 0xFFFF
        act32 = t.read32(ACTUATOR_BASE + 32 * 4)
        t.resume()

        print(f'SAMPLES = {s}')
        print(f'N_ROUTES= {n}')
        print(f'ACT[32] = {act32}')
        print(f'SHADOW  = 0x{shdw:08X}')
        print(f'ODR     = 0x{odr:08X}')
        print(f'S5CR    = 0x{s5cr:08X} EN={s5cr&1}')
        print(f'S5NDTR  = {s5ndtr}')
        print(f'CH13CR  = 0x{ch13:02X}')
        print(f'DIER    = 0x{dier:04X} (CC4DE=bit12)')
        print(f'ARR     = {arr}')
        print(f'CCR4    = {ccr4}')

        if shdw != 0 and odr == 0:
            print(f'[!!] SHADOW={shdw:08X} 但 ODR=0 → DMA 未工作!')
        elif shdw != 0 and odr != 0:
            print(f'[OK] SHADOW={shdw:08X},ODR={odr:08X} → DMA 可能工作')

        # 3. 检查一致性
        # 如果 DMA 稳定工作,ODR 应该在各种读出中保持一致 (或在高低之间切换)
        unique_odrs = set(odr_list)
        print(f'ODR 多次读: {[f"0x{v:02X}" for v in odr_list]}')
        print(f'SAMPLES:     {s_list}')
        print(f'ODR unique values: {len(unique_odrs)}: {[f"0x{v:02X}" for v in unique_odrs]}')

        if len(unique_odrs) == 1 and 0xFFFFFFFBFF in unique_odrs:
            print(f'[!!] ODR 每次读都 = 0xFFFFFFFF → pyocd 返回值,不能信')
        elif all(v == odr_list[0] for v in odr_list) and odr_list[0] != 0xFFFFFFFF:
            print(f'[OK] ODR 稳定 = 0x{odr_list[0]:02X} (不是 0xFFFFFFFF) → DMA 可能工作')
        else:
            print(f'[CHECK] ODR 值有变化:')
            for v in sorted(unique_odrs):
                print(f'       0x{v:02X} (count={odr_list.count(v)})')


if __name__ == "__main__":
    main()
