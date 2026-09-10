#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_ports.py — 串口工具自检 (H723 平台接线检查用)

三档能力:
  1) 默认        : 列出所有串口 + VID/PID，逐个尝试打开并写测试字节
  2) --loopback  : 对指定口做真回环测试 (需先短接该模块的 TX ↔ RX)
  3) --baud      : 指定波特率 (默认 115200)

用法:
  python tools/h723_ports.py
  python tools/h723_ports.py --loopback COM14
  python tools/h723_ports.py --loopback COM14 --baud 921600

判据:
  打开成功         → 驱动 + 端口正常
  写入返回 N 字节   → 发送路径正常
  回环回读 == 写入  → 收发全通 (唯一能证明"读"也正常的测试)
"""
import sys, time, argparse

try:
    import serial
    import serial.tools.list_ports as lp
except ImportError:
    print("需要 pyserial: pip install pyserial")
    sys.exit(2)

# 已知桥片 VID/PID → 人类可读名
KNOWN = {
    ("1A86", "7523"): "CH340",
    ("1A86", "55D3"): "CH343",
    ("1A86", "5523"): "CH341",
    ("10C4", "EA60"): "CP2102",
    ("0403", "6001"): "FT232",
    ("0403", "6015"): "FT231",
    ("0D28", "0204"): "DAPLink(CDC 虚拟串口)",
    ("0483", "5740"): "STM32 原生 USB CDC",
}

PAYLOAD = b"DCL-CONTROLLER-LOOPBACK-TEST-0123456789"


def identify(hwid):
    """从 hwid 串里提取 VID:PID 并翻译"""
    for (vid, pid), name in KNOWN.items():
        if ("VID:PID=%s:%s" % (vid, pid)) in hwid.upper():
            return name
    return "?"


def list_and_probe(baud):
    print("=== 串口清单 ===")
    ports = list(lp.comports())
    if not ports:
        print("  (无串口)")
        return
    for p in ports:
        print("  %-8s %-38s [%s]" % (p.device, p.description, identify(p.hwid)))
        print("           %s" % p.hwid)
    print()
    print("=== 打开 + 写测试 (波特率 %d) ===" % baud)
    for p in ports:
        try:
            s = serial.Serial(p.device, baud, timeout=0.3)
            s.reset_input_buffer(); s.reset_output_buffer()
            n = s.write(PAYLOAD)
            s.flush()
            time.sleep(0.2)
            r = s.read(128)
            s.close()
            note = "回读 %d 字节" % len(r) if r else "无回读 (对面无设备/无回环, 正常)"
            print("  [OK  ] %-8s 写入 %d 字节, %s" % (p.device, n, note))
        except Exception as e:
            print("  [FAIL] %-8s %s" % (p.device, e))


def loopback(port, baud):
    print("=== 回环测试: %s @ %d ===" % (port, baud))
    print("  前提: 该模块的 TX 与 RX 已短接")
    try:
        s = serial.Serial(port, baud, timeout=1.0)
    except Exception as e:
        print("  [FAIL] 打开失败: %s" % e)
        return 1
    ok = True
    for trial in range(3):
        s.reset_input_buffer(); s.reset_output_buffer()
        s.write(PAYLOAD)
        s.flush()
        time.sleep(0.3)
        r = s.read(len(PAYLOAD))
        match = (r == PAYLOAD)
        print("  第 %d 次: 发 %d 字节, 收 %d 字节 → %s"
              % (trial + 1, len(PAYLOAD), len(r), "一致" if match else "不一致"))
        if r and r != PAYLOAD:
            print("           收到: %s" % r.hex(" "))
            print("           期望: %s" % PAYLOAD.hex(" "))
        if not match:
            ok = False
    s.close()
    print()
    if ok:
        print("=== 回环 %s: 3/3 PASS — 收/发路径全通 ===" % port)
        return 0
    print("=== 回环 %s: FAIL ===" % port)
    print("  排查: ① TX/RX 是否真的短接 ② 电平档位是否 3.3V ③ 波特率是否被限速")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loopback", metavar="COM", help="对指定串口做 TX-RX 回环测试")
    ap.add_argument("--baud", type=int, default=115200)
    a = ap.parse_args()
    if a.loopback:
        return loopback(a.loopback, a.baud)
    list_and_probe(a.baud)
    return 0


if __name__ == "__main__":
    sys.exit(main())
