#!/usr/bin/env python3
"""最后一个判别: STOP/START 之后**静默 1.5 s**，看计数是否被打回。
对照: 同一序列但只静默 0.05 s。"""
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
d.send(0x12); time.sleep(0.15); d.send(0x11); time.sleep(0.15)
sts, pp = d.send(0x10, mk(OP_PID, 1, 128), expect_len=None)
print("deploy:", sts, "SHM=0x%08X" % shm)


def hd(tag):
    sts, q = d.send(0x22, struct.pack("<IH", shm + 0x3880, 2), expect_len=None)
    w, tk = struct.unpack("<2I", q[:8])
    print("  %-28s 环写=%-8d tick=%d" % (tag, w, tk))
    return w


for settle in (0.05, 1.5, 1.5):
    print("\n=== STOP/START → 静默 %.2f s ===" % settle)
    d.send(0x12); time.sleep(0.15)
    d.send(0x11)
    time.sleep(settle)
    hd("静默后第一次读")
    time.sleep(0.2)
    hd("再等 0.2 s 后")
d.close()
