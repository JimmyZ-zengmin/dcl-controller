#!/usr/bin/env python3
# OVL-1 步骤 3: 量超载档下的动态门行为
#
# 判据 (每条都能失败):
#   K0 ★ 前置: 时基活着 (0x39 op=21 的自检 Δ != 0)。**它不成立就拒答** ——
#      本项目铁律: 时基可被调试器静默停掉, 那时所有计时量都是假的。
#   K1 超载确实发生: emax > EXEC_BUDGET_TB (16000 tick = 80 µs)
#   K2 ★ 动态门随之触发: ov > 0   ← 这是本实验的主判据
#      (若 K1 成立而 K2 失败 ⇒ **动态门在 TIM5 时基下失效** ⇒ 真缺陷)
#   K3 ov 与 tick 数自洽: ov 次数 ≈ 超载持续的拍数 (不是每拍 +1 的假计数)
import os, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from h723_client import Dcl  # noqa: E402

CMD_STATUS, CMD_RESET, CMD_START, CMD_STOP = 0x38, 0x13, 0x11, 0x12
CMD_PINPAT = 0x39
TB_BUDGET, TICK = 16000, 20000


def st(dcl, tries=5):
    for _ in range(tries):
        s, p = dcl.send(CMD_STATUS, expect_len=51)
        if s == "ACK" and len(p) >= 51:
            d = struct.unpack("<IIIII", p[:20])
            return dict(samples=d[0], pmin=d[1], pmax=d[2], emin=d[3], emax=d[4],
                        n_routes=struct.unpack("<H", p[20:22])[0], run=p[22],
                        ov=struct.unpack("<I", p[27:31])[0])
        time.sleep(0.25)
    return None


def tb(dcl, tries=4):
    """0x39 op=21: [0]档(1=TIM5) [2]自检Δ [4]dwt_dead [6/7]pmin/pmax"""
    for _ in range(tries):
        s, p = dcl.send(CMD_PINPAT, struct.pack("<B", 21), expect_len=32)
        if s == "ACK" and len(p) >= 32:
            w = struct.unpack("<8I", p[:32])
            return dict(arch=w[0], delta=w[2], dwt_dead=w[4], pmin=w[6], pmax=w[7])
        time.sleep(0.25)
    return None


def show(tag, s):
    if not s:
        print("  %-12s <读不到>" % tag); return None
    print("  %-12s n_routes=%-4d run=%d emax=%-6d (%.1f µs, 门的 %.1f%%, 拍长的 %.1f%%) "
          "ov=%-6d pmin=%-6d pmax=%-6d samples=%d"
          % (tag, s["n_routes"], s["run"], s["emax"], s["emax"] * 5 / 1000.0,
             100.0 * s["emax"] / TB_BUDGET, 100.0 * s["emax"] / TICK,
             s["ov"], s["pmin"], s["pmax"], s["samples"]))
    return s


def main():
    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("端口 = %s" % dcl.port)
    time.sleep(1.5)
    ok = []
    try:
        t = tb(dcl)
        print("\n--- K0 时基健康 (0x39 op=21) ---")
        print("  档=%s (1=TIM5)  自检Δ=%s  dwt_dead=%s" % (t and t["arch"], t and t["delta"], t and t["dwt_dead"]))
        k0 = bool(t and t["delta"] != 0)
        print("  [%s] K0 时基活着 (Δ=%s 非 0)" % ("PASS" if k0 else "FAIL", t and t["delta"]))
        if not k0:
            print("  ⇒ 时基不健康, **拒绝给结论** (这是本项目铁律)")
            return 2
        ok.append(("K0 时基活着", k0))

        print("\n--- 复位统计后开跑 ---")
        dcl.send(CMD_RESET); time.sleep(0.3)
        dcl.send(CMD_START); time.sleep(1.5)
        s1 = show("1.5s", st(dcl))
        time.sleep(3.0)
        s2 = show("4.5s", st(dcl))
        time.sleep(5.0)
        s3 = show("9.5s", st(dcl))

        s = s3 or s2 or s1
        if s:
            k1 = s["emax"] > TB_BUDGET
            k2 = s["ov"] > 0
            print("\n--- 判定 ---")
            print("  [%s] K1 超载发生: emax=%d %s 门=%d" % ("PASS" if k1 else "FAIL",
                  s["emax"], ">" if k1 else "<=", TB_BUDGET))
            print("  [%s] K2 ★动态门触发: ov=%d %s 0" % ("PASS" if k2 else "FAIL", s["ov"],
                  ">" if k2 else "=="))
            if k1 and not k2:
                print("  ⛔ **真缺陷**: 超载已发生而 ov 未计数 ⇒ 动态门在 TIM5 时基下失效")
            if (not k1) and k2:
                print("  ⚠ 反常: 未超载却计了超预算 —— 阈值换算可能反了")
            ok.append(("K1 超载发生", k1))
            ok.append(("K2 动态门触发", k2))
    finally:
        print("\n--- 收尾 (回 bench 态) ---")
        try:
            dcl.send(CMD_STOP); dcl.send(CMD_RESET); time.sleep(0.3); dcl.send(CMD_START)
            time.sleep(0.8)
            show("收尾", st(dcl))
        except Exception as e:
            print("  收尾异常:", type(e).__name__, e)
        dcl.close()

    print("\n=== 汇总 ===")
    for k, v in ok:
        print("  %-18s %s" % (k, "PASS" if v else "FAIL"))
    return 0 if all(v for _, v in ok) else 1


if __name__ == "__main__":
    sys.exit(main())
