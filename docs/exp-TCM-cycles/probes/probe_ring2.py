#!/usr/bin/env python3
"""E4 取数异常判别（一次性探针）。

同时读 **环写计数** 与 **0x38.samples（RUN 拍数）**:
  · 两者同步推进        ⇒ 环没问题 ⇒ 异常在读数/窗口侧
  · samples 涨、环不涨  ⇒ **环写被条件跳过**（找条件）
  · 两者都停            ⇒ 引擎停了（自愈复位 / RUN 掉了）
"""
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


def snap(tag):
    sts, q = d.send(0x22, struct.pack("<IH", shm + 0x3880, 2), expect_len=None)
    w, tk = struct.unpack("<2I", q[:8]) if sts == "ACK" and len(q) >= 8 else (-1, -1)
    sts, s = d.send(0x38, expect_len=51)
    smp = struct.unpack("<I", s[:4])[0] if sts == "ACK" else -1
    nr = struct.unpack("<H", s[20:22])[0] if sts == "ACK" else -1
    print("  %-22s 环写=%-8d tick=%-9d samples=%-8d n_routes=%d" % (tag, w, tk, smp, nr))
    return w, tk, smp


print("\n[A] 部署前")
a0 = snap("baseline")

print("\n[B] deploy 128×PID div1")
d.send(0x12); time.sleep(0.15); d.send(0x11); time.sleep(0.15)
sts, pp = d.send(0x10, mk(OP_PID, 1, 128), expect_len=None)
print("  deploy:", sts, struct.unpack("<HI", pp[:6])[1] if sts == "ACK" else pp[:20])

print("\n[C] STOP → START（stats_reset 清环写计数）")
d.send(0x12); time.sleep(0.15)
d.send(0x11); time.sleep(0.02)
c0 = snap("START 后 ~20ms")

print("\n[D] 连续观察（每 0.5 s 一次，共 3 次）")
prev = c0
for i in range(3):
    time.sleep(0.5)
    cur = snap("+%.1fs" % (0.5 * (i + 1)))
    print("        Δ环写=%-7d Δsamples=%-7d  (Δ环写/Δsamples=%.3f)"
          % (cur[0] - prev[0], cur[2] - prev[2],
             (cur[0] - prev[0]) / (cur[2] - prev[2]) if cur[2] != prev[2] else 0))
    prev = cur
d.close()
