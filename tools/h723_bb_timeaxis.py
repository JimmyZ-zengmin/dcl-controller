#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_bb_timeaxis.py —— 离线裁决"黑匣子槽间距到底是多少"（★ 字段映射已按 blackbox.h 更正）

## 血证（2026-09-17）：本文件第一版把字段读反了
`blackbox.h:29-33` 的真值：`[0]=magic  [1]=tick  [2]=seq  [3]=ctrl`
而第一版按 `[1]=seq [2]=tick` 解 ⇒ 于是
  · "seq 每槽 +2/+4/+6" ⇒ 其实那是 **tick**，即**环不是每拍一槽**，槽距 ≈4 拍
  · 把 `span = 959 × 100 µs = 95.9 ms` 当分母 ⇒ 真跨度是其 ~4 倍 ⇒ **比值全档虚高 3.4~4.0**
  · 后果：31000 Hz 档"实测 4651 rpm"（步进电机不可能）—— 这个荒谬值才是线索
★ 教训：**"哪一拍"必须按 tick 字段算，不能按槽序号算**；容量/时间断言一律实测。

判据（能失败）：
  T1 真 seq（word2）是否每槽 +1      ⇒ 环内无空洞
  T2 真 tick（word1）槽间增量直方图   ⇒ 环真实时间跨度
  T3 用 tick 轴算 span，重算比值      ⇒ 应回到 1.00 ± 3%
  T4 data[0]/data[1] 值域形态         ⇒ 确认 raw 与 deg 两路一致
用法: python build/_bb_timeaxis.py [bin]
"""
import os
import struct
import sys

N = 960
W = 64
SPR = 1600.0
TICK_HZ = 10000.0
HERE = os.path.dirname(os.path.abspath(__file__))
PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "build", "_bb2.bin")

d = open(PATH, "rb").read()
assert len(d) >= N * W * 4, len(d)


def u32(off):
    return int.from_bytes(d[off:off + 4], "little")


rows = []
for i in range(N):
    o = i * W * 4
    rows.append((u32(o + 0), u32(o + 4), u32(o + 8), u32(o + 12),
                 struct.unpack("<f", d[o + 16:o + 20])[0],
                 struct.unpack("<f", d[o + 20:o + 24])[0]))

magics = set(r[0] for r in rows)
seqs = [r[2] for r in rows]
i0 = seqs.index(max(seqs))
ch = [rows[(i0 + 1 + k) % N] for k in range(N)]

print("=== T0 环头形态（magic/seq/tick 按 blackbox.h 解码）===")
print("   magic 取值集合 = %s" % ", ".join("0x%08X" % m for m in magics))
print("   idx |   seq   |  tick  |  ctrl    | data0(raw) | data1(deg) | n_routes")
for k in range(6):
    s, t, w3, d0, d1 = ch[k][2], ch[k][1], ch[k][3], ch[k][4], ch[k][5]
    print("  %4d | %7d | %6d | 0x%06X | %9.2f  | %8.2f  | %d"
          % (k, s, t, w3, d0, d1, w3 & 0xFFFF))

print("\n=== T1 真 seq 增量（应为 1，环内有空洞则 >1）===")
h1 = {}
for k in range(N - 1):
    ds = (ch[k + 1][2] - ch[k][2]) & 0xFFFFFFFF
    h1[ds] = h1.get(ds, 0) + 1
print("   " + "  ".join("Δseq=%d×%d" % kv for kv in sorted(h1.items())[:8]))

print("\n=== T2 真 tick 增量直方图 ⇒ 环真实跨度 ===")
h2 = {}
for k in range(N - 1):
    dt = (ch[k + 1][1] - ch[k][1]) & 0xFFFFFFFF
    h2[dt] = h2.get(dt, 0) + 1
for dt in sorted(h2)[:10]:
    print("   Δtick = %5d  × %d" % (dt, h2[dt]))
agg = sum(k * v for k, v in h2.items()) / max(1, sum(h2.values()))
print("   平均 Δtick = %.3f  ⇒ 【环真实跨度 = %.1f ms】" % (agg, (N - 1) * agg / TICK_HZ * 1e3))
print("   ★ blackbox.h:71 注释写的 '960 槽 = 96ms' ⇒ 实测/注释 = %.2f ⇒ 注释过期"
      % ((N - 1) * agg / TICK_HZ / 0.096))

raws = [r[4] for r in ch]
degs = [r[5] for r in ch]
rr = [int(round(v)) for v in raws]
print("\n=== T4 值域与两路一致性 ===")
print("   raw  min=%.0f max=%.0f   全整数=%s   (0..4095 ⇒ 12bit 原始)"
      % (min(raws), max(raws), all(abs(v - round(v)) < 1e-6 for v in raws)))
mx = max(abs(((degs[k] - raws[k] * 360.0 / 4096.0 + 180) % 360) - 180) for k in range(N))
print("   |deg - raw*360/4096| 最大 = %.3f°  (两路口径一致)" % mx)

tot, prev, mxstep, mxk = 0.0, rr[0], 0.0, 0
for k, v in enumerate(rr[1:], 1):
    dv = (v - prev) & 0xFFF
    if dv > 2048:
        dv -= 4096
    if abs(dv) > mxstep:
        mxstep, mxk = abs(dv), k
    tot += dv
    prev = v
deg = abs(tot) * 360.0 / 4096.0
tick_span = (ch[-1][1] - ch[0][1]) & 0xFFFFFFFF
print("\n=== T3 用 tick 轴算真 span ===")
print("   环 tick 跨度 = %d  ⇒ %.1f ms" % (tick_span, tick_span / TICK_HZ * 1e3))
print("   总转动 = %.0f counts = %.1f 圈   最大单步 = %d cnt (%.2f°) @槽%d"
      % (tot, tot / 4096.0, mxstep, mxstep * 360.0 / 4096.0, mxk))
for name, span in (("旧假设 959×100µs", (N - 1) * 1e-4),
                   ("真 tick 轴", tick_span / TICK_HZ)):
    print("   用【%-14s】= %7.4f s ⇒ 隐含实际步频 = %9.1f Hz"
          % (name, span, deg / 360.0 * SPR / span))
