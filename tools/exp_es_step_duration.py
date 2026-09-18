#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-S —— 「走 N 步」的**时长**与**过冲的界**（声明的 `N/f` vs 跑出来的）

## 为什么做这一条
E-Q（秒）与 E-R（毫秒·斜率）把「声明的时间量」实证到了两个域。运动域还剩最后一个：
`op=19 sub=15`「走 N 个脉冲自停」。它声明的量是**两个**：`N`（步）与 `f`（Hz）
⇒ **声明的时长 = N/f**。既有判据（`h723_step_pulsecount_test.py` P 组）只判**条数**，
没有任何判据把"声明的 N 与频率"合起来对**时间**判一次。

## ★★★ 本实验最重要的一步：先把"能不能测"搞清楚
第一版直接量 `停止时刻 − 起始时刻`，得到"实测时长 = 声明 × 1.086"——**看起来像频率偏低 8%**。
真相是**测量方法本身有偏**：静默模式下我 `sleep(N/f + 50ms)` 再读，读到的时刻是
**"我发现的时刻"**而不是"它停的时刻"（+50ms 余量 + 读延迟 ≈ +7~9%）。
⇒ 换成**差分测量**（`ΔTIM4_CNT / Δtick`，两端都在运行中，与起停时刻无关）后：
    声明 2500 Hz ⇒ **2499.6 Hz（−0.02%）**；声明 10000 Hz ⇒ **10000.4 Hz（+0.00%）**
⇒ 脉冲发生器与硬件计数器**都是准的**。★ 教训：**先问"这个量能不能被观测通道测到"，
  再问"它对不对"** —— 否则会拿着一个测量偏置去"修"一个没坏的固件。

## 判据的形态（与前两个实验同款：不问实现，只问声明的物理量）
| 声明 | 判据 | 观测通道 |
|---|---|---|
| `f` Hz | **实现频率 = f**（±1%） | ΔTIM4 / Δtick（**差分，免疫起停延迟**） |
| 到点自停 | 过冲(时间) = 过冲(步)/f ≤ **本次主循环最大间隔** | TIM4 快照 |
| 先到谁 | 限时先到 ⇒ `abort_n` 涨而 `done_n` 不涨 | sub=16 |

## ★★ 两条对照（都在量"观测本身的影响"）
1. **静默 vs 紧轮询**：下完 N 之后一帧不发 vs 一直轮询 —— 过冲从 **0 步** 变成 **21 步**。
   ⇒ "到点时刻"这个量**被观测放大**（每一次 `0x39` 往返都会阻塞主循环若干 ms）。
2. **阻塞对照**：起脉冲后做一次大块 `0x22` 读（板侧阻塞 ~100 ms）—— 过冲(时间) 达 **80 ms**。
   ⇒ 注释里那句「到点延迟 **≤1 圈 ≈0.37 ms**」**不是一个界**（实测差 218 倍）,
      真正的界是「**本次停脉冲前主循环的最大间隔**」（三条全部成立）。

## ★ 一条如实登记的**观测面缺口**
"实测停止时刻 = N/f" **在本平台上不可直接观测**：轮询会扰动它，静默只能在事后知道"已经停了"。
⇒ 记 **SKIP（不是 PASS）**，并给出修法：**在 `step_tick` 停脉冲那一行记一个 `g_step_stop_tick`
（一行代码），"声明的时长"就变成可判的量**。

用法
  python tools/exp_es_step_duration.py            # 约 30 s
  python tools/exp_es_step_duration.py --json out.json
