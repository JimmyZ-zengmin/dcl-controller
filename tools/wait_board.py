#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wait_board.py -- 等板子**管理面可用** (复位后别急着下结论)

═══ 为什么需要它 (2026-09-13 实测) ═══
  复位之后, 板子要 **~9s 起才应答**, 而且带过故障注入/垃圾配置时实测到 **22s / 33s**。
  这段时间主循环还没跑到协议处理 (SD 初始化/黑匣子那一段会阻塞很久)。
  ⇒ 如果在复位后立刻读, 会拿到 "0x64 无应答", 然后把它误判成:
       · "固件死了"
       · "看门狗进复位循环了"
       · "链路断了"
     三种都不是 —— 只是**没等够**。本工具把"等"这件事变成一个动作。

★ 判据口径 (重要): 本工具**只报等待时间与成功率**, 不做任何"板子好坏"的断言。
  复位循环的判据在 `mgmt.py --boot` 的"启动次数是否在涨"(反复读), 不在这里。

用法:
  python tools/wait_board.py                  # 等到 0x64 有应答 (最多 60s)
  python tools/wait_board.py --timeout 90     # 加长
  python tools/wait_board.py --port COM14 --need 6   # 连续 6 次应答才算稳 (默认 3)
退出码: 0 = 等到了; 2 = 超时 (此时才该怀疑链路/固件)
"""
import argparse
import sys
import time

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

try:
    import serial
except ImportError:
    print("!! need pyserial")
    sys.exit(2)

SYNC_MCU2PC = 0xC1
CMD_MANIFEST = 0x64


def crc_ccitt(d):
    c = 0xFFFF
    for b in d:
        c ^= (b << 8)
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if (c & 0x8000) else (c << 1) & 0xFFFF
    return c


def frame(cmd, pl=b""):
    body = bytes([cmd, len(pl) & 0xFF, (len(pl) >> 8) & 0xFF]) + pl
    x = crc_ccitt(body)
    return bytes([0xC0]) + body + bytes([x & 0xFF, x >> 8])


def probe(s, wait=0.4):
    """打一次 0x64, 返回收到的合法应答字节数 (0 = 无应答)。"""
    s.reset_input_buffer()
    s.write(frame(CMD_MANIFEST, bytes([0])))
    s.flush()
    t = time.time()
    buf = bytearray()
    while time.time() - t < wait:
        n = s.in_waiting
        if n:
            buf += s.read(n)
        else:
            time.sleep(0.003)
    # 只认"看着像协议帧"的: 首字节 0xC1
    return len(buf) if (buf and buf[0] == SYNC_MCU2PC) else 0


def main():
    ap = argparse.ArgumentParser()
    # ★ 默认自动找板子 (同 mgmt.py; 理由: 插拔后 COM 号会移位)
    ap.add_argument("--port", default=None)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--need", type=int, default=3, help="连续几次合法应答才算稳")
    a = ap.parse_args()

    port = a.port
    if port is None:
        import os, sys as _s
        _s.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from h723_client import find_board
        port = find_board()
        print("  (自动找板子 → %s)" % port)
    s = serial.Serial(port, 115200, timeout=0.05)
    t0 = time.time()
    first = None
    streak = 0
    tries = 0
    while time.time() - t0 < a.timeout:
        tries += 1
        n = probe(s)
        if n:
            if first is None:
                first = time.time() - t0
                print("★ 首次应答 t=%.2fs (%d 字节)" % (first, n))
            streak += 1
            if streak >= a.need:
                print("✅ 板子管理面可用: 首次 %.2fs, 连续 %d 次应答, 共试 %d 次 (%.1fs)"
                      % (first, streak, tries, time.time() - t0))
                s.close()
                return 0
        else:
            if streak:
                print("   (应答断了一次 t=%.2fs —— 可能刚复位/正忙)" % (time.time() - t0))
            streak = 0
        time.sleep(0.15)

    print("❌ %.0fs 内没能拿到连续 %d 次应答 (试了 %d 次)%s"
          % (a.timeout, a.need, tries,
             "" if first is None else " —— 但曾应答过 %.2fs"))
    print("   ⇒ 现在才该怀疑: 链路 / 固件 / 复位循环 (用 mgmt.py --boot 连读看'启动次数')")
    s.close()
    return 2


if __name__ == "__main__":
    sys.exit(main())
