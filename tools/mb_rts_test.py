#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mb_rts_test.py — 测 485 dongle 的发送使能是不是由 DTR/RTS 控制

动机 (2026-09-13):
  PC→板子方向: **连 1 个字节都不通**; 而板子→PC 方向逐字完好。
  §"发送侧完全不发"的一个高频原因: 廉价 USB↔485 dongle 用 **RTS 当 DE(发送使能)**,
  而 pyserial **打开端口时默认把 DTR 与 RTS 都拉高**。
  若该 dongle 的 DE 是**低有效**, 或 DE/RX 使能逻辑依赖 RTS 的某个电平,
  就会出现"收得到、发不出" —— 与实测完全吻合。
  ⇒ 用纯软件把 4 种 DTR/RTS 组合各试一遍, 这是**零硬件改动**的一次测量。

  ★ 若某一组合能通 ⇒ 结论是"dongle 需要上位机管理方向控制" ——
    修法在上位机 (打开端口后设 rts/dtr), 不涉及板子与模块。

用法:
  python tools/mb_rts_test.py --proto COM14 --mb COM15 --n 20
"""
import sys
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, re, time

try:
    import serial
except ImportError:
    print("!! need pyserial")
    sys.exit(2)

SYNC_MCU2PC = 0xC1


def crc_ccitt(d):
    c = 0xFFFF
    for b in d:
        c ^= (b << 8)
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if (c & 0x8000) else (c << 1) & 0xFFFF
    return c


def crc_modbus(d):
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if (c & 1) else (c >> 1)
    return c


def dcl_frame(cmd, payload=b""):
    body = bytes([cmd, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    x = crc_ccitt(body)
    return bytes([0xC0]) + body + bytes([x & 0xFF, x >> 8])


def mb_frame(addr, pdu):
    b = bytes([addr]) + bytes(pdu)
    x = crc_modbus(b)
    return b + bytes([x & 0xFF, (x >> 8) & 0xFF])


def xfer(s, f, wait=0.35):
    s.reset_input_buffer()
    s.write(f)
    s.flush()
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < wait:
        n = s.in_waiting
        if n:
            buf += s.read(n)
        else:
            time.sleep(0.002)
    return bytes(buf)


def parse_ack(r):
    if len(r) < 6 or r[0] != SYNC_MCU2PC:
        return None
    ln = r[2] | (r[3] << 8)
    if len(r) < 4 + ln + 2:
        return None
    body, crc_rx = r[1:4 + ln], (r[4 + ln] | (r[5 + ln] << 8))
    if crc_ccitt(body) != crc_rx:
        return None
    return (r[1], r[4:4 + ln])


def shm_addr(mapfile):
    pat = re.compile(r"\s+0x([0-9a-fA-F]+)\s+(g_shm)\s*$")
    for ln in open(mapfile, encoding="utf-8", errors="replace"):
        m = pat.match(ln)
        if m:
            return int(m.group(1), 16)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", default="COM14")
    ap.add_argument("--mb", default="COM15")
    ap.add_argument("--map", default="build/dcl_h723.map")
    ap.add_argument("--n", type=int, default=20, help="每种组合发几帧")
    ap.add_argument("--gap", type=float, default=0.02)
    a = ap.parse_args()

    proto = serial.Serial(a.proto, 115200, timeout=0.05)
    shm = shm_addr(a.map)
    if shm is None:
        print("!! 找不到 g_shm")
        return 2

    def diag():
        r = parse_ack(xfer(proto, dcl_frame(0x63)))
        if r is None:
            return None
        p = r[1]
        return [int.from_bytes(p[i:i + 4], "little") for i in range(0, len(p) // 4 * 4, 4)]

    f = mb_frame(1, [0x03, 0x9C, 0x41, 0x00, 0x0A])
    print("请求帧 = %s (%d 字节)" % (f.hex(" "), len(f)))
    print("\n DTR   RTS   Δbytes  maxrx  erracc  last_byte")
    for dtr in (True, False):
        for rts in (True, False):
            mb = serial.Serial(a.mb, 115200, timeout=0.05)
            try:
                mb.dtr = dtr
                mb.rts = rts
            except Exception as ex:
                print("  (无法设置 dtr/rts: %s)" % ex)
                mb.close()
                continue
            time.sleep(0.1)
            d0 = diag()
            for _ in range(a.n):
                mb.write(f); mb.flush()
                time.sleep(a.gap)
            time.sleep(0.3)
            d1 = diag()
            mb.close()
            time.sleep(0.15)
            if d0 is None or d1 is None:
                print("   %-5s %-5s  协议口读失败" % (dtr, rts))
                continue
            print("   %-5s %-5s  %6d  %5d  0x%02X    0x%02X"
                  % (dtr, rts, d1[0] - d0[0], d1[1], d1[4], d1[5]))

    print("\n 判读: 若某个组合 Δbytes>0 而其余为 0 ⇒ dongle 的**发送使能由上位机管理**,"
          "\n       修法在上位机侧 (打开端口后设 dtr/rts), 与板子/模块无关。"
          "\n       若四种组合全 0 ⇒ dongle 的 485 发送方向根本没工作 (或模块接收方向不通),"
          "\n       必须上仪器: 量 485 总线 A-B 差分 与 模块 TXD 引脚。")
    proto.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
