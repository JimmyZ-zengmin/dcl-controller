#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_stall_v2.py —— **阶段 2**：黑匣子量"实际/请求比 vs 频率"（失速拐点）+ 斜坡的真实收益

## ★★★ 2026-09-17 定案：本脚本第一版的分母是错的（两个独立错误叠加）
| # | 错 | 真值（证据） |
|---|---|---|
| 1 | 把 `slot[1]` 当 seq、`slot[2]` 当 tick | `blackbox.h:29-33` 是 `[0]magic [1]tick [2]seq [3]ctrl` ⇒ **读反了** |
| 2 | `span = 959 × 100 µs` | 环是**"变化才记"**（`blackbox.c:345`，内容没变就不占槽）⇒ 跨度**必须由 tick 字段算** |
后果：比值全档虚高 3.4~4.0 倍，报出"31000 Hz ⇒ 4651 rpm"这种**步进电机不可能**的数 —— 荒谬值才是线索。
⇒ 现在：`span` 由环内 `tick` 跨度算；并同时输出记录率，供交叉核对。

## 为什么这次能成（对照前四轮）
| 前四轮死因 | 这次 |
|---|---|
| 套件 26 命令/s 采样 vs 38 圈/2 s ⇒ 混叠 | 黑匣子：tick 级时间轴（10 kHz）+ 完整环 |
| "先停脉冲再读" ⇒ 进程启动 0.5 s，环早滚过 | **运动中途 halt**：环冻结在 halt 前那一段（稳态）|
| 反馈 `SENSOR[0]` 只 163 Hz | ★ 阶段 0：`sub=22 arg=1` + `sub=20 arg=10` ⇒ ~1 kHz |
| 首尾 raw 差 + 按预期解卷绕 = 循环论证 | **逐点**解卷绕，不依赖任何预期值 |
| `pyocd commander` 只读 60 KB（环 240 KB）| **pyocd API 单会话**读满 240 KB |

## 判据（都能失败）
- **R-neg（反向判据）**：低频档（1000 Hz）两臂比值必须落在 1.00±5% —— 若它也红，说明**度量本身坏了**，
  不是"发现规律"。★ 只有"能失败的判据"不够，还要有"不该红的不红"的判据。
- **不混叠**：按**编码器真正更新的样本**（AS5600 每 2 槽一更新）算最大单步 < 90°（用实测，不用 `f·Δt`）。
- **比值曲线** ⇒ 失速拐点；**突加 vs 斜坡** ⇒ 斜坡的真实收益。

