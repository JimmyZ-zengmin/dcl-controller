#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mb_rx_measure.py — PC→板子(PD6/USART2) 接收方向的**非侵入式**测量 (2026-09-12)

★ 前史 (为什么必须重做):
  此前所有 "bytes=0" 的读数全部作废 —— pyocd 每次 connect 都复位目标,
  而诊断区在 DTCM(上电清零), 拿到的只是"我自己刚清零后的值"。
  ⇒ 本脚本**全程不开 pyocd**: 诊断走 0x63, 通信标量走 0x61, 都从协议口(USART1)。
     观测不再改变被测对象 (铁律 0)。

★ 判据必须能失败:
  A 段(空转对照)  Δbytes 必须 = 0   —— 否则观测本身在污染计数器 (0x63 泄漏到 USART2)
  B 段(合法 Modbus) Δbytes > 0 且 Δframes_rx > 0  —— 这才是"收到了并且认得出"
  C 段(随机垃圾)  Δbytes > 0 而 Δframes_rx = 0 且 Δerr_crc > 0
                  —— 与 B 段配对: 区分"字节到了但内容/波特率不对" vs "根本没到"
  若 A>0 ⇒ 说明协议口 TXD 在物理上并接到 PD6, 那 B/C 的基线要改 (脚本会显式报出)。

用法:
  python tools/mb_rx_measure.py --proto COM14 --mb COM15
  python tools/mb_rx_measure.py --proto COM14            # 只做 A 段
"""
import sys
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, random, struct, time

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


def xfer(s, f, wait=0.5):
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
    body, crc_rx = r[1:4 + ln], (r[4 + ln] | (r[5 + ln] << 8))
    if crc_ccitt(body) != crc_rx:
        return None
    return (r[1], r[4:4 + ln])


def read_diag(proto):
    a = parse_ack(xfer(proto, dcl_frame(0x63)))
    if a is None:
        return None
    return struct.unpack("<%dI" % (len(a[1]) // 4), a[1][:len(a[1]) // 4 * 4])


def read_counters(proto):
    """0x61 → (frames_rx, frames_tx, err_crc, err_exc, state, tx_len)"""
    a = parse_ack(xfer(proto, dcl_frame(0x61)))
    if a is None:
        return None
    p = a[1]
    if len(p) < 2:
        return None
    tl = p[1]
    o = 2 + tl
    if len(p) < o + 16:
        return None
    g = lambda i: int.from_bytes(p[o + 4 * i:o + 4 * i + 4], "little")
    return (g(0), g(1), g(2), g(3), p[0], p[1])


DIAG_KEY = {0: "bytes", 1: "maxrx", 2: "short", 3: "last_isr", 4: "erracc",
            5: "last_byte", 6: "line_map", 7: "line_mk", 8: "MODER", 9: "AFRL",
            10: "PUPDR", 11: "cfg_mk", 12: "pd6_low", 13: "pd6_tot", 14: "prb_mk",
            16: "CR1", 17: "CR2", 18: "CR3", 19: "BRR", 20: "ISR", 21: "PRESC",
            22: "reg_mk"}


def snap(proto, tag):
    d = read_diag(proto)
    c = read_counters(proto)
    row = {"tag": tag, "diag": d, "ctr": c}
    if d is None:
        print("  [%s] 0x63 无有效应答" % tag)
    else:
        print("  [%s] bytes=%-6d maxrx=%-3d short=%-3d erracc=0x%02X last=0x%02X"
              % (tag, d[0], d[1], d[2], d[4], d[5]))
    if c is None:
        print("        0x61 无有效应答")
    else:
        print("        0x61: frames_rx=%-6d frames_tx=%-6d err_crc=%-5d err_exc=%-5d state=%d tx_len=%d"
              % c)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", default="COM14", help="协议口 (观察通道, 必须接在 PA10/PA9)")
    ap.add_argument("--mb", default=None, help="485 主站口 (USB→485 模块, 打到 PD6)")
    ap.add_argument("--n", type=int, default=20, help="每段发多少帧")
    a = ap.parse_args()

    proto = serial.Serial(a.proto, 115200, timeout=0.05)
    mb = serial.Serial(a.mb, 115200, timeout=0.05) if a.mb else None

    print("== 基线 ==")
    b0 = snap(proto, "base")

    print("\n== A 段 空转对照 (不发任何外部字节, 只重复读 3 次) ==")
    print("   判据: Δbytes 必须 = 0 (否则说明 0x63 自己泄漏进了 USART2)")
    time.sleep(0.5)
    a1 = snap(proto, "A1")

    if mb is not None:
        print("\n== B 段 合法 Modbus 帧 ×%d  →  %s ==" % (a.n, a.mb))
        print("   判据: Δbytes > 0 且 Δframes_rx > 0")
        b_base = snap(proto, "B-base")
        f = mb_frame(1, [0x03, 0x9C, 0x41, 0x00, 0x0A])
        for i in range(a.n):
            mb.write(f); mb.flush()
            time.sleep(0.02)
        time.sleep(0.4)
        b_aft = snap(proto, "B-after")

        print("\n== C 段 随机垃圾 ×%d (与 B 配对: 区分'没到' vs '到了但认不出') ==" % a.n)
        print("   判据: Δbytes > 0, 且 frames_rx 增量应为 0")
        rnd = random.Random(1234)
        for i in range(a.n):
            L = rnd.randint(6, 12)
            mb.write(bytes(rnd.randrange(1, 256) for _ in range(L))); mb.flush()
            time.sleep(0.02)
        time.sleep(0.4)
        c_aft = snap(proto, "C-after")

        print("\n== 增量汇总 ==")
        def d(tag1, tag0, idx):
            r = (tag1["diag"][idx] - tag0["diag"][idx]) & 0xFFFFFFFF
            return r
        print("  A: Δbytes = %d" % d(a1, b0, 0))
        print("  B: Δbytes = %d   Δframes_rx = %d   Δerr_crc = %d"
              % (d(b_aft, b_base, 0),
                 (b_aft["ctr"][0] - b_base["ctr"][0]) & 0xFFFFFFFF,
                 (b_aft["ctr"][2] - b_base["ctr"][2]) & 0xFFFFFFFF))
        print("  C: Δbytes = %d   Δframes_rx = %d   Δerr_crc = %d"
              % (d(c_aft, b_aft, 0),
                 (c_aft["ctr"][0] - b_aft["ctr"][0]) & 0xFFFFFFFF,
                 (c_aft["ctr"][2] - b_aft["ctr"][2]) & 0xFFFFFFFF))
    else:
        print("\n(未给 --mb, 只做了 A 段)")

    proto.close()
    if mb is not None:
        mb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
