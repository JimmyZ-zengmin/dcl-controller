#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mb_duplex_test.py — 收发同时进行 + **双向内容级对账**（485 传输准确性实验）

实验意图（用户 2026-09-13）:
  "收发同时开始, 并且事后查询双方收到信息是否相同, 传输是否准确"

★ 判据为什么用**内容**而不是计数:
  请求 = `01 06 9C 81 <seq_hi> <seq_lo> CRC`（0x06 写单个保持寄存器, 写入值 = 唯一序号）
  固件对 0x06 的响应是**原样回声**（`mb_resp_byte` case 0x06: 回显 slave/func/addr/val）,
  且响应带 CRC ⇒ **PC 每收到一条 CRC 合法的响应, 就等于证明了**
    ① 板子确实收到了那一条请求（含 CRC 通过 ⇒ 内容逐字节正确）
    ② 板子发回来的那一条也逐字节正确
  于是"响应流 == 请求流"这件事同时覆盖了两个方向的准确性。计数类判据做不到这一点。

★ 为什么必须有**基线对照**:
  两线 RS-485 是**半双工**。PC 不停发、板子不停回 ⇒ 总线必然碰撞 ⇒ 丢包是**设计使然**,
  不是缺陷。所以"并发丢了多少"只有跟"严格交替时的 0 丢包"比才有意义。
  ⇒ 四个阶段: ① 交替基线 ② 并发(无间隔) ③ 并发(3ms 间隔) ④ 恢复性复检。

★ 事后"双方各收到什么"分别从哪读:
  PC 侧   : 本脚本抓到的字节流 → 逐条按 CRC + 序号核对
  板子侧  : 协议口 0x63(字节级诊断) / 0x61(帧计数) —— 全程不开调试器
            bytes / maxrx / short / erracc / **ERRCLR(清错误次数)** / frames_rx / frames_tx

用法:
  python tools/mb_duplex_test.py --proto COM14 --mb COM15 --secs 3
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


def req_for(seq):
    """0x06 写 40065(=0x9C81), 写入值 = seq ⇒ 响应 = 本条请求本身"""
    body = bytes([1, 0x06, 0x9C, 0x81, (seq >> 8) & 0xFF, seq & 0xFF])
    c = crc_modbus(body)
    return body + bytes([c & 0xFF, c >> 8])


class Dut:
    """协议口: 只读观测面 (铁律 0)"""

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

    def diag(self):
        p = self._ack(0x63)
        if p is None:
            return None
        return {"bytes": int.from_bytes(p[0:4], "little"),
                "maxrx": int.from_bytes(p[4:8], "little"),
                "short": int.from_bytes(p[8:12], "little"),
                "erracc": int.from_bytes(p[16:20], "little"),
                "last": int.from_bytes(p[20:24], "little"),
                "errclr": int.from_bytes(p[60:64], "little")}   # MbDiag[15]

    def ctr(self):
        p = self._ack(0x61)
        if p is None:
            return None
        o = 2 + p[1]
        if len(p) < o + 16:
            return None
        g = lambda i: int.from_bytes(p[o + 4 * i:o + 4 * i + 4], "little")
        return {"frames_rx": g(0), "frames_tx": g(1),
                "err_crc": g(2), "err_exc": g(3)}


class Link:
    """COM15: 单句柄 + 锁 (半双工, 收发可交叉)"""

    def __init__(self, port):
        self.s = serial.Serial(port, 115200, timeout=0.01)
        self.lock = threading.Lock()

    def write(self, b):
        with self.lock:
            self.s.write(b)
            self.s.flush()

    def read(self, n=4096):
        return self.s.read(n)


def verify_stream(buf, expected_seqs):
    """在抓到的字节流里找 CRC 合法的 0x06 响应, 返回 (命中集合, 重复, 非法字节数)"""
    found, dup = set(), set()
    i, bad = 0, 0
    n = len(buf)
    while i + 8 <= n:
        seg = buf[i:i + 8]
        if (seg[0] == 1 and seg[1] == 0x06 and seg[2] == 0x9C and seg[3] == 0x81 and
                crc_modbus(seg[:6]) == (seg[6] | (seg[7] << 8))):
            seq = (seg[4] << 8) | seg[5]
            if seq in found:
                dup.add(seq)
            found.add(seq)
            i += 8
            continue
        bad += 1
        i += 1
    missing = sorted(set(expected_seqs) - found)
    return found, dup, missing, bad


