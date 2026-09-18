#!/usr/bin/env python3
# OVL-1c: ov=1 是"瞬态"还是"持续超载"?
#   n=72 时 emax=17118 > 门=16000 而 ov 只有 1 ⇒ 两种解释:
#     (a) 只有**热重载那一拍**超了 (reload 让该拍 +~720 tick) ⇒ 持续态其实在门下
#     (b) 持续超载但 ov 计数有 bug
#   切法: 部署后**隔一段时间连读 ov** ——
#     保持 1 不动 ⇒ (a);  随时间线性增长 ⇒ (b)
import os, struct, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from h723_client import Dcl

OP_PID = 0x05
SRC_CONST, DST_WIRE, ACTIVE = 2, 2, 1
TB_BUDGET = 16000


def mk(n):
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, OP_PID,
                                  ACTIVE, i, 1, 0, 0, 0, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    return struct.pack("<HHH", n, n, 1) + routes + params + b"\x00" * 16


def st(dcl):
    s, p = dcl.send(0x38, expect_len=51)
    if s != "ACK" or len(p) < 51:
        return None
    d = struct.unpack("<IIIII", p[:20])
    return dict(samples=d[0], pmin=d[1], pmax=d[2], emin=d[3], emax=d[4],
                ov=struct.unpack("<I", p[27:31])[0])


dcl = Dcl(os.environ.get("DCL_PORT") or None)
print("port", dcl.port); time.sleep(1.0)
try:
    for n in (72, 80):
        dcl.send(0x13); time.sleep(0.2); dcl.send(0x11); time.sleep(0.2)
        for _ in range(3):
            s, p = dcl.send(0x10, mk(n), expect_len=6)
            if s == "ACK":
                break
            time.sleep(0.4)
        print("\n=== n=%d  (deploy %s) ===" % (n, s))
        t0 = time.time()
        for k in range(7):
            time.sleep(1.5)
            r = st(dcl)
            if not r:
                print("  <读不到>"); break
            dt = time.time() - t0
            print("  t=%4.1fs  samples=%-7d emax=%-6d ov=%-6d  (ov/samples = %.4f%%)"
                  % (dt, r["samples"], r["emax"], r["ov"], 100.0 * r["ov"] / max(r["samples"], 1)))
finally:
    dcl.send(0x12); time.sleep(0.2)
    dcl.send(0x13); time.sleep(0.3)
    dcl.send(0x11); time.sleep(0.3)
    print("\n收尾 ok")
    dcl.close()
