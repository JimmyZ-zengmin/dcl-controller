#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_swd_read.py — 不经串口，直接经 SWD 读固件全局量

为什么需要它
------------
串口(CH340)不在时，协议口不通，但 **SWD(DAPLink) 在** ⇒ 固件把结果写进全局量，
PC 端直接读内存即可验收。本项目"零外设验证手法"的同一条路子。

★★ 纪律（踩过坑的）：
  · **不能用 `-c reset`** —— 启动会把观测面清零，"读之前先复位" = 自毁证据。
  · 用 **`connect_mode=halt`**（挂核但不复位），而不是 `attach`
    （某些 CMSIS-DAP 探针在 attach 下初始化不了 AP）。
  · ★ 只读 **RAM**。**AHB 外设寄存器不要用调试器读**（本项目实测会返回垃圾 0xABFFFFFF）。

用法：
    python h723_swd_read.py 0x2000007C 0x2000000C ...        # 读一串 u32
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    addrs = [int(a, 0) for a in sys.argv[1:]]
    if not addrs:
        print("用法: h723_swd_read.py <addr> [addr ...]")
        return

    from pyocd.core.helpers import ConnectHelper
    sess = ConnectHelper.session_with_chosen_probe(
        target_override="stm32h723xx",
        options={"connect_mode": "halt", "frequency": 2000000},
        blocking=False,
    )
    if sess is None:
        print("✗ 找不到探针")
        return
    with sess:
        t = sess.target
        t.halt()                      # 挂核 (不复位)
        for a in addrs:
            try:
                v = t.read32(a)
                print("0x%08X = 0x%08X  (%d)" % (a, v, v))
            except Exception as e:
                print("0x%08X = ✗ %s" % (a, e))
        t.resume()                    # 读完放开, 让固件继续跑


if __name__ == "__main__":
    main()
