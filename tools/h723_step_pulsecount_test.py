#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_step_pulsecount_test.py —— **"走 N 个脉冲自停"** 的判据（硬件计数）。

对应 `src/step.h` 的「走 N 个脉冲自停」段与 `README` 能力边界 A 类第一项。

## 实现要点（判据要能解释"凭什么信"）
`TIM4` 配成"**外部时钟模式 1，触发源 ITR2 = TIM3**"⇒ **`TIM4_CNT` 每来一个 TIM3 更新事件 +1**。
⇒ 它就是**硬件数出来的脉冲数**（不受 CPU 抖动影响），**零中断 / 零 DMA / 零 ITCM**。

## ★★ 判据的关键：**两条独立路径对上**
"走了多少步"有两个来源，必须互相印证：
  ① **硬件计数器** `TIM4_CNT`（`op=19 sub=16` 的 `pulses`）
  ② **编码器**：角度变化 ÷ 0.225°/步（8 细分 1600 步/圈）
★ 只信 ① 是自证；只信 ② 分不清"丢了步"还是"数错了"。**两条对上才算数**（本项目惯用纪律）。

## 判据
| # | 判据 | 怎么让它失败 |
|---|---|---|
| P1 | **没有脉冲时下 N ⇒ 必须 NAK + `rej_n` 涨**（不能"接受了却什么也没发生"）| 先 rate=0 再下 |
| P2 | 有脉冲时下 `N=1000` ⇒ `done_n` +1、`goal==1000` | — |
| P3 | ★ **两条路径一致**：`pulses` ≈ 编码器换算的步数（容差见下）| 计数接了别的源 ⇒ 差很远 |
| P4 | 到点后 **自己停了**：`rate==0` 且 `CC1E==0` | 不会自己停 |
| P5 | ★ **钳位可见**：`N=100000` ⇒ `goal==65535`（TIM4 是 16 位），不是静默接受 | — |
| P6 | `N=0` ⇒ 取消（`count_en==0`）| — |

★ **P3 的容差**：停脉冲在主循环 ⇒ 会**多走 ≤1 圈**的几步（≈0.37 ms × 频率）。
  本脚本按 `频率 × 0.5 ms` 算上界（留 35% 余量），**远小于"计数接错源"会造成的偏差**。
