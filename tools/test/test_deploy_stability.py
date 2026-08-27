#!/usr/bin/env python3
"""
任务8: 多次部署稳定性测试 — 10 次连续部署无失败

验证目标:
  - 多次部署 v2.0 格式二进制到 DTCM
  - 每次部署后验证 N_ROUTES / PROGRAM_MAGIC 正确
  - 验证路由表数据完整性 (读回对比)
  - 验证部署后引擎状态正确
  - 10 次全部通过判定为 PASS

用法:
  python tools/test/test_deploy_stability.py

依赖:
  - pyOCD (连接调试器)
  - STM32H723 开发板 + JLink/STLink
  - 固件已烧录 (firmware/h723-core0)
"""

import sys
import os
import struct
import time

# 添加编译器路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', '..', 'ide', 'compiler'))

from dcl_compiler import DCLCompiler
from pyocd.core.helpers import ConnectHelper


# ── 地址常量 (与 memory_map.h 一致) ──
N_ROUTES_ADDR   = 0x20000040
N_PARAMS_ADDR   = 0x20000044
N_STATES_ADDR   = 0x20000048
PROG_MAGIC_ADDR = 0x2000004C
ROUTE_TABLE_BASE = 0x20001710
PARAM_TABLE_BASE = 0x20005710
STATE_TABLE_BASE = 0x20007710

EXPECTED_MAGIC = 0x50523047  # "PR0G"

# ── 测试 DCL 程序 ──
TEST_PROGRAMS = {
    "简单逻辑 (8 routes)": """
SENSOR a FROM ADC1_CH0 SCALE 1.0 0.0
SENSOR b FROM ADC1_CH1 SCALE 1.0 0.0
LOGIC result = a AND b
OUTPUT result TO GPIO_PE0
""",
    "PID控制 (12 routes)": """
SENSOR temp_raw FROM ADC1_CH0 SCALE 0.1 0.0
FILTER temp FROM temp_raw LOWPASS a=0.1
PID heater FROM temp SP=75 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
OUTPUT heater TO TIM1_CH1
""",
    "定时器+计数器 (15 routes)": """
SENSOR pulse FROM ADC1_CH0
TIMER t1: IN=pulse, PT=3s → Q=timed_out
COUNTER c1: CU=timed_out, PV=100 → Q=batch_done, CV=count
LOGIC done = batch_done
OUTPUT done TO GPIO_PE0
""",
    "反应釜控制 (reactor_control.dcl)": None,  # 从文件加载
}


def load_source(dcl_path: str = None, inline_source: str = None) -> str:
    """加载 DCL 源码"""
    if inline_source is not None:
        return inline_source
    if dcl_path:
        for enc in ['utf-8-sig', 'utf-8', 'gbk', 'latin-1']:
            try:
                with open(dcl_path, 'r', encoding=enc) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
    raise RuntimeError("无法加载源码")


def compile_source(source: str):
    """编译 DCL 源码, 返回 (binary, stats)"""
    compiler = DCLCompiler()
    compiler.parse(source)
    compiler.topological_sort()
    compiler.validate_resources()
    binary = compiler.generate_binary()

    n_routes = struct.unpack_from('<H', binary, 8)[0]
    n_params = struct.unpack_from('<H', binary, 10)[0]
    n_states = struct.unpack_from('<H', binary, 12)[0]

    stats = {
        "n_routes": n_routes,
        "n_params": n_params,
        "n_states": n_states,
        "binary_size": len(binary),
        "wires": compiler.next_wire,
    }

    return binary, stats


