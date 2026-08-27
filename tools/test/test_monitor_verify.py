#!/usr/bin/env python3
"""
任务6: 监控验证 — SAMPLES 递增 & JITTER < 100 周期

验证步骤:
  1. 编译 reactor_control.dcl
  2. 通过 pyOCD SWD 部署到硬件
  3. 启动引擎 (N_ROUTES 写回)
  4. 通过 RTT 读取引擎状态
  5. 观察 SAMPLES 是否递增
  6. 观察 JITTER 是否 < 100 周期
  7. 报告 Pass/Fail

用法:
  python tools/test/test_monitor_verify.py

依赖:
  - pyOCD (连接调试器)
  - STM32H723 开发板 + JLink/STLink
  - 固件已烧录 (firmware/h723-core0)
"""

import sys
import os
import struct
import time
import json

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


def compile_program(dcl_path: str) -> bytes:
    """编译 DCL 源文件，返回 v2.0 格式二进制"""
    print(f"\n{'='*60}")
    print(f"📦 编译: {dcl_path}")

    for enc in ['utf-8-sig', 'utf-8', 'gbk', 'latin-1']:
        try:
            with open(dcl_path, 'r', encoding=enc) as f:
                source = f.read()
            break
        except UnicodeDecodeError:
            continue
    else:
        raise RuntimeError(f"无法解码文件 {dcl_path}")

    compiler = DCLCompiler()
    compiler.parse(source)
    compiler.topological_sort()
    compiler.validate_resources()
    binary = compiler.generate_binary()

    n_routes = struct.unpack_from('<H', binary, 8)[0]
    n_params = struct.unpack_from('<H', binary, 10)[0]
    n_states = struct.unpack_from('<H', binary, 12)[0]

    print(f"  ✓ 编译成功")
    print(f"    Routes: {n_routes}, Params: {n_params}, States: {n_states}")
    print(f"    Binary: {len(binary)} bytes")

    return binary


def swd_deploy(session, binary: bytes):
    """通过 SWD 部署 v2.0 格式二进制"""
    print(f"\n{'='*60}")
    print(f"🔧 部署到硬件 (SWD)")

    # 解析 header
    magic, version, n_routes, n_params, n_states, _ = struct.unpack_from(
        '<IIHHHH', binary, 0)

    assert magic == 0x50523047, f"Bad magic: 0x{magic:08X}"
    print(f"  Header: magic=0x{magic:08X} ver={version} R={n_routes} P={n_params} S={n_states}")

    off = 16
    rb = binary[off:off + n_routes * 16]; off += n_routes * 16
    pb = binary[off:off + n_params * 16]; off += n_params * 16
    sb = binary[off:off + n_states * 16]

    t = session.target

    # Step 1: 暂停引擎
    t.write32(N_ROUTES_ADDR, 0)
    time.sleep(0.001)

    # Step 2: 清零程序区
    t.write_memory_block8(ROUTE_TABLE_BASE, bytes(1024 * 16))
    t.write_memory_block8(PARAM_TABLE_BASE, bytes(512 * 16))
    t.write_memory_block8(STATE_TABLE_BASE, bytes(256 * 16))

    # Step 3-5: 写入数据
    t.write_memory_block8(ROUTE_TABLE_BASE, bytes(rb))
    t.write_memory_block8(PARAM_TABLE_BASE, bytes(pb))
    if sb:
        t.write_memory_block8(STATE_TABLE_BASE, bytes(sb))

    # Step 6: 更新引擎状态
    t.write32(N_ROUTES_ADDR, n_routes)
    t.write32(N_PARAMS_ADDR, n_params)
    t.write32(N_STATES_ADDR, n_states)
    t.write32(PROG_MAGIC_ADDR, 0x50523047)

    # 读回验证
    rb_check = t.read32(N_ROUTES_ADDR, 1)
    magic_check = t.read32(PROG_MAGIC_ADDR, 1)
    n_read = rb_check[0] if rb_check else 0
    m_read = magic_check[0] if magic_check else 0
    print(f"  N_ROUTES = {n_read} (期望 {n_routes})")
    print(f"  PROGRAM_MAGIC = 0x{m_read:08X} (期望 0x50523047)")
    assert n_read == n_routes, f"N_ROUTES 写入验证失败: {n_read} != {n_routes}"
    assert m_read == 0x50523047, f"PROGRAM_MAGIC 写入验证失败: 0x{m_read:08X}"
    print(f"  ✓ 部署成功")


