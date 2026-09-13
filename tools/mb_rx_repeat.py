#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mb_rx_repeat.py — 逐轮隔离 + **直读 DTCM 原始缓冲**：485 接收到底发生了什么

背景 (2026-09-13):
  PC→板子方向长期 0 字节。某次首测出现: 30 帧里 Δbytes=5, Δframes_rx=1, err_crc=1,
  last_byte=0x00 —— 而帧 `01 03 9C 41 00 0A BB 89` 的**前 5 字节正好是 `01 03 9C 41 00`**,
  即"收到一帧的开头就被截断"。随后 12 轮单帧复测全部 Δbytes=0。
  ⇒ 必须分清: 稳定复现的"截断" vs 一次性瞬态 vs 我的测量假象。

本工具把判据做成**内容级**而不是计数级:
  · 每轮前后用 `0x22 READ_BURST` 直读 MbCtrl_t (SHM+0x4B20) 与 **RX 缓冲 (SHM+0x4B60)**;
  · 于是"落在 DTCM 里的字节是什么"是**直接看到的**, 不是从计数推出来的;
  · 全部走协议口, **不开调试器、不复位目标** (铁律 0)。

用法:
  python tools/mb_rx_repeat.py --proto COM14 --mb COM15 --rounds 12
  python tools/mb_rx_repeat.py --proto COM14 --mb COM15 --rounds 8 --burst 30
