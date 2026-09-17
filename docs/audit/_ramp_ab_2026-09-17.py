#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_ramp_ab.py —— 一次性对照实验（**临时脚手架，不进交付物**）

## 要回答的问题
"斜坡的收益"到底是什么？三个候选自变量：
  ① **斜率**（升频快慢）      ② **改频方式**（阶跃 vs 连续）    ③ **改频时是否切断脉冲**
  线索：`step_set_rate()` 每次都 `TIM_CCER &= ~CC1E` ⇒ **完整路径每次都切断一个脉冲**；
        而固件斜坡走**轻量路径** ⇒ **零切断**。

## 设计（同一频率、同一稳态测量 ⇒ 唯一变量 = 改频方式）
| 段 | 怎么加 | 切断次数 |
|---|---|---|
| **C** | `sub=1 arg=31000` 一次（固件单次阶跃）| **1** |
| **A** | 外部每 80 ms 发一次 `sub=1`，37 次 300→31000（**复刻套件 ramp**）| **37** |
| **B** | `sub=17 arg=20000` + `sub=1 arg=31000`（固件斜坡，轻量路径）| **0** |

★ 三段都：等稳态 → 读 raw0 → 保持 2.0 s → 读 raw1 ⇒ 比值 = 实测步数 / 理论步数。
★ 解卷绕按**预期值**做（不能用固定 >2048 阈值 —— 那是今天踩过的坑）。

用法: python build/_ramp_ab.py
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from h723_client import Dcl, find_board       # noqa: E402

HZ = 31000
SEC = 2.0
SPR = 1600


def main():
    d = Dcl(find_board())

    def st0():
        s, p = d.send(0x39, bytes([19, 0]) + struct.pack("<I", 0))
        return struct.unpack("<24I", p[:96]) if (s == "ACK" and len(p) >= 96) else None

    def ramp():
        s, p = d.send(0x39, bytes([19, 19]))
        return struct.unpack("<8I", p[:32]) if (s == "ACK" and len(p) == 32) else None

    def sx(n, v):
        return d.send(0x39, bytes([19, n]) + struct.pack("<I", v))[0]

    def prep():
        sx(13, 0); sx(5, 1); sx(3, 1); sx(2, 0)
        sx(17, 0); sx(1, 0); sx(6, 0)
        time.sleep(0.4)

    def steady_wait(timeout=6.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = ramp()
            if r and r[3] >= HZ:          # rate_actual 到位
                return True
            time.sleep(0.08)
        return False

    def measure():
        raw0 = st0()[8]
        time.sleep(SEC)
        raw1 = st0()[8]
        dr = (raw1 - raw0) & 0xFFF
        exp = HZ * SEC / float(SPR) * 4096.0
        while dr - exp > 2048.0:
            dr -= 4096
        while exp - dr > 2048.0:
            dr += 4096
        return dr / exp

    res = {}

    # ── C 固件单次阶跃（1 次切断）──
    prep()
    sx(1, HZ); steady_wait(); res["C 固件单次(1 次切断)"] = measure()
    prep()

    # ── A 外部分段阶梯，复刻套件 ramp（37 次切断）──
    start, nseg, seg_ms = 300, 37, 80
    sx(1, start)
    for k in range(1, nseg + 1):
        sx(1, int(start + (HZ - start) * k / nseg))
        time.sleep(seg_ms / 1000.0)
    steady_wait(); res["A 外部分段(37 次切断)"] = measure()
    prep()

    # ── B 固件斜坡（0 次切断）──
    sx(17, 20000); sx(1, HZ); steady_wait(); res["B 固件斜坡(0 次切断)"] = measure()
    prep()

    print("=== 同一频率 %d Hz、同一稳态测量、唯一变量 = 改频方式 ===" % HZ)
    base = res["C 固件单次(1 次切断)"]
    for k in ("C 固件单次(1 次切断)", "A 外部分段(37 次切断)", "B 固件斜坡(0 次切断)"):
        v = res[k]
        print("  %-26s 比值 %.3f  (≈%.0f rpm)  %s"
              % (k, v, v * HZ / SPR * 60.0,
                 "" if k.startswith("C") else "相对 C %+.1f%%" % (100 * (v - base) / base)))
    print()
    if res["A 外部分段(37 次切断)"] < base * 0.9:
        print("⇒ ★★ 分段切断**确实**是失步元凶 ⇒ 斜坡的价值 = **改频不切断脉冲**")
    else:
        print("⇒ 分段切断**不是**主因 ⇒ 收益另有来源（或 0.565 与电机状态有关）")
    d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
