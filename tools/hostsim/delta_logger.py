#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""delta_logger.py —— 「数据变化全量上传电脑记录」的上位机侧 (2026-09-22)

═══════════════════════════════════════════════════════════════════════════════
它是什么
═══════════════════════════════════════════════════════════════════════════════
板子侧新增了一个**增量上传环**（`OFF_DELTA_RING`，见 `src/engine.h`）：
每拍比对 60 个映射通道，**哪个变了就推一条 16 B 的记录**（tick / seq / 通道号 / 值位型）。
本文件以**低频率**（默认 10 Hz）把它拉过来，**落盘成全量时间序列**。

## 为什么"低频"也吃不丢 —— 实测账
  · 板子侧实测变化率：静止 **39 条/s**，运动（1200 Hz）**312 条/s**
  · 一次拉取往返 ≈ 10 ms(请求) + 90 ms(1024 B 阻塞发送) = 100 ms ⇒ 10 Hz
  · 单次最多 64 条 ⇒ 消费能力 **640 条/s** > 312 条/s ⇒ **余量 2 倍**
  · 环 198 条 @312 条/s = **0.63 s 窗口** ⇒ 10 Hz(0.1 s) 有 **6 倍余量**
  ⇒ **"全吃"成立**，而且 `dropped` 计数会告诉你在不在丢（不静默）。

## 为什么不用"上位机高频轮询 + 自己判变化"
  实测 `0x22` 读 1 字的往返就是 **10.0 ms**（且 96.5% 是协议固定开销）⇒ 轮询上限 ≈100 Hz。
  而被测变化率 100~312 Hz ⇒ **轮询率 < 变化率 ⇒ 一定丢**。
  ⇒ 缓冲**必须**在板子侧；低频只负责"把缓冲搬空"。

## ★★ 口径（务必先读）
  · `tick` 来自板子（`SHM+0x08 HEARTBEAT` 同源），**是唯一权威时间**。
  · 记录是**"变化事件"**，不是"每拍采样"。重建逐拍状态 = 拿完整快照打底 + 按 tick
    应用变化（等价于板子黑匣子的无损 RLE）。
  · `ch` 是**映射槽号**（0..59），不是 SHM 偏移。映射表 = `blackbox.c` 的 `s_bb_map_def`。
  · **掩码**决定哪些槽会上传。板子默认排除 8/9/10/25（AI 三路 ADC 噪声 + 其镜像）——
    实测那三路各 620 Hz，是**采样噪声不是状态**；不排除的话环窗口从 0.63 s 掉到 97 ms。

用法:
    python tools/hostsim/delta_logger.py --secs 30            # 记 30 s
    python tools/hostsim/delta_logger.py --forever --hz 10
    python tools/hostsim/delta_logger.py --resume out.csv     # 断点续传（读回 from_seq）
