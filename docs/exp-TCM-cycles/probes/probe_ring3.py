#!/usr/bin/env python3
"""环写计数会不会**自己掉**？连续读 12 次（只读 2 个字，不做大读）。
掉 ⇒ 有周期性清零；不掉 ⇒ 是"大读"或别的动作触发的。"""
import os, struct, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from h723_client import Dcl

OP_PID, SRC_CONST, DST_WIRE, ACTIVE = 0x05, 2, 2, 1


def mk(op, div, n):
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  ACTIVE, i, (i % 64) + 1, 0, 0, div, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    return (struct.pack("<HHH", n, n, min(n, 64) + 1) + routes + params
            + b"\x00" * (16 * (min(n, 64) + 1)) + b"\x00" * 16)


d = Dcl(os.environ.get("DCL_PORT") or None)
sts, p = d.send(0x38, expect_len=51)
shm = struct.unpack("<I", p[23:27])[0]
print("SHM=0x%08X" % shm)
d.send(0x12); time.sleep(0.15); d.send(0x11); time.sleep(0.15)
sts, pp = d.send(0x10, mk(OP_PID, 1, 128), expect_len=None)
print("deploy:", sts)
d.send(0x12); time.sleep(0.15); d.send(0x11); time.sleep(0.05)
print("\n连续读环头（每 0.2 s 一次）:")
prev = None
for i in range(12):
    sts, q = d.send(0x22, struct.pack("<IH", shm + 0x3880, 2), expect_len=None)
    w, tk = struct.unpack("<2I", q[:8])
    mark = ""
    if prev is not None:
        dlt = w - prev
        mark = "  Δ=%+d%s" % (dlt, "   ★★★ 掉了！" if dlt < 0 else "")
    print("  #%-2d 环写=%-7d tick=%-9d%s" % (i, w, tk, mark))
    prev = w
    time.sleep(0.2)
print("\n现在做一次**大读**（256 字 = 2 块），然后立刻再读头:")
sts, q = d.send(0x22, struct.pack("<IH", shm + 0x3890, 200), expect_len=None)
print("  第1块:", sts, len(q))
sts, q2 = d.send(0x22, struct.pack("<IH", shm + 0x3890 + 800, 56), expect_len=None)
print("  第2块:", sts, len(q2))
for i in range(3):
    sts, q = d.send(0x22, struct.pack("<IH", shm + 0x3880, 2), expect_len=None)
    w, tk = struct.unpack("<2I", q[:8])
    print("  大读后 #%d 环写=%-7d tick=%-9d  Δ=%+d" % (i, w, tk, w - prev))
    prev = w
    time.sleep(0.2)
d.close()
