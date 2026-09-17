#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_step_ramp_test.py —— **轨迹规划 / 加减速（斜坡限幅）** 判据。

对应 `README` 能力边界 **A 类第一项** 与 `src/step.h` 的"轨迹规划"段。

## 为什么它最值钱
实测：**突加** 31000 Hz ⇒ 只能用到 **627 rpm**（比值 0.565，**掉步**）；
      **带斜坡** ⇒ **~1100 rpm**。⇒ **缺斜坡 = 可用转速被砍 47%**。

## 实现要点（判据要能解释"凭什么信"）
斜坡挂在 **`step_set_rate()`** 里 —— 那是**唯一**的频率写入口
（脚手架 `sub=1` 与程序面 `wire[12]` 都经过它）⇒ 两条通路同时获得限幅。
推进在主循环 `step_tick`，落地走**轻量路径**（只写预装载 `ARR`/`CCR1`）
⇒ **不关 `CC1E`、不写 `EGR.UG`** ⇒ 不切断脉冲、**不污染"走 N 步"的硬件计数**。

## 判据
| # | 判据 | 怎么让它失败 |
|---|---|---|
| R1 | ★ **默认关 ⇒ 既有行为零变化**：`ramp_hz_s==0` 时 `sub=1 arg=31000` **立即**到 31000 | 回归保护 |
| R2 | ★ **斜坡真的在爬**：设 20000 Hz/s 后，`rate_out` 出现中间值（不是一步到位）| — |
| R3 | ★ **斜率符合**：从 0 爬到 31000 用时 ≈ 31000/20000 = **1.55 s**（±50%）| 斜率被忽略 |
| R4 | ★ **停永远是立即**（安全）：斜坡开着时 `sub=1 arg=0` ⇒ `rate` **立刻** 0 | 慢慢降 = 不安全 |
| R5 | ★★★ **收益兑现**：突加 vs 斜坡，**斜坡的有效转速显著更高** | 两者一样 ⇒ 斜坡白做 |

用法: python tools/h723_step_ramp_test.py [--port COM21]
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

STEPS_PER_REV = 1600          # 8 细分
DEG_PER_STEP = 360.0 / STEPS_PER_REV
N_PASS = N_FAIL = N_SKIP = 0


def record(name, ok, detail=""):
    global N_PASS, N_FAIL
    if ok:
        N_PASS += 1
        print("  [PASS] %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        N_FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  " + detail) if detail else ""))


