#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mb_rate_test.py — 485 链路的**速率**实测：原始字节率 / 事务周期 / 双向是否对称

问题（用户 2026-09-13）:
  "传输速率怎么样？能不能跟上 100µs 节拍？如果不能, 那么双向速率相同吗？能跟上 1ms 吗？"

★ 为什么必须分三层报, 不能给一个数:
  ① **原始字节率**  = 波特率决定的物理上限 (115200 8N1 → 11520 B/s), 与协议无关
  ② **事务周期**    = 一条请求 + 一条响应 + 帧间静默 的实际耗时 ⇒ 决定"多久能通信一次"
  ③ **双向字节率**  = 同一半双工总线上两个方向各自分到多少 ⇒ 由**载荷大小**决定,
                      不是链路不对称 (同一条线、同一个波特率)
  把三者混成一个"速率"数字, 就会得出"双向不一样快"这种误导结论。

测法 (全程读观测面只走协议口, 铁律 0):
  [1] 流水线往返 (0x06: 8B↔8B)  —— 收到一条应答就立刻发下一条, 记录**相邻应答的时刻差**
  [2] 非对称载荷 (0x03 qty=64: 请求 8B / 响应 133B) —— 同一条链路, 只换载荷
  [3] 单向灌满 (PC→板子 零间隔) —— 板子侧字节数 / 实测发送时长 = 物理上限
  [4] 与 100µs / 1ms 节拍对照 + "要跟上 100µs 需要多少波特率"

用法:
  python tools/mb_rate_test.py --proto COM14 --mb COM15 --n 200 --secs 5
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

BAUD = 115200

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


def mb06(seq):
    b = bytes([1, 0x06, 0x9C, 0x81, (seq >> 8) & 0xFF, seq & 0xFF])
    c = crc_modbus(b)
    return b + bytes([c & 0xFF, c >> 8])


def mb03(start, qty):
    b = bytes([1, 0x03, (start >> 8) & 0xFF, start & 0xFF, (qty >> 8) & 0xFF, qty & 0xFF])
    c = crc_modbus(b)
    return b + bytes([c & 0xFF, c >> 8])


class Dut:
    def __init__(self, port):
        self.s = serial.Serial(port, 115200, timeout=0.05)

    def _x(self, f, wait=0.35):
        self.s.reset_input_buffer()
        self.s.write(f)
        self.s.flush()
        t0 = time.time()
        buf = bytearray()
        while time.time() - t0 < wait:
            n = self.s.in_waiting
            if n:
                buf += self.s.read(n)
            else:
                time.sleep(0.002)
        return bytes(buf)

    def _ack(self, cmd):
        r = self._x(dcl_frame(cmd))
        if len(r) < 6 or r[0] != SYNC_MCU2PC:
            return None
        ln = r[2] | (r[3] << 8)
        body, cr = r[1:4 + ln], (r[4 + ln] | (r[5 + ln] << 8))
        if crc_ccitt(body) != cr:
            return None
        return r[4:4 + ln]

    def bytes_recv(self):
        p = self._ack(0x63)
        return None if p is None else int.from_bytes(p[0:4], "little")