用法: python tools/h723_stall_sweep.py [--quick]
"""
import os
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from h723_client import Dcl, find_board       # noqa: E402

SYS_PY = "C:/Users/min/AppData/Local/Programs/Python/Python313/python.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
DUMP_BIN = os.path.join(HERE, "..", "build", "_bb2.bin")   # 产物落 build/ (已 gitignore)
BB_DUMP = os.path.join(HERE, "h723_bb_dump.py")
SLOT_W = 64          # 256 B = 64 字
N_SLOTS = 960
SPR = 1600.0         # 1600 步/圈 (1/8 微步) —— 由"走 N 步 vs 编码器"静态对照确认
TICK_HZ = 10000.0
TICK_MS = 100.0      # 单位: µs
ALIAS_MAX_DEG = 90.0
FREQS = [1000, 2000, 4000, 6000, 8000, 10000, 12000, 14000, 16000, 18000, 20000, 24000, 28000, 31000]
FREQS_QUICK = [1000, 4000, 12000, 18000, 31000]


def dump_bb():
    r = subprocess.run([SYS_PY, BB_DUMP, DUMP_BIN],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(DUMP_BIN):
        return None
    with open(DUMP_BIN, "rb") as f:
        data = f.read()
    return data if len(data) >= SLOT_W * 4 * N_SLOTS else None


def u32(d, off):
    return int.from_bytes(d[off:off + 4], "little")


def analyse(d):
    """★ 字段: [0]magic [1]tick [2]seq [3]ctrl [4..63]数据(默认表 data[0]=SENSOR[0] raw)"""
    ticks, seqs, raws, degs = [], [], [], []
    for i in range(N_SLOTS):
        o = i * SLOT_W * 4
        ticks.append(u32(d, o + 4))
        seqs.append(u32(d, o + 8))
        raws.append(int(round(struct.unpack("<f", d[o + 16:o + 20])[0])))
        degs.append(struct.unpack("<f", d[o + 20:o + 24])[0])
    if len(set(u32(d, i * SLOT_W * 4) for i in range(N_SLOTS))) != 1:
        return None                                   # magic 不唯一 ⇒ 环没被完整填充
    i0 = seqs.index(max(seqs))
    ch = [(i0 + 1 + k) % N_SLOTS for k in range(N_SLOTS)]
    T = [ticks[i] for i in ch]
    R = [raws[i] for i in ch]
    D = [degs[i] for i in ch]

    # ── 时间轴：由 tick 字段算，不由槽数算 ──
    span_ticks = (T[-1] - T[0]) & 0xFFFFFFFF
    if span_ticks == 0:
        return None
    dt_hist = {}
    for k in range(N_SLOTS - 1):
        dt = (T[k + 1] - T[k]) & 0xFFFFFFFF
        dt_hist[dt] = dt_hist.get(dt, 0) + 1

    # ── 逐点解卷绕（离散样本 = 编码器真正更新过的值；中间槽是保持值）──
    tot, prev, mx, nn = 0.0, R[0], 0.0, 1
    for v in R[1:]:
        if v == prev:
            continue
        dv = (v - prev) & 0xFFF
        if dv > 2048:
            dv -= 4096
        if abs(dv) > mx:
            mx = abs(dv)
        tot += dv
        prev = v
        nn += 1
    # 两路口径一致性（deg 字段 vs raw 换算）
    dmax = max(abs(((D[k] - R[k] * 360.0 / 4096.0 + 180) % 360) - 180) for k in range(N_SLOTS))
    return dict(span_s=span_ticks / TICK_HZ, ticks=span_ticks, hist=dt_hist,
                n_distinct=nn, max_step_deg=mx * 360.0 / 4096.0,
                deg=abs(tot) * 360.0 / 4096.0, cross_deg=dmax)


def main():
    quick = "--quick" in sys.argv
    freqs = FREQS_QUICK if quick else FREQS
    d = Dcl(find_board())
    bad = []

    def sx(n, v, name=""):
        st, p = d.send(0x39, bytes([19, n]) + struct.pack("<I", v))
        if st != "ACK":
            bad.append("sub=%d(%s)=%s" % (n, name, st))
        return st, p

    def prep():
        """★ 使能放最后（`sub=6` 按 hold 重算）；★ 开拍内反馈（阶段 0）。"""
        sx(1, 0, "rate"); sx(6, 0, "stop"); sx(17, 0, "slope"); sx(13, 0, "src")
        sx(5, 1, "enapol"); sx(2, 0, "dir")
        sx(22, 1, "sm_mode"); sx(20, 10, "period")
        time.sleep(0.3)
        sx(3, 1, "ena")                      # ★ 使能最后
        time.sleep(0.3)

    print("=== 阶段 2：黑匣子测实际/请求比（拍内反馈 ~1kHz；运动中途 halt）===")
    print("   频率Hz |  臂  | 实际/请求 | 环跨度ms | 记录率/s | 独立样本 | 最大单步° | 不混叠")
    rows = []
    for f in freqs:
        for tag, slope in (("突加", 0), ("斜坡", 1)):
            prep()
            if slope:
                sx(17, int(f * 2), "slope")
            sx(1, f, "rate")
            t0 = time.time()
            while time.time() - t0 < 5.0:
                st, p = sx(19, 0, "rampst")
                if st == "ACK" and len(p) == 32 and struct.unpack("<8I", p[:32])[3] >= f:
                    break
                time.sleep(0.05)
            time.sleep(0.4)                  # 稳态段
            data = dump_bb()                 # ★ 运动中途 halt
            sx(1, 0, "rate"); sx(17, 0, "slope"); sx(22, 0, "sm_mode"); sx(20, 100, "period")
            time.sleep(0.3)
            if data is None:
                print("  %6d | %-4s |   dump失败" % (f, tag))
                continue
            a = analyse(data)
            if a is None:
                print("  %6d | %-4s |   环magic 不唯一" % (f, tag))
                continue
            exp = f * a["span_s"] / SPR * 360.0
            ratio = a["deg"] / exp if exp else 0.0
            ok = a["max_step_deg"] < ALIAS_MAX_DEG
            rows.append((f, tag, ratio, a, ok))
            print("  %6d | %-4s |   %6.3f  |  %7.1f |  %7.0f |  %6d  |  %6.2f   | %s"
                  % (f, tag, ratio, a["span_s"] * 1e3, a["n_distinct"] / a["span_s"],
                     a["n_distinct"], a["max_step_deg"], "✓" if ok else "✗ 混叠,判无效"))

    # ── 反向判据：低频档必须 ≈1.00 ──
    print("\n=== R-neg 反向判据：1000 Hz 两臂应落在 1.00±5% ===")
    for f, tag, ratio, a, ok in rows:
        if f == 1000:
            v = "✓" if abs(ratio - 1.0) <= 0.05 else "✗ 度量本身可疑"
            print("   1000 Hz %-4s 比值 %.3f ⇒ %s" % (tag, ratio, v))
    print("\n   两路口径交叉核对 |deg - raw*360/4096|: %s"
          % ("  ".join("%.3f°" % a["cross_deg"] for _, _, _, a, _ in rows[:4])))
    if bad:
        print("\n!! 有命令未 ACK: %s" % ", ".join(sorted(set(bad))))
    d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
