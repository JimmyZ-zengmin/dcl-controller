#!/usr/bin/env python3
"""
h723_proto.py — H723 协议层 PC 侧验证 (阶段 3.1)

对接口: USART1 @ 115200 (板子 PA9=TX / PA10=RX ← 接 H1 排针 6/5 脚)
        PC 侧需要 USB-UART 桥 (CH340 → COM14)

★ 设计原则: **不只测 happy path**。
  只测"发一条合法请求、收到合法响应"是无法说明协议层是对的 ——
  因为一个"永远回固定字节"的假固件也能过。
  所以每个用例都要求**可失败**:
    T1 正常协商      → 必须回 ACK + 正确 fw/cap
    T2 未知命令      → 必须回 NAK 且载荷是 "bad cmd" (证明命令分发真的在跑)
    T3 CRC 故意写错  → 必须**无响应** (证明 CRC 真的在查, 不是照单全收)
    T4 长度超上限    → 必须**无响应** (证明解析器会丢弃非法长度)
    T5 逐字节慢发    → 仍能正确解析 (证明是流式状态机, 不是整包赌运气)
    T6 连发两帧      → 必须**收到两帧** (证明帧间状态机复位正确
                                       —— 只收第一帧是经典缺陷)
用法:
    python tools/h723_proto.py                     # 自动找 CH340
    python tools/h723_proto.py --port COM14
    python tools/h723_proto.py --raw               # 附带每帧 hex 转储(与 LA 对账用)
"""
import argparse
import sys
import time

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    print("!! 需要 pyserial: pip install pyserial")
    sys.exit(2)

SYNC_PC2MCU = 0xC0
SYNC_MCU2PC = 0xC1
STS_ACK = 0x00
STS_NAK = 0xFF

CMD_GET_VERSION = 0x01
CMD_NAMES = {
    0x01: "GET_VERSION", 0x10: "DEPLOY", 0x11: "START", 0x12: "STOP", 0x13: "RESET",
    0x20: "READ", 0x21: "WRITE", 0x22: "READ_BURST", 0x23: "WRITE_BURST", 0x24: "FORCE",
    0x38: "ENGINE_STATUS", 0x44: "SEQ_DEPLOY", 0x60: "MB_INJECT", 0x61: "MB_RESP",
    0x62: "MB_CFG",
}
CAP_NAMES = [
    (0x0001, "MULTICYCLE"), (0x0002, "HOTRELOAD"), (0x0004, "PERSISTENT"),
    (0x0008, "STATE_COLD"), (0x0010, "WIRE2_FLAG"), (0x0020, "VERINFO"),
    (0x0040, "SEQ"), (0x0080, "FORCE"), (0x0100, "COMM"),
    (0x0200, "HMI"), (0x0400, "AI"),
]

# 本平台「应该」声明什么 (与 src/transport.h 的 DCL_CAP_H723_IMPL 必须一致)
EXPECT_FW = 0x0200
EXPECT_CAP = 0x0001 | 0x0020


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def build_frame(cmd: int, payload: bytes = b"", *, corrupt_crc=False, sync=SYNC_PC2MCU,
                force_len=None) -> bytes:
    body = bytes([cmd, 0, 0]) + payload if force_len is None else \
        bytes([cmd, force_len & 0xFF, (force_len >> 8) & 0xFF]) + payload
    c = crc16(body)
    if corrupt_crc:
        c ^= 0xFFFF
    return bytes([sync]) + body + bytes([c & 0xFF, c >> 8])


def hexdump(b: bytes) -> str:
    return " ".join("%02X" % x for x in b)


