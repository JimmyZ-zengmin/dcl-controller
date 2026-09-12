#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sd_batch_sweep.py — 扫"单批字节数 vs 单批卡顿/丢包"

背景: 丢包判据 = 落后量 > 环槽数; 实测卡顿全在 SD 写内部([59]≈[60]),
      且单批 64KB 时最长卡顿从 68ms 涨到 118ms ⇒ 怀疑**批越大、卡内部一次刷得越多、尾巴越长**。
      批大小可由 SD_CFG[9] 覆盖 (带魔数门), 所以不必重编译就能扫。

每档: 预写 SD_CFG[9]=批槽数 + [15]=魔数 → 复位 → 跑 N 秒 → 读诊断。
诊断 (SD_DIAG @0x24000200):
  [56] 最大待落盘  [57] 本次丢包  [58] 本次块数
  [59] 两次落盘最大间隔(拍)  [60] 落盘内部最长耗时(拍)  [61] 慢轮询(>20ms)次数
  [62] 生效的批大小(槽)
"""
import re
import subprocess
import sys
import time

CFG = 0x24000400
DIAG = 0x24000200
MAGIC = 0xF00DBEEF
TARGET = "stm32h723xx"
SECS = 12


def py(args):
    p = subprocess.run(["pyocd", "cmd", "-t", TARGET, "-O", "connect_mode=halt"] + args,
                       capture_output=True, text=True, errors="replace")
    return p.stdout + p.stderr


def rd(idx):
    out = py(["-c", "read32 0x%08X" % (DIAG + idx * 4)])
    for l in out.splitlines():
        m = re.match(r"^([0-9a-f]{8}):\s+([0-9a-f]{8})", l.strip())
        if m:
            return int(m.group(2), 16)
    return None


def run(batch_slots):
    # ★ 魔数必须用十六进制写: 十进制 4027463407 会被 pyocd 当成非法参数而静默不写
    py(["-c", "write32 0x%08X %d" % (CFG + 36, batch_slots),     # [9]
        "-c", "write32 0x%08X 0x%08X" % (CFG + 60, MAGIC)])       # [15] 魔数门
    py(["-c", "reset", "-c", "sleep %d" % (SECS * 1000), "-c", "go"])
    d = {i: rd(i) for i in (56, 57, 58, 59, 60, 61, 62)}
    return d


def main():
    print("=== 批大小 vs 单批卡顿 (每档 %d 秒) ===" % SECS)
    print("  批槽数  批字节  生效  落到卡上  丢包   最大待落盘  最大间隔   写内最长   慢轮询")
    for b in (64, 128, 256):
        d = run(b)
        print("  %-6d  %-6dB  %-4d  %-8d  %-5d  %-10d  %6.1fms   %6.1fms   %d"
              % (b, b * 256, d[62] or 0, d[58] or 0, d[57] or 0, d[56] or 0,
                 (d[59] or 0) * 0.1, (d[60] or 0) * 0.1, d[61] or 0))
        time.sleep(1)
    print("\n判读: 看『写内最长』是否随批变大而变长; 『丢包』是否归零; 『落到卡上』够不够产量。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
