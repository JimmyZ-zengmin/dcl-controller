#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mb_board_tx.py — 确认"板子 → PC"方向仍然通 (经隧道注入请求, 让板子从真总线应答)

为什么用隧道注入: 这样**不需要 PC→板子方向可用** —— 请求由协议口(USART1)注入,
固件照常走完 Modbus 状态机, 并从 **USART2(PD5)** 把应答推到真 485 总线上。
于是这条测量只依赖:
    PD5 → 模块 TTL→485 → A/B 总线 → dongle 485 接收 → PC
即"板子→PC"那一半。

用法:
  python tools/mb_board_tx.py --proto COM14 --mb COM15 --n 10
"""
import sys
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, time

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


def xfer(s, f, wait=0.4):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", default="COM14")
    ap.add_argument("--mb", default="COM15")
    ap.add_argument("--n", type=int, default=10)
    a = ap.parse_args()

    proto = serial.Serial(a.proto, 115200, timeout=0.05)
    mb = serial.Serial(a.mb, 115200, timeout=0.05)

    def ctr():
        """0x61 → (frames_rx, frames_tx, err_crc, err_exc, state, tx_len)"""
        st = parse_ack(xfer(proto, dcl_frame(0x61)))
        if st is None:
            return None
        p = st[1]
        o = 2 + p[1]
        if len(p) < o + 16:
            return None
        g = lambda i: int.from_bytes(p[o + 4 * i:o + 4 * i + 4], "little")
        return (g(0), g(1), g(2), g(3), p[0], p[1])

    req = mb_frame(1, [0x03, 0x9C, 0x41, 0x00, 0x0A])      # 读 40001 起 10 个寄存器
    exp_len = 3 + 2 * 10 + 2                               # 25 字节
    print("注入请求 = %s" % req.hex(" "))
    print("期望应答 = %d 字节 (addr+func+bytecnt+20+CRC)" % exp_len)

    # ★ 自校验: 先看固件到底发没发。若 frames_tx 不涨, 那"总线上没收到"这句话
    #   根本不构成证据 (测的是空) —— 必须先排除"固件没发"这一层。
    c0 = ctr()
    if c0 is None:
        print("!! 0x61 读失败, 无法自校验")
    else:
        print("基线 0x61: frames_rx=%d frames_tx=%d err_crc=%d err_exc=%d state=%d tx_len=%d"
              % c0)

    ok = 0
    seen = []
    for i in range(a.n):
        r = xfer(proto, dcl_frame(0x60, req))
        st = parse_ack(r)
        if st is None:
            print("  第%d次: 0x60 无有效 ACK (%s)" % (i, r.hex(" ")[:40] if r else "空"))
            continue
        if st[0] != 0x00:
            print("  第%d次: 0x60 NAK (sts=0x%02X, %s)" % (i, st[0], st[1][:20]))
            continue
        # 听总线
        mb.reset_input_buffer()
        t0 = time.time()
        buf = bytearray()
        while time.time() - t0 < 0.6:
            n = mb.in_waiting
            if n:
                buf += mb.read(n)
            else:
                time.sleep(0.002)
        c = crc_modbus(bytes(buf[:len(buf) - 2])) if len(buf) >= 3 else None
        good = (len(buf) >= 3 and c is not None and
                buf[-2] | (buf[-1] << 8) == c)
        seen.append(bytes(buf))
        if good:
            ok += 1
        ct = ctr()
        ftx = "?" if ct is None else str(ct[1] - c0[1])
        print("  第%d次: 总线收到 %2d 字节  %-46s %-10s frames_tx+%s"
              % (i, len(buf), buf.hex(" ")[:46], "CRC OK" if good else "无/坏", ftx))
        time.sleep(0.05)

    c1 = ctr()
    print("\n== 结果 ==")
    if c0 and c1:
        print("  固件侧 frames_tx: %d → %d (Δ=%d, 预期 %d)"
              % (c0[1], c1[1], c1[1] - c0[1], a.n))
        if c1[1] == c0[1]:
            print("  ★ 固件**一次都没发** ⇒ '总线上没收到'这句话不成立, 先查注入路径 (tx_uart/state)。")
            proto.close(); mb.close(); return 0
    print("  485 总线上收到完整合法应答: %d/%d 次" % (ok, a.n))
    if ok:
        print("  ⇒ 板子→PC 方向仍然通: PD5 → 模块 → A/B → dongle 接收 → PC 全链正常。")
        print("  ⇒ 因此 PC→板子 的断点只能在 **dongle 的 485 发送方向** 或 **模块的 485→TTL 接收方向**。")
    else:
        print("  ⇒ 固件已发出但总线上收不到 ⇒ **这一侧现在也断了**。")
        print("     两个方向同时断 ⇒ 优先怀疑: 485 A/B 接线被动过 / 模块供电 / 模块本身。")
    proto.close()
    mb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
