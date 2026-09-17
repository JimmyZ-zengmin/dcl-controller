#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_bb_measure.py —— 用**黑匣子**测"实际/请求比"（**临时脚手架，不进交付物**）

## 为什么必须换到黑匣子（前两轮的路都被实测否掉）
- 套件 `ramp/speed`：**26 命令/s** 采样 vs "2 s 走 38 圈" ⇒ **混叠**（它自己打印过警告）；
- 我的"逐档 + PC 采样"：**采样间隔抖动**（平均 14.8 ms 但个别更短）⇒ 最大单步到 **179.4°**
  ⇒ 解卷绕临界乱跳（算出 1733 这种比值）。

## 黑匣子为什么行（*实测确认过的结构*）
`src/blackbox.h`：**每拍 256 B 快照 × 960 槽 = 96 ms @10 kHz 拍**，槽 = `[4 字头][60 字数据]`，
默认映射 `s_bb_map_def[0] = BB_ME(0,0)` ⇒ **数据[0] = `SENSOR[0]`（AS5600 raw）**、数据[1] = 角度。
⇒ ★★★ **时间轴是硬件拍（100 µs），不是 PC 墙钟** —— **采样抖动这一项彻底消失**。
⇒ 且 `SENSOR[0]` 由绑定表每 10 拍回填（**1 kHz 角度更新率**，受 AS5600 事务 ~450 µs 限制）
   ⇒ @31000 Hz 每采样 31 步 = **7°** ⇒ 远小于 180° ⇒ **不混叠**。

## 流程
1. 前置：写绑定表（否则 `SENSOR[0]` 恒 0）+ 使能**放最后**；
2. 起脉冲 @f，跑 0.6 s（保证环里全是稳态）；
3. **停脉冲 → 立刻 SWD dump** 环（128 KB）⇒ 环里就是"停车前 96 ms"；
4. 解析：按槽头序号找**最新**槽，取连续 960 槽，读 `SENSOR[0]`，**按拍计时**解卷绕；
5. 比值 = 实测角度增量 / (f × 96 ms / 1600 × 360)。

用法: python build/_bb_measure.py [频率Hz ...]
"""
import os
import re
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from h723_client import Dcl, find_board       # noqa: E402

BB_BASE = 0x24004000
SLOT_W = 64          # 256 B = 64 字
N_SLOTS = 960
DUMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bb_dump.txt")


SYS_PY = "C:/Users/min/AppData/Local/Programs/Python/Python313/python.exe"
DUMP_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bb.bin")


def dump_bb():
    """★ 用 **pyocd API**（跑在系统 python 里）单会话读整个环。
    ★★ 时机：在**运动中途** dump —— `halt` 把环冻结在"halt 前 96 ms"，
       而那正是**稳态高速段**。★ 若"先停脉冲再读"，进程启动的 ~0.5 s 早让环滚过去了。"""
    r = subprocess.run([SYS_PY, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bb_dump.py"),
                        DUMP_BIN], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(DUMP_BIN):
        return None
    with open(DUMP_BIN, "rb") as f:
        data = f.read()
    if len(data) < SLOT_W * 4 * N_SLOTS:
        return None
    return data


def main():
    freqs = [int(x) for x in sys.argv[1:]] or [1000, 4000, 12000, 20000, 31000]
    d = Dcl(find_board())

    def sx(n, v):
        return d.send(0x39, bytes([19, n]) + struct.pack("<I", v))[0]

    print("=== 黑匣子测量（10 kHz 拍级时间轴；角度 1 kHz 更新）===")
    print("   频率Hz | 槽数 | 实测角增量° | 预期角增量° | 比值  | 单步最大° | 判定")
    for f in freqs:
        # 前置：★ 使能放最后（`sub=6` 会按 hold=0 把使能关掉 —— 今天踩过两次）
        sx(1, 0); sx(6, 0); sx(17, 0); sx(13, 0); sx(5, 1); sx(2, 0); sx(3, 1)
        time.sleep(0.35)
        sx(1, f)
        time.sleep(0.6)                       # 保证环里 96 ms 全是稳态
        sx(1, 0)                              # ★ 停脉冲后环里就是"停车前 96 ms"
        time.sleep(0.05)
        data = dump_bb()
        if data is None:
            print("  %6d | dump 失败 ⇒ 判**无效**" % f)
            continue
        # 槽 = 64 字；头 4 字，数据 60 字 ⇒ 数据[0] = SENSOR[0]（raw）在字偏移 4
        seqs, raws = [], []
        for i in range(N_SLOTS):
            off = i * SLOT_W * 4
            hdr_seq = int.from_bytes(data[off + 4:off + 8], "little")
            seqs.append(hdr_seq)
            v = int.from_bytes(data[off + 16:off + 20], "little")
            raws.append(struct.unpack("<f", struct.pack("<I", v))[0])
        i0 = seqs.index(max(seqs))
        order = [(i0 + 1 + k) % N_SLOTS for k in range(N_SLOTS)]
        rr = [int(round(raws[i])) for i in order]
        tot, prev, mx = 0.0, rr[0], 0.0
        for v in rr[1:]:
            dv = (v - prev) & 0xFFF
            if dv > 2048:
                dv -= 4096
            if abs(dv) > mx:
                mx = abs(dv)
            tot += dv
            prev = v
        deg_meas = abs(tot) * 360.0 / 4096.0
        span = (N_SLOTS - 1) * 100e-6          # 拍 → 秒（★ 硬件时间轴，无抖动）
        deg_exp = f * span / 1600.0 * 360.0
        ratio = deg_meas / deg_exp if deg_exp else 0.0
        mx_deg = mx * 360.0 / 4096.0
        ok = mx_deg < 90.0                     # ★ 不混叠的**实测**判据
        print("  %6d | %4d | %10.1f | %10.1f | %.3f | %8.2f | %s"
              % (f, N_SLOTS, deg_meas, deg_exp, ratio, mx_deg,
                 ("✓ 不混叠" if ok else "✗ 混叠, 判无效")))
    d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
