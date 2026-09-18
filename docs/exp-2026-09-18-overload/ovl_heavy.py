#!/usr/bin/env python3
# OVL 核心: 最重**合法**程序的真实 ISR 成本 vs 运行期动态门 (EXEC_BUDGET_TB = 80 µs)
#
# 为什么这是核心: ARCH-GUARANTEE-MAP 的 G3 只有一个缺口 —— 静态门从未触发。
#   但"门没触发"有两种可能: (a) 程序真的远低于门, (b) 门的阈值算错了(那它就是空判据)。
#   ⇒ 必须把**真实成本**量出来, 与门比。
#
# 时基: 交付档 = TIM5 @200 MHz ⇒ 1 tick = 5 ns。EXEC_BUDGET_TB = TB_US(80) = 16000 tick。
#   拍长 100 µs = 20000 tick。
#
# 判据:
#   H1 时基活着: 0x13 RESET -> 0x11 START -> pmin 非 0 (否则全部读数是假的, 本项目铁律)
#   H2 重程序(DIRECT×128 div0) emax 与 轻程序基线同量级偏小
#   H3 最重合法(PID×128 div0) 的 emax 被读出, 且 ov 计数与 emax 自洽
#      (emax <= 16000 ⇒ ov 必为 0; 若 emax > 16000 而 ov==0 ⇒ **动态门坏了** ← 这才是要抓的)
#   H4 拍周期未被拉长: pmax 不因重程序而显著增大 (预算门"确认拍没被拉长")
import os, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from h723_client import Dcl  # noqa: E402

CMD_DEPLOY, CMD_STATUS, CMD_RESET, CMD_START, CMD_STOP = 0x10, 0x38, 0x13, 0x11, 0x12
OP_DIRECT, OP_PID = 0x00, 0x05
SRC_CONST, DST_WIRE, ACTIVE = 2, 2, 1
TB_BUDGET, TICK = 16000, 20000        # EXEC_BUDGET_TB / 拍长 (TB tick @200MHz)


def mk(n, op, div=0):
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  ACTIVE, i, (1 if op == OP_PID else 0), 0, 0, div, 0)
                      for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for i in range(n))
    ns = 1 if op == OP_PID else 0
    return struct.pack("<HHH", n, n, ns) + routes + params + b"\x00" * 16 * ns


def st(dcl, tries=4):
    """0x38 读取 (带重试 —— 本链路有偶发丢应答)"""
    for _ in range(tries):
        s, p = dcl.send(CMD_STATUS, expect_len=51)
        if s == "ACK" and len(p) >= 51:
            d = struct.unpack("<IIIII", p[:20])
            n_routes = struct.unpack("<H", p[20:22])[0]
            run = p[22]
            ov, = struct.unpack("<I", p[27:31])
            return dict(samples=d[0], pmin=d[1], pmax=d[2], emin=d[3], emax=d[4],
                        n_routes=n_routes, run=run, ov=ov)
        time.sleep(0.2)
    return None


def dep(dcl, payload, label):
    for _ in range(3):
        s, p = dcl.send(CMD_DEPLOY, payload, expect_len=6)
        if s == "ACK" and len(p) >= 6:
            seq, b = struct.unpack("<HI", p[:6])
            print("  deploy %-22s ACK  seq=%d budget=%d" % (label, seq, b))
            return True
        if s == "NAK":
            print("  deploy %-22s NAK  %r" % (label, p.decode("utf-8", "replace")))
            return False
        time.sleep(0.4)
    print("  deploy %-22s TIMEOUT (重试后)" % label)
    return False


def show(tag, s):
    if not s:
        print("  %-10s <读不到>" % tag); return None
    print("  %-10s n_routes=%-4d run=%d emax=%-6d (%.1f µs, 门的 %.1f%%)  emin=%-4d  ov=%-4d  "
          "pmin=%-6d pmax=%-6d  samples=%d"
          % (tag, s["n_routes"], s["run"], s["emax"], s["emax"] * 5 / 1000.0,
             100.0 * s["emax"] / TB_BUDGET, s["emin"], s["ov"], s["pmin"], s["pmax"], s["samples"]))
    return s


def main():
    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("端口 = %s" % dcl.port)
    time.sleep(1.0)                    # 端口打开可能复位板子 —— 多等
    ok = []
    try:
        print("\n--- H1: 时基活着 (0x13 RESET -> 0x11 START -> pmin 非 0) ---")
        dcl.send(CMD_RESET); time.sleep(0.3)
        dcl.send(CMD_START); time.sleep(1.2)
        s = show("基线", st(dcl))
        h1 = bool(s and s["pmin"] > 0)
        print("  [%s] H1 时基活着 (pmin=%s 非 0)" % ("PASS" if h1 else "FAIL", s and s["pmin"]))
        ok.append(("H1 时基活着", h1))
        base_emax = s["emax"] if s else 0

        print("\n--- H2/H4: 轻程序对照 DIRECT×128 div0 (budget 应为 7168) ---")
        dep(dcl, mk(128, OP_DIRECT), "DIRECT×128 div0")
        time.sleep(2.0)
        sd = show("DIRECT", st(dcl))

        print("\n--- H3: 最重**合法**程序 PID×128 div0 (budget 应为 18560) ---")
        dep(dcl, mk(128, OP_PID), "PID×128 div0")
        time.sleep(2.0)
        sp = show("PID", st(dcl))
        time.sleep(2.0)
        sp2 = show("PID+2s", st(dcl))

        print("\n--- H3 判定 ---")
        if sp:
            h3a = sp["emax"] <= TB_BUDGET
            h3b = (sp["ov"] == 0) if h3a else (sp["ov"] > 0)
            print("  [%s] emax=%d %s 门=%d" % ("PASS" if h3a else "FAIL", sp["emax"],
                  "<=" if h3a else ">", TB_BUDGET))
            print("  [%s] ov=%d 与 emax 自洽 (emax<=门 ⇒ ov 应为 0)"
                  % ("PASS" if h3b else "FAIL", sp["ov"]))
            ok.append(("H3a emax <= 动态门", h3a))
            ok.append(("H3b ov 与 emax 自洽", h3b))
            print("  ★ 重程序吃掉的拍比例 = %.1f%% (门是 80%%)" % (100.0 * sp["emax"] / TICK))
        if sd and sp:
            h2 = sp["emax"] > sd["emax"]
            h4 = abs(sp["pmax"] - sd["pmax"]) <= max(200, 0.02 * max(sp["pmax"], sd["pmax"]))
            print("  [%s] H2 重程序 emax(%d) > 轻程序 emax(%d)" % ("PASS" if h2 else "FAIL", sp["emax"], sd["emax"]))
            print("  [%s] H4 拍周期未被拉长 (DIRECT pmax=%d / PID pmax=%d)"
                  % ("PASS" if h4 else "FAIL", sd["pmax"], sp["pmax"]))
            ok.append(("H2 重>轻", h2))
            ok.append(("H4 拍未拉长", h4))
    finally:
        print("\n--- 收尾 ---")
        try:
            dcl.send(CMD_STOP); dcl.send(CMD_RESET); time.sleep(0.3); dcl.send(CMD_START)
            time.sleep(0.8)
            show("收尾", st(dcl))
        except Exception as e:
            print("  收尾异常:", type(e).__name__, e)
        dcl.close()

    print("\n=== 汇总 ===")
    for k, v in ok:
        print("  %-24s %s" % (k, "PASS" if v else "FAIL"))
    return 0 if all(v for _, v in ok) else 1


if __name__ == "__main__":
    sys.exit(main())