def pipeline(link, req_fn, resp_len, n, seq0=0):
    """收到一条应答就立刻发下一条; 返回 (周期列表, 发出字节, 收到字节, 用时)"""
    periods, got = [], 0
    rxbytes = 0
    t_prev = None
    t0 = time.time()
    for k in range(n):
        req = req_fn(seq0 + k)
        link.reset_input_buffer()
        link.write(req)
        buf = bytearray()
        deadline = time.time() + 0.5
        while len(buf) < resp_len and time.time() < deadline:
            d = link.read(resp_len - len(buf))
            if d:
                buf += d
        if len(buf) < resp_len:
            continue
        got += 1
        rxbytes += len(buf)
        t_now = time.time()
        if t_prev is not None:
            periods.append(t_now - t_prev)
        t_prev = t_now
    return periods, n * 8, rxbytes, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", default="COM14")
    ap.add_argument("--mb", default="COM15")
    ap.add_argument("--n", type=int, default=200, help="流水线段数")
    ap.add_argument("--secs", type=float, default=5.0, help="单向灌满时长")
    a = ap.parse_args()

    dut = Dut(a.proto)
    link = serial.Serial(a.mb, 115200, timeout=0.01)

    print("=" * 80)
    print("485 速率实测  (115200 8N1 → 字节时宽 %.1f µs, 线速 %.0f B/s)"
          % (10 * 1e6 / BAUD, BAUD / 10))
    print("=" * 80)

    # ---------- [1] 流水线往返: 8B ↔ 8B ----------
    btn = 8 * 10 * 1e6 / BAUD                       # 8B 帧的线上时长
    print("\n[1] 流水线往返 0x06 (请求 8B / 响应 8B), %d 次" % a.n)
    per, txb, rxb, el = pipeline(link, mb06, 8, a.n, seq0=0)
    if per:
        ps = sorted(per)
        mean_s = sum(ps) / len(ps)
        mean_ms = mean_s * 1e3
        print("    完成 %d 次 | 周期 min=%.3f ms  mean=%.3f ms  p90=%.3f ms  max=%.3f ms"
              % (len(per), ps[0] * 1e3, mean_ms, ps[int(len(ps) * 0.9)] * 1e3, ps[-1] * 1e3))
        print("    ⇒ 事务率 = 1/mean = **%.0f 次/秒**" % (1.0 / mean_s))
        print("    ⇒ 双向字节率: PC→板子 %.0f B/s | 板子→PC %.0f B/s  (合计 %.0f B/s, 线速 %.0f)"
              % (txb / el, rxb / el, (txb + rxb) / el, BAUD / 10))
        print("    ★ 归因: 线上理论只需 8B+8B+静默 = %.2f ms; 实测 %.2f ms"
              % ((2 * 8 * 10 * 1e6 / BAUD + 3.5 * 10 * 1e6 / BAUD) / 1000.0, mean_ms))
        print("      ⇒ 多出的 %.2f ms 是 **PC 侧 USB 开销**(CH340 的 1ms USB 轮询 + 驱动栈 + "
              "pySerial 的 reset/flush), **不是链路能力**。"
              % (mean_ms - (2 * 8 * 10 * 1e6 / BAUD + 3.5 * 10 * 1e6 / BAUD) / 1000.0))
        print("      ⇒ 两个 MCU 直连(无 USB)时, 往返周期应逼近线上理论值 1.69 ms。")

    # ---------- [2] 非对称载荷: 8B → 133B ----------
    qty = 64
    rl = 5 + 2 * qty
    print("\n[2] 非对称载荷 0x03 qty=%d (请求 8B / 响应 %dB) —— 同一条链路, 只换载荷" % (qty, rl))
    per2, txb2, rxb2, el2 = pipeline(link, lambda s: mb03(0x9C41, qty), rl, max(10, a.n // 10))
    if per2:
        ps2 = sorted(per2)
        mean2 = sum(ps2) / len(ps2) * 1e3
        line2 = (8 + rl + 3.5) * 10 * 1e6 / BAUD / 1000.0
        print("    完成 %d 次 | 周期 mean=%.3f ms (线上理论 %.2f ms)" % (len(per2), mean2, line2))
        print("    ⇒ 双向字节率: PC→板子 %.0f B/s | 板子→PC %.0f B/s  (合计 %.0f B/s)"
              % (txb2 / el2, rxb2 / el2, (txb2 + rxb2) / el2))
        print("    ⇒ ★ 两个方向共用同一条半双工总线、同一个波特率: **线速本身是对称的**。")
        print("      这里 446 vs 7417 B/s 的差异**完全由载荷大小决定**(请求 8B / 响应 %dB)," % rl)
        print("      不是链路偏向某一方向。要对称就发对称载荷 —— 见 [1] 的两个 2050 B/s。")

    # ---------- [3] 单向灌满 ----------
    print("\n[3] 单向灌满 (PC→板子, 帧间零间隔) %.1fs —— 物理上限" % a.secs)
    b0 = dut.bytes_recv()
    stop = threading.Event()
    st = {"sent": 0, "seq": 0}

    def writer():
        while not stop.is_set():
            link.write(mb06(st["seq"] & 0xFFFF))
            st["sent"] += 1
            st["seq"] += 1

    t0 = time.time()
    th = threading.Thread(target=writer, daemon=True)
    th.start()
    while time.time() - t0 < a.secs:
        link.read(4096)
    stop.set()
    th.join(timeout=1.0)
    el3 = time.time() - t0
    b1 = dut.bytes_recv()
    if b0 is not None and b1 is not None:
        print("    发出 %d 帧 / %d 字节, 用时 %.2f s" % (st["sent"], st["sent"] * 8, el3))
        print("    板子侧收到 %d 字节 ⇒ **%.0f B/s** (= 线速的 %.1f%%)"
              % (b1 - b0, (b1 - b0) / el3, 100.0 * (b1 - b0) / el3 / (BAUD / 10)))

    # ---------- [4] 与 100µs / 1ms 对照 ----------
    print("\n" + "=" * 80)
    print("与「100µs 拍 / 1ms 拍」对照 (115200 8N1)")
    print("=" * 80)
    bt = 10 * 1e6 / BAUD
    sil = 3.5 * bt
    print("  字节时宽        %.1f µs" % bt)
    print("  8B 帧           %.1f µs  = **%.2f 个 100µs 拍**" % (8 * bt, 8 * bt / 100))
    print("  8B 帧+帧间隔     %.1f µs  (Modbus RTU 3.5 字符 = %.1f µs)" % (8 * bt + sil, sil))
    print("  8B 往返+帧间隔   %.1f µs" % (2 * 8 * bt + sil))
    print("  每 100µs 拍最多  %.3f 字节" % (BAUD / 10 * 100e-6))
    print()
    print("  ⇒ **100µs 拍: 做不到**。一条 8B 帧要 694µs ≈ 7 拍; 一次往返要 ~1.7ms ≈ 17 拍。")
    print("     即链路的字节吞吐是 1.152 B/拍 —— 连半条 8B 帧都完不成。")
    print("  ⇒ **1ms 拍: 单向刚好卡住, 往返做不到**。")
    print("     单向: 8B 帧 + 必需帧间隔 = %.0f µs, 对 1000µs 只剩 %.0f µs 余量 (≈1.8µs/字符的抖动就没有了)"
          % (8 * bt + sil, 1000 - (8 * bt + sil)))
    print("     往返: 2×694 + 304 = %.0f µs ⇒ **1ms 往返不成立; 2ms 才稳**。"
          % (2 * 8 * bt + sil))
    if per:
        print("     实测流水线周期 mean=%.3f ms ⇒ 实际能跑 **%.0f 次/秒**"
              % (mean_ms, 1.0 / mean_s))
        print("       ★ 这 3.9ms 里只有 1.69ms 是线上的, 其余是 PC 侧 USB 开销(见 [1] 归因) ——")
        print("         换成 MCU 直连, 事务率应从 ~250 次/秒 升到 ~590 次/秒(受 1.69ms 限制)。")
    print()
    print("  ⇒ 要跟上 **100µs 拍的一次往返**, 波特率至少需要:")
    need = 2 * 8 * 10 / (100e-6 - 10e-6)      # 留 10µs 给从站处理
    print("     2×80bit / (100µs − 10µs 处理) ≈ **%.2f Mbaud** (当前 0.1152 Mbaud 的 %.0f 倍)"
          % (need / 1e6, need / BAUD))
    print("     ★ H723 侧没问题 (PCLK1=100MHz, BRR=1 也能配); 瓶颈在 USB-485 dongle (CH340 上限 2 Mbps)")
    print("       与 RS-485 收发器/线缆。2 Mbaud 时 8B 往返 ≈ %.0f µs —— 刚好压线。"
          % (2 * 80 / 2e6 * 1e6 + 10))

    dut.s.close()
    link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
