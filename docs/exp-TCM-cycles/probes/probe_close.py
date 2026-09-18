#!/usr/bin/env python3
"""测: **关闭串口**是否会把板子按进复位循环（下一个进程继承坏状态）。

同一个进程里 open→读→close 三轮, 看每轮刚打开时的 tick:
  · 第 1 轮 tick 很大、之后突然变小  ⇒ 是"上一轮 close"造成的 ⇒ 关口的 DTR/RTS 嫌疑
  · 每轮都是小值                    ⇒ 是持续性的, 与 close 无关
"""
import os, struct, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from h723_client import Dcl

SHM = 0x20004E80


def one(tag):
    d = Dcl(os.environ.get("DCL_PORT") or None)
    time.sleep(0.4)
    try:
        sts, q = d.send(0x22, struct.pack("<IH", SHM + 0x3880, 2), expect_len=None)
        w, tk = struct.unpack("<2I", q[:8]) if sts == "ACK" and len(q) >= 8 else (-1, -1)
        sts2, s = d.send(0x38, expect_len=51)
        smp = struct.unpack("<I", s[:4])[0] if sts2 == "ACK" else -1
        print("  %-22s 环写=%-9d tick=%-10d samples=%d" % (tag, w, tk, smp))
        time.sleep(0.5)
        sts, q = d.send(0x22, struct.pack("<IH", SHM + 0x3880, 2), expect_len=None)
        w2, tk2 = struct.unpack("<2I", q[:8])
        print("  %-22s 环写=%-9d tick=%-10d   Δtick=%+d"
              % ("  0.5s 后", w2, tk2, tk2 - tk))
    finally:
        d.close()


for i in range(3):
    print("\n=== 第 %d 轮 open→读→close ===" % (i + 1))
    one("刚打开")
    print("  (已 close, 等 1.5 s 再开下一轮)")
    time.sleep(1.5)
