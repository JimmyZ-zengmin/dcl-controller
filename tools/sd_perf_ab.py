#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sd_perf_ab.py — SD 落盘吞吐 A/B 实测 (卡必须插在板子上)

原理: 固件在复位前从 SRAM 配置字 `0x24002000` 读一次档位 (读后清零):
    [0] 数据期 CLKDIV  (0 ⇒ 默认 2)
    [1] 1 = 发 CMD6 把卡切到 High Speed (50MHz 前提)
    [2] 1 = 只测写 (跳过回读校验 —— 测吞吐必须置)
耗时用**拍计数**(100us/拍)量, 由 main 计时后 sd_set_perf() 回填诊断区。
 ⇒ 换档位不需要重新编译烧录, 只改 SRAM 配置字。

诊断区 `0x24030000` (字偏移):
    [14] 写成块数   [26] 回读校验(0=全对)   [30] 数据期 CLKCR   [31] CMD6 的 R1
    [32] 用的 CLKDIV  [33] 落盘耗时(拍)  [34] 写成块数  [35] 累计块数

吞吐换算: MB/s = 块数 × 512B / (拍数 × 100us) / 1e6 = 块数 × 5.12 / 拍数
"""
import subprocess
import sys

TARGET = "stm32h723xx"
CFG = 0x24002000
DIAG = 0x24030000

VARIANTS = [
    ("1-bit @25MHz 默认速  (基线)", 2, 0),
    ("1-bit @25MHz HighSpeed",      2, 1),
    ("1-bit @50MHz HighSpeed",      1, 1),
    ("1-bit @50MHz 默认速",          1, 0),
]


def pyocd(args):
    cmd = ["pyocd", "cmd", "-t", TARGET, "-O", "connect_mode=halt"] + args
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    return p.stdout + p.stderr


def read_diag(idx):
    out = pyocd(['-c', 'read32 0x%08X' % (DIAG + idx * 4)])
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("2403"):
            parts = line.split(":")
            if len(parts) >= 2:
                return int(parts[1].split()[0], 16)
    return None


def run_variant(name, cdiv, hs):
    pyocd(['-c', 'write32 0x%08X %d' % (CFG + 0, cdiv),
           '-c', 'write32 0x%08X %d' % (CFG + 4, hs),
           '-c', 'write32 0x%08X 1' % (CFG + 8)])
    pyocd(['-c', 'reset', '-c', 'sleep 9000'])
    r = {i: read_diag(i) for i in (0, 14, 26, 30, 31, 32, 33, 34)}
    blk, tck = r[34] or 0, r[33] or 0
    mbs = (blk * 5.12 / tck) if tck else 0.0
    print("  %-26s CLKCR=0x%04X CLKDIV=%s CMD6_R1=0x%05X  写%3d块 耗时%5d拍(%5.0fms)  **%.2f MB/s**%s"
          % (name, r[30] or 0, r[32], r[31] or 0, blk, tck, tck * 0.1, mbs,
             "" if blk == 256 else "  ⚠未写完(stage=%s)" % r[0]))
    return mbs, blk, tck


def main():
    print("=== SD 落盘吞吐 A/B (每档: 预写配置字 → 复位 → 读回耗时) ===")
    res = []
    for name, cdiv, hs in VARIANTS:
        res.append((name,) + run_variant(name, cdiv, hs))
    print("\n=== 汇总 ===")
    need = 2.56
    for name, mbs, blk, tck in res:
        flag = "✅ 够" if (mbs >= need and blk == 256) else ("❌ 不够" if blk == 256 else "❌ 未写完")
        print("  %-26s %6.2f MB/s   (需求 %.2f)  %s" % (name, mbs, need, flag))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