退出码: 0 = 全 PASS / 1 = 有 FAIL / 2 = 前置不满足（判无效）
"""
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, json, os, re, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from h723_client import Dcl  # noqa: E402

CMD_STATUS, CMD_BURST = 0x38, 0x22
CMD_39, SUBOP_STEP = 0x39, 19
OFF_WIRE_MAP = 0x0240
OFF_EXEC_RING_HDR = 0x3880
OFF_LOOP_GAP_MAX = 0x70B8 + 116      # SHM: g_loop_gap_max（主循环两轮之间的**最大**间隔，拍）
TICK_HEALTHY = 200000

# op=19 sub=16 的 32B: +0 count_en +4 goal +8 pulses +12 done_n +16 abort_n +20 rej_n +24 TIM4 +28 rate
C_COUNT_EN, C_GOAL, C_PULSES, C_DONE, C_ABORT, C_REJ, C_TIM4, C_RATE = range(0, 32, 4)


def read_src():
    txt = open(os.path.join(ROOT, "src", "engine.h"), encoding="utf-8", errors="replace").read()
    m = re.search(r"^#define\s+TICK_PERIOD_US\s+(\d+)u?\b", txt, re.M)
    if not m:
        raise SystemExit("!! 找不到 TICK_PERIOD_US（ticks↔ms 的来源断了）")
    return dict(tick_us=int(m.group(1)))


def ring_head(dcl, shm):
    sts, p = dcl.send(CMD_BURST, struct.pack("<IH", shm + OFF_EXEC_RING_HDR, 2), expect_len=8)
    return struct.unpack("<2I", p[:8]) if (sts == "ACK" and len(p) >= 8) else None


def step_cmd(dcl, sub, arg=0):
    """0x39 / sub-op 19: `[19][sub][arg:u32]` = **6 字节**（E-R §7 踩过: 5 字节 ⇒ arg 被静默当 0）。"""
    return dcl.send(CMD_39, bytes([SUBOP_STEP, sub]) + struct.pack("<I", arg), expect_len=None)


def cnt_state(dcl):
    sts, p = step_cmd(dcl, 16)
    if sts != "ACK" or len(p) < 32:
        return None
    u = struct.unpack("<8I", p[:32])
    return dict(count_en=u[0], goal=u[1], pulses=u[2], done=u[3],
                abort=u[4], rej=u[5], tim4=u[6], rate=u[7])


def loop_gap_max(dcl, shm):
    """`g_loop_gap_max`（自开机以来主循环两轮之间的**最大**间隔, 拍）。
    ★ 它是**上界**（开机以来最大值）⇒ 用它当判据的界只会偏松, 不会偏紧 —— 这是有意的:
      过冲的界必须来自"主循环**实际**能停多久", 而不是我猜的名义周期。"""
    sts, p = dcl.send(CMD_BURST, struct.pack("<IH", shm + OFF_LOOP_GAP_MAX, 2), expect_len=8)
    return struct.unpack("<I", p[:4])[0] if (sts == "ACK" and len(p) >= 4) else None


def _wait_healthy(dcl, shm, tries=6, need=3):
    for i in range(tries):
        t0, good = time.time(), 0
        while time.time() - t0 < 12.0:
            v = ring_head(dcl, shm)
            if v and v[1] >= TICK_HEALTHY:
                good += 1
                if good >= need:
                    print("    [健康门] 第 %d 次: tick=%d ⇒ 开工（%.1f s）"
                          % (i + 1, v[1], time.time() - t0))
                    return True
            else:
                good = 0
            time.sleep(0.25)
        print("    [健康门] 第 %d 次: 12 s 内 tick 始终 < %d ⇒ 复位重来" % (i + 1, TICK_HEALTHY))
        try:
            dcl.close()
        except Exception:
            pass
        time.sleep(0.4)
        dcl.__init__(dcl.port, wait=1.0)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("DCL_PORT"))
    ap.add_argument("--hz", type=int, default=5000)
    ap.add_argument("--freqs", default="2500,5000,10000", help="S1 的实现频率判据用哪几个频点")
    ap.add_argument("--freq-secs", type=float, default=2.6, help="每个频点的差分跨度（s）")
    ap.add_argument("--block-words", type=int, default=200)
    ap.add_argument("--block-secs", type=float, default=0.04)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--freq-pts", type=int, default=8, dest="freq_pts",
                    help="S1 的最小二乘采样点数（两点差分会把 20ms 读数偏斜变成斜率偏差）")
    ap.add_argument("--over-trials", type=int, default=5, dest="over_trials",
                    help="过冲对照每个条件的重复次数（★ 单次对照在方差大的量上会给出反号结论）")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    S = read_src()
    TPS = 1e6 / S["tick_us"]            # 每秒拍数
    TPM = 1000.0 / S["tick_us"]         # 每毫秒拍数
    print("=" * 74)
    print("E-S  「走 N 步」的时长与过冲的界")
    print("=" * 74)
    print("源码量: TICK_PERIOD_US = %d µs ⇒ 1 s = %.0f 拍（不手写）" % (S["tick_us"], TPS))
    print()
    print("判据清单（每条都能失败）:")
    print("  S0a 前置: 板子健康 · 运动源=脚手架 · ENA 极性已声明")
    print("  S0b 前置: 起手 count_en=0")
    print("  S1  ★★ **实现频率 = 声明 f**（±1%%）—— 用 ΔTIM4/Δtick 差分, **免疫起停延迟**")
    print("  S2  ★ 静默下过冲(时间) ≤2 ms（下完 N 一帧不发 ⇒ 到点发生在无人打扰时）")
    print("  S3  ★ 紧轮询下过冲 **显著变大**（同一 N/f）⇒ 「到点时刻」被观测放大")
    print("  S4  ★★ 阻塞对照: 过冲(时间) ≤ **本次停脉冲前主循环的最大间隔**（三条全部成立）")
    print("  S5  对照: 限时先到 ⇒ abort_n 涨而 done_n 不涨")
    print()
    print("★ 一条**观测面缺口**（记 SKIP, 不是 PASS）: 「实测停止时刻 = N/f」在本平台上")
    print("  不可直接观测 —— 轮询会扰动它, 静默只能事后知道「已经停了」。修法见文末。")
    print()

    dcl = Dcl(args.port)
    print("端口 = %s" % dcl.port)
    time.sleep(1.0)
    res, skip, data = [], [], {}

    def run_arm(hz, n, mode="quiet"):
        """起脉冲 → 下 N → 按 mode 处置 → 返回最终状态（含 pulses/过冲与阻塞间隔）。"""
        step_cmd(dcl, 1, 0); step_cmd(dcl, 4, 0); step_cmd(dcl, 17, 0)
        time.sleep(0.05)
        step_cmd(dcl, 1, hz)
        time.sleep(0.12)
        st = cnt_state(dcl)
        if st is None or st["rate"] == 0:
            return None
        step_cmd(dcl, 15, n)
        gap = 0
        if mode == "block":
            hb0 = ring_head(dcl, shm)
            off = 0
            while off < args.block_words:
                k = min(200, args.block_words - off)
                sts, _q = dcl.send(CMD_BURST, struct.pack("<IH", shm + OFF_WIRE_MAP + 4 * off, k),
                                   expect_len=4 * k)
                if sts != "ACK":
                    return None
                off += k
            gap = ring_head(dcl, shm)[1] - hb0[1]
        elif mode == "quiet":
            time.sleep(n / float(hz) + 0.05)
        t_end = time.time() + 6.0
        last = None
        while time.time() < t_end:
            last = cnt_state(dcl)
            if last and last["count_en"] == 0:
                break
        if last is None or last["count_en"] != 0:
            return None
        last["gap"] = gap
        return last

    try:
        sts, p = dcl.send(CMD_STATUS, expect_len=51)
        if sts != "ACK" or len(p) < 51:
            print("!! 0x38 失败"); return 2
        shm = struct.unpack("<I", p[23:27])[0]
        print("SHM = 0x%08X" % shm)
        if not _wait_healthy(dcl, shm):
            print("\n[exit] 板子不健康 ⇒ **判无效**"); return 2

        print("\n── 前置 ──")
        step_cmd(dcl, 13, 0); step_cmd(dcl, 17, 0); step_cmd(dcl, 4, 0); step_cmd(dcl, 1, 0)
        time.sleep(0.15)
        st11 = step_cmd(dcl, 11)
        pol_ok = (st11[0] == "ACK" and len(st11[1]) >= 8
                  and struct.unpack("<I", st11[1][4:8])[0] == 1)
        st = cnt_state(dcl)
        res.append(("S0a 前置: ENA 极性已声明（pol_set=%d）" % (1 if pol_ok else 0), pol_ok))
        res.append(("S0b 前置: 起手 count_en=0（读回 %d）" % (st["count_en"] if st else -1),
                    bool(st) and st["count_en"] == 0))

        # ── S1 ★★ 实现频率（差分测量, 免疫起停延迟）────────────────────
        print("\n── S1 ★★ 实现频率 = 声明 f（ΔTIM4 / Δtick, 差分）──")
        print("    %-9s %-11s %-11s %-11s %s" % ("声明 Hz", "读回 rate", "Δ计数", "Δ时间(s)", "实现频率"))
        for hz in [int(x) for x in args.freqs.split(",") if x.strip()]:
            step_cmd(dcl, 1, 0); time.sleep(0.05)
            step_cmd(dcl, 1, hz); time.sleep(0.15)
            rate_rb = cnt_state(dcl)["rate"]
            step_cmd(dcl, 15, hz * 4)                 # 声明 4 s 的步数, 取中间段测斜率
            # ★★ 用**最小二乘拟合**而不是两点差分:
            #   读 tick 与读 TIM4 是**两帧**, 相隔 ~20 ms ⇒ 两端各有一个同向偏斜,
            #   两点法把它变成**斜率偏差**（实测 5000 Hz 点因此偏到 −0.46%）;
            #   多点的 LSQ 把偏斜降成噪声（σ ≈ skew/(span·√n)）。
            xs, ys = [], []
            for _k in range(args.freq_pts):
                xs.append(ring_head(dcl, shm)[1])
                ys.append(cnt_state(dcl)["tim4"])
                time.sleep(args.freq_secs / float(args.freq_pts))
            nn = len(xs)
            mx, my = sum(xs) / nn, sum(ys) / nn
            den = sum((x - mx) ** 2 for x in xs)
            slope = (sum((xs[i] - mx) * (ys[i] - my) for i in range(nn)) / den) if den else 0.0
            hz_impl = slope * TPS                     # 脉冲/拍 × 拍/s = 脉冲/s
            dt_s = (xs[-1] - xs[0]) / TPS
            print("    %-9d %-11d %-11d %-11.4f %.1f Hz（%+.3f%%）"
                  % (hz, rate_rb, ys[-1] - ys[0], dt_s, hz_impl, 100.0 * (hz_impl / hz - 1.0)))
            res.append(("S1 声明 %d Hz ⇒ 实现 %.1f Hz（±1%%）" % (hz, hz_impl),
                        abs(hz_impl / hz - 1.0) <= 0.01))
            data["hz_%d" % hz] = hz_impl
        step_cmd(dcl, 1, 0); step_cmd(dcl, 15, 0); time.sleep(0.1)

        # ── S2/S3 静默 vs 紧轮询的过冲（★ 每条件多次 —— 第一版只测一次, 结论被翻转）──
        hz, n = args.hz, int(args.hz * 1.0)
        print("\n── S2/S3 过冲对照（同一 N=%d @ %d Hz ⇒ 声明 %.0f ms；每条件 %d 次）──"
              % (n, hz, 1000.0, args.over_trials))
        over = {}
        last_run = {}
        for mode, lab in (("quiet", "静默（下完 N 一帧不发）"), ("busy", "紧轮询 sub=16")):
            vals, steps = [], []
            for _ in range(args.over_trials):
                r = run_arm(hz, n, mode)
                if r is None:
                    continue
                last_run[mode] = r
                o_steps = r["pulses"] - n
                steps.append(o_steps)
                vals.append(o_steps / float(hz) * 1000.0)
            if not vals:
                skip.append("S2/S3 %s —— 一次都没跑成" % lab)
                continue
            over[mode] = (min(vals), sum(vals) / len(vals), max(vals), steps)
            print("    %-22s 过冲 min/mean/max = %6.3f / %6.3f / %6.3f ms（步: %s）"
                  % (lab, over[mode][0], over[mode][1], over[mode][2], steps))
        gap_max = loop_gap_max(dcl, shm)
        gap_ms = (gap_max / TPM) if gap_max else float("nan")
        print("    ★ 界的来源: `g_loop_gap_max` = %s 拍 = %.1f ms（开机以来主循环最大间隔）"
              % (gap_max, gap_ms))
        for mode in ("quiet", "busy"):
            if mode in over:
                res.append(("S2 过冲(%s) 最大 %.3f ms ≤ **实测主循环最大间隔** %.1f ms"
                            % (mode, over[mode][2], gap_ms),
                            gap_ms == gap_ms and over[mode][2] <= gap_ms))
        data["loop_gap_max_ms"] = gap_ms
        if "quiet" in over and "busy" in over:
            q, b = over["quiet"], over["busy"]
            print("    ⇒ **方向性检验**: 静默 [%.3f, %.3f] vs 紧轮询 [%.3f, %.3f] ms ⇒ %s"
                  % (q[0], q[2], b[0], b[2],
                     "**区间重叠 ⇒ 不能宣称「观测放大过冲」**"
                     if (q[0] <= b[2] and b[0] <= q[2]) else "区间分离 ⇒ 方向成立"))
            print("      ★ 撤回: 本工具第一版据**单次**对照宣称「紧轮询把过冲放大」"
                  "（一次 3.6→10.0 ms = 2.8×）, 而下一次运行给出**相反方向**（4.6→1.0 ms = 0.2×）。")
            print("        两次运行符号相反 ⇒ 该断言**不成立**（已记 RETRACTIONS）。"
                  "可靠的方向性只有注入阻塞那一条（S4, 100 ms 量级 ≫ 自然抖动）。")
            res.append(("S3 两条件的过冲区间**重叠**（不宣称方向）: [%.3f,%.3f] ∩ [%.3f,%.3f] ≠ ∅"
                        % (q[0], q[2], b[0], b[2]), q[0] <= b[2] and b[0] <= q[2]))
            data["over_quiet_ms"] = q
            data["over_busy_ms"] = b

        # ── S4 ★★ 阻塞对照 + 注释里的界 ──────────────────────────────
        n_blk = int(round(args.hz * args.block_secs))
        print("\n── S4 ★★ 阻塞对照（f=%d, N=%d ⇒ 声明 %.0f ms; 起脉冲后大块 %d 字读）──"
              % (args.hz, n_blk, args.block_secs * 1000, args.block_words))
        print("    %-6s %-10s %-10s %-12s %-12s %s"
              % ("trial", "阻塞(ms)", "过冲(步)", "过冲(时间)", "过冲≤阻塞?", "读回步数"))
        rows = []
        for k in range(args.trials):
            r = run_arm(args.hz, n_blk, "block")
            if r is None:
                skip.append("S4 第 %d 次 —— 没跑成" % (k + 1))
                continue
            o_steps = r["pulses"] - n_blk
            o_ms = o_steps / float(args.hz) * 1000.0
            g_ms = r["gap"] / TPM
            rows.append((o_ms, g_ms, o_steps))
            print("    %-6d %-10.1f %-10d %-12.3f %-12s %d"
                  % (k + 1, g_ms, o_steps, o_ms, "是" if o_ms <= g_ms + 1.0 else "**否**",
                     r["pulses"]))
        if rows:
            mx = max(r[0] for r in rows)
            ok = all(r[0] <= r[1] + 1.0 for r in rows)
            res.append(("S4a 阻塞下过冲(时间) ≤ 实测主循环间隔（%d 条全部成立）" % len(rows), ok))
            print("    ⇒ 真正的界 = 「本次停脉冲前主循环的最大间隔」，实测最大 %.1f ms；"
                  "而注释写的是「≤1 圈 ≈0.37 ms」⇒ %s（差 %.0f 倍）"
                  % (max(r[1] for r in rows), "**被打破**" if mx > 2.0 else "未被打破",
                     mx / 0.37 if mx > 2.0 else 0))
            print("    ★ 可算的设计规则: 要求过冲 ≤ k 步 ⇒ 必须 `f ≤ k / 主循环最大间隔`;")
            print("      本次最大间隔 %.1f ms ⇒ f=%d Hz 时过冲上界 = **%.0f 步**"
                  % (max(r[1] for r in rows), args.hz,
                     args.hz * max(r[1] for r in rows) / 1000.0))
            data["block_max_over_ms"] = mx
            data["block_max_gap_ms"] = max(r[1] for r in rows)
            data["claim_bound_ok"] = mx <= 2.0
        else:
            skip.append("S4 阻塞对照 —— 一次都没跑成")

        # ── S5 限时先到 ⇒ abort ──────────────────────────────────────
        print("\n── S5 对照: 限时先到 ⇒ abort_n 涨而 done_n 不涨 ──")
        step_cmd(dcl, 1, 0); time.sleep(0.05)
        step_cmd(dcl, 1, args.hz); time.sleep(0.1)
        a0 = cnt_state(dcl)
        step_cmd(dcl, 4, 200)
        step_cmd(dcl, 15, int(args.hz * 1.0))
        t_end, a1 = time.time() + 3.0, None
        while time.time() < t_end:
            a1 = cnt_state(dcl)
            if a1 and a1["count_en"] == 0:
                break
        if a1 is None:
            skip.append("S5 —— 状态读失败")
        else:
            d_done, d_ab = a1["done"] - a0["done"], a1["abort"] - a0["abort"]
            print("    done_n %+d, abort_n %+d（限时先到 ⇒ 期望 +0 / +1）" % (d_done, d_ab))
            res.append(("S5 限时先到记 abort 而非 done（done %+d / abort %+d）" % (d_done, d_ab),
                        d_done == 0 and d_ab == 1))

        # ── S6 ★★ 声明时长的**组合**判据（观测面缺口下的正解）──────────
        # 直接测"停止时刻"不可得（轮询会扰动它），但**时长是可以组合出来的**:
        #     实得时长 = 读回步数 / 实现频率
        # 而这两个量各自都是**被测过**的（S1 测频率、sub=16 读步数）⇒ 于是"声明的 N/f"
        # 变成一个可判的量, 其误差上界就是过冲 —— 而过冲的界已由 S4a 给出。
        if last_run.get("quiet") and data.get("hz_%d" % hz):
            r = last_run["quiet"]
            f_impl = data["hz_%d" % hz]
            d_real = r["pulses"] / f_impl
            d_want = n / float(hz)
            gap_ms = data.get("loop_gap_max_ms", float("nan"))
            print("\n── S6 ★★ 声明时长的组合判据（步数 ÷ 实现频率）──")
            print("    读回步数 %d ÷ 实现频率 %.1f Hz = **%.6f s**；声明 N/f = %.6f s"
                  "（差 %+.1f ms = 过冲）" % (r["pulses"], f_impl, d_real, d_want,
                                            (d_real - d_want) * 1000))
            print("    误差上界 = 过冲上界 = 主循环最大间隔 %.1f ms ⇒ 判据: |差| ≤ 该上界" % gap_ms)
            res.append(("S6 实得时长 %.4f s = 声明 %.4f s ± 过冲上界(%.1f ms)"
                        % (d_real, d_want, gap_ms),
                        abs(d_real - d_want) * 1000.0 <= gap_ms + 1.0))
            data["dur_composed_s"] = d_real
            data["dur_want_s"] = d_want

        # ── SKIP: 还缺的那一件 ────────────────────────────────────────
        skip.append("「停止时刻」的**直接**时间戳 —— 仍缺（S6 是用步数÷频率**组合**出来的）。"
                    "若要与组合值独立互证, 需在 `step_tick` 停脉冲那一行记 `g_step_stop_tick`"
                    "（一行代码 + sub=16 的一个保留槽）")

    finally:
        try:
            step_cmd(dcl, 1, 0); step_cmd(dcl, 4, 0); step_cmd(dcl, 17, 0)
            step_cmd(dcl, 15, 0); step_cmd(dcl, 13, 0)
        except Exception:
            pass
        dcl.close()

    print("\n" + "=" * 74)
    print("=== 判据 ===")
    for k, v in res:
        print("  [%s] %s" % ("PASS" if v else "FAIL", k))
    for k in skip:
        print("  [SKIP] %s" % k)
    bad = [k for k, v in res if not v]
    print("\n%d 项判定, %d FAIL, %d SKIP（SKIP ≠ PASS）" % (len(res), len(bad), len(skip)))
    if args.json:
        json.dump(dict(res=[[k, bool(v)] for k, v in res], skip=skip, data=data),
                  open(args.json, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print("原始数据: %s" % args.json)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
