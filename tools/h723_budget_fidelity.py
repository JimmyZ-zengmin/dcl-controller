#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# h723_budget_fidelity.py —— **预算模型的保真度判据**（一条能失败的判据）
#
# ══════════════════ 为什么需要它（2026-09-18） ══════════════════
# 起因：`h723_io_isr_check.py` 的判据 ④ 是 `g_isr_overrun == 0`，
#       而 2026-09-18 的超载实验（docs/exp-2026-09-18-overload/）实测：
#         · 最重**合法**程序（128×PID div0，ITCM）只吃到运行期门的 **66.8%** ⇒ `ov` 恒 0
#         · 要让它非 0 必须加**模型外负载**（FLASH 扫描路径），而那不是合法配置的常态
#       ⇒ **`ov == 0` 对任何合法程序都不可能失败 = 空判据**（本项目最忌的那一族：
#         "一个永远为 0 的观测量看起来像'这个分支很干净'，实际是'从没被走到'"）。
#       `h723_jitter.py` 给 `ov` 加过 `--ov-expect pos` 做正对照，但它用的那套
#       `-DDCL_BOOT_SEL=0 -DDCL_BOOT_PROFILE=2` 配置**会让板子进复位循环**
#       （同一次实验实测）⇒ **那个正对照本身跑不起来**。
#
# ⇒ 本工具换一条**能失败**的判据：**部署期预算模型 vs 运行期实测**的比值。
#
# ══════════════════ 判据（每条都能失败） ══════════════════
#   F1 ★ 模型保真: 实测扫描成本 ≤ 预测预算 × --ratio-limit (默认 1.30)
#        实测扫描成本 = (emax − 空引擎基线) 换算成 DWT-cyc 当量
#        ★ 这条有**已观测的失败模式**: 2026-09-18 实测 FLASH 扫描路径的同一比值是
#          **3.03**（`k_op_cost_itcm[]` 只为 ITCM 标价）⇒ 若路径成本价没接上，F1 会红。
#   F2 动态门余量: emax ≤ EXEC_BUDGET_TB × --margin (默认 0.80)
#        ("有余量"判据 —— 取代"ov == 0 就算健康"那种不足以致败的写法)
#   F3 反向保护: 轻程序的 emax 必须**显著小于**重程序 (证明这个量在跟踪负载,
#        不是某个常数在冒充判据)
#   F4 前置: 时基活着 (`0x39 op=21` 自检 Δ ≠ 0); 不成立 ⇒ **拒绝给结论**
#
# 用法:
#   python tools/h723_budget_fidelity.py --port COM22
#   python tools/h723_budget_fidelity.py --port COM22 --ratio-limit 1.0   # ★ 应当 FAIL (自证判据能失败)
#
# 退出码: 0 = 全 PASS / 1 = 有 FAIL / 2 = 判无效(SKIP, 前置不满足)
import argparse, os, struct, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(errors="replace")   # ★ GBK 控制台上 print 一个 ⇒ 就崩（本项目踩过）
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass
from h723_client import Dcl  # noqa: E402

CMD_DEPLOY, CMD_STATUS, CMD_PINPAT = 0x10, 0x38, 0x39
CMD_RESET, CMD_START, CMD_STOP = 0x13, 0x11, 0x12
OP_PID = 0x05
SRC_CONST, DST_WIRE, ACTIVE = 2, 2, 1
EXEC_BUDGET_TB = 16000        # engine.h: TB_US(80) @TIM5/200MHz  ⇒ 80 µs
TB_NS_PER_TICK = 5.0          # TIM5 @200 MHz
DWT_NS_PER_CYC = 2.5          # CLK_SYSCLK 400 MHz
TICK_TB = 20000               # 100 µs

_RESULTS = []


def record(ok, name, detail=""):
    _RESULTS.append((ok, name))
    print("  [%s] %-46s %s" % ("PASS" if ok else "FAIL", name, detail))
    return ok


def mk_prog(n):
    """n 条 ACTIVE PID / div0 / 源 CONST / dst 各不同 / state 共享 1 —— 预算 = ceil(145n/1)。"""
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, OP_PID,
                                  ACTIVE, i, 1, 0, 0, 0, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    return struct.pack("<HHH", n, n, 1) + routes + params + b"\x00" * 16


def read_status(dcl, tries=5):
    for _ in range(tries):
        s, p = dcl.send(CMD_STATUS, expect_len=51)
        if s == "ACK" and len(p) >= 51:
            d = struct.unpack("<IIIII", p[:20])
            return dict(pmin=d[1], pmax=d[2], emax=d[4], ov=struct.unpack("<I", p[27:31])[0],
                        n_routes=struct.unpack("<H", p[20:22])[0])
        time.sleep(0.25)
    return None


