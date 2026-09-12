#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""min_text_read.py — 读"最小固件"从 USART1(COM14) 打出来的纯文本

★ 这是**最干净的观察方式**: 不需要 pyocd, 不需要协议帧 —— 就是把串口上出现的
  字符原样打印出来 (铁律 0: 观测不得改变被测对象)。

用法:
  python tools/min_text_read.py --port COM14 --secs 12
  python tools/min_text_read.py --port COM14 --secs 20 --feed COM15   # 边读边灌 Modbus 帧
"""
import sys
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, threading, time

try:
    import serial
except ImportError:
    print("!! need pyserial")
    sys.exit(2)


class Firehose(threading.Thread):
    """不停顿地发合法 Modbus 帧 —— 目的: 让模块 TXD 上一直有数据在跑,
    这样 B 段扫描才看得出"哪根脚上有活动" (占空比 ~50%, 翻转率最高)。

    ★ 允许**复用外部句柄** (ser=...) —— 因为 pyserial 不允许同一个口被打开两次,
      而"边灌边听同一个 485 口"是最常见的用法。第一版在这里写了个静默 no-op:
      `if listen and listen != feed` 才开监听句柄 ⇒ 当两者相同时**既不报错也不监听**,
      于是"C 段没收到"看起来像结论, 其实是我根本没收。
      ⇒ 同族铁律: **静默不做 = 假证据**。要么真的共用句柄, 要么显式报错。
    """

    def __init__(self, port, stop_evt, ser=None):
        super().__init__(daemon=True)
        self.port, self.evt, self.n, self.ser, self.own = port, stop_evt, 0, ser, (ser is None)

    def run(self):
        s = self.ser
        if s is None:
            try:
                s = serial.Serial(self.port, 115200, timeout=0.05)
            except Exception as ex:
                print("!! 发送口打开失败: %s" % ex)
                return
        f = bytes([1, 3, 0x9C, 0x41, 0x00, 0x0A, 0xC5, 0xCD])
        while not self.evt.is_set():
            try:
                s.write(f)
                s.flush()
                self.n += 1
            except Exception:
                break
        if self.own:
            s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM14")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--secs", type=float, default=10.0)
    ap.add_argument("--feed", default=None, help="同时在另一个口持续灌 Modbus 帧 (如 COM15)")
    ap.add_argument("--listen", default=None,
                    help="再监听一个口 (如 COM15) —— 用来验'板子从 PD5 反发的文本能不能到 PC'")
    a = ap.parse_args()

    evt = threading.Event()
    hose = None

    # ★ 先决定"监听口"怎么开 —— 若它和灌帧口是同一个, 必须**共用同一个句柄**
    #   (pyserial 不允许重复打开; 而静默跳过监听是绝对禁止的, 见 Firehose 的注释)
    s2 = None
    if a.listen:
        if a.listen == a.feed:
            try:
                s2 = serial.Serial(a.listen, a.baud, timeout=0.05)
                print("# %s 同时用于灌帧与监听 (共用句柄)" % a.listen)
            except Exception as ex:
                print("!! %s 打开失败: %s" % (a.listen, ex))
        else:
            try:
                s2 = serial.Serial(a.listen, a.baud, timeout=0.05)
            except Exception as ex:
                print("!! 监听口 %s 打开失败: %s" % (a.listen, ex))

    if a.feed:
        hose = Firehose(a.feed, evt, ser=(s2 if a.feed == a.listen else None))
        hose.start()
        print("# 同时在 %s 上不停灌合法 Modbus 帧" % a.feed)

    s = serial.Serial(a.port, a.baud, timeout=0.1)

    print("# 读 %s @ %d, %.0f 秒 …（原样输出）" % (a.port, a.baud, a.secs))
    if s2:
        print("# 同时监听 %s （前缀 MB> ）" % a.listen)
    t0 = time.time()
    try:
        while time.time() - t0 < a.secs:
            d = s.read(4096)
            if d:
                sys.stdout.write(d.decode("latin-1"))
                sys.stdout.flush()
            if s2:
                d2 = s2.read(4096)
                if d2:
                    sys.stdout.write("\nMB> " + d2.decode("latin-1").replace("\r", "\\r")
                                     .replace("\n", "\\n") + "\n")
                    sys.stdout.flush()
    finally:
        s.close()
        evt.set()
        if hose:
            time.sleep(0.3)
            print("\n# (灌了 %d 帧)" % hose.n)
        if s2:
            s2.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