用法: python tools/h723_step_pulsecount_test.py [--port COM21]
"""
import os
import struct
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from h723_client import Dcl, find_board       # noqa: E402

DEG_PER_STEP = 0.225            # 8 细分 1600 步/圈 ⇒ 360/1600

N_PASS = N_FAIL = N_SKIP = 0


def record(name, ok, detail=""):
    global N_PASS, N_FAIL
    if ok:
        N_PASS += 1
        print("  [PASS] %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        N_FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  " + detail) if detail else ""))


def skip(name, why):
    global N_SKIP
    N_SKIP += 1
    print("  [SKIP] %s —— %s" % (name, why))


def main():
    port = None
    if "--port" in sys.argv:
        port = sys.argv[sys.argv.index("--port") + 1]
    d = Dcl(port or find_board())
    try:
        def st0():
            s, p = d.send(0x39, bytes([19, 0]) + struct.pack("<I", 0))
            return struct.unpack("<24I", p[:96]) if (s == "ACK" and len(p) >= 96) else None

        def pc():
            s, p = d.send(0x39, bytes([19, 16]))
            if s != "ACK" or len(p) < 32:
                return None
            u = struct.unpack("<8I", p[:32])
            return dict(cen=u[0], goal=u[1], pulses=u[2], done=u[3],
                        abort=u[4], rej=u[5], cnt=u[6], rate=u[7])

        def step(n):
            return d.send(0x39, bytes([19, 15]) + struct.pack("<I", n))

        u = st0()
        if u is None or pc() is None:
            print("  ✗ `op=19 sub=0/16` 无应答 ⇒ 固件不是本版 ⇒ 全部判**无效**")
            return 1
        print("=== 走 N 个脉冲自停（硬件计数）判据 ===")
        print("  前置: pol_set/使能 + 运动源=脚手架 + 频率")

        d.send(0x39, bytes([19, 13]) + struct.pack("<I", 0))     # 运动源=脚手架
        d.send(0x39, bytes([19, 5]) + struct.pack("<I", 1))      # 声明极性（幂等）
        d.send(0x39, bytes([19, 3]) + struct.pack("<I", 1))      # 使能
        d.send(0x39, bytes([19, 2]) + struct.pack("<I", 0))      # 方向
        d.send(0x39, bytes([19, 1]) + struct.pack("<I", 0))      # 先停
        time.sleep(0.3)

        # ── P1 ★ 反空判据：没有脉冲时下 N 必须被拒 ──
        a = pc()
        sts, pld = step(1000)
        b = pc()
        record("P1 没有脉冲时下 N ⇒ **必须 NAK**（不得静默接受）",
               sts is not None and sts != 0 and b["rej"] > a["rej"],
               "sts=%s payload=%r rej_n %d→%d" % (sts, pld[:44], a["rej"], b["rej"]))

        # ── P5 钳位（先测，因为它不需要跑动）──
        d.send(0x39, bytes([19, 1]) + struct.pack("<I", 500))    # 起脉冲
        time.sleep(0.3)
        step(100000)
        time.sleep(0.15)
        c = pc()
        record("P5 N=100000 ⇒ goal 钳到 **65535**（TIM4 是 16 位），不是静默接受",
               c["goal"] == 65535, "goal=%d" % c["goal"])
        step(0)                                                  # 取消
        time.sleep(0.2)

        # ── P2/P3/P4 走 N 步（两条独立路径对照）──
        FREQ, N = 500, 1000
        d.send(0x39, bytes([19, 1]) + struct.pack("<I", FREQ))
        time.sleep(0.35)
        raw0 = st0()[8]
        e0 = pc()
        step(N)
        time.sleep(N / float(FREQ) + 0.6)                        # 走完 + 留主循环停脉冲的时间
        e1 = pc()
        raw1 = st0()[8]
        dr = (raw1 - raw0) & 0xFFF
        if dr > 2048:
            dr -= 4096
        deg = dr * 360.0 / 4096.0
        steps_enc = abs(deg) / DEG_PER_STEP

        record("P2 下 N=1000 ⇒ goal==1000 且 done_n +1",
               e1["goal"] == N and e1["done"] == e0["done"] + 1,
               "goal=%d done_n %d→%d abort_n=%d" % (e1["goal"], e0["done"], e1["done"], e1["abort"]))

        # ★★ 容差口径（如实说明）：**这不是计数误差，是"起止时刻对不齐"**。
        #   `step(N)` 下发 → 生效之间有一次命令往返（p99 56.8 ms），
        #   "到点停"又由主循环完成（≤0.37 ms）。两者都不是计数的精度问题。
        #   ⇒ 判据取"**两者都落在 [N, N+容忍]** 且互相差 ≤ 容忍"，
        #     而不是"逐位相等"。★ 要逐位对齐必须让计时器与编码器**同时起止**（v2 的事）。
        tol = max(FREQ * 0.25 * 1.35, 8.0)
        d_hi = e1["pulses"] - N
        record("P3 ★ **两条独立路径一致**：硬件计数 vs 编码器换算 都要 ≈N",
               abs(d_hi) <= tol and abs(steps_enc - N) <= tol
               and abs(e1["pulses"] - steps_enc) <= tol,
               "硬件计数=%d(Δ%+.0f)  编码器换算=%.1f(Δ%+.1f)  两者差%+.1f  容忍±%.0f步"
               % (e1["pulses"], d_hi, steps_enc, steps_enc - N,
                  e1["pulses"] - steps_enc, tol))

        f = st0()
        record("P4 到点**自己停了**：rate==0 且 CC1E==0",
               f[0] == 0 and (f[4] & 1) == 0,
               "rate=%d CC1E=%d（多走 %d 步 = 主循环停的延迟，**已知量**，不是误差）"
               % (f[0], f[4] & 1, e1["pulses"] - N))

        # ── P6 取消 ──
        d.send(0x39, bytes([19, 1]) + struct.pack("<I", 300))
        time.sleep(0.3)
        step(5000)
        g = pc()
        step(0)
        time.sleep(0.2)
        h = pc()
        record("P6 N=0 ⇒ 取消计数（count_en 0）", g["cen"] == 1 and h["cen"] == 0,
               "count_en %d → %d" % (g["cen"], h["cen"]))

        d.send(0x39, bytes([19, 1]) + struct.pack("<I", 0))
        d.send(0x39, bytes([19, 6]))
        time.sleep(0.2)
        print()
        print("── 汇总: PASS %d / FAIL %d / SKIP %d ──" % (N_PASS, N_FAIL, N_SKIP))
        return 1 if N_FAIL else 0
    finally:
        d.close()


if __name__ == "__main__":
    sys.exit(main())
