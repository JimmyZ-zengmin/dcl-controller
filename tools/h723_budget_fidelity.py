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
#          **3.03**（当时预算模型只有 ITCM 一张表）⇒ 若路径成本价没接上，F1 会红。
#        ★★ 同日晚些时候路径成本已按**逐原语**接上（`k_op_cost_flash[]`，见 engine.c）：
#          单标量 3.03 被证伪 —— 它对 DIRECT 低估 1.47 倍(放行超载)、对 PID 高估 1.21 倍
#          (误拒合法)。本工具**不假设**固件用哪种模型：预测值 `hb` 直接取 deploy ACK
#          回读的 budget（固件自报），所以模型换了判据自动跟着走。
#          ★ 这也意味着 F1 **测不出"固件自报模型与真实成本不符"** 这一类错
#            （分子实测、分母自报，两边同源就一起偏）—— 那条由成对实验兜
#            （`.tmpctl/scalar_hole.py`，见 docs/exp-2026-09-18-overload/）。
#   F2 动态门余量: emax ≤ **拍长** × --margin (默认 0.80 ⇒ 与动态门 80 µs 同口径)
#        ("有余量"判据 —— 取代"ov == 0 就算健康"那种不足以致败的写法)
#        ★★ 2026-09-18 改口径: 原式是 `emax ≤ EXEC_BUDGET_TB × 0.80` = **64% 拍**。
#          这在交付档(ITCM)看不出问题, 但 FLASH 档**静态门是承重的**(引擎可占满 65% 拍),
#          再加上非引擎 ISR (~7% 拍) ⇒ 总 ISR ≈ 72% 拍 > 64% ⇒ **判据与被测对象的口径
#          矛盾, 近门程序必然 FAIL**。判据口径错会伪装成"被测对象坏", 是本项目最贵的
#          那类假信号 ⇒ 改成对**拍长**取余量, 并把这个陷阱写在这里。
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


# ★★★ 2026-09-18 新增（仪器修复）: **稳态窗口**，用来把"热重载那一拍"从统计里摘出去。
#   依据 `main.c:3400-3404`:
#       uint8_t was_run = SHM_U8(g_shm, OFF_CTRL_ENGINE_RUN);
#       if (!was_run) { stats_reset(); }         /* STOP→START 转变时清统计 */
#   ⇒ `0x12 STOP` → `0x11 START` **清统计但不动程序表** ⇒ 得到纯稳态窗口。
#
#   ★★ 为什么必须有它: F5 原来用**算术归因**
#       `重载 = (emax − 空引擎基线) − 预测预算`
#   实测这条路给出 188 TB tick (0.94 µs)，而**直测真值是 2184 TB (4368 cyc, 10.9 µs)**
#   —— 差 **4.6 倍**，也就是说 F5 报的不是"上界"而是"下界"。
#   原因: 那个式子里三个量各有自己的口径误差（预算只是**摊薄**值、基线是**空引擎**、
#   emax 含**每 1024 拍一次的黑匣子重活**），误差叠在一个差值上。
#   ⇒ 正解 = **两个窗口直接相减**，不做任何算术归因:
#       窗口A（含部署那一拍）− 窗口B（STOP/START 后，无重载拍）
def steady_window(dcl, settle=2.0):
    """STOP → START ⇒ 清统计、**保留程序** ⇒ 不含重载拍的稳态窗口。"""
    dcl.send(CMD_STOP); time.sleep(0.15)
    dcl.send(CMD_START); time.sleep(0.15)
    time.sleep(settle)
    return read_status(dcl)


