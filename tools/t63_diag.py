#!/usr/bin/env python3
"""
t63_diag.py - 只用协议口读通信域诊断区 (0x63 MB_DIAG)

★ 为什么需要它 (2026-09-12 血证):
  之前所有"物理口收到 0 字节"的读数都是 **仪器伪影** —— pyocd 每次 connect
  都会复位目标, 而诊断区在 DTCM (上电清零)。读数 = "我刚清零后的值",
  与真实运行状态无关 (AXI 启动计数器 6 次读取 = 启动 1..6 实锤)。
  ⇒ 观测面必须走**协议口** (0x63), 全程不开 pyocd 会话。
     观测不得改变被测对象 (铁律 0)。

用法:
    python tools/t63_diag.py                 # 自动找 CH340 (逐个试)
    python tools/t63_diag.py --port COM14
    python tools/t63_diag.py --port COM14 --watch 5   # 每 2s 读一次, 看增量
"""
import sys
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, time, struct

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    print("!! need pyserial")
    sys.exit(2)

SYNC_PC2MCU = 0xC0
SYNC_MCU2PC = 0xC1
STS_ACK = 0x00


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    body = bytes([cmd, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    c = crc16(body)
    return bytes([SYNC_PC2MCU]) + body + bytes([c & 0xFF, c >> 8])


def recv_frame(ser, timeout=0.6):
    buf = bytearray()
    end = time.time() + timeout
    while time.time() < end:
        ch = ser.read(1)
        if not ch:
            continue
        buf += ch
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
            more = ser.read(need - len(buf))
            if more:
                buf += more
        if len(buf) < need:
            break
        frame = bytes(buf[:need])
        body, crc_rx = frame[1:-2], (frame[-2] | (frame[-1] << 8))
        if crc16(body) != crc_rx:
            return ("CRCBAD", frame)
        return (frame[1], frame[4:4 + plen])
    return None


# 诊断区索引 (必须与 src/modbus.c 的 MB_DIAG_* 一致)
NAMES = {
    0:  "bytes(物理口累计字节)",
    1:  "maxrx(见过最大 rx_len)",
    2:  "short(太短被丢次数)",
    3:  "last_isr",
    4:  "erracc(PE|FE|NE|ORE)",
    5:  "last_byte",
    6:  "line-map(IDR 位图)",
    7:  "line-map marker",
    8:  "GPIOD MODER",
    9:  "GPIOD AFRL",
    10: "GPIOD PUPDR",
    11: "cfg marker",
    12: "PD6 low samples",
    13: "PD6 total samples",
    14: "probe marker",
    15: "ERRCLR(清错误次数)",
    16: "USART2 CR1",
    17: "USART2 CR2",
    18: "USART2 CR3",
    19: "USART2 BRR",
    20: "USART2 ISR",
    21: "USART2 PRESC",
    22: "reg marker",
    23: "LAT_LAST(板内响应延迟,拍=100us)",
    24: "LAT_MIN(拍)",
    25: "LAT_MAX(拍)",
    26: "LAT_N(样本数)",
    27: "T_RX(内部,拍)",
    28: "FASTOK(早判帧成功次数)",
    29: "free",
}

KEY = [0, 1, 2, 3, 4, 5, 15, 16, 17, 18, 19, 20, 21, 22,
       23, 24, 25, 26, 28, 6, 8, 9, 10, 11]


def read_diag(ser, timeout=0.6):
    ser.reset_input_buffer()
    ser.write(build_frame(0x63))
    ser.flush()
    r = recv_frame(ser, timeout)
    return r


def show(payload):
    words = struct.unpack("<%dI" % (len(payload) // 4), payload[:len(payload) // 4 * 4])
    for i in KEY:
        if i >= len(words):
            break
        print("   [%2d] %-26s = 0x%08X  (%d)" % (i, NAMES[i], words[i], words[i]))
    return words


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--watch", type=int, default=0, help="重复次数 (间隔 2s)")
    ap.add_argument("--all-ports", action="store_true", help="对所有 CH340 口各试一次")
    a = ap.parse_args()

    cands = []
    if a.port:
        cands = [a.port]
    else:
        for p in list_ports.comports():
            if "CH340" in (p.description or "") or "1A86" in (p.hwid or ""):
                cands.append(p.device)
    if a.all_ports and not a.port:
        pass  # cands 已是全部 CH340

    if not cands:
        print("!! 没有找到 CH340 串口")
        return 3

    for port in cands:
        print("=== %s ===" % port)
        try:
            ser = serial.Serial(port, a.baud, timeout=0.05)
        except Exception as ex:
            print("   打开失败: %s" % ex)
            continue
        try:
            n = a.watch if a.watch > 0 else 1
            prev = None
            for k in range(n):
                r = read_diag(ser)
                if r is None:
                    print("   [%d] 无响应 (0 字节)" % k)
                elif r[0] == "CRCBAD":
                    print("   [%d] 收到帧但 CRC 错: %s" % (k, r[1].hex()))
                else:
                    sts, pl = r
                    print("   [%d] sts=0x%02X len=%d" % (k, sts, len(pl)))
                    w = show(pl)
                    if prev is not None:
                        for i in KEY:
                            if i < len(w) and i < len(prev):
                                d = (w[i] - prev[i]) & 0xFFFFFFFF
                                if d:
                                    print("        Δ[%2d] %-26s = %+d" % (i, NAMES[i], d))
                    prev = w
                if k + 1 < n:
                    time.sleep(2.0)
        finally:
            ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