"""
import argparse
import csv
import json
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))

SUB_DELTA_READ = 26        # op=19 sub=26: 读增量环, arg = from_seq
SLOTS = 198                # DELTA_SLOTS（与 src/engine.h 同步；下面有自检）
SLOT_SZ = 16
TICK_US = 100.0

# ★ 映射槽号 → 名字（与 src/blackbox.c 的 s_bb_map_def 逐项对应）
MAP_NAMES = ([("SENSOR", i) for i in range(14)] + [("FAULT", 0), ("FAULT", 1)]
             + [("WIRE", i) for i in range(16)] + [("ACT", i) for i in range(16)]
             + [("RTC", 0), ("RTC", 1)] + [("MB", i) for i in range(5)]
             + [("DO", 0)] + [("FORCE", i) for i in range(4)])


def ch_name(c):
    return "%s[%d]" % MAP_NAMES[c] if c < len(MAP_NAMES) else "ch%d" % c


def f32(bits):
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="delta_log")
    ap.add_argument("--secs", type=float, default=30.0)
    ap.add_argument("--forever", action="store_true")
    ap.add_argument("--hz", type=float, default=10.0, help="拉取频率（实测 10 Hz 足够）")
    ap.add_argument("--port", default=os.environ.get("DCL_PORT"))
    ap.add_argument("--resume", metavar="CSV", help="从已有 csv 续（读回最后一条 seq）")
    a = ap.parse_args()
    if a.forever:
        a.secs = 0

    from h723_client import Dcl, find_board

    base = os.path.splitext(a.out)[0]
    csv_path, meta_path = base + ".csv", base + ".meta.json"

    d = Dcl(a.port or find_board())
    print("  端口 %s" % d.port)
    try:
        shm = struct.unpack("<I", d.send(0x38, expect_len=51)[1][23:27])[0]
        hdr_addr = shm + 0x7FC0          # OFF_DELTA_HDR
        s, p = d.send(0x22, struct.pack("<IH", hdr_addr, 5), expect_len=None)
        assert s == "ACK" and len(p) >= 20, "读 DELTA_HDR 失败"
        w, last_tick, drop, mlo, mhi = struct.unpack("<5I", p[:20])
        print("  g_shm=0x%08X  DELTA_HDR=0x%08X" % (shm, hdr_addr))
        print("  掩码 lo=0x%08X hi=0x%08X  ⇒ 关掉的槽: %s"
              % (mlo, mhi, [i for i in range(60)
                            if not ((mlo >> i) & 1) if i < 32] +
                           [i for i in range(32, 60) if not ((mhi >> (i - 32)) & 1)][:6]))

        # ── 起始序号：断点续传 / 拿满整个环 ──
        if a.resume and os.path.exists(a.resume):
            last = None
            with open(a.resume, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    last = int(row.get("seq", 0))
            if last is not None:
                from_seq = last + 1
                print("  续传: 从 seq=%d 开始" % from_seq)
        else:
            from_seq = (w - SLOTS) & 0xFFFFFFFF
            print("  首次: 从环底 seq=%d 开始（拿满整个环）" % from_seq)

        rows_new = 0
        n_poll = 0
        dropped_total = 0
        t0 = time.time()
        f = open(csv_path, "w", newline="", encoding="utf-8-sig")
        cw = csv.writer(f)
        cw.writerow(["tick", "seq", "ch", "name", "val_bits", "val_float"])
        print("\n  采样中… (Ctrl-C 收尾)")

        try:
            while True:
                if a.secs and (time.time() - t0) >= a.secs:
                    break
                st, pl = d.send(0x39, struct.pack("<BBI", 19, SUB_DELTA_READ, from_seq),
                                expect_len=None)
                if st != "ACK" or not pl or len(pl) < 16:
                    print("  !! sub=26 失败 sts=%s len=%s" % (st, len(pl) if pl else 0))
                    time.sleep(0.5); continue
                cnt = pl[0]
                flags = pl[1]
                w_now, frm, drp = struct.unpack("<3I", pl[4:16])
                if flags & 1:
                    dropped_total += drp
                    print("  ★ **发生覆盖**：丢了 %d 条（累计 %d）—— 说明拉取跟不上" %
                          (drp, dropped_total))
                for i in range(cnt):
                    o = 16 + i * SLOT_SZ
                    if len(pl) < o + SLOT_SZ:
                        break
                    tk, sq, ch, bits = struct.unpack("<4I", pl[o:o + SLOT_SZ])
                    cw.writerow([tk, sq, ch, ch_name(ch), "0x%08X" % bits, repr(f32(bits))])
                    rows_new += 1
                if cnt:
                    from_seq = (frm + cnt) & 0xFFFFFFFF
                n_poll += 1
                if n_poll % 25 == 0:
                    print("    %d 次拉取 / %d 条 / %.1f Hz"
                          % (n_poll, rows_new, n_poll / max(1e-9, time.time() - t0)))
                w = max(0.0, t0 + n_poll / a.hz - time.time())
                if w > 0:
                    time.sleep(min(w, 0.5))
        except KeyboardInterrupt:
            print("\n  ^C —— 收尾")
        finally:
            f.close()
    finally:
        d.close()

    wall = time.time() - t0
    meta = dict(
        schema="DCL-DELTA-LOG v1",
        purpose="板子侧'变化事件流'的落盘（不是每拍采样；重建逐拍需快照 + 按 tick 应用）",
        created=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        port=str(d.port),
        source="0x39 op=19 sub=26 (OFF_DELTA_RING)",
        tick_us=TICK_US, tick_hz=1e6 / TICK_US,
        sample_rule="device_tick",
        device_tick_note="tick 来自板子 HEARTBEAT（每拍 +1）⇒ 可差分、可做时间轴",
        mask_lo="0x%08X" % mlo, mask_hi="0x%08X" % mhi,
        mask_note=("板子默认关掉槽 8/9/10/25 = AI 三路 ADC 噪声 + 其镜像。"
                   "实测那三路各 620 Hz（是采样噪声不是状态）；全开会让环窗口 0.63s→97ms。"),
        slots=SLOTS, slot_bytes=SLOT_SZ,
        poll_hz=a.hz, rows=rows_new, polls=n_poll,
        dropped=dropped_total,
        dropped_note="★ 非 0 说明拉取跟不上、环被覆盖 —— 这是唯一能「证明丢过」的量",
        channel_map="见 src/blackbox.c 的 s_bb_map_def（60 项）",
    )
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print("\n" + "=" * 68)
    print("  %d 条 / %.1f s ⇒ **%.0f 条/s**   拉取 %d 次 (%.1f Hz)"
          % (rows_new, wall, rows_new / max(1e-9, wall), n_poll, n_poll / max(1e-9, wall)))
    print("  带宽占用 ≈ %.2f KB/s（115200 的 %.0f%%）"
          % (rows_new * SLOT_SZ / max(1e-9, wall) / 1024,
             rows_new * SLOT_SZ / max(1e-9, wall) / 1024 / 11.5 * 100))
    print("  dropped = %d %s" % (dropped_total, "✓ 没丢" if dropped_total == 0 else "★ 丢了"))
    print("  → %s" % csv_path)
    print("  → %s   ★ 分析前先读它" % meta_path)
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
