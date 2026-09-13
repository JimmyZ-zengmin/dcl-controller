#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_485_ore_verify.py — 复现审计报告 `docs/audit/H723-485-RX-AUDIT.md` 的三条判据

审计结论 (2026-09-13, 提交 14fe5c6):
  根因① `mb_pull_rx` 只查 RXNE; ORE=1 且 RXNE=0 时 break ⇒ 永不读 RDR ⇒ ORE 永不清
        ⇒ H7 在 ORE 期间丢弃新字符 ⇒ **接收自锁死**。
  根因② 单字节 RDR + 100µs 轮询 > 86.8µs 字节间隔 ⇒ 相位漂移 ⇒ 周期性"一拍 2 字节"
        ⇒ 必然 ORE。(原注释"每拍最多 1.15 字节"是**平均值不是上界**。)
  修复  开 FIFOEN (深度 8) + 每拍主动清错误标志。

★ 本脚本要**独立复现**, 不照抄结论。三段判据:
  P1 慢发 (逐字节 2000µs ≫ 拍周期) → 应 8/8 全收  (证明链路与引脚都好)
  P2 快发 (整帧连续写, 86.8µs/字节) → 应部分丢失 (证明根因②)
  P3 不补清 ORE 再快发       → 应 Δbytes=0    (证明根因① 锁死)
  P4 清 ORE 后再快发         → 应立刻复活      (证明"锁死"是可逆的、且确实是 ORE 引起)
  P5 开 FIFOEN 后快发        → 应全收           (证明修复方向正确)

★ 分工 (铁律 0):
  · **改寄存器**用 pyocd —— 因为协议口没有"清 USART2 错误标志"这个命令; 属必要之恶,
    且每处都写清楚、用完 `go` 放核。
  · **读字节计数/帧计数**走协议口 0x63 / 0x61 —— 观测面绝不碰被测对象。

用法:
  python tools/h723_485_ore_verify.py --proto COM14 --mb COM15
