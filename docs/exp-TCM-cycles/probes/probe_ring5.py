#!/usr/bin/env python3
"""同进程对照: 全程不发 0x38(硬编码 SHM) vs 插一次 0x38 —— 看 tick/环写是否会掉。

★ 硬编码 SHM=0x20004E80 是刻意的: 取 shm 的唯一办法是读 0x38,
  所以"不发 0x38"就必须硬编码 —— 否则无法把 0x38 这个变量隔离出来。
"""
import os, struct, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from h723_client import Dcl

SHM = 0x20004E80          # ← 实测固定值
OP_PID, SRC_CONST, DST_WIRE, ACTIVE = 0x05, 2, 2, 1


def mk(op, div, n):
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  ACTIVE, i, (i % 64) + 1, 0, 0, div, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    return (struct.pack("<HHH", n, n, min(n, 64) + 1) + routes + params
            + b"\x00" * (16 * (min(n, 64) + 1)) + b"\x00" * 16)


d = Dcl(os.environ.get("DCL_PORT") or None)
print("端口 =", d.port)
prev = None


def r22(tag):
    global prev
    sts, q = d.send(0x22, struct.pack("<IH", SHM + 0x3880, 2), expect_len=None)
    w, tk = struct.unpack("<2I", q[:8])
    dt = (tk - prev[1]) if prev else 0
    dw = (w - prev[0]) if prev else 0
    print("   %-26s 环写=%-9d tick=%-10d  Δ环写=%-6d Δtick=%-6d %s"
          % (tag, w, tk, dw, dt, "★掉" if (dw < 0 or dt < 0) else ""))
    prev = (w, tk)
    return w, tk


print("\n=== 阶段 1: 全程只发 0x22, 一次 0x38 都不发 ===")
for i in range(5):
    r22("只 0x22 #%d" % i)
    time.sleep(0.3)

print("\n=== 阶段 2: 插一次 0x38, 然后继续只发 0x22 ===")
sts, p = d.send(0x38, expect_len=51)
smp = struct.unpack("<I", p[:4])[0]
print("   >>> 发了一次 0x38, samples=%d" % smp)
for i in range(5):
    r22("0x38 后 #%d" % i)
    time.sleep(0.3)

print("\n=== 阶段 3: deploy + STOP/START(不发 0x38), 再静默 1.5 s ===")
d.send(0x12); time.sleep(0.15); d.send(0x11); time.sleep(0.15)
sts, pp = d.send(0x10, mk(OP_PID, 1, 128), expect_len=None)
print("   deploy:", sts)
d.send(0x12); time.sleep(0.15); d.send(0x11)
print("   （已 START，静默 1.5 s，期间不发任何帧）")
time.sleep(1.5)
r22("静默后 #0")
time.sleep(0.3)
r22("静默后 #1")
d.close()
