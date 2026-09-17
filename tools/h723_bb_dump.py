#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_bb_dump.py —— 用 pyocd **API** 单会话 dump 黑匣子环（临时脚手架）

★ 为什么必须单会话：本机探针**每次会话开始/结束都会复位目标** ⇒ 分多次读拿到的是
  **不同时刻的环**（互相矛盾）。★ 而 `pyocd commander -c read32` 单次只有 ~60 KB，
  环是 **240 KB** ⇒ 读不全。⇒ 用 API 一次 `read_memory_block8` 读完（实测 1.4 s）。

★ 为什么"运动中途 halt"是对的：环只有 **96 ms**，若"先停脉冲再读"（进程启动就 ~0.5 s）
  **环早滚过去了**。而 `halt` 把环**冻结在"halt 前 96 ms"** —— 那正是**稳态高速段**。
  （TIM3 是硬件定时器，halt 期间脉冲照发，所以这不是"停下来再测"。）

用法: <系统 python> _bb_dump.py <输出bin路径>
"""
import sys
import time

from pyocd.core.helpers import ConnectHelper

BB_BASE = 0x24004000
BB_BYTES = 240 * 1024


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "_bb.bin"
    t0 = time.time()
    s = ConnectHelper.session_with_chosen_probe(
        target_override="stm32h723xx",
        options={"connect_mode": "halt", "resume_on_disconnect": True})
    s.open()
    try:
        tgt = s.target
        tgt.halt()
        data = bytes(tgt.read_memory_block8(BB_BASE, BB_BYTES))
        with open(out, "wb") as f:
            f.write(data)
        print("dump %d 字节 -> %s (%.1fs)" % (len(data), out, time.time() - t0))
        tgt.resume()
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
