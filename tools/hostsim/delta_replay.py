#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""delta_replay.py —— 把"变化事件流"重建成波形，并**并排画出两种口径**。

═══════════════════════════════════════════════════════════════════════════════
为什么要有它（这是"全量数据"最直接的兑现）
═══════════════════════════════════════════════════════════════════════════════
板子侧现在有一条**变化事件流**（`DELTA_RING`，`0x39 op=19 sub=26`）。
但它记的是"**哪个通道在什么时候变了**"，不是"每拍的值" —— 要变成能看的波形，
必须先**重建**：
    ① 打底：取一个已知状态（本工具用事件流的第一个值）
    ② 按 tick 顺序把事件**应用**上去 ⇒ 得到逐拍状态序列
    ③ 位置 → 差分 ⇒ 速度（分子分母**同为设备口径**：Δcounts / (Δtick × 拍长)）

★ 本工具同时画**两条对照曲线**（这是它的价值所在）：
    · **事件流重建**（每个点都是一次真实变化）——实测 ~60 Hz 分辨率
    · **模拟 12.5 Hz 轮询**（`bridge.py` 现在的做法）——同一条物理量，只是采样粗
  ⇒ 一眼看出"分辨率差在哪"，而不是靠说。

## ★★ 实测（2026-09-22，运动中 1500 Hz）
    事件流重建：392 点 / 6.51 s ⇒ 中位 0.658 mm/s，**std 0.0094**
    12.5 Hz 轮询：65 点 ⇒ 中位 0.660 mm/s，**std 0.0030**
    ⇒ 中位数一致（差 0.27%，两者**不矛盾**），但 **std 差 3.14×** ——
      12.5 Hz 看不到的那些抖动，事件流看得到。

## ⚠️ 一个必须知道的前提（本工具会在报告里标出）
读 `sub=26` 会**拖慢被测系统**：实测编码器率 **91.1 Hz（不读）→ 60.4 Hz（读）**。
机制 = 应答走**阻塞发送**（1040 B ≈ 90 ms 卡主循环），而主循环是编码器采样的调度者。
⇒ **事件流是"记录/分析"通道，不要拿来高频喂实时渲染。**

用法:
    python tools/hostsim/delta_replay.py .tmpctl/dl_wave.csv --html wave.html
    python tools/hostsim/delta_replay.py run.csv --pitch 8 --polarity 4096
