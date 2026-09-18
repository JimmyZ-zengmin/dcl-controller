#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-R —— **运动域的「声明时间量」实现了吗**（限时 / 斜坡斜率）

## 为什么做这一条
E-Q 在「秒」语义上做了同一件事（`docs/exp-EQ-dt-semantics.md`），并因此抓出**顺序档第二份 dt 表**。
运动域里同样有一串"程序里声明的量"，而它们此前只有**弱判据**：
  · 斜坡斜率：既有判据 R3 的容差是 **±50%**（`h723_step_ramp_test.py`），且只看 `rate_out` 有没有中间值
  · **限时（`sub=4`，毫秒）**：`tools/` 下**没有任何判据量过它的实现值**
  · "走 N 步"：只判脉冲数，不判"声明的时长"

## 判据的形态（与 E-Q 同款：**不问实现，只问声明的物理量**）
| 声明 | 判据 |
|---|---|
| 限时 `L` 毫秒 | ① **流逝率**必须 = **1000 ms/s**（对 ISR 直写的 tick 计数）② 到点**真的停**（`CC1E` 落 0），且停止时刻 − 起点 = `L` |
| 斜坡 `S` Hz/s | 从 `f1` 爬到 `f2` 的**实测时长** = `(f2−f1)/S` |
| 斜坡关 | **立即**到位（<20 ms 的对照） |

## ★★ 本实验的第二条判据：**同一声明在两种宿主条件下必须给出同一个实现值**
"限时流逝率"在**静默**（两次读数之间不发任何帧）与**持续读**（背靠背读 3 s）两种条件下各测一次。
  · 两者**必须相等**（±2%）—— 一个"时间"量不允许依赖上位机在不在读。
  · 而这个对照本身是**能失败**的：若它依赖主循环节奏，两个条件就会给出不同的数。
★ 这条之所以是本实验的核心：**真实部署恰好是"静默"那一种**。若限时只在被观测时才流逝，
  那这个安全网在真实运行下**等于不存在**，而所有"看着正常"的回归（都在持续读）都会漏掉它。

用法
  python tools/exp_er_motion_time.py                 # 全跑（约 20 s）
  python tools/exp_er_motion_time.py --json out.json
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

CMD_STATUS, CMD_STOP, CMD_START, CMD_BURST = 0x38, 0x12, 0x11, 0x22
# ★★ 步进命令住在 **0x39 的 sub-op 19** 里（`main.c` 的 `if (op == 19u)` 指的是
#   **0x39 载荷的第 0 字节**）。⇒ 载荷 = `[19][sub][arg:u32]` = **6 字节**。
#   ★ 两个坑都是我第一版踩的: ① 把 cmd 写成 `0x19`（那是另一个 op, 得到的是别家的应答）
#     ② 只发 5 字节 ⇒ `arg` 落进 `n >= 6u` 的门外 ⇒ **静默按 0 处理**（命令"发出去了", 值是 0）。
CMD_STEP = 0x39
SUBOP_STEP = 19
OFF_WIRE_MAP = 0x0240
OFF_EXEC_RING_HDR = 0x3880
W_LIMIT_AP = 65                      # wire[65] = 已应用限时余量镜像（每主循环刷新）
TICK_HEALTHY = 200000

# op=19 sub=0 的 112B 状态块（见 main.c 的 step 分支）
B_RATE, B_DIR, B_ENA, B_DL = 0, 4, 8, 12
B_CCER, B_DTMAX = 16, 64
# op=19 sub=11 装置态: +4 pol_set
# op=19 sub=13 运动源: 0=脚手架 1=程序面
# op=19 sub=14 运动面态: +0 src
# op=19 sub=19 斜坡态: +0 slope +4 cmd +8 out +12 actual +16 active +20 done_n


def read_src():
    """★ tick 周期从源码解析（与 E-Q 同纪律：**不手写 10**）。"""
    txt = open(os.path.join(ROOT, "src", "engine.h"), encoding="utf-8", errors="replace").read()
    m = re.search(r"^#define\s+TICK_PERIOD_US\s+(\d+)u?\b", txt, re.M)
    if not m:
        raise SystemExit("!! src/engine.h 找不到 TICK_PERIOD_US —— ticks↔ms 的来源断了")
    us = int(m.group(1))
    return dict(tick_us=us, ticks_per_ms=1000.0 / us)