"""
import sys
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, os, re, time

try:
    import serial
except ImportError:
    print("!! need pyserial")
    sys.exit(2)

SYNC_MCU2PC = 0xC1
SHM_DIAG_OFF = 0x4A10
OFF_MB_CTRL  = 0x4B20
OFF_MB_RX    = 0x4B60
RX_WORDS     = 64        # 256B


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


def u32(v):
    return v.to_bytes(4, "little")


def mb_frame(addr, pdu):
    b = bytes([addr]) + bytes(pdu)
    x = crc_modbus(b)
    return b + bytes([x & 0xFF, (x >> 8) & 0xFF])


def shm_addr(mapfile):
    pat = re.compile(r"\s+0x([0-9a-fA-F]+)\s+(g_shm)\s*$")
    for ln in open(mapfile, encoding="utf-8", errors="replace"):
        m = pat.match(ln)
        if m:
            return int(m.group(1), 16)
    return None


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


class Dut:
    def __init__(self, port):
        self.s = serial.Serial(port, 115200, timeout=0.05)

    def diag(self):
        a = parse_ack(xfer(self.s, dcl_frame(0x63)))
        if a is None:
            return None
        return list(int.from_bytes(a[1][i:i + 4], "little")
                    for i in range(0, len(a[1]) // 4 * 4, 4))

    def ctr(self):
        a = parse_ack(xfer(self.s, dcl_frame(0x61)))
        if a is None:
            return None
        p = a[1]
        o = 2 + p[1]
        if len(p) < o + 16:
            return None
        g = lambda i: int.from_bytes(p[o + 4 * i:o + 4 * i + 4], "little")
        return dict(frames_rx=g(0), frames_tx=g(1), err_crc=g(2), err_exc=g(3))

    def burst(self, addr, count):
        a = parse_ack(xfer(self.s, dcl_frame(0x22, u32(addr) + count.to_bytes(2, "little"))))
        if a is None or len(a[1]) < count * 4:
            return None
        w = [int.from_bytes(a[1][i:i + 4], "little") for i in range(0, count * 4, 4)]
        return b"".join(x.to_bytes(4, "little") for x in w)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", default="COM14")
    ap.add_argument("--mb", default="COM15")
    ap.add_argument("--map", default="build/dcl_h723.map")
    ap.add_argument("--rounds", type=int, default=12)
    ap.add_argument("--burst", type=int, default=1, help="每轮连发几帧")
    ap.add_argument("--gap", type=float, default=0.05)
    ap.add_argument("--sweep", default=None,
                    help="帧长扫描, 如 1,2,4,6,8,16,32 —— **本工具最有价值的一档**")
    a = ap.parse_args()

    shm = shm_addr(a.map)
    if shm is None:
        print("!! map 里找不到 g_shm")
        return 2
    ctrl_a, rx_a = shm + OFF_MB_CTRL, shm + OFF_MB_RX
    print("SHM=0x%08X  MbCtrl=0x%08X  RXbuf=0x%08X" % (shm, ctrl_a, rx_a))

    dut = Dut(a.proto)

    def ctrl():
        b = dut.burst(ctrl_a, 10)
        if b is None:
            return None
        return dict(state=b[0], rx_len=b[2], rx_pos=b[3], silent=b[6],
                    enabled=b[7], src=b[25], tx_uart=b[26])

    # ★★ 帧长扫描: 判据来自一个很具体的猜想 —— "发送侧的驱动使能窗口是固定时长"。
    #   若成立, 则**短帧每轮都能整帧通过, 长帧在某个字节数上被截断**,
    #   而那个字节数 × (10/115200) 就是窗口时长。这条把"链路完全不通"与
    #   "链路通但只开一个小窗口"分开, 而两者的修法完全不同。
    if a.sweep:
        mb = serial.Serial(a.mb, 115200, timeout=0.05)
        base = mb_frame(1, [0x03, 0x9C, 0x41, 0x00, 0x0A])
        print("\n== 帧长扫描 (每档 %d 轮, 每轮 1 帧) ==" % a.rounds)
        print(" 帧长  收到整帧的轮数/总轮数   收到字节数(各轮)")
        for n in [int(x) for x in a.sweep.split(",")]:
            f = (base * ((n // len(base)) + 1))[:n]     # 截取/重复成 n 字节
            ok = 0
            recs = []
            for r in range(a.rounds):
                d0 = dut.diag()
                mb.write(f); mb.flush()
                time.sleep(0.2)
                d1 = dut.diag()
                if d0 is None or d1 is None:
                    recs.append(-1)
                    continue
                got = d1[0] - d0[0]
                recs.append(got)
                if got >= n:
                    ok += 1
                time.sleep(a.gap)
            print("  %3d   %3d/%3d                    %s"
                  % (n, ok, a.rounds, " ".join(str(x) for x in recs)))
        print("\n 判读: 短的档全过、长的档被截断 ⇒ 发送侧驱动窗口 = 截断字节数 × 87µs,")
        print("        属**发送侧方向控制**问题 (dongle 或其 DE 逻辑), 板子侧无嫌疑。")
        mb.close()
        proto = dut.s
        proto.close()
        return 0

    f = mb_frame(1, [0x03, 0x9C, 0x41, 0x00, 0x0A])
    print("请求帧 = %s  (%d 字节)" % (f.hex(" "), len(f)))

    mb = serial.Serial(a.mb, 115200, timeout=0.05)
    print("\n轮  连发  Δbytes Δfrx Δcrc | state rx_len silent | RXbuf 前 16B")
    tot = 0
    for r in range(a.rounds):
        d0, c0, cb0 = dut.diag(), dut.ctr(), ctrl()
        if d0 is None or c0 is None or cb0 is None:
            print("  第%d轮: 协议口读失败 (0x63/0x61/0x22)" % r)
            break
        for _ in range(a.burst):
            mb.write(f); mb.flush()
            time.sleep(0.02)
        time.sleep(0.25)
        d1, c1, cb1 = dut.diag(), dut.ctr(), ctrl()
        buf = dut.burst(rx_a, 16)
        time.sleep(a.gap)
        if d1 is None or c1 is None or cb1 is None or buf is None:
            print("  第%d轮: 协议口读失败" % r)
            break
        db = d1[0] - d0[0]
        tot += db
        print("%2d  ×%-4d %6d %5d %4d | %5d %6d %6d | %s"
              % (r, a.burst, db, c1["frames_rx"] - c0["frames_rx"],
                 c1["err_crc"] - c0["err_crc"],
                 cb1["state"], cb1["rx_len"], cb1["silent"], buf[:16].hex(" ")))

    print("\n== 合计: 发出 %d 帧 × %d 字节 = %d 字节, 板子收到 %d 字节 =="
          % (a.rounds * a.burst, len(f), a.rounds * a.burst * len(f), tot))

    # 全缓冲转储 (最有价值的证据: DTCM 里到底躺着什么)
    print("\n== RX 缓冲全量转储 (256B, 只看非零附近) ==")
    full = dut.burst(rx_a, RX_WORDS)
    if full is None:
        print("  读失败")
    else:
        nz = [i for i, x in enumerate(full) if x]
        if not nz:
            print("  全 0 —— 从开机到现在, 没有任何字节进入过通信域 RX 缓冲")
        else:
            lo, hi = max(0, nz[0] - 8), min(len(full), nz[-1] + 9)
            for off in range(lo, hi, 16):
                seg = full[off:off + 16]
                print("  +%03X  %-47s |%s|"
                      % (off, seg.hex(" "),
                         "".join(chr(c) if 32 <= c < 127 else "." for c in seg)))
            print("  期望请求帧: %s" % f.hex(" "))
    proto = dut.s
    proto.close()
    mb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