def tb_healthy(dcl, tries=4):
    for _ in range(tries):
        s, p = dcl.send(CMD_PINPAT, struct.pack("<B", 21), expect_len=40)
        if s == "ACK" and len(p) >= 40:
            w = struct.unpack("<4I", p[:16])
            return w[0], w[1], w[2]          # 档, TB_HZ, 自检Δ
        time.sleep(0.25)
    return None


def deploy(dcl, n, tries=3):
    for _ in range(tries):
        s, p = dcl.send(CMD_DEPLOY, mk_prog(n), expect_len=None)   # ★ NAK 长度随门而变, 不能写死
        if s == "ACK" and len(p) >= 6:
            return struct.unpack("<HI", p[:6])[1]
        if s == "NAK":
            print("    deploy n=%d ⇒ NAK %r" % (n, p.decode("utf-8", "replace")))
            return None
        time.sleep(0.4)
    return None


def measure(dcl, n, settle=2.0):
    """RESET → deploy(n) → 稳定 → 读 emax(去掉基线) """
    dcl.send(CMD_RESET); time.sleep(0.2); dcl.send(CMD_START); time.sleep(0.2)
    budget = deploy(dcl, n)
    if budget is None:
        return None
    time.sleep(settle)
    st = read_status(dcl)
    return (budget, st) if st else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("DCL_PORT"))
    ap.add_argument("--ratio-limit", type=float, default=1.30,
                    help="F1 上限: 实测扫描 / 预测预算 (★ 传 1.0 可自证判据能失败)")
    ap.add_argument("--margin", type=float, default=0.80,
                    help="F2: emax 必须 <= EXEC_BUDGET_TB × 这个系数（有余量判据）")
    ap.add_argument("--heavy", type=int, default=128, help="重程序路数")
    ap.add_argument("--light", type=int, default=16, help="轻程序路数（F3 反向保护）")
    a = ap.parse_args()

    dcl = Dcl(a.port)
    print("端口 = %s" % dcl.port)
    time.sleep(1.0)
    try:
        t = tb_healthy(dcl)
        print("  时基: 档=%s TB_HZ=%s 自检Δ=%s" % (t if t else ("?", "?", "?")))
        if not (t and t[2] != 0):
            print("  [SKIP] F4 时基不健康 ⇒ **拒绝给结论**（本项目铁律）")
            return 2
        record(True, "F4 前置: 时基活着 (自检Δ != 0)", "Δ=%d, TB_HZ=%d" % (t[2], t[1]))
        tb_hz = t[1] or 200000000
        tick_ns = 1e9 / tb_hz

        base = measure(dcl, 0)
        base_emax = base[1]["emax"] if base else 0
        print("  空引擎基线 emax = %d tick" % base_emax)

        light = measure(dcl, a.light)
        heavy = measure(dcl, a.heavy)
        if not heavy:
            print("  [SKIP] 重程序部署/读数失败 ⇒ 判无效")
            return 2

        for tag, r in (("轻 n=%d" % a.light, light), ("重 n=%d" % a.heavy, heavy)):
            if r:
                b, s = r
                print("  %-10s 预测预算=%-6d  实测 emax=%-6d tick (%.1f µs, 门的 %.1f%%)  ov=%d"
                      % (tag, b, s["emax"], s["emax"] * tick_ns / 1000.0,
                         100.0 * s["emax"] / EXEC_BUDGET_TB, s["ov"]))

        hb, hs = heavy
        scan_tick = max(hs["emax"] - base_emax, 0)
        scan_cyc = scan_tick * tick_ns / DWT_NS_PER_CYC        # 换算成 DWT-cyc 当量
        ratio = scan_cyc / hb if hb else 0.0
        record(ratio <= a.ratio_limit, "F1 ★模型保真: 实测扫描/预测 ≤ %.2f" % a.ratio_limit,
               "实测 %.0f cyc / 预测 %d cyc = **%.3f**" % (scan_cyc, hb, ratio))

        record(hs["emax"] <= EXEC_BUDGET_TB * a.margin,
               "F2 动态门余量: emax ≤ 门 × %.2f" % a.margin,
               "emax=%d ≤ %.0f ⇒ 余量 %.1f%%" % (hs["emax"], EXEC_BUDGET_TB * a.margin,
                                                  100.0 * (1 - hs["emax"] / EXEC_BUDGET_TB)))

        if light:
            lb, ls = light
            record(ls["emax"] < hs["emax"], "F3 反向保护: 轻程序 emax 显著小于重程序",
                   "轻 %d < 重 %d" % (ls["emax"], hs["emax"]))
        else:
            record(False, "F3 反向保护: 轻程序读数缺失", "")
    finally:
        dcl.send(CMD_STOP); time.sleep(0.2)
        dcl.send(CMD_RESET); time.sleep(0.3); dcl.send(CMD_START)
        time.sleep(0.4)
        print("  (收尾: 回 bench 态)")
        dcl.close()

    bad = [r for r in _RESULTS if not r[0]]
    print("\n=== 汇总: %d 项, %d FAIL ===" % (len(_RESULTS), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