def swd_deploy_once(session, binary: bytes, label: str, deploy_idx: int):
    """单次部署并验证, 返回 (success, details)"""
    t = session.target
    details = {}

    # 解析 header
    magic, version, n_routes, n_params, n_states, _ = struct.unpack_from(
        '<IIHHHH', binary, 0)

    if magic != EXPECTED_MAGIC:
        return False, {**details, "error": f"Bad magic: 0x{magic:08X}"}

    off = 16
    rb = binary[off:off + n_routes * 16]; off += n_routes * 16
    pb = binary[off:off + n_params * 16]; off += n_params * 16
    sb = binary[off:off + n_states * 16]

    # Step 1: 暂停引擎
    t.write32(N_ROUTES_ADDR, 0)
    time.sleep(0.001)

    # Step 2: 清零
    t.write_memory_block8(ROUTE_TABLE_BASE, bytes(1024 * 16))
    t.write_memory_block8(PARAM_TABLE_BASE, bytes(512 * 16))
    t.write_memory_block8(STATE_TABLE_BASE, bytes(256 * 16))

    # Step 3-5: 写入
    t.write_memory_block8(ROUTE_TABLE_BASE, bytes(rb))
    t.write_memory_block8(PARAM_TABLE_BASE, bytes(pb))
    if sb:
        t.write_memory_block8(STATE_TABLE_BASE, bytes(sb))

    # Step 6: 更新引擎状态
    t.write32(N_ROUTES_ADDR, n_routes)
    t.write32(N_PARAMS_ADDR, n_params)
    t.write32(N_STATES_ADDR, n_states)
    t.write32(PROG_MAGIC_ADDR, EXPECTED_MAGIC)

    # ── 验证 ──
    time.sleep(0.005)  # 等待写入完成

    # 验证 N_ROUTES
    r_check = t.read32(N_ROUTES_ADDR, 1)
    n_routes_read = r_check[0] if r_check else 0
    details["n_routes_read"] = n_routes_read
    if n_routes_read != n_routes:
        details["error"] = f"N_ROUTES={n_routes_read} != 期望{n_routes}"
        return False, details

    # 验证 PROGRAM_MAGIC
    m_check = t.read32(PROG_MAGIC_ADDR, 1)
    magic_read = m_check[0] if m_check else 0
    details["magic_read"] = hex(magic_read)
    if magic_read != EXPECTED_MAGIC:
        details["error"] = f"MAGIC=0x{magic_read:08X} != 期望0x{EXPECTED_MAGIC:08X}"
        return False, details

    # 验证路由表数据完整性 (读回前几条对比)
    if n_routes > 0 and n_routes <= 1024:
        check_bytes = min(n_routes * 16, 256)  # 最多检查 256 字节
        raw_check = t.read_memory_block8(ROUTE_TABLE_BASE, check_bytes)
        expected_raw = bytes(rb[:check_bytes])
        if bytes(raw_check) != expected_raw:
            details["error"] = "路由表数据不匹配"
            return False, details

    return True, details


def main():
    print(f"╔{'═'*58}╗")
    print(f"║  任务8: 多次部署稳定性测试 — 10 次连续部署无失败  ║")
    print(f"╚{'═'*58}╝")

    # 准备测试程序
    reactor_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', '..', 'ide', 'compiler', 'reactor_control.dcl')

    programs = {}
    for name, source in TEST_PROGRAMS.items():
        if source is not None:
            programs[name] = source
        elif name == "反应釜控制 (reactor_control.dcl)" and os.path.exists(reactor_path):
            programs[name] = load_source(dcl_path=reactor_path)

    if not programs:
        print("❌ 没有可用的测试程序")
        sys.exit(1)

    print(f"\n📋 测试程序:")
    for name in programs:
        print(f"  - {name}")

    # 编译所有程序
    compiled = {}
    for name, source in programs.items():
        binary, stats = compile_source(source)
        compiled[name] = (binary, stats)
        print(f"\n  ✓ 编译 [{name}]:")
        print(f"      Routes={stats['n_routes']}, Params={stats['n_params']}, "
              f"States={stats['n_states']}, Size={stats['binary_size']}B")

    # 连接硬件
    print(f"\n{'='*60}")
    print(f"🔌 连接硬件 (pyOCD)")
    try:
        with ConnectHelper.session_with_chosen_probe(
                target_override='stm32h723xx') as session:
            print(f"  ✓ 已连接调试器")

            # 执行部署测试
            total_deploys = 10
            passed = 0
            failed = 0
            failures = []

            program_names = list(compiled.keys())

            print(f"\n{'='*60}")
            print(f"🔄 执行 {total_deploys} 次部署测试...\n")

            for i in range(1, total_deploys + 1):
                # 轮换使用不同的测试程序
                prog_idx = (i - 1) % len(program_names)
                prog_name = program_names[prog_idx]
                binary, stats = compiled[prog_name]

                print(f"  [{i:2d}/{total_deploys}] 部署: {prog_name} "
                      f"(R={stats['n_routes']}, P={stats['n_params']}, S={stats['n_states']})...",
                      end=" ", flush=True)

                success, details = swd_deploy_once(
                    session, binary, prog_name, i)

                if success:
                    print(f"✅  R={details.get('n_routes_read','?')} "
                          f"MAGIC={details.get('magic_read','?')}")
                    passed += 1
                else:
                    print(f"❌  {details.get('error', '未知错误')}")
                    failed += 1
                    failures.append((i, prog_name, details.get('error', '?')))

                time.sleep(0.01)  # 部署间隔

            # ── 结果汇总 ──
            print(f"\n{'='*60}")
            print(f"📊 测试结果汇总")
            print(f"  {'='*40}")
            print(f"  总部署次数: {total_deploys}")
            print(f"  ✅ 通过: {passed}")
            print(f"  ❌ 失败: {failed}")

            if failures:
                print(f"\n  失败详情:")
                for idx, name, err in failures:
                    print(f"    第 {idx} 次 [{name}]: {err}")

            stability_pct = (passed / total_deploys) * 100
            print(f"\n  稳定性: {stability_pct:.1f}%")
            print(f"  状态: {'✅ PASS' if stability_pct == 100 else '❌ FAIL'}")

            if passed == total_deploys:
                print(f"\n  🎉 恭喜! 全部通过!")
                sys.exit(0)
            else:
                print(f"\n  ⚠ 有 {failed} 次部署失败, 请检查硬件连接和固件状态")
                sys.exit(1)

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
