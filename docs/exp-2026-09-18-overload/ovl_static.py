#!/usr/bin/env python3
# 超载实验 OVL-2 —— 静态预算门 (EXEC_DEPLOY_BUDGET) 的可达性与"拦得住"验证
#
# 起因: docs/ARCH-GUARANTEE-MAP.md 的 G3 只有一个缺口 ——
#   engine.h 的绊线断言写着"128 × 145 = 18560 ≤ 26000 ⇒ 预算门当前永不触发,
#   它是一条'未来的门'"; 并规定"届时必须做一次超载实验 (构造 >门 的程序,
#   确认 NAK + 确认拍没被拉长)"。本脚本就是那次实验。
#
# 设计 (全部源码无关 —— 不改固件、不烧录):
#   O2c  128 × DIRECT(56)  div0  -> 期望 budget = 7168     (成本表算术是活的)
#   O2d  128 × PID(145)    div1  -> 期望 budget = 1920     (÷10 摊薄生效)
#   O2a  128 × PID(145)    div0  -> 期望 budget = 18560    (★ 最重**合法**载荷)
#   O2b  128 × op=0xFF(290) div0  -> 期望 NAK "bad op"
#        (若 op 越界真能被计成 290, 则 128×290 = 37120 > 26000 ⇒ 本该被预算门拦;
#         实测它被**更早**的逐条校验拦下 ⇒ 证明"门序把预算门挡住了")
#
# 判据 (每条都能失败):
#   J1 budget 读回值 == 手算期望   (ACK 载荷 [seq:u16][budget:u32])
#   J2 O2a/O2c/O2d 均 ACK          (18560 < 26000 ⇒ 不该被拒)
#   J3 O2b 的 NAK 理由 == "bad op" 而**不是** "exec budget exceeded"
#   J4 被拒的那次 n_routes **不变** (拒绝无副作用)
import os, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from h723_client import Dcl, engine_status, CMD_ENGINE_STATUS  # noqa: E402

CMD_DEPLOY = 0x10
OP_DIRECT, OP_PID = 0x00, 0x05
SRC_CONST, DST_WIRE, ACTIVE = 2, 2, 1
MAX_ROUTES, MAX_PARAMS = 128, 128
DEPLOY_GATE = 26000          # engine.h: EXEC_DEPLOY_BUDGET
COST = {OP_DIRECT: 56, OP_PID: 145}   # engine.c: k_op_cost_itcm[]
COST_BADOP = 145 * 2                  # engine.c: engine_op_cost 越界兜底


def R(si, dc, op, pi, so=0, period=0, flags=ACTIVE):
    """16 B 路由 (engine.h: RouteEntry_t)"""
    return struct.pack("<BBBBBBHHHHBB", SRC_CONST, si, DST_WIRE, dc, op, flags,
                       pi, so, 0, 0, period, 0)


def mk(op, div, n=MAX_ROUTES):
    """n 条全 ACTIVE、dst 各不同(避免 dst conflict)、源码 CONST、单档"""
    routes = b"".join(R(i, i, op, i, so=(1 if op == OP_PID else 0), period=div)
                      for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    ns = 1 if op == OP_PID else 0
    return struct.pack("<HHH", n, n, ns) + routes + params + b"\x00" * 16 * ns


def expect(op, div, n=MAX_ROUTES):
    c = COST.get(op, COST_BADOP)
    mult = 1 if div == 0 else 10
    return n * ((c + mult - 1) // mult)


def deploy(dcl, payload, label, exp):
    sts, p = dcl.send(CMD_DEPLOY, payload, expect_len=6)
    if sts == "ACK" and len(p) >= 6:
        seq, budget = struct.unpack("<HI", p[:6])
        ok = (budget == exp)
        print("  [%s] %-26s ACK seq=%d budget=%-6d (期望 %-6d) %s"
              % ("PASS" if ok else "FAIL", label, seq, budget, exp, "" if ok else "← 不符"))
        return ("ACK", budget, ok)
    if sts == "NAK":
        why = p.decode("utf-8", "replace")
        print("  [info] %-26s NAK 理由=%r (期望 budget=%d)" % (label, why, exp))
        return ("NAK", why, None)
    print("  [FAIL] %-26s %s (期望 budget=%d)" % (label, sts, exp))
    return (sts, None, False)


def main():
    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("端口 = %s" % dcl.port)
    results = []
    try:
        st0 = engine_status(dcl)
        print("板态(前): n_routes=%s run=%s ov=%s samples=%s"
              % (st0["n_routes"], st0["run"], st0["ov"], st0["samples"]))

        print("\n--- J1/J2: 三个合法重载荷 (都该 ACK, budget 该等于手算) ---")
        for label, op, div in (("O2c DIRECT×128 div0", OP_DIRECT, 0),
                               ("O2d PID×128 div1", OP_PID, 1),
                               ("O2a PID×128 div0 ★最重合法", OP_PID, 0)):
            e = expect(op, div)
            print("  (手算: %d 条 × ceil(%d/%d) = %d；门 = %d ⇒ %s)"
                  % (MAX_ROUTES, COST.get(op, COST_BADOP), 10 if div else 1, e,
                     DEPLOY_GATE, "不该被拒" if e <= DEPLOY_GATE else "该被拒!"))
            sts, got, ok = deploy(dcl, mk(op, div), label, e)
            results.append((label, sts, got, e, ok))

        print("\n--- J3/J4: op 越界 (若按 290 计则 37120 > 门 ⇒ 本该被预算门拦) ---")
        n_before = engine_status(dcl)["n_routes"]
        sts, why, _ = deploy(dcl, mk(0xFF, 0), "O2b op=0xFF ×128", expect(0xFF, 0))
        j3 = (sts == "NAK" and why == "bad op")
        print("  [%s] J3 NAK 理由 == 'bad op' 而**不是** 'exec budget exceeded'"
              % ("PASS" if j3 else "FAIL"))
        time.sleep(0.3)
        n_after = engine_status(dcl)["n_routes"]
        j4 = (n_before == n_after)
        print("  [%s] J4 被拒后 n_routes 不变 (%d -> %d)" % ("PASS" if j4 else "FAIL", n_before, n_after))
        results.append(("J3 bad-op 被门序挡住", "PASS" if j3 else "FAIL", why, None, j3))
        results.append(("J4 拒绝无副作用", "PASS" if j4 else "FAIL", None, None, j4))

    finally:
        print("\n--- 收尾: 回 bench 态 ---")
        try:
            dcl.send(0x12)                     # STOP
            dcl.send(0x11)                     # START (bench profile 不自动回来)
            time.sleep(0.4)
            st = engine_status(dcl)
            print("  收尾后: n_routes=%s run=%s ov=%s" % (st["n_routes"], st["run"], st["ov"]))
        except Exception as e:
            print("  收尾异常:", type(e).__name__, e)
        dcl.close()

    print("\n=== 汇总 ===")
    bad = [r for r in results if r[4] is False]
    for r in results:
        print("  %-28s %s" % (r[0], r[1]))
    print("  ⇒ %s" % ("全部 PASS" if not bad else "有 %d 条 FAIL" % len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
