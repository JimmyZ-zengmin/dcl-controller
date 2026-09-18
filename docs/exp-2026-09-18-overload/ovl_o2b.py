#!/usr/bin/env python3
# OVL-2 补测: O2b 为什么 TIMEOUT 而不是 NAK —— 配正对照切开
#   假设 A: 客户端用法问题 (NAK 载荷长度与 expect_len 不符被当成 TIMEOUT)
#   假设 B: 已知的"大 deploy 偶发停答" (2~4KB 载荷偶发静默、几十秒自愈)
#   假设 C: op=0xFF 这条路径本身有问题
#
# 判据 (每条都能失败):
#   C1 小载荷 (1 条, op=0xFF) -> NAK "bad op"      (正对照: 坏的 op 确实会被拒)
#   C2 小载荷 (1 条, op=0x05) -> ACK               (正对照: 好的 op 确实会过)
#   C3 大载荷 (128 条, op=0xFF) -> NAK "bad op"    (即 O2b 复现)
#   若 C1/C2 PASS 而 C3 TIMEOUT ⇒ 支持假设 B (与 op 无关, 与**载荷大小**有关)
import os, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from h723_client import Dcl, engine_status  # noqa: E402

CMD_DEPLOY = 0x10
OP_PID = 0x05
SRC_CONST, DST_WIRE, ACTIVE = 2, 2, 1


def mk(n, op, div=0):
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  ACTIVE, i, (1 if op == OP_PID else 0), 0, 0, div, 0)
                      for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for i in range(n))
    ns = 1 if op == OP_PID else 0
    return struct.pack("<HHH", n, n, ns) + routes + params + b"\x00" * 16 * ns


def go(dcl, payload, label, elen=6):
    t0 = time.time()
    sts, p = dcl.send(CMD_DEPLOY, payload, expect_len=elen)
    dt = time.time() - t0
    tag = "PASS" if (sts in ("ACK", "NAK")) else "TIMEOUT"
    why = p.decode("utf-8", "replace") if sts == "NAK" else (
        struct.unpack("<HI", p[:6]) if (sts == "ACK" and len(p) >= 6) else p[:20])
    print("  [%-7s] %-30s len=%-5d %.2fs  %s" % (tag, label, len(payload), dt, why))
    return sts, why


def main():
    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("端口 = %s" % dcl.port)
    out = []
    try:
        print("\n--- 正对照: 小载荷 ---")
        s1, w1 = go(dcl, mk(1, 0xFF), "C1 1条 op=0xFF")
        out.append(("C1", s1 == "NAK" and w1 == "bad op"))
        s2, w2 = go(dcl, mk(1, OP_PID), "C2 1条 op=PID")
        out.append(("C2", s2 == "ACK"))

        print("\n--- 被测: 大载荷 ---")
        s3, w3 = go(dcl, mk(128, 0xFF), "C3 128条 op=0xFF (O2b 复现)")
        out.append(("C3", s3 == "NAK" and w3 == "bad op"))

        print("\n--- 若 C3 仍 TIMEOUT: 立刻探活, 看板子是否活着 ---")
        if s3 == "TIMEOUT":
            for i in range(3):
                time.sleep(0.5)
                st, _ = dcl.send(0x01, expect_len=4)
                print("    探活 %d: %s" % (i + 1, st))
            print("    (若探活 ACK ⇒ 板子活着 ⇒ 是**那一条应答**丢了, 支持假设 B)")
            s3b, w3b = go(dcl, mk(128, 0xFF), "C3b 128条 op=0xFF 重试")
            out.append(("C3b", s3b == "NAK" and w3b == "bad op"))
    finally:
        dcl.send(0x12); dcl.send(0x11); time.sleep(0.3)
        st = engine_status(dcl)
        print("\n收尾: n_routes=%s run=%s ov=%s" % (st["n_routes"], st["run"], st["ov"]))
        dcl.close()

    print("\n=== 汇总 ===")
    for k, v in out:
        print("  %-5s %s" % (k, "PASS" if v else "FAIL"))
    return 0 if all(v for _, v in out) else 1


if __name__ == "__main__":
    sys.exit(main())
