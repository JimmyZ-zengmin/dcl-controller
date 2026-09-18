import os, struct, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from h723_client import Dcl

dcl = Dcl(os.environ.get("DCL_PORT") or None)
print("port", dcl.port); time.sleep(1.0)
print("--- 0x39 op=17..23 的应答长度 (expect_len=None) ---")
for op in range(17, 24):
    s, p = dcl.send(0x39, struct.pack("<B", op))
    print("  op=%-3d sts=%-8s len=%-4d %s" % (op, s, len(p), p[:16].hex() if p else ""))
    time.sleep(0.15)
print("--- 0x38 前 32 字节 (pmin/pmax 的权威来源) ---")
s, p = dcl.send(0x38)
if s == "ACK" and len(p) >= 31:
    d = struct.unpack("<IIIII", p[:20])
    print("  samples=%d pmin=%d pmax=%d emin=%d emax=%d  ov=%d"
          % (d[0], d[1], d[2], d[3], d[4], struct.unpack("<I", p[27:31])[0]))
else:
    print("  0x38 sts=%s len=%d" % (s, len(p)))
dcl.close()
