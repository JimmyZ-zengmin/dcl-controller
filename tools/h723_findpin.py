#!/usr/bin/env python3
"""
h723_findpin.py — H1 排针脚位实测定位 (不需要万用表 / 不需要照片)

背景: H1 是 2x4 排针, "第 5/6 脚"在 2x4 上是有歧义的 (取决于从哪个方向数)。
      板上丝印若看不清, 最省事的办法是**让板子自己告诉你哪个脚是 PA9**。

前提 (由固件提供): 烧一版"信标固件", PA9 每 300ms 主动发一帧合法 DCL 帧:
        bash build.sh -DDCL_BANNER_PERIOD=300
      (帧内容 = GET_VERSION 响应, 10 字节:
       C1 00 04 00 00 02 21 00 D8 AC —— 带 CRC, 不可能是噪声凑出来的)

用法:
    python tools/h723_findpin.py --listen          # ① 找 PA9: 把 CH340 的 RXD 逐脚试
    python tools/h723_findpin.py --scan-tx         # ② 找 PA10: RXD 已接好, 逐脚试 TXD
    python tools/h723_findpin.py --pins            # 打印 H1 脚位表 (来自板商原理图)

判读:
    --listen  每秒打印"收到几个合法 DCL 帧"。
              ★ 某只脚上开始稳定出现帧 ⇒ **那个脚就是 PA9** (H1 第 6 脚)。
              噪声/悬空会有少量原始字节, 但**解不出合法帧** —— 判据用 CRC, 不看字节数。
    --scan-tx 反复发 GET_VERSION。当 TXD 落到 PA10 上时, 板子会回 ACK
              (回在 PA9 上, 所以 RXD 必须已经接好) ⇒ 报"找到了"。
"""

# ★ Windows 控制台默认 GBK: 脚本自己 print 出来的个别字符 (⇒ / ✓ 等) 会以
#   UnicodeEncodeError **直接崩掉整个脚本** —— 数据都量到了, 却崩在"打印结论"这一步,
#   症状看起来像"脚本坏了"而不是"编码问题"。⇒ 统一在入口把 stdout 的错误策略改成
#   "永不抛" (换成 ?), 让验收脚本不可能因为自己的输出而失败。
#   (2026-09-11 实测: audit_m234 / w1 真的这么崩过一次, 整份结果都没打出来。)
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

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
CMD_GET_VERSION = 0x01

H1_PINS = """
H1 (排针_卧贴_2*4P) —— 来自板商原理图 sch_p1.png "SWD和USART1接口"
物理排布 (奇数/偶数 / Odd_Even):
        列A   列B
  行1    1     5     1=SWCLK PA14    5=USART1_RX PA10  ← CH340 TXD 接这里
  行2    2     6     2=SWDIO PA13    6=USART1_TX PA9   ← CH340 RXD 接这里
  行3    3     7     3=GND           7=RST (串 1K)
  行4    4     8     4=+5V           8=3.3V
★ 关键交叉验证: **PA9 与 SWDIO(2) 同一行**; **PA10 与 SWCLK(1) 同一行**。
  另: 3=GND / 8=3.3V / 4=+5V 可以用万用表一秒认出来, 认出任一个就能定位整列。
  CH340 只接 3 根: RXD/TXD/GND。**不要把 CH340 的 VCC 接上去**(板子已由 DAPLink 供电)。
"""


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    body = bytes([cmd, 0, 0]) + payload
    c = crc16(body)
    return bytes([SYNC_PC2MCU]) + body + bytes([c & 0xFF, c >> 8])


def hexdump(b) -> str:
    return " ".join("%02X" % x for x in b)


class FrameFinder:
    """流式找合法 DCL 帧 (MCU→PC 方向); 非法/噪声一律丢弃但计数"""

    def __init__(self):
        self.buf = bytearray()
        self.raw = 0
        self.frames = []

    def feed(self, chunk: bytes):
        self.raw += len(chunk)
        self.buf += chunk
        out = []
        while True:
            i = self.buf.find(bytes([SYNC_MCU2PC]))
            if i < 0:
                self.buf.clear()
                break
            if i:
                del self.buf[:i]
            if len(self.buf) < 4:
                break
            plen = self.buf[2] | (self.buf[3] << 8)
            need = 4 + plen + 2
            if len(self.buf) < need:
                if plen > 512:            # 不可能的长度 → 丢一个字节继续找
                    del self.buf[:1]
                    continue
                break
            frame = bytes(self.buf[:need])
            del self.buf[:need]
            body, crc_rx = frame[1:-2], (frame[-2] | (frame[-1] << 8))
            if crc16(body) == crc_rx:
                self.frames.append(frame)
                out.append(frame)
        return out


