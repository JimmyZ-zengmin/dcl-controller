"""
核心0 H723 — 抖动数据分析工具

从 DTCM 读取 RLE 事件日志 + 抖动直方图, 分析稳态抖动特性。

用法:
  1. 运行固件 3 分钟后
  2. pyocd commander -t stm32h723xx -c "read32 0x20000000 76; read32 0x20008700 256; read32 0x20008B00 <N>; exit"
  3. 将 read32 输出粘贴到此脚本, 或从文件读取
"""

import sys
import struct
from typing import List, Tuple

# 已知常量
EXPECTED_PERIOD = 19200  # DWT cycles @ 192MHz = 100μs
DWT_RES_NS = 1_000_000_000 / 192_000_000  # 5.208ns per cycle

def parse_timing_vars(raw: str) -> dict:
    """解析 0x20000000 区域的 timing 变量"""
    # 期望格式: "20000000:  HH HH HH HH  |....|"
    data = {}
    for line in raw.strip().split('\n'):
        if not line or ':' not in line:
            continue
        addr_str, rest = line.split(':', 1)
        addr = int(addr_str.strip(), 16)
        hex_part = rest.split('|')[0].strip()
        words = [int(w, 16) for w in hex_part.split()]
        for i, w in enumerate(words):
            data[addr + i * 4] = w

    def r(off): return data.get(off, 0)

    return {
        'exec_min':    r(0x00),
        'exec_max':    r(0x04),
        'period_min':  r(0x08),
        'period_max':  r(0x0C),
        'samples':     r(0x10),
        'heartbeat':   r(0x18),
        'clock_hz':    r(0x1C),
        'timer_hz':    r(0x20),
        'exec_total':  r(0x24),
        'dev_abs_max': r(0x28),
        'dev_abs_smp': r(0x2C),
        'dev_abs_t0':  r(0x30),
        'dev_pos_max': r(0x34),
        'dev_neg_max': r(0x38),
        'period_exact':r(0x3C),
        'period_near': r(0x40),
        'period_far':  r(0x44),
    }

def parse_histogram(raw: str) -> List[int]:
    """解析 0x20008700 区域的 256-bin 直方图"""
    bins = [0] * 256
    for line in raw.strip().split('\n'):
        if not line or ':' not in line:
            continue
        addr_str, rest = line.split(':', 1)
        addr = int(addr_str.strip(), 16)
        hex_part = rest.split('|')[0].strip()
        words = [int(w, 16) for w in hex_part.split()]
        for i, w in enumerate(words):
            bin_idx = ((addr + i * 4) - 0x20008700) // 4
            if 0 <= bin_idx < 256:
                bins[bin_idx] = w
    return bins

def parse_event_log(raw: str) -> List[Tuple[int, int]]:
    """解析 0x20008B00 区域的 RLE 事件日志
    返回: [(起始样本号, 偏离值), ...] 列表
    """
    events = []
    # 先读 header
    data = {}
    for line in raw.strip().split('\n'):
        if not line or ':' not in line:
            continue
        addr_str, rest = line.split(':', 1)
        addr = int(addr_str.strip(), 16)
        hex_part = rest.split('|')[0].strip()
        words = [int(w, 16) for w in hex_part.split()]
        for i, w in enumerate(words):
            data[addr + i * 4] = w

    write_idx = data.get(0x20008B00, 0)
    overflow  = data.get(0x20008B04, 0)
    cur_dev   = data.get(0x20008B08, 0)
    cur_run   = data.get(0x20008B0C, 0)

    # 读取事件条目
    for i in range(write_idx):
        sample_idx = data.get(0x20008B10 + i * 8, 0)
        deviation  = data.get(0x20008B10 + i * 8 + 4, 0)
        # 处理有符号 int32
        if deviation > 0x7FFFFFFF:
            deviation -= 0x100000000
        events.append((sample_idx, deviation))

    # 当前 run (最后一段)
    if cur_run > 0:
        last_start = events[-1][0] + 1 if events else 11
        events.append((last_start, cur_dev if cur_dev < 0x80000000 else cur_dev - 0x100000000))

    return events

def print_header(tv: dict):
    """打印测试摘要"""
    samples = tv['samples']
    clk = tv['clock_hz']

    print("=" * 60)
    print(f"  核心0 H723 抖动分析报告")
    print(f"  SYSCLK: {clk/1e6:.0f} MHz  |  ISR 周期: 100μs  |  样本: {samples:,}")
    print("=" * 60)
    print()

    # 执行时间
    exec_avg = tv['exec_total'] / samples if samples > 0 else 0
    print(f"  ISR 执行时间:  max={tv['exec_max']} cyc ({tv['exec_max']*DWT_RES_NS:.0f}ns)")
    print(f"                  avg={exec_avg:.0f} cyc ({exec_avg*DWT_RES_NS:.0f}ns)")
    print()