class Link:
    def __init__(self, ser, verbose=False):
        self.ser = ser
        self.verbose = verbose
        self.bad = 0

    def send(self, frame: bytes):
        if self.verbose:
            print("      TX: %s" % hexdump(frame))
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        self.ser.flush()

    def recv_frame(self, timeout=0.5):
        """读一个完整帧; 返回 (sts, payload) 或 None"""
        buf = bytearray()
        end = time.time() + timeout
        while time.time() < end:
            chunk = self.ser.read(1)
            if not chunk:
                continue
            buf += chunk
            # 找同步头
            if buf[0] != SYNC_MCU2PC:
                i = buf.find(bytes([SYNC_MCU2PC]))
                if i < 0:
                    buf.clear()
                    continue
                del buf[:i]
            if len(buf) < 4:
                continue
            plen = buf[2] | (buf[3] << 8)
            need = 4 + plen + 2
            while len(buf) < need and time.time() < end:
                more = self.ser.read(need - len(buf))
                if more:
                    buf += more
            if len(buf) < need:
                break
            frame = bytes(buf[:need])
            if self.verbose:
                print("      RX: %s" % hexdump(frame))
            body, crc_rx = frame[1:-2], (frame[-2] | (frame[-1] << 8))
            if crc16(body) != crc_rx:
                self.bad += 1
                return ("CRCBAD", frame)          # 本地也校验一遍: 别信"看起来像帧"
            return (frame[1], frame[4:4 + plen])
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None, help="串口 (默认自动找 CH340)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--raw", action="store_true", help="打印每帧 hex (与 LA 对账用)")
    ap.add_argument("--timeout", type=float, default=0.5)
    a = ap.parse_args()

    port = a.port
    if not port:
        for p in list_ports.comports():
            if "CH340" in (p.description or "") or "1A86" in (p.hwid or ""):
                port = p.device
                break
    if not port:
        print("!! 找不到 CH340 串口。用 --port 指定, 或确认驱动/接线。")
        print("   当前可见:")
        for p in list_ports.comports():
            print("     %-8s %s" % (p.device, p.description))
        return 2

    print("串口 %s @ %d" % (port, a.baud))
    ser = serial.Serial(port, a.baud, timeout=0.05)
    time.sleep(0.2)
    link = Link(ser, verbose=a.raw)
    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))
        print("  [%s] %-34s %s" % ("PASS" if cond else "FAIL", name, detail))

    # 先排掉上电横幅 (可能已经在缓冲里)
    ser.reset_input_buffer()

    # ── T1 版本/能力协商 ──
    link.send(build_frame(CMD_GET_VERSION))
    r = link.recv_frame(a.timeout)
    if r is None:
        check("T1 GET_VERSION 有响应", False, "超时无响应")
    else:
        sts, pl = r
        fw = pl[0] | (pl[1] << 8) if len(pl) >= 4 else None
        cap = pl[2] | (pl[3] << 8) if len(pl) >= 4 else None
        check("T1.1 状态为 ACK", sts == STS_ACK, "sts=0x%02X" % (sts if isinstance(sts, int) else 0))
        check("T1.2 载荷 4 字节", len(pl) == 4, "len=%d" % len(pl))
        check("T1.3 fw=0x%04X" % EXPECT_FW, fw == EXPECT_FW, "实际 0x%04X" % (fw or 0))
        check("T1.4 cap=0x%04X (宣称=实现)" % EXPECT_CAP, cap == EXPECT_CAP,
              "实际 0x%04X" % (cap or 0))
        if cap is not None:
            print("        声明的能力: %s" % ", ".join(
                n for bit, n in CAP_NAMES if cap & bit) or "(无)")
            print("        未声明:     %s" % ", ".join(
                n for bit, n in CAP_NAMES if not cap & bit))

    # ── T2 未知命令必须被显式拒绝 ──
    link.send(build_frame(0x99))
    r = link.recv_frame(a.timeout)
    ok = r is not None and r[0] == STS_NAK and r[1] == b"bad cmd"
    check("T2 未知命令 → NAK 'bad cmd'", ok,
          "sts=%s payload=%r" % (r[0] if r else None, r[1] if r else None))

    # ── T3 CRC 错必须被丢弃 (无响应) ──
    link.send(build_frame(CMD_GET_VERSION, corrupt_crc=True))
    r = link.recv_frame(a.timeout)
    check("T3 CRC 错 → 无响应", r is None,
          "响应=%r (CRC 校验没生效!)" % (r,))

    # ── T4 长度超上限必须被丢弃 ──
    link.send(build_frame(CMD_GET_VERSION, b"\x00" * 8, force_len=0x1FFF))
    r = link.recv_frame(a.timeout)
    check("T4 长度超限 → 无响应", r is None, "响应=%r" % (r,))

    # ── T5 逐字节慢发仍能解析 (流式状态机) ──
    ser.reset_input_buffer()
    frame = build_frame(CMD_GET_VERSION)
    for by in frame:
        ser.write(bytes([by]))
        ser.flush()
        time.sleep(0.002)                      # 每字节间隔 2ms, 远大于字节时间
    r = link.recv_frame(a.timeout)
    check("T5 分片慢发 → 仍解析成功", r is not None and r[0] == STS_ACK,
          "sts=%s" % (r[0] if r else None))

    # ── T6 连发两帧必须都收到 (帧间复位) ──
    ser.reset_input_buffer()
    ser.write(build_frame(CMD_GET_VERSION) + build_frame(CMD_GET_VERSION))
    ser.flush()
    n = 0
    t_end = time.time() + 1.0
    while n < 2 and time.time() < t_end:
        rr = link.recv_frame(0.3)
        if rr:
            n += 1
    check("T6 连发两帧 → 收到两帧", n == 2, "收到 %d 帧" % n)

    ser.close()

    npass = sum(1 for _, ok, _ in results if ok)
    print("\n结果: %d/%d PASS%s" % (npass, len(results),
                                    "  (本地 CRC 校验失败 %d 次)" % link.bad if link.bad else ""))
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