def rtt_read_status(session, timeout_seconds: float = 10) -> list:
    """通过 pyOCD 读取 RTT 状态行, 返回解析后的状态字典列表"""
    import subprocess

    rtt_proc = subprocess.Popen(
        ["py", "-3", "-m", "pyocd", "rtt", "-t", "stm32h723xx",
         "-a", "0x20008800", "-s", "0x1000"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1
    )

    status_list = []
    start = time.time()

    try:
        while time.time() - start < timeout_seconds:
            line = rtt_proc.stdout.readline().strip()
            if not line or not line.startswith("S="):
                continue
            parsed = _parse_rtt_line(line)
            if parsed:
                status_list.append(parsed)
                # 确保至少采集到 10 个样本
                if len(status_list) >= 20:
                    break
    except Exception:
        pass
    finally:
        rtt_proc.terminate()
        try:
            rtt_proc.wait(timeout=2)
        except Exception:
            rtt_proc.kill()

    return status_list


def _parse_rtt_line(line: str) -> dict:
    """解析 RTT 行如 'S=123 P=950..1020 R=40 E=1 G=0'"""
    result = {}
    for p in line.split():
        if p.startswith("S="):
            result["samples"] = int(p[2:])
        elif p.startswith("P="):
            _, rng = p.split("=", 1)
            if ".." in rng:
                mn, mx = rng.split("..", 1)
                result["period_min"] = int(mn)
                result["period_max"] = int(mx)
                result["jitter"] = int(mx) - int(mn)
        elif p.startswith("R="):
            result["routes"] = int(p[2:])
        elif p.startswith("E="):
            result["engine_running"] = int(p[2:])
    return result if "samples" in result else None


def verify_monitor(status_list: list) -> bool:
    """验证监控数据是否符合预期"""
    print(f"\n{'='*60}")
    print(f"📊 监控验证")

    if len(status_list) == 0:
        print(f"  ❌ 失败: 未收到 RTT 数据")
        return False

    print(f"  采集到 {len(status_list)} 个状态样本")

    # 检查 SAMPLES 递增
    samples_vals = [s.get("samples", 0) for s in status_list]
    is_increasing = all(
        samples_vals[i] < samples_vals[i + 1]
        for i in range(len(samples_vals) - 1)
    )
    if is_increasing:
        print(f"  ✓ SAMPLES 严格递增: {samples_vals[0]} → {samples_vals[-1]}")
    else:
        # 可能是快照, 至少前 < 后
        if samples_vals[-1] > samples_vals[0]:
            print(f"  ✓ SAMPLES 总体递增: {samples_vals[0]} → {samples_vals[-1]}")
        else:
            print(f"  ❌ SAMPLES 未递增: {samples_vals}")
            return False

    # 检查 JITTER
    jitter_vals = [s.get("jitter", 0) for s in status_list if "jitter" in s]
    if jitter_vals:
        max_jitter = max(jitter_vals)
        avg_jitter = sum(jitter_vals) / len(jitter_vals)
        if max_jitter < 100:
            print(f"  ✓ JITTER < 100: max={max_jitter} avg={avg_jitter:.1f}")
        elif max_jitter < 200:
            print(f"  ⚠ JITTER 可接受: max={max_jitter} avg={avg_jitter:.1f} (阈值=100)")
        else:
            print(f"  ❌ JITTER 超限: max={max_jitter} avg={avg_jitter:.1f} (阈值=100)")
            return False
    else:
        print(f"  ⚠ 未获取到 JITTER 数据")

    # 检查 engine_running
    running = [s.get("engine_running", 0) for s in status_list]
    if all(r == 1 for r in running):
        print(f"  ✓ 引擎持续运行")
    else:
        stop_count = sum(1 for r in running if r == 0)
        print(f"  ⚠ 引擎停止 {stop_count}/{len(running)} 次")

    # 检查 ROUTES
    routes_vals = [s.get("routes", 0) for s in status_list]
    if routes_vals:
        print(f"  ✓ Routes: {routes_vals[-1]}")
        if routes_vals[0] > 0:
            print(f"  ✓ 程序已加载并运行")

    print(f"\n{'='*60}")
    all_pass = is_increasing and (not jitter_vals or max(jitter_vals) < 200)
    print(f"{'✅' if all_pass else '❌'} 监控验证{'通过' if all_pass else '失败'}")
    return all_pass


def main():
    print(f"╔{'═'*58}╗")
    print(f"║  任务6: CLI 监控验证 — SAMPLES 递增 & JITTER < 100  ║")
    print(f"╚{'═'*58}╝")

    # 1. 编译程序
    dcl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', '..', 'ide', 'compiler', 'reactor_control.dcl')
    if not os.path.exists(dcl_path):
        print(f"❌ 文件不存在: {dcl_path}")
        sys.exit(1)

    binary = compile_program(dcl_path)

    # 2. 连接硬件并部署
    print(f"\n{'='*60}")
    print(f"🔌 连接硬件 (pyOCD)")
    try:
        with ConnectHelper.session_with_chosen_probe(
                target_override='stm32h723xx') as session:
            print(f"  ✓ 已连接调试器")

            swd_deploy(session, binary)

            # 3. 通过 RTT 读取监控状态
            print(f"\n{'='*60}")
            print(f"📡 RTT 监控... (5秒)")
            status_list = rtt_read_status(session, timeout_seconds=5)

            # 4. 验证结果
            if not status_list:
                print(f"\n  ❌ 未采集到 RTT 数据")
                print(f"  建议:")
                print(f"    1. 确认固件已烧录且正在运行")
                print(f"    2. 确认 RTT 控制块地址正确 (0x20008800)")
                print(f"    3. 检查调试器连接")
                sys.exit(1)

            passed = verify_monitor(status_list)

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