def print_jitter_summary(tv: dict):
    """打印抖动摘要 (正确的定义: 抖动 = max |deviation|)"""
    dev_max = tv['dev_abs_max']
    dev_pos = tv['dev_pos_max']
    dev_neg = tv['dev_neg_max']
    samples = tv['samples']

    print(f"  ── 稳态抖动 (跳过前10个样本, {samples-10:,} 个有效样本) ──")
    print(f"  最大正向偏离: +{dev_pos} cyc (+{dev_pos*DWT_RES_NS:.1f}ns)")
    print(f"  最大负向偏离: -{dev_neg} cyc (-{dev_neg*DWT_RES_NS:.1f}ns)")
    print(f"  ★ 系统抖动 = {dev_max} cycles = {dev_max*DWT_RES_NS:.1f}ns p-p")
    if tv['dev_abs_smp'] > 0:
        print(f"    发生在样本 #{tv['dev_abs_smp']:,}")
    print()

    print(f"  零偏离命中: {tv['period_exact']:,} ({tv['period_exact']/samples*100:.2f}%)")
    print(f"  ±1 cycle:    {tv['period_near']:,}")
    print(f"  >10 cycles:  {tv['period_far']:,}")
    print()

def print_event_log(events: List[Tuple[int, int]], tv: dict):
    """打印 RLE 事件日志 — 展示稳态 vs 跳动"""
    samples = tv['samples']
    total_dev_cycles = 0

    print(f"  ── 周期偏离事件日志 (RLE, {len(events)} 个 run) ──")
    print(f"  {'样本#':>10}  {'偏离(cyc)':>10}  {'偏离(ns)':>10}  {'持续样本':>10}  说明")
    print(f"  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*20}")

    anomaly_count = 0
    for i, (start, dev) in enumerate(events):
        # 计算 run 长度
        if i + 1 < len(events):
            run_len = events[i+1][0] - start
        else:
            run_len = samples - start

        dev_ns = dev * DWT_RES_NS

        # 分类
        if dev == 0:
            kind = "✓ 稳态基线"
        elif abs(dev) <= 2:
            kind = "○ 微小波动"
        elif abs(dev) <= 10:
            kind = "● 轻微跳动"
        else:
            kind = "★ 异常跳动!"
            anomaly_count += 1

        # 只打印非零偏离或长稳态段
        if dev != 0 or run_len > samples * 0.1:  # 非零 or 超过10%总样本
            print(f"  {start:>10,}  {dev:>+10}  {dev_ns:>+10.1f}  {run_len:>10,}  {kind}")

        total_dev_cycles += abs(dev) * run_len

    # 当前 run (最后一段, 未写入日志)
    if events:
        last_dev = events[-1][1]
        if last_dev != 0:
            print(f"  (当前)      {last_dev:>+10}  {last_dev*DWT_RES_NS:>+10.1f}  {'(进行中)':>10}  当前 run")

    print()
    if anomaly_count > 0:
        print(f"  ⚠ 发现 {anomaly_count} 次异常跳动 (>10 cycles)")
    else:
        print(f"  ✅ 稳态运行, 无异常跳动")
    print()

def print_histogram(bins: List[int]):
    """打印抖动直方图分布"""
    non_zero = [(i-128, bins[i]) for i in range(256) if bins[i] > 0]
    if not non_zero:
        return

    print(f"  ── 抖动直方图 ({len(non_zero)} 个非空 bin) ──")
    max_count = max(b[1] for b in non_zero)
    bar_width = 40

    for dev, count in non_zero:
        bar_len = int(count / max_count * bar_width) if max_count > 0 else 0
        bar = '█' * bar_len
        dev_ns = dev * DWT_RES_NS
        pct = count / sum(b[1] for b in non_zero) * 100
        label = "← 精确命中" if dev == 0 else ""
        print(f"  {dev:+4d} cyc ({dev_ns:+7.1f}ns): {count:>10,} ({pct:5.1f}%) {bar} {label}")
    print()

def main():
    # 从标准输入读取数据
    print("等待粘贴 pyOCD read32 输出 (Ctrl+D 或 Ctrl+Z 结束)...", file=sys.stderr)
    raw = sys.stdin.read()

    # 分段: 按地址范围
    timing_raw = ""
    hist_raw = ""
    evlog_raw = ""

    for line in raw.split('\n'):
        if not line.strip() or ':' not in line:
            continue
        addr = int(line.split(':')[0].strip(), 16)
        if 0x20000000 <= addr < 0x20000100:
            timing_raw += line + '\n'
        elif 0x20008700 <= addr < 0x20008B00:
            hist_raw += line + '\n'
        elif 0x20008B00 <= addr < 0x2000CB00:
            evlog_raw += line + '\n'
        elif 0x20000000 <= addr < 0x20000060:
            timing_raw += line + '\n'  # catch timing vars

    tv = parse_timing_vars(timing_raw)

    if tv['samples'] == 0:
        print("错误: 未读取到有效数据。请确认固件正在运行。", file=sys.stderr)
        sys.exit(1)

    print_header(tv)
    print_jitter_summary(tv)

    try:
        events = parse_event_log(evlog_raw) if evlog_raw else []
        if events:
            print_event_log(events, tv)
    except Exception as e:
        print(f"  事件日志解析异常: {e}")

    try:
        bins = parse_histogram(hist_raw) if hist_raw else []
        if bins:
            print_histogram(bins)
    except Exception as e:
        print(f"  直方图解析异常: {e}")

if __name__ == '__main__':
    main()
