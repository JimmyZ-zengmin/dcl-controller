import os, struct, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from h723_client import Dcl

OP_PID = 0x05
SRC_CONST, DST_WIRE, ACTIVE = 2, 2, 1


def mk(n):
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, OP_PID,
                                  ACTIVE, i, 1, 0, 0, 0, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    return struct.pack("<HHH", n, n, 1) + routes + params + b"\x00" * 16


dcl = Dcl(os.environ.get("DCL_PORT") or None)
print("port", dcl.port); time.sleep(1.2)

s, p = dcl.send(0x01, expect_len=4)
print("0x01 ->", s, p.hex() if p else None)

s, p = dcl.send(0x38, expect_len=51)
print("0x38 ->", s, "len", len(p))

print("\n--- deploy with expect_len=None (看到底回什么) ---")
for n in (4, 32, 59, 60):
    pay = mk(n)
    t0 = time.time()
    s, p = dcl.send(0x10, pay, expect_len=None)
    print("  N=%-4d len=%-5d -> sts=%-8s len=%-3d %.2fs  %r"
          % (n, len(pay), s, len(p), time.time() - t0, p[:40]))
    time.sleep(0.5)

s, p = dcl.send(0x38, expect_len=51)
print("0x38 after ->", s, "len", len(p))
dcl.close()