def ring_head(dcl, shm):
    """(写计数, g_tick_count) —— ISR 直写, 无主循环镜像滞后（E-Q §2.1 的结论）。"""
    sts, p = dcl.send(CMD_BURST, struct.pack("<IH", shm + OFF_EXEC_RING_HDR, 2), expect_len=8)
    if sts != "ACK" or len(p) < 8:
        return None
    return struct.unpack("<2I", p[:8])


def step_cmd(dcl, sub, arg=0):
    """0x39 / sub-op 19: `[19][sub][arg:u32]` = 6 字节（★ 少了第 2 字节 arg 会被静默当 0）。"""
    sts, p = dcl.send(CMD_STEP, bytes([SUBOP_STEP, sub]) + struct.pack("<I", arg), expect_len=None)
    return sts, p


def step_read0(dcl):
    """sub=0 = **只查询, 零副作用**（112B）。★ 用它而不是 sub=1 之类 —— 读不能改变被测对象。"""
    sts, p = step_cmd(dcl, 0)
    if sts != "ACK" or len(p) < 68:
        return None
    return dict(rate=struct.unpack("<I", p[B_RATE:B_RATE + 4])[0],
                ena=struct.unpack("<I", p[B_ENA:B_ENA + 4])[0],
                deadline=struct.unpack("<I", p[B_DL:B_DL + 4])[0],
                ccer=struct.unpack("<I", p[B_CCER:B_CCER + 4])[0],
                dtmax=struct.unpack("<I", p[B_DTMAX:B_DTMAX + 4])[0])


