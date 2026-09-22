#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演示: 起一段真实运动, 同时用 recorder 记录 —— 证明记录文件"能拿来分析"。

按项目纪律走（`MEMORY.md` §5.15 / §〇 第 6 条）:
  ① 开工先 `sub=5 arg=1` 声明 ENA 极性, 再 `sub=11` **读回**确认(不假设"设了就算");
  ② 使能后**断言 pe9_actual == 1**(判物理状态看引脚实读, 不看逻辑位);
  ③ 起脉冲前先设**限时**(安全网: 到点自动 step_stop_safe);
  ④ 收尾 `sub=6`。
"""
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))   # tools/hostsim/ → 仓库根
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, HERE)                                  # recorder.py 就在同目录

from h723_client import Dcl, find_board                      # noqa: E402
from recorder import Rec, COLS, TICK_US_EXPECT, MAGIC_EXPECT  # noqa: E402

HZ = float(os.environ.get("DEMO_HZ", 1500))
SLOPE = int(os.environ.get("DEMO_SLOPE", 12000))
LIMIT_MS = 8000          # 安全网: 最多 8 s
REC_SECS = 5.0
REC_HZ = 5.0


def op19(d, sub, arg=0):
    return d.send(0x39, struct.pack("<BBI", 19, sub, arg), expect_len=None)


def st11(d):
    s, p = d.send(0x39, struct.pack("<BBI", 19, 11, 0), expect_len=32)
    if s != "ACK" or len(p) < 32:
        return None
    u = lambda o: struct.unpack("<I", p[o:o + 4])[0]
    return dict(rc_last=u(0), pol_set=u(4), pol=u(8), stop_hold=u(12),
                rej_n=u(16), mismatch_n=u(20),
                pe9_intent=u(24), pe9_actual=u(28))


def main():
    d = Dcl(os.environ.get("DCL_PORT") or find_board())
    print("  端口 %s" % d.port)
    try:
        # ── ① 声明极性 + **读回**（不假设"设了就算"）──
        op19(d, 5, 1)
        s = st11(d)
        print("  ① 极性: pol_set=%s pol=%s  (期望 1/1)" % (s["pol_set"], s["pol"]))
        assert s["pol_set"] == 1 and s["pol"] == 1, "极性没设上 —— '设了就算'必错, 停"

        # ── ② 使能 + **断言引脚实读** ──
        op19(d, 3, 1)
        s = st11(d)
        print("  ② 使能: pe9_intent=%d pe9_actual=%d  mismatch_n=%d  (期望 1/1)"
              % (s["pe9_intent"], s["pe9_actual"], s["mismatch_n"]))
        assert s["pe9_actual"] == 1, "PE9 实读不是 1 ⇒ 驱动器没使能, 停"

        # ── ③ 限时(安全网) + 方向 + 频率 ──
        op19(d, 4, LIMIT_MS)
        op19(d, 2, 0)
        op19(d, 13, 0)          # 运动源 = 脚手架直控 (0=默认)
        print("  ③ 起脉冲: %g Hz (斜坡 %d, 限时 %d ms)" % (HZ, SLOPE, LIMIT_MS))
        op19(d, 1, int(HZ))
        time.sleep(0.4)         # 等它起来

        # ── ④ 记录（reuse recorder 的采样器）──
        r = Rec(d, pitch=8.0, hz=REC_HZ, slow_every=5)
        r.shm = struct.unpack("<I", d.send(0x38, expect_len=51)[1][23:27])[0]
        e = r.fetch_manifest()
        r.off_ctrl = e["addr"]
        r.ctrl_words = max(4, e["words"])
        print("  g_shm=0x%08X  SHM_CTRL=0x%08X" % (r.shm, r.off_ctrl))

        rows, t0 = [], time.time()
        while time.time() - t0 < REC_SECS:
            row, ev = r.sample()
            rows.append(row)
            if ev:
                print("  ★ 事件 @tick=%d %s" % (ev["tick"],
                      {k: v for k, v in ev.items() if k.startswith("d_") and v}))
            time.sleep(max(0.0, 1.0 / REC_HZ))
        print("  ④ 采到 %d 点" % len(rows))

        # ── ⑤ 停 ──
        op19(d, 6)
        s = st11(d)
        print("  ⑤ 已停: pe9_actual=%d mismatch_n=%d rej_n=%d"
              % (s["pe9_actual"], s["mismatch_n"], s["rej_n"]))
    finally:
        try:
            op19(d, 1, 0)
            op19(d, 6)
        except Exception:
            pass
        d.close()

    if len(rows) < 3:
        print("!! 点太少, 不写文件")
        return 2

    # ── 分析: 用**板子口径**算的 vel 到底能不能看出运动 ──
    print("\n  ══ 用记录文件自身算（不是另采一遍）══")
    raw0, raw1 = rows[0]["SENSOR0"], rows[-1]["SENSOR0"]
    pos = [x["pos_mm"] for x in rows]
    vel = [x["vel_mm_s"] for x in rows[1:]]
    span_tick = rows[-1]["tick"] - rows[0]["tick"]
    print("  raw     : %d → %d" % (raw0, raw1))
    print("  pos_mm  : %.3f → %.3f  (跨度 %.3f mm)" % (pos[0], pos[-1], pos[-1] - pos[0]))
    print("  板子时长: %d 拍 = %.3f s" % (span_tick, span_tick * TICK_US_EXPECT / 1e6))
    print("  vel_mm_s: min %.2f  max %.2f  中位 %.2f" %
          (min(vel), max(vel), sorted(vel)[len(vel) // 2]))
    # ★ 板子口径的平均速度 vs 「总位移 / 总板子时长」—— 两者必须一致（同口径自洽判据）
    v_avg_tick = (pos[-1] - pos[0]) / (span_tick * TICK_US_EXPECT / 1e6)
    v_avg_pc = (pos[-1] - pos[0]) / (float(rows[-1]["pc_t"]) - float(rows[0]["pc_t"]))
    print("  平均速度(板子 tick 口径) = %.2f mm/s" % v_avg_tick)
    print("  平均速度(PC 墙钟口径)    = %.2f mm/s   ← ★ 对比看: 口径不同, 数就不同" % v_avg_pc)
    print("  偏差 %.1f%%" % (abs(v_avg_tick - v_avg_pc) / max(1e-9, abs(v_avg_tick)) * 100))

    import csv
    out = os.environ.get("DEMO_OUT") or os.path.join(os.getcwd(), "demo_motion.csv")
    for row in rows:
        for c in COLS:
            row.setdefault(c, "")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    print("\n  → %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
