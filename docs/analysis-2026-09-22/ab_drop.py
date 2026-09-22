#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ab_drop.py — 「上传丢条率」的 A/B 判据（能失败）

════════════════════════════════════════════════════════════════════════════
为什么有这个脚本
════════════════════════════════════════════════════════════════════════════
2026-09-22 长稳分析实测：**运动态上传丢 ~20%**（产出 419 条/s，消费上限 329）。
根因是"每轮往返太慢"（`sub=26` 的 400 B 按当时 5.2 KB/s 的泵速要 77 ms
⇒ 轮询率只有 13.7 Hz ⇒ 上限 = 13.7 × DELTA_READ_MAX(24) = 329 < 419）。

于是每次动"上传通路"（`UART_TX_PUMP_US` / `DELTA_READ_MAX` / 每轮命令数 / 波特率）
都需要一个**统一的、能失败的**验收判据。本脚本就是它。

════════════════════════════════════════════════════════════════════════════
判据（两条，缺一不可）
════════════════════════════════════════════════════════════════════════════
① **正判据**：运动态窗口内 `Δdrop == 0`（一条不丢）。
② ★★ **反向判据（正对照）**：窗口内**必须确实在运动**（`ap_hz` 中位 > 100）。
   —— 否则"电机停着 ⇒ 不丢"会被判成 PASS，那是个**必然成立的假绿**
   （静止时产出 150 条/s ≪ 上限，本来就不丢）。

★★ 第三条（防"把丢藏起来"）：窗口内轮询率与产出都要报出来。
   如果某次"优化"是靠**降低产出**（比如关掉位置通道）来让 drop 归零的，
   那 `产出` 会明显下降 —— 一眼可见。**位置是核心数据，不许被优化掉。**

用法:
    python docs/analysis-2026-09-22/ab_drop.py            # 默认 60 s 窗口
    python docs/analysis-2026-09-22/ab_drop.py --secs 90
    python docs/analysis-2026-09-22/ab_drop.py --no-motion   # 只测静止态（应 0 丢）

退出码: 0 = 通过（不丢且确实在动）; 1 = 有丢; 2 = 通道/前置条件不满足
"""
import argparse
import json
import statistics as st
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"


def get(path, timeout=8):
    return json.load(urllib.request.urlopen(BASE + path, timeout=timeout))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=60.0)
    ap.add_argument("--no-motion", action="store_true",
                    help="不主动起跑（只测当前状态）")
    ap.add_argument("--min-ap", type=float, default=100.0,
                    help="反向判据：运动态最小 ap_hz（默认 100）")
    a = ap.parse_args()

    try:
        get("/state")
    except Exception as e:
        print("!! 连不上 bridge（%s）—— 先把它跑起来" % e)
        return 2

    if not a.no_motion:
        # ★ 起跑（声明运动源）+ 归零坐标系，确保处于"运动态"这个**丢条只在此时发生**的条件
        try:
            get("/zero")
            time.sleep(0.4)
            get("/motion?on=1&src=program&A=3000&slope=30000")
        except Exception as e:
            print("!! 起跑失败: %s" % e)
            return 2
        time.sleep(3)

    d0 = get("/state")
    t0 = time.time()
    rows = []
    while time.time() - t0 < a.secs:
        d = get("/state")
        rows.append(d)
        time.sleep(1.0)
    d1 = rows[-1]
    dt = time.time() - t0

    def col(k):
        return [r[k] for r in rows if r.get(k) is not None]

    tick_span = (col("tick")[-1] - col("tick")[0])
    dev_dt = tick_span * 1e-4                       # ★ 用设备拍校准窗口（不用 PC 墙钟）
    d_drop = (col("drop")[-1] - col("drop")[0])
    drop_hz = d_drop / max(1e-9, dev_dt)
    evt_hz = st.median(col("evt_rate"))
    rate_hz = st.median(col("rate"))
    ap_med = st.median(col("ap_hz"))
    per_round = evt_hz / max(1e-9, rate_hz)

    print("=" * 70)
    print("上传丢条率验收（窗口 %.1f s PC / %.1f s 设备拍 ⇒ 偏差 %.1f%%）"
          % (dt, dev_dt, abs(dev_dt - dt) / max(1e-9, dt) * 100))
    print("=" * 70)
    print("  轮询率      : %.1f Hz" % rate_hz)
    print("  读到(消费)  : %.0f 条/s" % evt_hz)
    print("  每轮读走    : %.1f 条  （上限 = DELTA_READ_MAX）" % per_round)
    print("  Δdrop       : %d 条 ⇒ **%.1f 条/s**" % (d_drop, drop_hz))
    print("  ★ 产出(推)  : %.0f 条/s  = 消费 + 丢弃" % (evt_hz + drop_hz))
    print("  ap_hz 中位  : %.0f Hz" % ap_med)
    top = d1.get("ch_top") or []
    if top:
        print("  产出前 3 通道: " + ", ".join(
            "%s %.0f/s" % (x["name"], x["hz"]) for x in top[:3]))
    print("-" * 70)

    ok_drop = (d_drop == 0)
    ok_motion = (ap_med >= a.min_ap) or a.no_motion
    print("  ① 正判据  Δdrop==0        : %s（%d）"
          % ("PASS" if ok_drop else "FAIL", d_drop))
    print("  ② 反向判据 确实在运动      : %s（ap 中位 %.0f，阈值 %.0f）"
          % ("PASS" if ok_motion else "FAIL", ap_med, a.min_ap))
    if not ok_motion:
        print("     ★ 电机几乎没动 ⇒ 这个窗口**测不出上传能力**，"
              "「不丢」是必然成立的假绿（静止产出 150 条/s ≪ 上限）。")
    print("=" * 70)
    if ok_drop and ok_motion:
        print("结论: 通过 —— 运动态下一条不丢（丢率 0 条/s，产出 %.0f 条/s）" % (evt_hz))
        return 0
    if not ok_drop:
        print("结论: **仍丢 %.1f 条/s（%.1f%%）** ⇒ 上传能力仍不足" % (drop_hz,
              drop_hz / max(1e-9, evt_hz + drop_hz) * 100))
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