def step_read(dcl, sub, n):
    sts, p = step_cmd(dcl, sub)
    if sts != "ACK" or len(p) < n:
        return None
    return list(struct.unpack("<%dI" % (n // 4), p[:n]))


def _wait_healthy(dcl, shm, tries=6, need=3):
    for i in range(tries):
        t0, good = time.time(), 0
        while time.time() - t0 < 12.0:
            v = ring_head(dcl, shm)
            if v and v[1] >= TICK_HEALTHY:
                good += 1
                if good >= need:
                    print("    [健康门] 第 %d 次: tick=%d 环写=%d ⇒ 开工（%.1f s）"
                          % (i + 1, v[1], v[0], time.time() - t0))
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
    ap.add_argument("--quiet-s", type=float, default=3.0, help="静默观测窗（s）")
    ap.add_argument("--busy-s", type=float, default=3.0, help="持续读观测窗（s）")
    ap.add_argument("--limit-ms", type=int, default=2000, help="R3 声明的限时（ms）")
    ap.add_argument("--pulse-hz", type=int, default=1000, help="R3 的脉冲频率")
    ap.add_argument("--ramp-slope", type=int, default=500, help="R4 声明的斜率（Hz/s）")
    ap.add_argument("--ramp-to", type=int, default=2000, help="R4 的斜坡终点（Hz）")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    S = read_src()
    TPM = S["ticks_per_ms"]          # 10.0
    print("=" * 74)
    print("E-R  运动域的「声明时间量」实现了吗（限时 / 斜坡斜率）")
    print("=" * 74)
    print("源码量: TICK_PERIOD_US = %d µs ⇒ **1 ms = %.0f 拍**（不手写 10）"
          % (S["tick_us"], TPM))
    print()
    print("判据清单（每条都能失败）:")
    print("  E0a 前置: 板子健康（ISR 直写的 tick 在百万级）")
    print("  E0b 前置: 运动源 = 脚手架（程序面会每圈覆写 wire[12..15]，量到的是别人下的指令）")
    print("  E0c 前置: ENA 极性已声明（否则 sub=1 起脉冲的路径与 fail-closed 纠缠）")
    print("  E0d 前置: 起手态 = 限时 0 / 斜坡 0 / 频率 0")
    print("  R1 ★ 静默下 **限时流逝率 = 1000 ms/s ±2%%**（对 tick 计数）")
    print("  R2 ★ 持续读下 同一声明 流逝率 = 1000 ms/s ±2%%")
    print("  R3 ★★ **R1 与 R2 必须相等**（±2%%）—— 时间量不许依赖上位机在不在读")
    print("  R3b ★★ 由 R1 推出的**部署条件**（无上位机）实现限时 = 声明 ±3%%")
    print("  R4 ★ 到点真的停: CC1E 落 0，且 (停止时刻 − 起点) = **声明的限时**（±3%%）")
    print("  R5 ★ 斜坡: 从 %.0f 爬到 %d Hz 的实测时长 = (%d−起)/%d s（±2%%）"
          % (0, args.ramp_to, args.ramp_to, args.ramp_slope))
    print("  R6 对照: 斜坡**关**时立即到位（<20 ms）")
    print()

    dcl = Dcl(args.port)
    print("端口 = %s" % dcl.port)
    time.sleep(1.0)
    res, skip, data = [], [], {}

    try:
        sts, p = dcl.send(CMD_STATUS, expect_len=51)
        if sts != "ACK" or len(p) < 51:
            print("!! 0x38 失败"); return 2
        shm = struct.unpack("<I", p[23:27])[0]
        print("SHM = 0x%08X" % shm)
        if not _wait_healthy(dcl, shm):
            print("\n[exit] 板子不健康 ⇒ **判无效**"); return 2

        # ── 前置 ──────────────────────────────────────────────────────
        print("\n── 前置 ──")
        h = ring_head(dcl, shm)
        res.append(("E0a 板子健康: ISR 直写 tick = %s ≥ %d" % (h[1] if h else "?", TICK_HEALTHY),
                    bool(h) and h[1] >= TICK_HEALTHY))
        step_cmd(dcl, 13, 0)                       # 运动源 = 脚手架
        m14 = step_read(dcl, 14, 32)
        src = m14[0] if m14 else -1
        res.append(("E0b 运动源 = 脚手架（读回 %d）" % src, src == 0))
        st11 = step_read(dcl, 11, 32)
        pol_set = st11[1] if st11 else -1
        res.append(("E0c ENA 极性已声明（pol_set = %d）" % pol_set, pol_set == 1))
        step_cmd(dcl, 17, 0)                       # 斜坡关
        step_cmd(dcl, 4, 0)                        # 限时 0
        step_cmd(dcl, 1, 0)                        # 频率 0
        time.sleep(0.2)
        s0 = step_read0(dcl)
        if s0 is None:
            print("!! 读 step 状态失败 ⇒ 判无效"); return 2
        print("    起手态: rate=%d deadline=%d ccer=0x%X dt_max=%d"
              % (s0["rate"], s0["deadline"], s0["ccer"], s0["dtmax"]))
        res.append(("E0d 起手态 rate=0 / deadline=0 / 斜坡=0",
                    s0["rate"] == 0 and s0["deadline"] == 0))

        # ── R1/R2: 限时流逝率（静默 vs 持续读）─────────────────────────
        L_BIG = 20000            # 20 s：两个窗口都吃不穿它（否则测到的是"饱和"不是"速率"）
        print("\n── R1 静默窗口（%.1f s, 期间**不发任何帧**）──" % args.quiet_s)
        step_cmd(dcl, 4, L_BIG)
        h0 = ring_head(dcl, shm); a0 = step_read0(dcl)
        time.sleep(args.quiet_s)
        h1 = ring_head(dcl, shm); a1 = step_read0(dcl)
        d_tick = h1[1] - h0[1]
        d_dl = a0["deadline"] - a1["deadline"]
        # ★ 单位（第一版错在这里）: Δdeadline 是 **ms**, Δtick/TPM 也是 **ms**
        #   ⇒ 商是**无量纲**的, 要 ×1000 才是「ms 流逝 / 真实秒」。
        #   错的那版把 746 ms/s 印成 "0.7 ms/s", 于是 R1 相对偏差 −99.9%、
        #   而 R3 的 |差|/1000 变成 0.02% ⇒ **把 25% 的失配读成"完全一致"**。
        rate_q = (d_dl * 1000.0 / (d_tick / TPM)) if d_tick else float("nan")
        print("    tick %d → %d（Δ=%d 拍 = %.1f ms）· deadline %d → %d（Δ=%d ms）"
              % (h0[1], h1[1], d_tick, d_tick / TPM, a0["deadline"], a1["deadline"], d_dl))
        print("    ⇒ 流逝率 = %.1f ms/s（声明 1000）· 相对 %+.1f%%"
              % (rate_q, 100.0 * (rate_q / 1000.0 - 1.0)))
        print("    ★ 读数含义: 若 Δ=0 而 Δtick≈%d, 说明**这 %d 拍里的每一次主循环**都满足"
              % (int(args.quiet_s * 1000 * TPM), d_tick))
        print("      `floor(dt/10)==0` ⇒ 限时在那段时间里**一步都没走**。")
        res.append(("R1 静默下流逝率 = 1000 ms/s ±2%%（实测 %.1f）" % rate_q,
                    abs(rate_q / 1000.0 - 1.0) <= 0.02))
        data["rate_quiet"] = rate_q

        print("\n── R2 持续读窗口（%.1f s, 背靠背读）──" % args.busy_s)
        step_cmd(dcl, 4, L_BIG)
        h0 = ring_head(dcl, shm); a0 = step_read0(dcl)
        t_end = time.time() + args.busy_s
        n = 0
        while time.time() < t_end:
            step_read0(dcl); n += 1
        h1 = ring_head(dcl, shm); a1 = step_read0(dcl)
        d_tick = h1[1] - h0[1]
        d_dl = a0["deadline"] - a1["deadline"]
        rate_b = (d_dl * 1000.0 / (d_tick / TPM)) if d_tick else float("nan")
        print("    %d 次读 · tick Δ=%d 拍 = %.1f ms · deadline Δ=%d ms" % (n, d_tick, d_tick / TPM, d_dl))
        print("    ⇒ 流逝率 = %.1f ms/s（声明 1000）· 相对 %+.1f%%"
              % (rate_b, 100.0 * (rate_b / 1000.0 - 1.0)))
        res.append(("R2 持续读下流逝率 = 1000 ms/s ±2%%（实测 %.1f）" % rate_b,
                    abs(rate_b / 1000.0 - 1.0) <= 0.02))
        data["rate_busy"] = rate_b
        dev = abs(rate_q - rate_b) / 1000.0
        res.append(("R3 ★★ 两种宿主条件下的流逝率必须相等（|差| ≤2%%; 实测 %.1f vs %.1f = %.1f%%）"
                    % (rate_q, rate_b, 100.0 * dev), dev <= 0.02))
        # ★ 部署条件（**没有上位机在读**）下的实现限时 = L / 流逝率
        impl = L_BIG / (rate_q / 1000.0)
        print("    ★ 部署条件推演: 静默流逝率 %.1f ms/s ⇒ 声明 %d ms 的限时**实际会走 %.0f ms**"
              "（%+.1f%%）" % (rate_q, args.limit_ms, impl * args.limit_ms / L_BIG,
                              100.0 * (rate_q / 1000.0 - 1.0)))
        res.append(("R3b 部署条件（静默）下的实现限时 = 声明 ±3%%（实得 %.0f ms / 声明 %d ms）"
                    % (impl * args.limit_ms / L_BIG, args.limit_ms),
                    abs(rate_q / 1000.0 - 1.0) <= 0.03))

        # ── R4: 到点真的停，且停止时刻 = 声明的限时 ────────────────────
        print("\n── R4 到点自停（声明限时 %d ms @ %d Hz）──" % (args.limit_ms, args.pulse_hz))
        step_cmd(dcl, 4, 0)
        step_cmd(dcl, 1, args.pulse_hz)
        time.sleep(0.2)
        sa = step_read0(dcl)
        step_cmd(dcl, 4, args.limit_ms)            # ★ 起算点 = 这一帧之后立刻读的 tick
        h0 = ring_head(dcl, shm)
        print("    起点 tick=%d, 起脉冲后 rate=%d CCER=0x%X" % (h0[1], sa["rate"], sa["ccer"]))
        t_limit = time.time() + 3.0 * (args.limit_ms / 1000.0) + 2.0
        stopped, h1, sl = False, None, None
        while time.time() < t_limit:
            sl = step_read0(dcl)
            if sl and (sl["ccer"] & 1) == 0:
                h1 = ring_head(dcl, shm); stopped = True; break
        if not stopped:
            print("    !! %.1f s 内 **CC1E 一直是 1**（限时没让它停）" % (3.0 * args.limit_ms / 1000.0 + 2.0))
            res.append(("R4 到点自停: 声明 %d ms 内 CC1E 落 0" % args.limit_ms, False))
            skip.append("R4 的『停止时刻 = 声明限时』—— 根本没停, 无法评时刻（不是 PASS）")
        else:
            real_ms = (h1[1] - h0[1]) / TPM
            print("    停止: tick=%d ⇒ 实测 %.0f ms（声明 %d）· 相对 %+.1f%%"
                  % (h1[1], real_ms, args.limit_ms, 100.0 * (real_ms / args.limit_ms - 1.0)))
            print("    ★ 判据是**单侧**的: 实现值允许**偏晚**（主循环被长阻塞时 dt 被钳到 100 ms，"
                  "宁可晚停也不早停），但不许偏早、也不许差出容差。")
            res.append(("R4 到点自停 + 停止时刻 = 声明 %d ms ±3%%（实测 %.0f）"
                        % (args.limit_ms, real_ms),
                        (sl["ccer"] & 1) == 0
                        and abs(real_ms / args.limit_ms - 1.0) <= 0.03))
            data["stop_ms"] = real_ms

        # ── R5/R6: 斜坡斜率 ──────────────────────────────────────────
        print("\n── R5 斜坡（声明 %d Hz/s, 0 → %d Hz）──" % (args.ramp_slope, args.ramp_to))
        step_cmd(dcl, 4, 0)
        step_cmd(dcl, 1, 0)                        # 先停
        step_cmd(dcl, 17, args.ramp_slope)         # 开斜坡（起点 = 当前硬件频率 = 0）
        time.sleep(0.15)
        r19 = step_read(dcl, 19, 32)
        print("    开斜坡后: slope=%d cmd=%d out=%d" % (r19[0], r19[1], r19[2]))
        step_cmd(dcl, 1, args.ramp_to)
        h0 = ring_head(dcl, shm)
        t0 = time.time()
        t_end = t0 + 3.0 * (args.ramp_to / float(args.ramp_slope)) + 2.0
        got, h1 = None, None
        while time.time() < t_end:
            r19 = step_read(dcl, 19, 32)
            if r19 and r19[2] >= args.ramp_to and r19[4] == 0:      # out 到目标 且 ramp_active=0
                h1 = ring_head(dcl, shm); got = r19; break
        if not got:
            print("    !! 斜坡没在预期时间内到目标")
            res.append(("R5 斜坡按声明到达目标 %d Hz" % args.ramp_to, False))
            skip.append("R5 的『爬升时长』—— 没到目标, 无法评时刻（不是 PASS）")
        else:
            real_s = (h1[1] - h0[1]) / (TPM * 1000.0)
            want_s = args.ramp_to / float(args.ramp_slope)
            print("    tick %d → %d ⇒ 实测爬升 %.4f s（声明 %d/%d = %.4f s）· 相对 %+.2f%%"
                  % (h0[1], h1[1], real_s, args.ramp_to, args.ramp_slope, want_s,
                     100.0 * (real_s / want_s - 1.0)))
            res.append(("R5 斜坡爬升时长 = 声明斜率倒推的 %.3f s ±2%%（实测 %.4f）" % (want_s, real_s),
                        abs(real_s / want_s - 1.0) <= 0.02))
            data["ramp_s"] = real_s

        print("\n── R6 对照: 斜坡关 ⇒ 立即到位 ──")
        step_cmd(dcl, 1, 0); step_cmd(dcl, 17, 0); time.sleep(0.1)
        h0 = ring_head(dcl, shm)
        step_cmd(dcl, 1, args.ramp_to)
        r19 = step_read(dcl, 19, 32)
        h1 = ring_head(dcl, shm)
        dt_ms = (h1[1] - h0[1]) / TPM
        # ★ 容差怎么定（第一版写死 20 ms 是错的 —— 那量的是**两帧串口往返**，不是固件行为）:
        #   对照要区分的是"立即"与"按斜率爬"，而爬完要 4 s ⇒ 判据取
        #   "确认到位所用的时间 < 斜坡时长的 10%" 即可把两者判死，且不受串口延迟影响。
        ramp_ms = args.ramp_to / float(args.ramp_slope) * 1000.0
        print("    rate_out=%d（目标 %d）· 命令→确认间隔 %.1f ms（斜坡时长 %.0f ms 的 %.1f%%）"
              % (r19[2], args.ramp_to, dt_ms, ramp_ms, 100.0 * dt_ms / ramp_ms))
        res.append(("R6 斜坡关时立即到位（out=%d, 确认间隔 %.1f ms < 斜坡时长的 10%%）"
                    % (r19[2], dt_ms),
                    r19[2] == args.ramp_to and dt_ms < 0.10 * ramp_ms))

    finally:
        try:
            step_cmd(dcl, 1, 0)          # 停脉冲
            step_cmd(dcl, 4, 0)          # 清限时
            step_cmd(dcl, 17, 0)         # 关斜坡
            step_cmd(dcl, 13, 0)         # 运动源回脚手架（**复位前状态**）
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