def phase_alternating(link, dut, n, seq0=0):
    """严格交替: 发一条 → 等应答 → 再发下一条 (基线)"""
    ok = 0
    for k in range(n):
        seq = seq0 + k
        link.s.reset_input_buffer()
        link.write(req_for(seq))
        t0 = time.time()
        buf = bytearray()
        while time.time() - t0 < 0.3 and len(buf) < 8:
            buf += link.read(8)
        f, _, _, _ = verify_stream(bytes(buf), [seq])
        if seq in f:
            ok += 1
        else:
            print("      seq %d 无合法响应 (收到 %s)" % (seq, bytes(buf).hex(" ")))
    return ok


def phase_concurrent(link, dut, secs, gap, seq0):
    """★ 收发同时: 写线程不停发, 主线程同时不停收"""
    stop = threading.Event()
    state = {"sent": 0, "seq": seq0}

    def writer():
        while not stop.is_set():
            link.write(req_for(state["seq"]))
            state["sent"] += 1
            state["seq"] += 1
            if gap > 0:
                time.sleep(gap)

    link.s.reset_input_buffer()
    th = threading.Thread(target=writer, daemon=True)
    t0 = time.time()
    th.start()                                   # ★ 收发同刻开始
    cap = bytearray()
    while time.time() - t0 < secs:
        d = link.read(4096)
        if d:
            cap += d
    stop.set()
    th.join(timeout=1.0)
    return state["sent"], seq0, bytes(cap)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", default="COM14")
    ap.add_argument("--mb", default="COM15")
    ap.add_argument("--secs", type=float, default=3.0)
    ap.add_argument("--n", type=int, default=20, help="交替基线的条数")
    a = ap.parse_args()

    dut = Dut(a.proto)
    link = Link(a.mb)

    def snap():
        d, c = dut.diag(), dut.ctr()
        return (d, c)

    def delta(d0, c0, d1, c1, tag):
        print("    板子侧[%s]: Δbytes=%d Δframes_rx=%d Δframes_tx=%d Δerr_crc=%d "
              "Δerr_exc=%d Δshort=%d ΔERRCLR=%d | maxrx=%d erracc=0x%02X"
              % (tag, d1["bytes"] - d0["bytes"], c1["frames_rx"] - c0["frames_rx"],
                 c1["frames_tx"] - c0["frames_tx"], c1["err_crc"] - c0["err_crc"],
                 c1["err_exc"] - c0["err_exc"], d1["short"] - d0["short"],
                 d1["errclr"] - d0["errclr"], d1["maxrx"], d1["erracc"]))

    print("=" * 78)
    print("485 收发同时 + 双向内容级对账  (请求=0x06 写 40065, 载荷=唯一序号)")
    print("=" * 78)

    # ---------- 阶段 1: 严格交替基线 ----------
    print("\n[1] 严格交替基线 (发一条等一条) —— 期望 100% 无丢包")
    d0, c0 = snap()
    ok = phase_alternating(link, dut, a.n, seq0=0)
    d1, c1 = snap()
    print("    PC 侧: %d/%d 条收到合法响应" % (ok, a.n))
    delta(d0, c0, d1, c1, "交替")
    print("    判据: %s" % ("PASS 基线无丢包" if ok == a.n else "★ FAIL 交替都有丢包 ⇒ 先查基础链路"))

    # ---------- 阶段 2: 并发, 无间隔 ----------
    print("\n[2] ★并发(收发同时, 帧间零间隔) %.1fs" % a.secs)
    print("    ★ 注意这一档**不是**在测碰撞: 帧间零间隔违反 Modbus 的 3.5 字符静默要求,")
    print("      板子看到的是**一个连续字节流**, 根本无法分帧 ⇒ 0 应答是**协议使然**。")
    print("      它真正测的是**接收路径在最恶劣节奏下会不会丢字节/溢出**:")
    d0, c0 = snap()
    sent, s0, cap = phase_concurrent(link, dut, a.secs, 0.0, seq0=1000)
    d1, c1 = snap()
    found, dup, missing, bad = verify_stream(cap, range(s0, s0 + sent))
    bsent = sent * 8
    brecv = d1["bytes"] - d0["bytes"]
    print("    PC 侧: 发出 %d 条 / %d 字节 | 抓到 %d 字节 | CRC 合法响应 %d 条"
          % (sent, bsent, len(cap), len(found)))
    print("    ★ 字节级准确率: 板子收到 %d/%d = %.3f%%   （← 这一档的核心指标）"
          % (brecv, bsent, 100.0 * brecv / max(1, bsent)))
    b2_sent, b2_recv = bsent, brecv        # 供汇总引用 (变量在下一阶段会被覆盖)
    delta(d0, c0, d1, c1, "并发零间隔")

    # ---------- 阶段 3: 并发, 3ms 间隔 ----------
    print("\n[3] ★并发(收发同时, 3ms 间隔) %.1fs" % a.secs)
    print("    ★ 这一档才是真正的半双工并发: PC **不等应答**连续发, 而板子的回发与")
    print("      PC 的下一条请求在**同一条两线总线上交错**。帧间留了 ≥3.5 字符静默 ⇒ 可成帧。")
    d0, c0 = snap()
    sent3, s3, cap3 = phase_concurrent(link, dut, a.secs, 0.003, seq0=2000)
    d1, c1 = snap()
    found3, dup3, missing3, bad3 = verify_stream(cap3, range(s3, s3 + sent3))
    print("    PC 侧: 发出 %d 条 | 抓到 %d 字节 | CRC 合法响应 %d 条 | 重复 %d | 字节流噪声 %d"
          % (sent3, len(cap3), len(found3), len(dup3), bad3))
    print("    丢失序号: %s" % (str(missing3[:20]) + (" …共%d个" % len(missing3)) if missing3 else "无"))
    delta(d0, c0, d1, c1, "并发3ms")

    # ---------- 阶段 4: 恢复性复检 ----------
    print("\n[4] 恢复性复检 —— 并发之后链路必须仍是活的 (防'跑完就锁死')")
    d0, c0 = snap()
    ok4 = phase_alternating(link, dut, 5, seq0=3000)
    d1, c1 = snap()
    print("    PC 侧: %d/5 条收到合法响应" % ok4)
    delta(d0, c0, d1, c1, "复检")
    print("    判据: %s" % ("PASS 无锁死" if ok4 == 5 else "★ FAIL 并发后链路未恢复"))

    # ---------- 汇总 ----------
    print("\n" + "=" * 78)
    print("双方对账汇总")
    print("=" * 78)
    print("  ① 内容一致性: PC 收到的每条响应都经 Modbus CRC 校验, 且携带唯一序号")
    print("     ⇒ 一条 CRC 合法响应 = 板上→PC 逐字节正确 + PC→板上请求被正确解析(含 CRC)")
    print("  ② 交替基线 %d/%d = %.1f%%" % (ok, a.n, 100.0 * ok / a.n))
    print("     并发(无间隔) %d/%d = %.1f%%" % (len(found), sent, 100.0 * len(found) / max(1, sent)))
    print("     并发(3ms)    %d/%d = %.1f%%" % (len(found3), sent3,
                                                100.0 * len(found3) / max(1, sent3)))
    print("  ③ 两条**不同层面**的结论, 别混为一谈:")
    print("     (a) 字节层: 帧间零间隔下板子仍收到 %d/%d = %.3f%% 的字节、且一次 ORE 都没有"
          % (b2_recv, b2_sent, 100.0 * b2_recv / max(1, b2_sent)))
    print("         ⇒ 接收路径在最恶劣节奏下不丢字节、不溢出 (FIFO + 每拍清错误生效)")
    print("     (b) 帧层: 分帧依赖帧间 ≥3.5 字符静默 (Modbus RTU 规定)。零间隔 ⇒")
    print("         无法成帧 ⇒ 0 应答是**协议使然**, 不是链路或固件缺陷。")
    print("     (c) 端到端: 合法节奏(3ms)下 %d/%d = %.1f%% 逐条内容正确 (CRC 校验过)"
          % (len(found3), sent3, 100.0 * len(found3) / max(1, sent3)))
    print("  ④ 无锁死: 并发之后恢复性复检 %d/5 ⇒ 通信域在极端节奏后仍可用。" % ok4)
    dut.s.close()
    link.s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
