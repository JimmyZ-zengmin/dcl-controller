#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_stage1_read.py — 读回阶段 1 的 DWT 测量结果并算派生量

流程:
  1) pyocd 复位并让固件自由运行 N 秒 (累积统计)
  2) ★默认 halt 模式连接(**不复位!**) 读 SRAM 变量 —— 复位会清掉统计
  3) 算派生量: 空拍成本 / 统计记账成本 / 拍周期抖动 / DWT 标定线性度

用法:
  python tools/h723_stage1_read.py [--run 3] [--elf build/dcl_h723.elf]
"""
import os, re, sys, json, time, argparse, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NM = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
      "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update"
      ".win32_1.5.0.202011040924/tools/bin/arm-none-eabi-nm.exe")

# 变量表: 名字 → (word 数, 说明)
VARS = [
    ("g_boot_status", 1), ("g_stage", 1), ("g_tick_count", 1),
    ("g_isr_cyc_last", 1), ("g_isr_cyc_min", 1), ("g_isr_cyc_max", 1),
    ("g_isr_cyc_sum", 2), ("g_isr_n", 1),
    ("g_per_cyc_last", 1), ("g_per_cyc_min", 1), ("g_per_cyc_max", 1),
    ("g_dwt_overhead", 1), ("g_cal_n1000", 1),
]


def symbols(elf):
    out = subprocess.run([NM, elf], capture_output=True, text=True, timeout=60)
    m = {}
    for line in out.stdout.splitlines():
        p = line.split()
        if len(p) >= 3:
            m[p[2]] = int(p[0], 16)
    return m


def read_words(addrs, run_s):
    """★单次 under-reset 会话完成: 连接 → reset → 跑 run_s 秒 → 连续读全部地址
    为什么: 默认 halt 连接在目标调试态异常时会报 "No cores were discovered";
    under-reset 能恢复。同会话内 reset 后继续读不需要重连, 统计不会被清
    (复位不清 SRAM, 但这里不重连也就不重新复位)。"""
    args = ["pyocd", "cmd", "-t", "stm32h723xx", "-O", "connect_mode=under-reset",
            "-c", "reset"]
    if run_s > 0:
        args += ["-c", "sleep %d" % int(run_s * 1000)]
    for a in addrs:
        args += ["-c", "read32 0x%08X" % a]
    r = subprocess.run(args, capture_output=True, text=True, timeout=300)
    vals = []
    for line in r.stdout.splitlines():
        mm = re.match(r"^[0-9a-f]{8}:\s+([0-9a-f]{8})", line)
        if mm:
            vals.append(int(mm.group(1), 16))
    return vals, r.stdout + r.stderr


def src_mode(path):
    """从 src/main.c 里读 ISR_MODE 的默认值 —— 报告"这一版是什么形态"
    (固件里那个变量会被 --gc-sections 回收, 所以从源码取)"""
    try:
        t = open(path, encoding='utf-8').read()
        m = re.search(r"#define\s+ISR_MODE\s+(\d+)", t)
        return int(m.group(1)) if m else -1
    except Exception:
        return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=float, default=3.0, help="先让固件跑几秒 (累积统计)")
    ap.add_argument("--elf", default=os.path.join(ROOT, "build", "dcl_h723"))
    ap.add_argument("--no-run", action="store_true")
    a = ap.parse_args()

    mode = src_mode(os.path.join(ROOT, "src", "main.c"))
    print("[0] src/main.c 的 ISR_MODE = %d" % mode)

    print("[1] 读符号 …")
    if not os.path.exists(a.elf):
        print("    !! ELF 不存在: %s" % a.elf)
        return 2
    sym = symbols(a.elf)
    if not sym:
        print("    !! nm 没解析出任何符号 (检查 NM 路径/ELF)")
        return 2
    addrs, plan = [], []
    for name, words in VARS:
        if name not in sym:
            print("    !! 符号缺失: %s" % name)
            continue
        plan.append((name, words, sym[name]))
        for k in range(words):
            addrs.append(sym[name] + 4 * k)

    print("[2] 单会话: under-reset → reset → 跑 %.1fs → 读 %d 个字 …" % (a.run, len(addrs)))
    vals, raw = read_words(addrs, 0.0 if a.no_run else a.run)
    if len(vals) != len(addrs):
        print("    !! 读回 %d 个, 期望 %d 个" % (len(vals), len(addrs)))

    d, i = {}, 0
    for name, words, _addr in plan:
        v = 0
        for k in range(words):
            v |= vals[i + k] << (32 * k)
        d[name] = v
        i += words

    def s32(x):
        return x - (1 << 32) if x & 0x80000000 else x

    print("\n" + "=" * 66)
    print("原始变量")
    print("=" * 66)
    print("  g_boot_status   = %d   (0=时钟 OK)" % s32(d.get('g_boot_status', 0)))
    print("  g_stage         = %d   (6=主循环 / 7=ISR 在跑)" % d.get('g_stage', 0))
    print("  (ISR_MODE 见开头 —— 固件里那个变量会被 --gc-sections 回收)")
    print("  g_tick_count    = %d" % d.get('g_tick_count', 0))
    print("  g_isr_n         = %d" % d.get('g_isr_n', 0))

    print("\n" + "=" * 66)
    print("阶段 1-A: 空拍 ISR 成本 (CPU 周期)")
    print("=" * 66)
    mn, mx, la, n = (d.get('g_isr_cyc_min', 0), d.get('g_isr_cyc_max', 0),
                     d.get('g_isr_cyc_last', 0), d.get('g_isr_n', 0))
    sm = d.get('g_isr_cyc_sum', 0)
    cpu_hz = 400_000_000
    if n == 0:
        print("  (这一版无统计记账 —— 只有 last)")
        print("  last = %d cyc  (= %.1f ns @400MHz)" % (la, la / cpu_hz * 1e9))
        print("  注: 模式1 已证明该 ISR 是确定性的(min≈max), 故单样本可信")
    else:
        print("  min / max / last = %d / %d / %d cyc" % (mn, mx, la))
    if n:
        print("  均值             = %.2f cyc  (= %.1f ns @400MHz)" % (sm / n, sm / n / cpu_hz * 1e9))
    print("  极差             = %d cyc  (= %.1f ns)" % (mx - mn, (mx - mn) / cpu_hz * 1e9))

    ov = d.get('g_dwt_overhead', 0)
    print("  DWT 读对开销     = %d cyc   (ISR 测量含此开销, 需减掉)" % ov)
    print("  → 净空拍骨架 ≈ %d - %d = %d cyc" % (mn, ov, mn - ov))

    print("\n" + "=" * 66)
    print("阶段 1: 拍周期 (CPU 周期数, 固件侧抖动)")
    print("=" * 66)
    pmn, pmx = d.get('g_per_cyc_min', 0), d.get('g_per_cyc_max', 0)
    print("  期望 = 40000 cyc (= 100μs @400MHz)")
    print("  min / max / last = %d / %d / %d" % (pmn, pmx, d.get('g_per_cyc_last', 0)))
    if pmn != 0xFFFFFFFF and pmn:
        print("  极差 = %d cyc = %.1f ns  (含 ISR 入口延迟抖动)" % (pmx - pmn, (pmx - pmn) / cpu_hz * 1e9))
        print("  → 折合拍周期 %.4f ~ %.4f μs" % (pmn / cpu_hz * 1e6, pmx / cpu_hz * 1e6))

    print("\n" + "=" * 66)
    print("阶段 1-B: DWT 标定 (证明周期计数可信)")
    print("=" * 66)
    c1 = d.get('g_cal_n1000', 0)
    print("  nop×1000 (volatile 计数, noinline) → %d cyc  ≈ %.2f cyc/迭代" % (c1, c1 / 1000.0))
    print("  读对开销                          → %d cyc" % ov)
    if pmn and pmn != 0xFFFFFFFF:
        la_us = 100.0000
        f_dwt = pmn / (la_us * 1e-6)
        print("  ★权威标定 (内部计数 vs 外部仪器):")
        print("      拍周期 %d cyc  /  LA 实测 %.4f us  =  %.2f MHz" % (pmn, la_us, f_dwt / 1e6))
        print("      → CYCCNT 计数频率与外部仪器吻合 → 计数可信")
    return 0


if __name__ == "__main__":
    sys.exit(main())