"""
import argparse
import csv
import json
import os
import struct
import sys

TICK_US = 100.0
COUNTS_PER_REV = 4096.0
# ★ 映射槽号 → (段, 索引)，与 src/blackbox.c 的 s_bb_map_def 逐项对应
MAP_NAMES = ([("SENSOR", i) for i in range(14)] + [("FAULT", 0), ("FAULT", 1)]
             + [("WIRE", i) for i in range(16)] + [("ACT", i) for i in range(16)]
             + [("RTC", 0), ("RTC", 1)] + [("MB", i) for i in range(5)]
             + [("DO", 0)] + [("FORCE", i) for i in range(4)])


def f32(bits):
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def load(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append((int(r["tick"]), int(r["ch"]), int(r["val_bits"], 16)))
    rows.sort(key=lambda x: x[0])          # ★ 按 tick 排序（seq 会回绕）
    return rows


def rebuild(rows, deg_ch=1, pitch=8.0):
    """从 deg 事件重建 (tick, 累计 revs) 序列。返回 [(tick, revs)]"""
    enc = [(t, f32(b)) for t, c, b in rows if c == deg_ch]
    if len(enc) < 3:
        return [], enc
    out, tot, prev = [], 0.0, None
    for t, d in enc:
        if prev is not None:
            dd = d - prev
            if dd < -180.0:
                dd += 360.0
            elif dd > 180.0:
                dd -= 360.0
            tot += dd / 360.0                     # revs
        prev = d
        out.append((t, tot * pitch))              # mm
    return out, enc


def speeds(seq):
    """逐点速度（分子分母同为设备口径）"""
    v = []
    for i in range(1, len(seq)):
        dk = seq[i][0] - seq[i - 1][0]
        if dk > 0:
            v.append(((seq[i][1] - seq[i - 1][1]) / (dk * TICK_US * 1e-6), seq[i][0]))
    return v


def thin(seq, every_ticks):
    out, last = [], None
    for t, v in seq:
        if last is None or t - last >= every_ticks:
            out.append((t, v))
            last = t
    return out


def svg_poly(pts, t0, tspan, w, h, vmin, vmax, color, dash=""):
    if not pts or vmax <= vmin:
        return ""
    def X(t):
        return (t - t0) / tspan * (w - 8) + 4
    def Y(v):
        return h - 4 - (v - vmin) / (vmax - vmin) * (h - 8)
    d = " ".join("%s%.2f,%.2f" % ("M" if i == 0 else "L", X(t), Y(v))
                 for i, (t, v) in enumerate(pts))
    da = ' stroke-dasharray="%s"' % dash if dash else ""
    return ('<path d="%s" fill="none" stroke="%s" stroke-width="1.4"%s/>' % (d, color, da))


def panel(title, note, series, t0, tspan):
    """series = [(label, pts, color, dash)]"""
    W, H = 880, 150
    allv = [v for _, pts, _, _ in series for _, v in pts]
    if not allv:
        return ""
    vmin, vmax = min(allv), max(allv)
    pad = (vmax - vmin) * 0.12 or max(abs(vmax), 1e-9) * 0.12
    vmin -= pad; vmax += pad
    parts = ['<div class="p"><div class="t">%s</div><div class="n">%s</div>' % (title, note)]
    parts.append('<svg viewBox="0 0 %d %d" width="100%%" height="%d">' % (W, H, H))
    parts.append('<rect x="0" y="0" width="%d" height="%d" fill="#fbfbfc" stroke="#e3e6e9"/>' % (W, H))
    for i in range(1, 5):
        y = H * i / 5
        parts.append('<line x1="0" y1="%.1f" x2="%d" y2="%.1f" stroke="#eef0f2"/>' % (y, W, y))
    for lab, pts, col, dash in series:
        parts.append(svg_poly(pts, t0, tspan, W, H, vmin, vmax, col, dash))
    parts.append('<text x="6" y="12" font-size="10" fill="#888">%.3f</text>' % vmax)
    parts.append('<text x="6" y="%d" font-size="10" fill="#888">%.3f</text>' % (H - 4, vmin))
    parts.append('</svg><div class="lg">')
    for lab, pts, col, dash in series:
        st = ' style="border-top:2px %s %s"' % ("dashed" if dash else "solid", col)
        parts.append('<span><i class="sw"%s></i>%s (%d 点)</span>' % (st, lab, len(pts)))
    parts.append('</div></div>')
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--html", default=None)
    ap.add_argument("--pitch", type=float, default=8.0)
    ap.add_argument("--poll-hz", type=float, default=12.5, help="对照曲线的轮询率")
    a = ap.parse_args()

    rows = load(a.csv)
    if not rows:
        print("!! CSV 空"); return 2
    pos, enc = rebuild(rows, pitch=a.pitch)
    dt_ev = [enc[i][0] - enc[i - 1][0] for i in range(1, len(enc))]
    import statistics as st
    span = (enc[-1][0] - enc[0][0]) * TICK_US * 1e-6 if len(enc) > 1 else 0

    print("══ 事件流重建 ══")
    print("  总事件 %d 条 / %.2f s ⇒ %.0f 条/s" % (len(rows), span, len(rows) / max(1e-9, span)))
    by = {}
    for _, c, _ in rows:
        by[c] = by.get(c, 0) + 1
    for c, n in sorted(by.items(), key=lambda kv: -kv[1])[:6]:
        nm = "%s[%d]" % MAP_NAMES[c] if c < len(MAP_NAMES) else "ch%d" % c
        print("    ch=%2d %-12s %5d 条 ⇒ %6.1f 条/s" % (c, nm, n, n / max(1e-9, span)))
    if dt_ev:
        print("  编码器事件 Δtick: 中位 %d  min %d  max %d ⇒ 中位率 %.0f Hz"
              % (st.median(dt_ev), min(dt_ev), max(dt_ev), 1e6 / (st.median(dt_ev) * TICK_US)))
        print("  ★ 中位率 vs 实际率(%d 条): 差 %.0f%% ⇒ **长尾说明有卡顿**（不是丢包）"
              % (len(enc), abs(1e6 / (st.median(dt_ev) * TICK_US) - len(enc) / max(1e-9, span))
                 / max(1e-9, len(enc) / max(1e-9, span)) * 100))

    vel = speeds(pos)
    every = int(round(1e6 / a.poll_hz / TICK_US))
    pos_poll = thin(pos, every)
    vel_poll = speeds(pos_poll)
    print()
    print("══ 两种口径 ══")
    print("  事件流 : %4d 点 / %.2f s = %6.1f Hz | 中位 %.3f  std %.5f mm/s"
          % (len(vel), span, len(vel) / max(1e-9, span),
             st.median([v for v, _ in vel]), st.pstdev([v for v, _ in vel])))
    if vel_poll:
        print("  轮询   : %4d 点 / %.2f s = %6.1f Hz | 中位 %.3f  std %.5f mm/s"
              % (len(vel_poll), span, len(vel_poll) / max(1e-9, span),
                 st.median([v for v, _ in vel_poll]), st.pstdev([v for v, _ in vel_poll])))
        r = st.pstdev([v for v, _ in vel]) / max(1e-12, st.pstdev([v for v, _ in vel_poll]))
        print("  ★ std 比 = %.2f×（事件流看得到的抖动，轮询看不到）" % r)

    if not a.html:
        return 0
    t0, t1 = pos[0][0], pos[-1][0]
    tspan = max(1, t1 - t0)
    event_rate = []
    win = 1000                                     # 100 ms 窗
    bucket = {}
    for t, _, _ in rows:
        bucket[t // win] = bucket.get(t // win, 0) + 1
    event_rate = sorted((k * win, v * 10.0) for k, v in bucket.items())   # 条/s

    h = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
         '<title>增量流回放 — %s</title><style>' % os.path.basename(a.csv),
         'body{margin:0;padding:22px;background:#f4f6f8;font:13px/1.6 system-ui,"Microsoft YaHei",sans-serif;color:#222}',
         'h1{font-size:17px;margin:0 0 4px}.sub{color:#666;font-size:12px;margin-bottom:16px}',
         '.p{background:#fff;border:1px solid #e3e6e9;border-radius:10px;padding:12px 14px;margin-bottom:14px}',
         '.t{font-weight:600;font-size:13px}.n{color:#777;font-size:11px;margin:2px 0 8px}',
         '.lg{display:flex;gap:16px;font-size:11px;color:#666;margin-top:6px;flex-wrap:wrap}',
         '.sw{width:14px;height:0;display:inline-block;margin-right:5px;vertical-align:middle}',
         '.k{display:flex;gap:22px;flex-wrap:wrap;background:#fff;border:1px solid #e3e6e9;'
         'border-radius:10px;padding:12px 14px;margin-bottom:14px}',
         '.k div{font-size:12px}.k b{font-size:15px;font-variant-numeric:tabular-nums}',
         '.warn{background:#fff8f0;border-color:#f0d0a0;color:#7a4a00}',
         '</style></head><body>']
    h.append('<h1>增量流回放 — %s</h1>' % os.path.basename(a.csv))
    h.append('<div class="sub">数据源 <code>0x39 op=19 sub=26</code>（DELTA_RING，板子侧"变化才记"）· '
             '时间轴 = 板子 tick（每拍 +1，100 µs）· %d 条事件 / %.2f s</div>'
             % (len(rows), span))
    h.append('<div class="k">')
    h.append('<div>事件率<br><b>%.0f</b> 条/s</div>' % (len(rows) / max(1e-9, span)))
    h.append('<div>编码器事件<br><b>%d</b> 条 (%.0f Hz)</div>'
             % (len(enc), len(enc) / max(1e-9, span)))
    h.append('<div>Δ区间中位<br><b>%d</b> 拍</div>' % (st.median(dt_ev) if dt_ev else 0))
    if vel and vel_poll:
        h.append('<div>速度 std 比<br><b>%.2f×</b></div>'
                 % (st.pstdev([v for v, _ in vel]) / max(1e-12, st.pstdev([v for v, _ in vel_poll]))))
    h.append('</div>')

    h.append(panel("位置（mm）", "从 deg 事件解绕重建 · 每点 = 一次真实变化",
                   [("事件流", pos, "#185FA5", ""),
                    ("12.5 Hz 轮询", pos_poll, "#BA7517", "5,3")], t0, tspan))
    if vel:
        h.append(panel("线速度（mm/s）", "Δcounts / (Δtick × 拍长) —— 分子分母同为设备口径",
                       [("事件流", vel, "#0F6E56", ""),
                        ("12.5 Hz 轮询", vel_poll, "#BA7517", "5,3")], t0, tspan))
    h.append(panel("事件率（条/s，100 ms 窗）", "↑ 越高说明这一段时间系统越忙",
                   [("事件率", event_rate, "#A32D2D", "")], t0, tspan))
    h.append('<div class="p warn"><div class="t">⚠️ 一个必须知道的前提</div>'
             '<div class="n">读 <code>sub=26</code> 会<b>拖慢被测系统</b>：实测编码器率 '
             '<b>91.1 Hz（不读）→ 60.4 Hz（读）</b>。机制 = 应答走<b>阻塞发送</b>'
             '（1040 B ≈ 90 ms 卡主循环），而主循环是编码器采样的调度者。'
             '⇒ 事件流是<b>记录/分析</b>通道，不要拿来高频喂实时渲染。</div></div>')
    h.append('</body></html>')
    with open(a.html, "w", encoding="utf-8") as f:
        f.write("".join(h))
    print("\n  → %s" % a.html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
