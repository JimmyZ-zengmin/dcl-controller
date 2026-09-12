#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shm_channel_audit.py — 量"当前固件/程序实际用了多少个 SHM 通道"

为什么需要: 黑匣子记录要按"实际用到的通道数"裁剪, 而不是按 SHM 的分配上限
(SENSOR_MAP 64 / ACTUATOR_STATUS 64 / WIRE_MAP 128) —— 直接按上限带会让
每条记录 1KB+, 远超带宽。

判据: `.dtcm_shm` 是 NOLOAD, `cold_start_reset()` 只清**已登记**的域 ⇒
      没被用到的通道会留上电垃圾 (|v|>1e9 / NaN / Inf / 反常小) ⇒ 可据此分类。
      (被用到且恰好奇大的通道会被误判 —— 所以本工具只报"可疑个数", 不当作铁证。)

SHM 基址从符号 s_bb_shm 读; 三段偏移见 engine.h:
  OFF_SENSOR_MAP 0x40 (64×f32) / OFF_ACTUATOR_STATUS 0x140 (64×f32) / OFF_WIRE_MAP 0x240 (128×f32)
"""
import ctypes
import ctypes.wintypes as wt
import math
import re
import struct
import subprocess
import sys

TARGET = "stm32h723xx"
SHM_PTR = 0x20007FC8      # s_bb_shm (blackbox.c) —— map 里确认过


def py(args):
    p = subprocess.run(["pyocd", "cmd", "-t", TARGET, "-O", "connect_mode=halt"] + args,
                       capture_output=True, text=True, errors="replace")
    return p.stdout + p.stderr


def read_words(addr, n):
    out = []
    CH = 24
    for base in range(0, n, CH):
        cnt = min(CH, n - base)
        args = []
        for i in range(cnt):
            args += ["-c", "read32 0x%08X" % (addr + (base + i) * 4)]
        args += ["-c", "go"]
        txt = py(args)
        for l in txt.splitlines():
            m = re.match(r"^([0-9a-f]{8}):\s+([0-9a-f]{8})", l.strip())
            if m:
                out.append(int(m.group(2), 16))
    return out


def f32(u):
    return struct.unpack_from("<f", struct.pack("<I", u))[0]


def suspicious(f):
    if math.isnan(f) or math.isinf(f):
        return True
    a = abs(f)
    return a > 1e9 or (a > 0 and a < 1e-20)   # 明显是位模式垃圾


def main():
    out = py(["-c", "read32 0x%08X" % SHM_PTR])
    m = re.search(r"^([0-9a-f]{8}):\s+([0-9a-f]{8})", out.splitlines()[0].strip() if out.splitlines() else "")
    shm = None
    for l in out.splitlines():
        mm = re.match(r"^([0-9a-f]{8}):\s+([0-9a-f]{8})", l.strip())
        if mm:
            shm = int(mm.group(2), 16)
    if shm is None:
        print("[X] 读不到 s_bb_shm")
        return 2
    print("SHM 基址 = 0x%08X" % shm)

    maps = [("SENSOR  ", 0x40, 64), ("ACTUATOR", 0x140, 64), ("WIRE    ", 0x240, 128)]
    total_susp = 0
    for name, off, cnt in maps:
        w = read_words(shm + off, cnt)
        sus = [i for i, u in enumerate(w) if suspicious(f32(u))]
        print("  %s: 分配 %3d 个, **可疑(疑似未使用) %3d 个**, 有效 %3d 个  前8=%s"
              % (name, cnt, len(sus), cnt - len(sus),
                 ["%.4g" % f32(u) for u in w[:8]]))
        total_susp += len(sus)
    print("\n=> 三段合计: 分配 %d 个, 可疑 %d 个, 有效 %d 个"
          % (64 + 64 + 128, total_susp, 256 - total_susp))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