def open_port(port, baud):
    if not port:
        for p in list_ports.comports():
            if "CH340" in (p.description or "") or "1A86" in (p.hwid or ""):
                port = p.device
                break
    if not port:
        print("!! 找不到 CH340。用 --port 指定。当前可见:")
        for p in list_ports.comports():
            print("   %-8s %s" % (p.device, p.description))
        return None, None
    return serial.Serial(port, baud, timeout=0.05), port


def cmd_listen(args):
    ser, port = open_port(args.port, args.baud)
    if ser is None:
        return 2
    print("监听 %s @ %d  (最多 %ds)\n" % (port, args.baud, int(args.sec)))
    print("  ┌ 把 CH340 的 **RXD** 线逐脚碰 H1。**每只脚至少停 2 秒**(信标 300ms 一帧)。")
    print("  └ 屏幕每秒刷新一行。某只脚上开始出现「合法帧」⇒ 那就是 PA9。\n")
    finder = FrameFinder()
    t_end = time.time() + args.sec
    t_line = time.time() + 1.0
    found_at = None
    while time.time() < t_end:
        chunk = ser.read(4096)
        if chunk:
            for f in finder.feed(chunk):
                if found_at is None:
                    found_at = time.time()
                    print("\n  ★★ 收到合法 DCL 帧 → **这个脚就是 PA9 (H1 第 6 脚)!**")
                    print("     (与 SWDIO 同一行的那个脚)  帧: %s" % hexdump(f))
                else:
                    print("     帧: %s" % hexdump(f))
        if found_at and time.time() - found_at > 1.0:
            break
        if time.time() >= t_line:
            t_line += 1.0
            mark = "  ← 有信号!" if finder.frames else ""
            sys.stdout.write("  合法帧 %3d 个 | 原始字节 %6d (悬空噪声)%s\n"
                             % (len(finder.frames), finder.raw, mark))
            sys.stdout.flush()
        time.sleep(0.05)
    ser.close()
    print("\n统计: 合法 DCL 帧 %d 个 / 原始字节 %d" % (len(finder.frames), finder.raw))
    if finder.frames:
        print("结论: 探针所在的那只脚 = PA9。保持接好, 然后跑 --scan-tx 找 PA10。")
        return 0
    print("结论: 这只脚上没有 DCL 帧 —— 原始字节是悬空噪声 (解不出 CRC 合法帧)。")
    print("      → 换一只脚继续试。")
    return 1


def cmd_scan_tx(args):
    ser, port = open_port(args.port, args.baud)
    if ser is None:
        return 2
    print("扫描 TX 方向: %s @ %d  (最多 %ds)" % (port, args.baud, int(args.sec)))
    print("前提: CH340 的 RXD 已经接在 **PA9** 上 (否则即使 TXD 接对了也看不到回包)")
    print("做法: 把 CH340 的 **TXD** 线逐脚碰 H1, 每只脚停 3 秒。\n")
    finder = FrameFinder()
    req = build_frame(CMD_GET_VERSION)
    t_end = time.time() + args.sec
    t_line = time.time() + 3.0
    n_tx = 0
    while time.time() < t_end:
        ser.write(req)
        ser.flush()
        n_tx += 1
        chunk = ser.read(4096)
        if chunk:
            for f in finder.feed(chunk):
                if f[1] == 0x00:          # ACK
                    print("\n  ★★ 收到 ACK → **这个脚就是 PA10 (H1 第 5 脚)!**")
                    print("     (与 SWCLK 同一行的那个脚)  帧: %s" % hexdump(f))
                    ser.close()
                    print("\n结论: TXD 所在的那只脚 = PA10。两根线都定位完成。")
                    return 0
        if time.time() >= t_line:
            t_line += 3.0
            sys.stdout.write("  已发 %4d 次 GET_VERSION | 合法帧 %d 个 | 原始字节 %6d\n"
                             % (n_tx, len(finder.frames), finder.raw))
            sys.stdout.flush()
        time.sleep(0.05)
    ser.close()
    print("统计: 发了 %d 次 GET_VERSION, 合法帧 %d 个 / 原始字节 %d"
          % (n_tx, len(finder.frames), finder.raw))
    print("未找到 PA10。检查: ① RXD 是否真的接在 PA9 上 ② 板子是否在跑")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--listen", action="store_true", help="找 PA9 (碰 RXD)")
    ap.add_argument("--scan-tx", action="store_true", help="找 PA10 (碰 TXD)")
    ap.add_argument("--pins", action="store_true", help="打印 H1 脚位表")
    ap.add_argument("--sec", type=float, default=60.0, help="每轮最多跑多少秒")
    a = ap.parse_args()

    if a.pins or not (a.listen or a.scan_tx):
        print(H1_PINS)
        if not (a.listen or a.scan_tx):
            return 0
    if a.listen:
        return cmd_listen(a)
    return cmd_scan_tx(a)


if __name__ == "__main__":
    sys.exit(main())
