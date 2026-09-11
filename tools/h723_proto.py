#!/usr/bin/env python3
"""
h723_proto.py — H723 协议层 PC 侧验证 (阶段 3.1)

对接口: USART1 @ 115200 (板子 PA9=TX / PA10=RX ← 接 H1 排针 6/5 脚)
        PC 侧需要 USB-UART 桥 (CH340 → COM14)

★ 设计原则: **不只测 happy path**。
  只测"发一条合法请求、收到合法响应"是无法说明协议层是对的 ——
  因为一个"永远回固定字节"的假固件也能过。
  所以每个用例都要求**可失败**:
    T0 链路活性      → GET_VERSION 必须回 ACK (以下所有"无响应"类判据的前提)
    T1 正常协商      → 必须回 ACK + 正确 fw/cap
    T2 未知命令      → 必须回 NAK 且载荷是 "bad cmd" (证明命令分发真的在跑)
    T3 CRC 故意写错  → 必须**无响应** (证明 CRC 真的在查, 不是照单全收) + 阳性对照
    T4 长度超上限    → 必须**无响应** (证明解析器会丢弃非法长度) + 阳性对照
    T5 逐字节慢发    → 仍能正确解析 (证明是流式状态机, 不是整包赌运气)
    T6 连发两帧      → 必须**收到两帧** (证明帧间状态机复位正确
                                       —— 只收第一帧是经典缺陷)

★★ 为什么需要 T0 与"阳性对照" (第一版缺这个, 是个真缺陷):
  "无响应" 这个判据在**链路根本不通**时也成立 —— 拔掉 USB 线, T3/T4 会全绿。
  即判据恒真 (与固件侧 OA23 同族: 只有"能失败"的判据才是证据)。
  修法两条:
    ① T0 先证明链路活; 链路不活则后续全部记 SKIP (不计 PASS, 退出码 2)
    ② T3/T4 的"无响应"之后各加一次**阳性对照**: 再发合法帧必须回 ACK
       —— 证明那阵沉默是"故意丢弃"而不是"链路已经死了"

用法:
    python tools/h723_proto.py                     # 自动找 CH340
    python tools/h723_proto.py --port COM14
    python tools/h723_proto.py --raw               # 附带每帧 hex 转储(与 LA 对账用)
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

# 本平台「应该」声明什么 —— ★ 必须与 src/transport.h 的 DCL_CAP_H723_IMPL 逐位一致。
# (A4 事故: 阶段 3.2 落地了热重载却忘了改 transport.h, 工具也就跟着"期望"了错的数。
#  两处都是手工同步的, 所以这里写清楚来源, 改一处必须改另一处。)
EXPECT_FW = 0x0200
EXPECT_CAP = (0x0001    # MULTICYCLE
              | 0x0002  # HOTRELOAD  (阶段 3.2 落地)
              | 0x0004  # PERSISTENT (W2.4 落地)
              | 0x0010  # WIRE2_FLAG (A3 修复后 ISR 真的按标志办事)
              | 0x0020  # VERINFO
              | 0x0040  # SEQ        (W3 落地 — 2026-09-11)
              | 0x0080  # FORCE      (W2 落地)
              | 0x0100  # COMM       (W4 落地 — Modbus RTU 从站, 2026-09-11)
              | 0x0800  # MACRO      (W5 落地 — 字节码 VM 0x40/0x41/0x42, 2026-09-11)
              | 0x0400) # AI         (W5 落地 — ADC1 16bit + AI 3 通道 SENSOR[8..10])
#         未声明: STATE_COLD(0x0008) / HMI(0x0200) —— 见 transport.h 的两条清单。
#         (DI/HIL 与 S3 一样无专用能力位; 其存在经 0x36 自检 + SENSOR 观测证明)
# ★★ 同族事故复现记录 (2026-09-11):
#   这个常量在 W2/W2.4/W3 三次落地中**都没有被同步** —— 直到 W3 收口时顺手核对
#   才发现它停在 0x0033 (只有阶段 3 的位)。也就是说: 即便 PC 侧接线后跑这个工具,
#   它也会因为"期望 0x0033, 实际 0x00F7"而报 FAIL —— 而**那不是固件错**。
#   这正是 transport.h 里 A4 事故的同族: "两处手工同步, 改一处必忘另一处"。
#   ⇒ 可靠做法: 只保留**一处**真值源。这里改为从 ELF 里读不到 (cap 是立即数),
#     所以退而求其次 —— 在工具里写明"必须与 src/transport.h 的
#     DCL_CAP_H723_IMPL 逐位一致", 并在收口检查表里列上"改能力位 → 同步此文件"。


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
    try:
        # M4 sentinel: un-halt the core if a pyocd session left it halted
        from h723_w1 import revive_if_dead as _rv
        _rv(port)
    except Exception:
        pass

    ser = serial.Serial(port, a.baud, timeout=0.05)
    time.sleep(0.2)
    link = Link(ser, verbose=a.raw)
    results = []          # (name, state, detail)  state ∈ {PASS, FAIL, SKIP}

    def check(name, cond, detail="", skip=False):
        state = "SKIP" if skip else ("PASS" if cond else "FAIL")
        results.append((name, state, detail))
        print("  [%s] %-38s %s" % (state, name, detail))
        return state == "PASS"

    def probe(timeout=None):
        """阳性对照: 发一条合法 GET_VERSION, 返回 (sts, payload) 或 None"""
        link.send(build_frame(CMD_GET_VERSION))
        return link.recv_frame(timeout or a.timeout)

    # 先排掉上电横幅 (可能已经在缓冲里)
    ser.reset_input_buffer()

    # ══ T0 链路活性 —— 后续所有「无响应」类判据的前提 ══
    r = probe()
    if r is None:
        check("T0 链路活性 (GET_VERSION→ACK)", False,
              "超时。检查: ① H1 的 6/5 脚是否对调 ② GND 是否共地 ③ 固件是否在跑")
    else:
        check("T0 链路活性 (GET_VERSION→ACK)", r[0] == STS_ACK,
              "sts=%s payload=%s" % (r[0] if isinstance(r[0], int) else r[0],
                                     hexdump(r[1]) if isinstance(r[1], bytes) else r[1]))

    if not results[-1][1] == "PASS":
        # ★ 链路不活: 后面全部 SKIP —— 绝不让「无响应」在链路死时变成 PASS
        for nm in ["T1.1 状态为 ACK", "T1.2 载荷 4 字节",
                   "T1.3 fw=0x%04X" % EXPECT_FW, "T1.4 cap=0x%04X (宣称=实现)" % EXPECT_CAP,
                   "T2 未知命令 → NAK 'bad cmd'",
                   "T3 CRC 错 → 无响应", "T3b 阳性对照: 之后仍能响应",
                   "T4 长度超限 → 无响应", "T4b 阳性对照: 之后仍能响应",
                   "T5 分片慢发 → 仍解析成功", "T6 连发两帧 → 收到两帧"]:
            check(nm, False, "链路不活, 未测", skip=True)
        ser.close()
        print("\n结果: 链路不活 —— 全部用例记 SKIP (不是 PASS)。")
        return 2

    # ── T1 版本/能力协商 (复用 T0 已读到的响应) ──
    if r is not None:
        sts, pl = r
        fw = pl[0] | (pl[1] << 8) if len(pl) >= 4 else None
        cap = pl[2] | (pl[3] << 8) if len(pl) >= 4 else None
        check("T1.1 状态为 ACK", sts == STS_ACK, "sts=0x%02X" % (sts if isinstance(sts, int) else 0))
        check("T1.2 载荷 4 字节", len(pl) == 4, "len=%d" % len(pl))
        check("T1.3 fw=0x%04X" % EXPECT_FW, fw == EXPECT_FW, "实际 0x%04X" % (fw or 0))
        check("T1.4 cap=0x%04X (宣称=实现)" % EXPECT_CAP, cap == EXPECT_CAP,
              "实际 0x%04X" % (cap or 0))
        if cap is not None:
            print("        声明的能力: %s" % (", ".join(
                n for bit, n in CAP_NAMES if cap & bit) or "(无)"))
            print("        未声明:     %s" % ", ".join(
                n for bit, n in CAP_NAMES if not cap & bit))

    # ── T2 未知命令必须被显式拒绝 ──
    link.send(build_frame(0x99))
    r = link.recv_frame(a.timeout)
    ok = r is not None and r[0] == STS_NAK and r[1] == b"bad cmd"
    check("T2 未知命令 → NAK 'bad cmd'", ok,
          "sts=%s payload=%r" % (r[0] if r else None, r[1] if r else None))

    # ── T3 CRC 错必须被丢弃 (无响应) + 阳性对照 ──
    link.send(build_frame(CMD_GET_VERSION, corrupt_crc=True))
    r = link.recv_frame(a.timeout)
    check("T3 CRC 错 → 无响应", r is None,
          "响应=%r (CRC 校验没生效!)" % (r,))
    r2 = probe()
    check("T3b 阳性对照: 之后仍能响应", r2 is not None and r2[0] == STS_ACK,
          "sts=%s  ← 证明 T3 的沉默是「故意丢弃」而非链路死了"
          % (r2[0] if r2 else None))

    # ── T4 长度超上限必须被丢弃 + 阳性对照 ──
    link.send(build_frame(CMD_GET_VERSION, b"\x00" * 8, force_len=0x1FFF))
    r = link.recv_frame(a.timeout)
    check("T4 长度超限 → 无响应", r is None, "响应=%r" % (r,))
    r2 = probe()
    check("T4b 阳性对照: 之后仍能响应", r2 is not None and r2[0] == STS_ACK,
          "sts=%s" % (r2[0] if r2 else None))

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

    npass = sum(1 for _, s, _ in results if s == "PASS")
    nfail = sum(1 for _, s, _ in results if s == "FAIL")
    nskip = sum(1 for _, s, _ in results if s == "SKIP")
    print("\n结果: %d PASS / %d FAIL / %d SKIP  (共 %d 项)%s"
          % (npass, nfail, nskip, len(results),
             "  本地 CRC 校验失败 %d 次" % link.bad if link.bad else ""))
    return 0 if (nfail == 0 and nskip == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
