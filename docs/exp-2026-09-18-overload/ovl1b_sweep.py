#!/usr/bin/env python3
# OVL-1b 步骤 2: 在 FLASH 扫描档下**扫路数**, 找动态门 (EXEC_BUDGET_TB = 16000 tick = 80 µs)
#   第一次被触发的那一档 —— 然后确认 ov 真的计了数。
#
# 为什么用 FLASH 扫描档: 静态门保证"合法程序永远够不到动态门"(实测最重 66.8% 门).
#   ⇒ 动态门的用途只能是**模型外负载**的兜底. FLASH 扫描路径正是模型外的
#      (成本表 k_op_cost_itcm 只为 ITCM 路径标价).
#
# 判据 (每条都能失败):
#   M0 时基活着 (0x39 op=21 Δ != 0); 不成立 ⇒ 拒答
#   M1 emax 随路数单调上升 (证明负载真的在加)
#   M2 ★ 存在一档使 emax > 16000 且 ov > 0  ⇒ 动态门在 TIM5 时基下**仍然有效**
#   M3 反例保护: 在 emax <= 16000 的档上 ov 必须**仍为 0** (否则阈值换算反了)
import os, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from h723_client import Dcl  # noqa: E402

CMD_DEPLOY, CMD_STATUS, CMD_PINPAT = 0x10, 0x38, 0x39
CMD_RESET, CMD_START, CMD_STOP = 0x13, 0x11, 0x12
OP_PID = 0x05
SRC_CONST, DST_WIRE, ACTIVE = 2, 2, 1
TB_BUDGET, TICK = 16000, 20000


def mk(n, op=OP_PID, div=0):
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  ACTIVE, i, 1, 0, 0, div, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    return struct.pack("<HHH", n, n, 1) + routes + params + b"\x00" * 16


def st(dcl, tries=3):
    for _ in range(tries):
        s, p = dcl.send(CMD_STATUS, expect_len=51)
        if s == "ACK" and len(p) >= 51:
            d = struct.unpack("<IIIII", p[:20])
            return dict(samples=d[0], pmin=d[1], pmax=d[2], emin=d[3], emax=d[4],
                        n_routes=struct.unpack("<H", p[20:22])[0], run=p[22],
                        ov=struct.unpack("<I", p[27:31])[0])
        time.sleep(0.3)
    return None


def alive(dcl):
    s, p = dcl.send(0x01, expect_len=4)
    return s == "ACK" and len(p) == 4


def main():
    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("端口 = %s" % dcl.port)
    time.sleep(1.5)
    rows = []
    try:
        s, p = dcl.send(CMD_PINPAT, struct.pack("<B", 21), expect_len=40)
        # 实测布局: [0]档(1=TIM5) [1]TB_HZ [2]自检Δ [3]...
        w = struct.unpack("<4I", p[:16]) if (s == "ACK" and len(p) >= 40) else None
        print("M0 时基: 档=%s TB_HZ=%s Δ=%s" % (w and w[0], w and w[1], w and w[2]))
        if not (w and w[2] != 0):
            print("  [FAIL] M0 时基不健康 ⇒ 拒绝给结论")
            return 2
        print("  [PASS] M0 时基活着\n")

        for n in (40, 48, 56, 64, 72, 80):
            dcl.send(CMD_RESET); time.sleep(0.2); dcl.send(CMD_START); time.sleep(0.2)
            okd = False
            for _ in range(3):
                s, p = dcl.send(CMD_DEPLOY, mk(n), expect_len=6)
                if s == "ACK":
                    okd = True; break
                time.sleep(0.4)
            if not okd:
                print("  n=%-3d deploy 失败(%s) ⇒ 停" % (n, s)); break
            time.sleep(2.5)
            r = st(dcl)
            if not r:
                print("  n=%-3d <读不到 0x38> ⇒ 停 (板子可能已不应答)" % n)
                print("        探活:", alive(dcl)); break
            rows.append((n, r))
            print("  n=%-3d emax=%-6d (%.1f µs, 门的 %5.1f%%, 拍长的 %5.1f%%)  ov=%-6d  pmax=%-6d"
                  % (n, r["emax"], r["emax"] * 5 / 1000.0, 100.0 * r["emax"] / TB_BUDGET,
                     100.0 * r["emax"] / TICK, r["ov"], r["pmax"]))
            if r["ov"] > 0 and r["emax"] > TB_BUDGET:
                print("        ★ 命中: 这一档动态门被触发")
                break
            if not alive(dcl):
                print("        ⚠ 探活失败 ⇒ 停"); break

        print("\n--- 判定 ---")
        over = [r for _, r in rows if r["emax"] > TB_BUDGET]
        under = [r for _, r in rows if r["emax"] <= TB_BUDGET]
        m1 = all(rows[i][1]["emax"] <= rows[i + 1][1]["emax"] for i in range(len(rows) - 1)) if len(rows) > 1 else None
        m2 = any(r["ov"] > 0 for r in over) if over else False
        m3 = all(r["ov"] == 0 for r in under)
        print("  [%s] M1 emax 随路数单调上升" % ({True: "PASS", False: "FAIL", None: "SKIP"}[m1]))
        print("  [%s] M3 未超载档 ov 仍为 0 (反向保护)" % ("PASS" if m3 else "FAIL"))
        if not over:
            print("  [SKIP] M2 本档位范围内 emax 未超过门 ⇒ 没到触发点 (提高路数或换负载)")
        else:
            print("  [%s] M2 ★动态门触发: ov=%d (emax=%d > 门=%d)"
                  % ("PASS" if m2 else "FAIL", max(r["ov"] for r in over),
                     max(r["emax"] for r in over), TB_BUDGET))
            if not m2:
                print("  ⛔ **真缺陷**: 超载已发生而 ov 未计数 ⇒ 动态门在 TIM5 时基下失效")
    finally:
        print("\n--- 收尾 ---")
        try:
            dcl.send(CMD_STOP); dcl.send(CMD_RESET); time.sleep(0.3); dcl.send(CMD_START)
            time.sleep(0.5)
            print("  收尾后探活:", alive(dcl))
        except Exception as e:
            print("  收尾异常:", type(e).__name__, e)
        dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
