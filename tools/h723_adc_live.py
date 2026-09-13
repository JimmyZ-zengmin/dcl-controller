#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_adc_live.py — AI 模拟量实时读数 (0x37 ADC_SCAN)，用来验证"电位器 → ADC"这条链路。

## 为什么单独做一个
`h723_w5.py` 是**离线验收**（跑一遍给 PASS/FAIL）；本工具是**实时观测**：
连续读同一批通道并原地刷新，一边转电位器一边看数值是否跟随 ——
这才是"这条链路真的通了"的直接证据。

## 通道映射 (与 src/adc.h 逐字一致)
    PA0 = ADC1_INP16 = ch16   ← AI 通道 0 (AI_SENSOR_BASE=8 ⇒ SENSOR[8])
    PA1 = ADC1_INP17 = ch17   ← AI 通道 1
    PA4 = ADC12_INP18 = ch18  ← AI 通道 2
    PA5 = ADC12_INP19 = ch19   (HIL 反馈, 由 hil.c 用)

## 判据 (每条都能失败)
  P1 电位器在动 ⇒ 目标通道读数**跟着变**（不动就一直同值 ⇒ 要么没接上、要么 ADC 没采到该脚）
  P2 满量程    ⇒ 转到 3.3V 端应接近 65535；转到 GND 端应接近 0
  P3 16bit     ⇒ 读数应能超出 4095（12bit 上限）—— 证明走的是 16bit ADC1 而不是 12bit
  P4 定量      ⇒ 电压 = raw × 3.3 / 65535（VREF+ = VDDA = 3.3V，见原理图 U15/R1 那条链）

## 用法
    python tools/h723_adc_live.py [COMxx]                # 默认观测 ch16/17/18
    python tools/h723_adc_live.py COM18 --ch 16 --n 60   # 只测 PA0, 采 60 次
    python tools/h723_adc_live.py COM18 --raw            # 只看原始码
"""
import sys
import time
import os
import serial

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from h723_proto import crc16

CH_NAME = {16: "PA0", 17: "PA1", 18: "PA4", 19: "PA5"}


def mk(cmd, p=b""):
    b = bytes([cmd, len(p) & 0xFF, (len(p) >> 8) & 0xFF]) + p
    c = crc16(b)
    return bytes([0xC0]) + b + bytes([c & 0xFF, c >> 8])


def talk(ser, fr, to=1.5):
    ser.reset_input_buffer()
    ser.write(fr)
    ser.flush()
    buf = b""
    end = time.time() + to
    while time.time() < end:
        ch = ser.read(1)
        if not ch:
            continue
        buf += ch
        i = buf.find(b"\xC1")
        if i < 0:
            buf = b""
            continue
        if i > 0:
            buf = buf[i:]
        if len(buf) < 4:
            continue
        n = buf[2] | (buf[3] << 8)
        need = 4 + n + 2
        while len(buf) < need and time.time() < end:
            m = ser.read(need - len(buf))
            if m:
                buf += m
        if len(buf) >= need:
            return buf[1], buf[4:4 + n]
    return None, None


def scan(ser, ch0, cnt):
    """0x37 ADC_SCAN [ch0:u8][cnt:u8] → ACK cnt*2 字节 (每通道 u16 LE)"""
    sts, pl = talk(ser, mk(0x37, bytes([ch0, cnt])))
    if sts != 0 or pl is None or len(pl) < cnt * 2:
        return None
    return [int.from_bytes(pl[i * 2:i * 2 + 2], "little") for i in range(cnt)]


def main():
    args = sys.argv[1:]
    port = "COM18"
    if args and not args[0].startswith("-"):
        port = args[0]
    show_raw = "--raw" in args
    live = "--live" in args or not args or all(a.startswith("-") for a in args)

    ch0, cnt = 16, 3
    if "--ch" in args:
        ch0 = int(args[args.index("--ch") + 1]); cnt = 1

    n = 40
    if "--n" in args:
        n = int(args[args.index("--n") + 1])

    ser = serial.Serial(port, 115200, timeout=0.2)
    print("=== AI 实时读数 @ %s   通道 %s ===" %
          (port, ", ".join("%d(%s)" % (ch0 + i, CH_NAME.get(ch0 + i, "?")) for i in range(cnt))))
    print("★ 现在缓缓转动电位器 —— 数值应当跟着变；不动则应基本稳定\n")

    hdr = "  ".join("%-11s" % ("ch%d %s" % (ch0 + i, CH_NAME.get(ch0 + i, "?"))) for i in range(cnt))
    print("    " + hdr)
    print("    " + "-" * len(hdr))

    first = None
    last = None
    vals = []
    for k in range(n):
        v = scan(ser, ch0, cnt)
        if v is None:
            print("    无应答 (链路/固件?)")
            time.sleep(0.2)
            continue
        vals.append(v)
        if first is None:
            first = list(v)
        last = list(v)
        if show_raw:
            line = "  ".join("%-11d" % x for x in v)
        else:
            line = "  ".join("%-11s" % ("%.4fV" % (x * 3.3 / 65535.0)) for x in v)
        print("  %3d %s" % (k, line), end="\r")
        time.sleep(0.25)
    print()

    if first and last:
        print("\n=== 判据 ===")
        d0 = abs(last[0] - first[0])
        print("  起始 %s → 结束 %s" % (first, last))
        if vals:
            col = [v[0] for v in vals]
            lo, hi = min(col), max(col)
            print("  目标通道(ch%d) 全程: min=%d max=%d 极差=%d (%.4fV ~ %.4fV)"
                  % (ch0, lo, hi, hi - lo, lo * 3.3 / 65535.0, hi * 3.3 / 65535.0))
            if hi - lo > 64:
                print("  ✓ P1 全程读数在变 (极差=%d) ⇒ 电位器/ADC 链路是通的" % (hi - lo))
            else:
                print("  ✗ P1 全程几乎不变 (极差=%d)" % (hi - lo))
                print("      ⇒ 三选一: ① 电位器没转 ② 滑臂没接到该脚 ③ ADC 没采到这个脚")
            if hi > 4095:
                print("  ✓ P3 最大值 %d > 4095 ⇒ 确实是 16bit ADC1 通路" % hi)
            else:
                print("  △ P3 最大值 %d ≤ 4095 —— 若转到了 3.3V 端, 则可疑 (像是 12bit)" % hi)
            # 悬空对照通道 (未接线的那些) 应当仍在飘且不成比例
            for i in range(1, cnt):
                c = [v[i] for v in vals]
                print("  · 对照 ch%d(%s): min=%d max=%d 极差=%d"
                      % (ch0 + i, CH_NAME.get(ch0 + i, "?"), min(c), max(c), max(c) - min(c)))
        print("  · P2 满量程: 3.3V 端应≈65535, GND 端应≈0")
    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