def main():
    port = None
    if "--port" in sys.argv:
        port = sys.argv[sys.argv.index("--port") + 1]
    d = Dcl(port or find_board())

    def st0():
        s, p = d.send(0x39, bytes([19, 0]) + struct.pack("<I", 0))
        return struct.unpack("<24I", p[:96]) if (s == "ACK" and len(p) >= 96) else None

    def ramp():
        s, p = d.send(0x39, bytes([19, 19]))
        if s != "ACK" or len(p) != 32:        # ★ 长度必须校验（未实现的 sub 会返回 96B 残留）
            return None
        u = struct.unpack("<8I", p[:32])
        return dict(slope=u[0], cmd=u[1], out=u[2], actual=u[3],
                    active=u[4], done=u[5])

    def sx(n, v):
        return d.send(0x39, bytes([19, n]) + struct.pack("<I", v))[0]

    try:
        if st0() is None or ramp() is None:
            print("  ✗ `sub=0/19` 无应答或长度不对 ⇒ 固件不是本版 ⇒ 全部判**无效**")
            return 1
        print("=== 轨迹规划（斜坡限幅）判据 ===")

        # 前置：极性 + 使能 + 方向 + 运动源=脚手架
        sx(13, 0); sx(5, 1); sx(3, 1); sx(2, 0)
        sx(17, 0); sx(1, 0); sx(6, 0)         # 斜坡关 + 停 + 清零
        time.sleep(0.4)

        # ── R1 ★ 默认关 ⇒ 立即到目标（回归保护）──
        sx(17, 0)
        sx(1, 31000)
        time.sleep(0.25)
        r = ramp()
        # ★ 容差：`step_set_rate` 明确"回报**实际**频率，不是请求值"（ARR 是整数除法）
        #   ⇒ 31000 请求对应 actual=31250（量化）是**正确行为**。判据要按量化容差，不是等值。
        tol_hz = max(31000 * 0.02, 100)
        record("R1 默认(斜坡关) ⇒ 请求**立即**生效（既有行为零变化）",
               r["slope"] == 0 and abs(r["actual"] - 31000) <= tol_hz and r["out"] == r["cmd"],
               "slope=%d cmd=%d out=%d actual=%d (容差±%.0f，ARR 量化)"
               % (r["slope"], r["cmd"], r["out"], r["actual"], tol_hz))
        sx(1, 0); time.sleep(0.3)

        # ── R2/R3 斜坡：爬坡过程与用时 ──
        sx(17, 20000)                          # 斜率 20000 Hz/s
        sx(1, 31000)
        t0 = time.time()
        mids, reached_at = [], None
        while time.time() - t0 < 3.2:
            r = ramp()
            if r and 0 < r["out"] < 31000:
                mids.append(r["out"])
            if r and r["out"] >= 31000 and reached_at is None:
                reached_at = time.time() - t0
                break
            time.sleep(0.06)
        record("R2 斜坡**真的在爬**（出现中间值，不是一步到位）",
               len(mids) >= 3, "采到 %d 个中间值，例: %s" % (len(mids), mids[:6]))
        exp = 31000.0 / 20000.0                # 1.55 s
        if reached_at is None:
            record("R3 斜率符合（爬完约 %.2f s）" % exp, False, "3.2 s 内没爬到目标")
        else:
            record("R3 斜率符合：0→31000 @20000 Hz/s ⇒ 用时 ≈ %.2f s" % exp,
                   abs(reached_at - exp) <= exp * 0.5,
                   "实测 %.2f s（期望 %.2f±%.2f）" % (reached_at, exp, exp * 0.5))

        # ── R4 ★ 停永远是立即 ──
        sx(17, 2000)                           # 用**慢**斜坡，让"立即停"与"慢慢降"能区分
        sx(1, 30000)
        time.sleep(0.5)
        sx(1, 0)                               # 请求停
        time.sleep(0.2)
        r = ramp()
        record("R4 停**永远是立即**（斜坡开着也不慢慢降 —— 安全语义）",
               r["actual"] == 0, "actual=%d out=%d cmd=%d" % (r["actual"], r["out"], r["cmd"]))

        # ── R5 ★★★ 收益兑现：突加 vs 斜坡 ──
        # ★★ 判据设计（同一性对照）：**两段都先进入稳态再计步**。
        #   否则斜坡那段的前 1.55 s 在爬坡（平均半频）⇒ 总步数被"吃掉" ⇒ 比不出提升，
        #   而结论会是"斜坡没用"——**那是判据的错，不是功能的错**。
        def run_case(slope, hz, sec):
            sx(6, 0); sx(17, 0); time.sleep(0.3)
            if slope:
                sx(17, slope)
            sx(1, hz)
            # 等进入稳态（斜坡段要等爬完；突加段 0.4 s 即稳）
            t0 = time.time()
            while time.time() - t0 < 4.0:
                r = ramp()
                if r and r["actual"] >= hz:
                    break
                time.sleep(0.05)
            raw0 = st0()[8]
            time.sleep(sec)
            r2 = st0()[8]
            sx(1, 0); sx(17, 0); time.sleep(0.4)
            dr = (r2 - raw0) & 0xFFF
            exp_c = hz * sec / float(STEPS_PER_REV) * 4096.0
            while dr - exp_c > 2048.0:
                dr -= 4096
            while exp_c - dr > 2048.0:
                dr += 4096
            return dr / exp_c                      # 稳态段"实际/理论"比值

        k_flat = run_case(0, 31000, 2.0)
        k_ramp = run_case(20000, 31000, 2.0)
        rpm = lambda k: k * 31000.0 / STEPS_PER_REV * 60.0
        record("R5 ★★★ 收益兑现（**稳态段**对照）：斜坡的有效转速显著更高",
               k_ramp > k_flat * 1.15,
               "突加 比值%.3f(≈%.0f rpm)  vs  斜坡 比值%.3f(≈%.0f rpm)  ⇒ 提升 %+.0f%%"
               % (k_flat, rpm(k_flat), k_ramp, rpm(k_ramp),
                  100.0 * (k_ramp - k_flat) / max(k_flat, 1e-9)))

        sx(6, 0); sx(17, 0)
        print()
        print("── 汇总: PASS %d / FAIL %d / SKIP %d ──" % (N_PASS, N_FAIL, N_SKIP))
        return 1 if N_FAIL else 0
    finally:
        d.close()


if __name__ == "__main__":
    sys.exit(main())
