#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_revive.py — 解卡: 把被 pyocd 留在 halt 的核放开, 并验证串口恢复。

★ 为什么需要它 (外部审计 M4):
  pyocd 会话若以 halt 收尾 (或被 Ctrl-C 打断), 核会**停在暂停**;
  此后所有串口工具都会报"无响应" —— 而**用 pyocd 去查"为什么串口没响应"本身
  又会让串口没响应** (观察者效应)。这条把解卡变成一条命令, 不用再靠猜。

用法:
    python tools/h723_revive.py            # 复位 + 放开核 + 用 0x01 验证串口恢复
    python tools/h723_revive.py --port COM14
"""
import argparse
import subprocess
import sys
import time

_HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
sys.path.insert(0, _HERE)

PYOCD = ["pyocd", "cmd", "-t", "stm32h723xx", "-O", "connect_mode=under-reset"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    a = ap.parse_args()

    from h723_w1 import build_frame
    import serial
    from h723_modbus import find_port

    port = find_port(a.port)
    print("端口: %s" % port)

    def alive():
        try:
            with serial.Serial(port, 115200, timeout=0.4) as s:
                time.sleep(0.15); s.reset_input_buffer()
                s.write(build_frame(0x01)); s.flush()
                return bool(s.read(64))
        except Exception:
            return False

    print("[1] 复位并放开核 (reset → go) …")
    r = subprocess.run(PYOCD + ["-c", "reset", "-c", "go", "-c", "sleep 400"],
                       capture_output=True, text=True, timeout=120)
    print("    pyocd rc=%d" % r.returncode)

    print("[2] 用 0x01 验证串口 …")
    ok = alive()
    print("    链路: %s" % ("活 ✅" if ok else "仍无响应 ❌"))
    if not ok:
        print("    仍不通的话, 依次检查: ① COM 口是否被别的进程占用 (关掉其它测试脚本/串口助手)")
        print("    ② 板子是否在上电 ③ 跑 python tools/h723_wire_probe.py 看物理层")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
