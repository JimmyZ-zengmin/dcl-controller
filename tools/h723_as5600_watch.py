#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_as5600_watch.py — 记录 AS5600 角度随时间的变化（转轴跟随性验证）

用法:  python h723_as5600_watch.py [秒数]

判据（都能失败）
----------------
· **跟随**: 转轴时应看到 raw 连续变化；不动时应**稳定**(不抖)
· **方向一致**: 单向转轴 ⇒ 相邻增量方向应一致（同号占比高）
  ★ 用**环形增量** `((d + 2048) % 4096) - 2048` 处理 0↔4095 回绕
· **不丢帧/不误码**: `err_n` 不涨、`i2c nak` 不涨
· **读数稳定性**: 静止时的不同取值个数（应为个位数）
"""
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from h723_client import Dcl, find_board   # noqa: E402

C = 0x39


def rd(d):
    sts, p = d.send(C, bytes([0x12]))
    if sts != "ACK" or len(p) < 40:
        return None
    w = struct.unpack("<10I", p[:40])
    return dict(raw=w[0], deg=w[1] / 1000.0, status=w[2], md=w[3],
                ok=w[4], err=w[5], lasterr=w[6], tx=w[7], i2cok=w[8], nak=w[9])


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    d = Dcl(find_board(), wait=0.6)
    try:
        print("=" * 74)
        print("AS5600 角度记录  时长 %.0fs   —— 现在可以转轴" % secs)
        print("=" * 74)
        a = rd(d)
        if a is None:
            print("✗ op=18 无应答")
            return
        t0 = time.time()
        prev = a["raw"]
        rows = [(0.0, prev, a["deg"])]
        deltas = []
        while time.time() - t0 < secs:
            r = rd(d)
            if r is None:
                continue
            if r["raw"] != prev:
                dt = time.time() - t0
                ring = ((r["raw"] - prev + 2048) % 4096) - 2048
                deltas.append(ring)
                rows.append((dt, r["raw"], r["deg"]))
                print("  %6.2fs  raw=%4d  %8.2f°   Δ=%+5d" % (dt, r["raw"], r["deg"], ring))
                prev = r["raw"]
            time.sleep(0.002)

        b = rd(d)
        print()
        print("=" * 74)
        print("统计")
        print("=" * 74)
        vals = [x[1] for x in rows]
        print("  采样变化次数        : %d" % len(deltas))
        print("  raw 取值范围        : %d .. %d  (跨度 %d = %.1f°)"
              % (min(vals), max(vals), max(vals) - min(vals), (max(vals) - min(vals)) * 360.0 / 4096.0))
        print("  恰好跟满了多少度    : 见上（>0 说明轴被转动过）")
        if deltas:
            pos = sum(1 for x in deltas if x > 0)
            neg = sum(1 for x in deltas if x < 0)
            same = max(pos, neg) / float(len(deltas)) * 100.0
            print("  增量方向            : 正 %d / 负 %d  ⇒ 单向占比 %.1f%%"
                  % (pos, neg, same))
            print("  增量幅度            : 中位 %+d LSB (%.1f°)  最大 %+d LSB (%.1f°)"
                  % (sorted(deltas)[len(deltas) // 2],
                     sorted(deltas)[len(deltas) // 2] * 360.0 / 4096.0,
                     max(deltas, key=abs), max(deltas, key=abs) * 360.0 / 4096.0))
        print("  AS5600 ok_n         : %d → %d  (+%d)" % (a["ok"], b["ok"], b["ok"] - a["ok"]))
        print("  AS5600 err_n        : %d → %d  %s" % (a["err"], b["err"],
              "✓ 无错" if b["err"] == a["err"] else "✗ **读失败过**"))
        print("  I2C nak             : %d → %d  %s" % (a["nak"], b["nak"],
              "✓ 无 NAK" if b["nak"] == a["nak"] else "✗ 有 NAK"))
        print("  STATUS              : 0x%02X   MD(磁铁OK)=%d" % (b["status"], b["md"]))
        print()
        if not deltas:
            print("  ⇒ 角度**没变**：轴没转（或转得极小）。静止读数稳定是好事。")
        elif len(vals) > 2 and max(vals) - min(vals) > 40:
            print("  ⇒ ✅ 角度**跟着轴在变**，且 err=0 / nak=0 ⇒ 跟随性成立。")
        else:
            print("  ⇒ ⚠ 有变化但幅度很小，可能是抖动或微动，需再看。")
    finally:
        d.close()


if __name__ == "__main__":
    main()