"""
import sys
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, re, subprocess, time

try:
    import serial
except ImportError:
    print("!! need pyserial")
    sys.exit(2)

SYNC_MCU2PC = 0xC1
USART2 = 0x40004400
U_CR1, U_ISR, U_ICR, U_RDR = USART2 + 0x00, USART2 + 0x1C, USART2 + 0x20, USART2 + 0x24
CR1_OFF = 0x0000000D           # UE|TE|RE         (FIFO 关)
CR1_ON  = 0x2000000D           # FIFOEN|UE|TE|RE  (FIFO 开)


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


# ---------------- pyocd: 只用来"改寄存器" ----------------

def pyocd(chain, timeout=120):
    args = ["pyocd", "cmd", "-t", "stm32h723xx", "-O", "connect_mode=under-reset"]
    for c in chain:
        args += ["-c", c]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    vals = []
    for ln in out.splitlines():
        m = re.match(r"\s*[0-9a-fA-F]{8}\s*:\s*([0-9a-fA-F]{8})", ln)
        if m:
            vals.append(int(m.group(1), 16))
    return vals, out


def set_usart2(fifo_on, clear_err, boot_ms=400, show=False):
    """reset → go → halt → (清错误) → (设 CR1) → go。返回 (ISR, CR1) 读回。"""
    chain = ["reset", "go", "sleep %d" % boot_ms, "halt"]
    if clear_err:
        chain.append("write32 0x%08X 0x1FF" % U_ICR)
    if fifo_on is not None:
        # ★ H7 要求 FIFOEN 在 UE=0 时配置 ⇒ 先关 UE 再整写 CR1
        chain += ["write32 0x%08X 0x00000000" % U_CR1,
                  "write32 0x%08X 0x%08X" % (U_CR1, CR1_ON if fifo_on else CR1_OFF)]
    chain += ["read32 0x%08X" % U_ISR, "read32 0x%08X" % U_CR1, "go"]
    vals, out = pyocd(chain)
    if show:
        print(out)
    isr = vals[0] if len(vals) > 0 else None
    cr1 = vals[1] if len(vals) > 1 else None
    return isr, cr1


# ---------------- 协议口: 读字节级诊断 ----------------

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

    def diag(self):
        r = self._x(dcl_frame(0x63))
        if len(r) < 6 or r[0] != SYNC_MCU2PC:
            return None
        ln = r[2] | (r[3] << 8)
        body, cr = r[1:4 + ln], (r[4 + ln] | (r[5 + ln] << 8))
        if crc_ccitt(body) != cr:
            return None
        p = r[4:4 + ln]
        return [int.from_bytes(p[i:i + 4], "little") for i in range(0, len(p) // 4 * 4, 4)]

    def ctr(self):
        r = self._x(dcl_frame(0x61))
        if len(r) < 6 or r[0] != SYNC_MCU2PC:
            return None
        ln = r[2] | (r[3] << 8)
        p = r[4:4 + ln]
        o = 2 + p[1]
        if len(p) < o + 16:
            return None
        g = lambda i: int.from_bytes(p[o + 4 * i:o + 4 * i + 4], "little")
        return dict(frames_rx=g(0), frames_tx=g(1), err_crc=g(2), err_exc=g(3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", default="COM14")
    ap.add_argument("--mb", default="COM15")
    ap.add_argument("--slow-us", type=int, default=2000, help="慢发时每字节间隔 (µs)")
    ap.add_argument("--show-raw", action="store_true")
    a = ap.parse_args()

    dut = Dut(a.proto)
    mb = serial.Serial(a.mb, 115200, timeout=0.05)
    req = mb_frame(1, [0x03, 0x9C, 0x41, 0x00, 0x0A])
    print("请求帧 = %s (%d 字节)" % (req.hex(" "), len(req)))

    def snap(tag):
        d = dut.diag()
        c = dut.ctr()
        if d is None or c is None:
            print("  [%s] 协议口读失败" % tag)
            return None
        print("  [%-22s] bytes=%-4d maxrx=%-3d short=%-3d erracc=0x%02X last=0x%02X"
              " | frames_rx=%-3d err_crc=%-3d"
              % (tag, d[0], d[1], d[2], d[4], d[5], c["frames_rx"], c["err_crc"]))
        return d

    print("\n===== P0 当前现场 =====")
    isr, cr1 = set_usart2(fifo_on=None, clear_err=False, show=a.show_raw)
    print("  pyocd 读回: USART2 ISR=0x%08X  CR1=0x%08X  (FIFOEN=%s)"
          % (isr or 0, cr1 or 0, "开" if (cr1 or 0) & 0x20000000 else "关"))
    snap("P0 现场")

    print("\n===== P1 慢发 (每字节 %d µs, 远大于 100µs 拍周期) =====" % a.slow_us)
    set_usart2(fifo_on=False, clear_err=True)
    d0 = snap("P1 起点")
    for b in req:
        mb.write(bytes([b])); mb.flush()
        time.sleep(a.slow_us / 1e6)
    time.sleep(0.4)
    d1 = snap("P1 慢发 1 帧后")
    if d0 and d1:
        print("  ⇒ Δbytes = %d / 期望 %d  %s"
              % (d1[0] - d0[0], len(req), "★ 全收 (链路/引脚都好)" if d1[0] - d0[0] >= len(req) - 1 else "!! 没收全"))

    print("\n===== P2 快发 (整帧连续写, 86.8µs/字节 < 100µs 拍) =====")
    set_usart2(fifo_on=False, clear_err=True)
    d0 = snap("P2 起点")
    mb.write(req); mb.flush()
    time.sleep(0.4)
    d1 = snap("P2 快发 1 帧后")
    if d0 and d1:
        print("  ⇒ Δbytes = %d / 期望 %d  %s"
              % (d1[0] - d0[0], len(req), "★ 丢字节 (根因②成立)" if d1[0] - d0[0] < len(req) else "全收"))

    print("\n===== P3 不补清错误再快发 (验证根因①: ORE 未清 ⇒ 锁死) =====")
    d0 = snap("P3 起点 (ORE 保持)")
    for _ in range(3):
        mb.write(req); mb.flush(); time.sleep(0.05)
    time.sleep(0.4)
    d1 = snap("P3 快发 3 帧后")
    if d0 and d1:
        print("  ⇒ Δbytes = %d / 期望 %d  %s"
              % (d1[0] - d0[0], 3 * len(req),
                 "★★ Δ=0 ⇒ 锁死复现 (ORE 未清, 接收彻底停摆)" if d1[0] == d0[0] else "仍有字节进来"))

    print("\n===== P4 清 ORE 后再快发 (验证锁死可逆、且确由 ORE 引起) =====")
    isr, cr1 = set_usart2(fifo_on=None, clear_err=True)
    print("  清后 pyocd 读回: ISR=0x%08X" % (isr or 0))
    d0 = snap("P4 起点")
    for _ in range(3):
        mb.write(req); mb.flush(); time.sleep(0.05)
    time.sleep(0.4)
    d1 = snap("P4 快发 3 帧后")
    if d0 and d1:
        print("  ⇒ Δbytes = %d  %s"
              % (d1[0] - d0[0],
                 "★ 接收立刻复活 ⇒ 锁死确由 ORE 造成" if d1[0] > d0[0] else "!! 没复活, 审计的根因①不成立"))

    print("\n===== P5 开 FIFOEN 后快发 (验证修复方向) =====")
    isr, cr1 = set_usart2(fifo_on=True, clear_err=True)
    print("  pyocd 读回: CR1=0x%08X  (FIFOEN=%s)" % (cr1 or 0, "开" if (cr1 or 0) & 0x20000000 else "关"))
    d0 = snap("P5 起点")
    n_tx = 4
    mb.reset_input_buffer()
    for _ in range(n_tx):
        mb.write(req); mb.flush(); time.sleep(0.05)
    time.sleep(0.5)
    d1 = snap("P5 快发 4 帧后")
    # 收总线上的应答
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < 0.6:
        n = mb.in_waiting
        if n:
            buf += mb.read(n)
        else:
            time.sleep(0.002)
    ok = 0
    for i in range(len(buf) - 24):
        seg = buf[i:i + 25]
        if len(seg) == 25 and crc_modbus(bytes(seg[:23])) == (seg[23] | (seg[24] << 8)):
            ok += 1
    if d0 and d1:
        print("  ⇒ Δbytes = %d / 期望 %d  %s"
              % (d1[0] - d0[0], n_tx * len(req),
                 "★★ 全收 (修复方向成立)" if d1[0] - d0[0] >= n_tx * len(req) else "仍有丢失"))
    print("  ⇒ 总线上收到 CRC 合法应答: %d 段 (发了 %d 帧)" % (ok, n_tx))

    print("\n===== 收尾 =====")
    print("  ★ 当前 CR1 是 pyocd 写的**运行期状态**, 复位/重烧即失效 —— 要固化必须改源码。")
    dut.s.close()
    mb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