# ★★ 2026-09-18 新增: 重程序路数**不能写死 128**。
#   原默认 `--heavy 128` 是在"交付档预算 = 145 cyc/条 ⇒ 128×145 = 18560 ≤ 门 26000"那个
#   前提下成立的; 路径成本价接上之后, **同一个 128 在 FLASH 档是 128×363 = 46464 > 26000**
#   ⇒ deploy 被 NAK ⇒ `measure()` 返回 None ⇒ 本工具判 **SKIP(判无效)**, F1 在 FLASH 档
#   **根本跑不起来**。这正是"工具的隐含前提随固件改变而静默失效"那一类。
#   ⇒ 改成从固件**自己**问出单价, 再取门下方最大的 N —— 模型换了、路径换了都自动跟得上。
def auto_heavy(dcl, gate=26000, max_routes=128):
    """★ 2026-09-18 二次修正: 自适应路数**必须同时受"条数上限"约束**。
    第一版只取 `gate // 单价` ⇒ 交付档算出 179，而 `MAX_ROUTES = 128`
    ⇒ deploy NAK `counts exceed max` ⇒ 工具又**静默变 SKIP**。
    —— 与"写死 128 在 FLASH 档 NAK"是**同一个病族的两个方向**:
    判据的适用域是两个约束的交集, 少写一个就会在某个档位上失效。
    (engine.h: `MAX_ROUTES`; main.c:1790 `if (nr > MAX_ROUTES ...) return "counts exceed max"`)"""
    b1 = deploy(dcl, 1)
    if not b1:
        return None, None
    n = max(min(gate // b1, max_routes), 1)   # 门 // 单价, 再按条数上限封顶
    print("  [自适应] 固件自报单价 %d cyc/条 ⇒ 门 %d 下方 %d 条, 条数上限 %d ⇒ N = %d"
          % (b1, gate, gate // b1, max_routes, n))
    return n, b1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("DCL_PORT"))
    ap.add_argument("--ratio-limit", type=float, default=1.30,
                    help="F1 上限: 实测扫描 / 预测预算 (★ 传 1.0 可自证判据能失败)")
    ap.add_argument("--margin", type=float, default=0.80,
                    help="F2: emax 必须 <= **拍长** × 这个系数 (默认 0.80 = 与动态门同口径)。"
                         "★ 不要改成 `EXEC_BUDGET_TB × 0.80` (= 64%% 拍): 静态门本身"
                         "允许引擎占 65%% 拍, 再加非引擎 ISR ⇒ FLASH 档(D 静态门承重)"
                         "任何近门程序都必然 FAIL —— 那是判据口径错, 不是被测对象坏")
    ap.add_argument("--heavy", type=int, default=0,
                    help="重程序路数; **0 = 自适应**(默认) —— 从固件问出单价再取门下方最大 N。"
                         "写死路数会在 FLASH 档被 NAK 从而静默变成 SKIP")
    ap.add_argument("--light", type=int, default=16, help="轻程序路数（F3 反向保护）")
    ap.add_argument("--settle", type=float, default=2.0,
                    help="每次测量后的稳定等待秒数（F5 的稳态窗口也用它）")
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

        n_heavy = a.heavy
        if n_heavy == 0:
            n_heavy, unit = auto_heavy(dcl)
            if not n_heavy:
                print("  [SKIP] 自适应路数失败(单价探测被拒) ⇒ 判无效")
                return 2
            record(True, "F0 自适应重程序路数 (N 随路径成本自动缩放)",
                   "单价 %d cyc/条 ⇒ N=%d (写死 128 在 FLASH 档会 NAK ⇒ 静默 SKIP)"
                   % (unit, n_heavy))
        if n_heavy < 2:
            print("  [SKIP] 自适应得到 N=%d (门下放不下两条) ⇒ 判无效" % n_heavy)
            return 2

        light = measure(dcl, a.light)
        heavy = measure(dcl, n_heavy)
        if not heavy:
            print("  [SKIP] 重程序(n=%d)部署/读数失败 ⇒ 判无效" % n_heavy)
            return 2

        for tag, r in (("轻 n=%d" % a.light, light), ("重 n=%d" % n_heavy, heavy)):
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

        record(hs["emax"] <= TICK_TB * a.margin,
               "F2 动态门余量: emax ≤ 拍 × %.2f (=%.0f TB)" % (a.margin, TICK_TB * a.margin),
               "emax=%d (拍 %.1f%%) ≤ %.0f ⇒ 距拍长余 %.1f%%; 动态门 %.0f TB"
               % (hs["emax"], 100.0 * hs["emax"] / TICK_TB, TICK_TB * a.margin,
                  100.0 * (1 - hs["emax"] / TICK_TB), float(EXEC_BUDGET_TB)))

        if light:
            lb, ls = light
            record(ls["emax"] < hs["emax"], "F3 反向保护: 轻程序 emax 显著小于重程序",
                   "轻 %d < 重 %d" % (ls["emax"], hs["emax"]))
        else:
            record(False, "F3 反向保护: 轻程序读数缺失", "")

        # ── F5 热重载那一拍 (2026-09-18 补; 缺口②) ─────────────────────────────
        # ★ 背景: 热重载在 ISR **扫描之前**整段执行 ⇒ 部署那一拍的 di = 扫描 + 重载。
        #   `emax` 是"自上次 RESET 以来的最大 di", 而本次测量**包含部署那次重载**
        #   ⇒ **F2 已经把重载拍一起兜住了**（这正是缺口②"Criterion 侧已覆盖"的部分）。
        #
        # ★★★ 2026-09-18 二次修正（**原实现给出的是一个错到 4.6 倍的数**）:
        #   原式 `重载 = (emax − 空引擎基线) − 预测预算` 是**算术归因**, 三个量各有口径误差:
        #     · "预测预算"只是**摊薄**值（按 div 除过的），不是那一拍的真实扫描量
        #     · "空引擎基线"是**空程序**的 ISR，不等于重载那一拍的非引擎部分
        #     · `emax` 还含**每 1024 拍一次的黑匣子重活**（`blackbox.c:310`）
        #   实测: 原式给 188 TB (0.94 µs)，而**直测真值 2184 TB (4368 cyc, 10.9 µs)** ——
        #   差 4.6 倍 ⇒ **它报的不是"上界"而是"下界"**，方向都错了。
        #   正解 = **两窗口直减**，不做任何算术归因:
        #     窗口A = 含部署那一拍（本次 measure 的结果, 已在 hs 里）
        #     窗口B = STOP/START 后的稳态窗口（`steady_window`，无重载拍）
        st_steady = steady_window(dcl, a.settle)
        if st_steady is None:
            record(False, "F5 热重载拍可测 (稳态窗口读数)", "STOP/START 后 0x38 读失败")
        else:
            reload_tick = max(hs["emax"] - st_steady["emax"], 0)
            reserve_tick = 35.0 * 1000.0 / tick_ns     # engine.h 注释里给热重载留的 35 µs 余量
            record(reload_tick <= reserve_tick,
                   "F5 热重载拍在预算余量内 (**直测值** ≤ engine.h 的 35 µs)",
                   "含部署窗口 emax=%d − 稳态窗口 emax=%d ⇒ 重载 **%d tick = %.2f µs** ≤ %.0f tick"
                   % (hs["emax"], st_steady["emax"], reload_tick,
                      reload_tick * tick_ns / 1000.0, reserve_tick))
            print("       ★ 两窗口直减, 不做算术归因 —— 原实现（(emax−基线)−摊薄预算）"
                  "给的是**下界**(0.94 µs), 与直测差 4.6×; 见 docs/exp-TCM-cycles/ §A1.1")
        print("       ★ 本判据**不再依赖 F1**（不需要「模型准」这个前提），也**不需要**把 "
              "`g_reload_cyc` 暴露到协议面 —— 两窗口差已经把那个量直接量出来了"
              "（它本来也读不到: `0x22` 的 `eng_valid_rrange` 不放行 SHM 之外的 DTCM 全局）")
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
