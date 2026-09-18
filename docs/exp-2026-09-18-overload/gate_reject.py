#!/usr/bin/env python3
# 静态门"拒绝路径"首次实测 —— 路径成本价修复后才可能做这件事
#
# 背景: 修复前, 静态门对**任何合法载荷都不可达**(最重合法 18560 < 门 26000), 所以它的
#   "拒绝路径"从来没被走到过 —— 一条从没被触发过的判据不能信。
#   修复后 FLASH 档 (BOOT_SEL=0) 的预算 = N × ceil(145×303/100) = N × 440:
#       N=59 ⇒ 25960 ≤ 26000  ⇒ 该 ACK
#       N=60 ⇒ 26400 >  26000  ⇒ 该 **NAK "exec budget exceeded"**  ★首次
#
# 判据 (每条都能失败):
#   P1 门下方 ACK 且 budget 读回 == N×440 (成本模型在 FLASH 档也自洽)
#   P2 ★ 门上方 NAK, 理由 **正是** "exec budget exceeded" (不是别的门先拦)
#   P3 被拒那次无副作用 (n_routes 不变, deploy_ok 不涨)
import os, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from h723_client import Dcl  # noqa: E402

OP_PID = 0x05
SRC_CONST, DST_WIRE, ACTIVE = 2, 2, 1
PER_ROUTE = 440          # ceil(145 × 303 / 100)
GATE = 26000


def mk(n):
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, OP_PID,
                                  ACTIVE, i, 1, 0, 0, 0, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    return struct.pack("<HHH", n, n, 1) + routes + params + b"\x00" * 16


def st(dcl, tries=4):
    for _ in range(tries):
        s, p = dcl.send(0x38, expect_len=51)
        if s == "ACK" and len(p) >= 51:
            d = struct.unpack("<IIIII", p[:20])
            return dict(n_routes=struct.unpack("<H", p[20:22])[0], emax=d[4],
                        ov=struct.unpack("<I", p[27:31])[0])
        time.sleep(0.3)
    return None


def dep(dcl, n, tries=3):
    for _ in range(tries):
        # ★ expect_len 必须为 None: NAK 的理由长度随门而变
        #   (`"exec budget exceeded"` = 20 B, 不是 ACK 的 6 B)。写死 6 ⇒ 应答被丢弃 3 次 ⇒ 假 TIMEOUT。
        s, p = dcl.send(0x10, mk(n), expect_len=None)
        if s == "ACK" and len(p) >= 6:
            return ("ACK", struct.unpack("<HI", p[:6])[1], None)
        if s == "NAK":
            return ("NAK", None, p.decode("utf-8", "replace"))
        time.sleep(0.5)
    return ("TIMEOUT", None, None)


def main():
    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("port", dcl.port); time.sleep(1.2)
    res = []
    try:
        print("\n每受试路数: 期望预算 = N × %d ；门 = %d" % (PER_ROUTE, GATE))
        for n in (32, 58, 59, 60, 64, 128):
            exp = n * PER_ROUTE
            tag = "该 ACK" if exp <= GATE else "★该 NAK"
            s0 = st(dcl)
            before = s0["n_routes"] if s0 else -1
            r = dep(dcl, n)
            time.sleep(0.4)
            s1 = st(dcl)
            after = s1["n_routes"] if s1 else -1
            if r[0] == "ACK":
                ok = (r[1] == exp)
                print("  N=%-4d 期望预算 %-6d (%s) ⇒ ACK budget=%-6d %s   n_routes %d→%d"
                      % (n, exp, tag, r[1], "✅" if ok else "❌不符", before, after))
                res.append(("N=%d ACK budget 命中" % n, ok))
            elif r[0] == "NAK":
                ok = (r[2] == "exec budget exceeded")
                print("  N=%-4d 期望预算 %-6d (%s) ⇒ NAK %r %s   n_routes %d→%d"
                      % (n, exp, tag, r[2], "✅" if ok else "❌理由不对", before, after))
                res.append(("N=%d NAK 理由正确" % n, ok))
                res.append(("N=%d 拒绝无副作用" % n, before == after))
            else:
                print("  N=%-4d 期望预算 %-6d (%s) ⇒ TIMEOUT" % (n, exp, tag))
                res.append(("N=%d 有应答" % n, False))
    finally:
        dcl.send(0x12); time.sleep(0.2); dcl.send(0x13); time.sleep(0.3)
        dcl.send(0x11); time.sleep(0.3)
        print("\n收尾 ok")
        dcl.close()
    print("\n=== 汇总 ===")
    for k, v in res:
        print("  %-28s %s" % (k, "PASS" if v else "FAIL"))
    return 0 if all(v for _, v in res) else 1


if __name__ == "__main__":
    sys.exit(main())
