#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_modbus.py — W4 通信域 Modbus RTU 从站验收

★ 两段式验证 (对应硬件的两半):
   【隧道段】协议栈验证 —— 0x60 注入原始 RTU 帧, 状态机跑完整协议栈
              (CRC 校验/功能码分发/响应组装/异常码), 只是"字节来源"不同。
              **零物理层依赖** ⇒ 硬件没齐也能把协议栈验完。
   【物理段】波形验证 —— 0x62 [1][1] 让响应从 PA2 (USART2_TX) 真发,
              用 LA 抓 TTL 侧波形 + Saleae 的 Modbus RTU 解码器。
              需要 LA 接线; 不需要 PC 侧的 USB 转 485。

★ 判据设计 (每条都能失败):
   · 响应帧的 **CRC 由本脚本独立复算** —— 不是"固件自己说对"。
     这是"对端视角"纪律: 外部实现一个校验器, 才叫真的验了。
   · 异常路径与正常路径**都测** —— 只测正常路径无法区分"严格实现"与"全放行"。
   · 坏 CRC / 非本站地址 → **必须无响应** (静默丢弃), 并核对 tx_len=0。
   · 写读闭环: 写 40065 → 读回, 排除"读的是常量"这种假通过。

用法:
    python tools/h723_modbus.py                 # 隧道段全套 (默认)
    python tools/h723_modbus.py --la            # 追加物理段 (需 LA 接 PA2)
    python tools/h723_modbus.py --port COM14
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
import struct
import sys
import time

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    print("!! 需要 pyserial: pip install pyserial")
    sys.exit(2)

CMD_MB_INJECT = 0x60
CMD_MB_RESP   = 0x61
CMD_MB_CFG    = 0x62
CMD_RESET     = 0x13
CMD_STOP      = 0x12

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  [%s] %-60s %s" % ("PASS" if ok else "FAIL", name, detail))


# ══════════ Modbus RTU 帧工具 (本脚本独立实现, 用于"对端视角"核对) ══════════

def mb_crc16(buf: bytes) -> int:
    """Modbus CRC16 (多项式 0xA001, 初值 0xFFFF) — 独立实现, 不复用固件参数"""
    crc = 0xFFFF
    for b in buf:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc


def mb_frame(addr: int, func: int, body: bytes) -> bytes:
    p = bytes([addr, func]) + body
    c = mb_crc16(p)
    return p + bytes([c & 0xFF, (c >> 8) & 0xFF])


def mb_check(resp: bytes) -> bool:
    """核对响应的 CRC —— **必须**由本脚本复算, 不能只看固件回了什么"""
    if len(resp) < 4:
        return False
    got = resp[-2] | (resp[-1] << 8)
    return mb_crc16(resp[:-2]) == got


def find_port(explicit):
    if explicit:
        return explicit
    for p in list_ports.comports():
        d = (p.description or "") + (p.hwid or "")
        if "CH340" in d or "1A86" in d:
            return p.device
    ports = list(list_ports.comports())
    return ports[0].device if ports else None


def open_serial(port, baud=115200, timeout=0.5):
    """打开串口, 并**立刻把 DTR/RTS 释放到非有效态**。

    ★★★ 为什么必须有这一句 (2026-09-11 真实事故, 代价 = 一整轮开发停摆):
      如果 CH340 的 `RTS` 被接到了板子的 `NRST` (想用 PC 复位板子), 那么**任何工具
      只要一打开串口**, pyserial/驱动就会 assert RTS ⇒ **NRST 被按住** ⇒
        · 串口 0 字节 (板子在复位里)
        · 连 SWD 的非复位 attach 也失败 (`SWD/JTAG communication failure (WAIT ACK)`)
      两个接口**同时**失效 ⇒ 症状看起来像"固件挂了/探针坏了", 排查方向被完全带偏。
      实测: 拆掉那根线后 SWD 立刻恢复 5/5, 串口恢复 ACK。
    ⇒ 本函数把"释放复位"变成结构事实, 而不是靠使用者记得。即使没接那根线也无害
      (CH340 的 RTS 空闲态本来就是非有效)。

    ★ 反过来说: 要做"PC 复位板子"(S3 套件 T15 的重启半段), 直连 RTS→NRST 是**错的**
      接法 —— 正确做法是 **RTS ——100nF—— NRST**(交流耦合: 只有电平跳变产生一个短脉冲,
      稳态按不住板子), 并且要确认 RTS 输出是 3.3V 而不是 5V。
    """
    s = serial.Serial(port, baud, timeout=timeout)
    try:
        s.setDTR(False)
        s.setRTS(False)
    except Exception:
        pass
    return s


class Link:
    """最小协议客户端 (与 h723_w1.py 同款; 该文件修过一个 bytes(int) 类型 bug,
    这里的 sts 保持为 **int** —— 见 h723_w1.py:128 的审计记录)"""

    def __init__(self, ser, verbose=False):
        self.ser, self.verbose = ser, verbose

    def xact(self, cmd, payload=b"", timeout=0.6):
        from h723_w1 import build_frame, crc16, hexdump
        frame = build_frame(cmd, payload)
        if self.verbose:
            print("      TX: %s" % hexdump(frame))
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        self.ser.flush()
        buf = bytearray()
        end = time.time() + timeout
        while time.time() < end:
            ch = self.ser.read(1)
            if not ch:
                continue
            buf += ch
            if buf[0] != 0xC1:
                del buf[0]
                continue
            if len(buf) < 4:
                continue
            plen = buf[2] | (buf[3] << 8)
            need = 4 + plen + 2
            if len(buf) < need:
                if plen > 4096:
                    del buf[0]
                continue
            body = bytes(buf[1:need - 2])
            crc_rx = buf[need - 2] | (buf[need - 1] << 8)
            if crc16(body) == crc_rx:
                return buf[1], bytes(buf[4:need - 2])
            del buf[0]
        return None, b""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--la", action="store_true", help="追加物理段 (需 LA 接 PA2)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

    port = find_port(a.port)
    try:
        # M4 sentinel: un-halt the core if a pyocd session left it halted
        from h723_w1 import revive_if_dead as _rv
        _rv(port)
    except Exception:
        pass

    if not port:
        print("!! 找不到串口")
        return 2
    print("端口: %s @ 115200\n" % port)

    with serial.Serial(port, 115200, timeout=0.05) as ser:
        L = Link(ser, a.verbose)
        time.sleep(0.3)

        # ── T0: 链路 + 工具自检 (审计建议的闸门; 防"工具坏了但判据照跑") ──
        print("── T0 前置 ──")
        sts, p = L.xact(0x01)
        record("T0 链路活性 (GET_VERSION→ACK)", sts == 0 and len(p) >= 4, "sts=%s" % sts)
        if sts != 0:
            print("  !! 链路不活, 后续无法判定"); return 2
        s_bad, _ = L.xact(0x7F)
        record("T0c 工具自检: 未实现命令(0x7F)→NAK (证明 sts 判据能失败)",
               s_bad == 0xFF, "sts=%s" % s_bad)
        if s_bad != 0xFF:
            print("  !! sts 判据不可信, 提前终止"); return 2

        L.xact(CMD_RESET)
        time.sleep(0.2)

        def inject(fr, wait=0.05):
            s, _ = L.xact(CMD_MB_INJECT, fr)
            if s != 0:
                return None, None, s
            time.sleep(wait)               # 状态机推进 (约 8 拍)
            s2, r = L.xact(CMD_MB_RESP)
            if s2 != 0 or len(r) < 2:
                return None, None, s2
            tl = r[1]
            return r[0], (bytes(r[2:2 + tl]) if tl else b""), 0

        # ══════════ 隧道段: 协议栈 ══════════
        print("\n── 隧道段: 协议栈 (0x60 注入 → 0x61 读回, 零物理层) ──")

        # ① 异常: 未支持功能码
        st, resp, rc = inject(mb_frame(1, 0x42, b"\x00\x00\x00\x00"))
        record("① 异常路径: 未支持功能码 0x42 → [01][C2][01]",
               resp is not None and len(resp) == 5 and resp[1] == 0xC2 and resp[2] == 0x01
               and mb_check(resp), "resp=%s" % (resp.hex() if resp else None))

        # ② 写单寄存器 40065 → 回显 (含 CRC 独立核对)
        st, resp, rc = inject(mb_frame(1, 0x06, struct.pack(">HH", 40065, 0x1234)))
        record("② 写路径: func06 写 40065=0x1234 → 回显且 CRC 正确",
               resp is not None and len(resp) == 8 and resp[4:6] == b"\x12\x34"
               and mb_check(resp), "resp=%s" % (resp.hex() if resp else None))

        # ③ 写读闭环: 读回刚写的值 (排除"读的是常量")
        st, resp, rc = inject(mb_frame(1, 0x03, struct.pack(">HH", 40065, 1)))
        record("③ 读路径: func03 读 40065 → 数据 0x1234 (写读闭环)",
               resp is not None and len(resp) == 7 and resp[3:5] == b"\x12\x34"
               and mb_check(resp), "resp=%s" % (resp.hex() if resp else None))

        # ④ 唯一写者: 写只读区 (40001-40064) → 异常 02
        st, resp, rc = inject(mb_frame(1, 0x06, struct.pack(">HH", 40001, 0x0001)))
        record("④ 唯一写者: func06 写只读区 40001 → 异常 02",
               resp is not None and len(resp) == 5 and resp[1] == 0x86 and resp[2] == 0x02
               and mb_check(resp), "resp=%s" % (resp.hex() if resp else None))

        # ⑤ 写多寄存器 0x10
        body = struct.pack(">HHB", 40065, 2, 4) + struct.pack(">HH", 0xAAAA, 0xBBBB)
        st, resp, rc = inject(mb_frame(1, 0x10, body))
        record("⑤ 写多: func10 写 40065..66 → 回显 6 字节",
               resp is not None and len(resp) == 8 and resp[1] == 0x10 and mb_check(resp),
               "resp=%s" % (resp.hex() if resp else None))
        st, resp, rc = inject(mb_frame(1, 0x03, struct.pack(">HH", 40065, 2)))
        record("⑤b 写多后读回 (func10 真的写进去了)",
               resp is not None and len(resp) == 9 and resp[3:7] == b"\xaa\xaa\xbb\xbb"
               and mb_check(resp), "resp=%s" % (resp.hex() if resp else None))

        # ⑥ 坏 CRC → 静默丢弃
        bad = bytearray(mb_frame(1, 0x03, struct.pack(">HH", 40065, 1)))
        bad[-1] ^= 0xFF
        st, resp, rc = inject(bytes(bad))
        record("⑥ 坏 CRC → 无响应且 tx_len=0 (静默丢弃)",
               resp is not None and len(resp) == 0, "state=%s tx_len=%d" % (st, len(resp or b"")))

        # ⑦ 非本站地址 → 静默
        st, resp, rc = inject(mb_frame(9, 0x03, struct.pack(">HH", 40065, 1)))
        record("⑦ 非本站地址(9) → 无响应", resp is not None and len(resp) == 0, "state=%s" % st)

        # ⑧ 越界地址 → 异常 02
        st, resp, rc = inject(mb_frame(1, 0x03, struct.pack(">HH", 40200, 1)))
        record("⑧ 越界地址 (40200) → 异常 02",
               resp is not None and len(resp) == 5 and resp[1] == 0x83 and resp[2] == 0x02
               and mb_check(resp), "resp=%s" % (resp.hex() if resp else None))

        # ⑨ 非法数量 → 异常 03
        st, resp, rc = inject(mb_frame(1, 0x03, struct.pack(">HH", 40001, 200)))
        record("⑨ 数量越界 (200 > 125) → 异常 03",
               resp is not None and len(resp) == 5 and resp[1] == 0x83 and resp[2] == 0x03
               and mb_check(resp), "resp=%s" % (resp.hex() if resp else None))

        # ⑨b ★ M1 回归 (外部审计 W4 的 P1): **合法大 qty 区** (62..125)
        #   旧缺陷: BUILD 的组装界限用了 MB_MAX_FRAME(128, 请求帧上限) —— 而这里组装的是
        #   响应帧, 长度 = 3+2*qty+2 由 qty 决定, 与请求长度无关。qty≥62 时 total>128,
        #   b_pos 永远到不了 total ⇒ 状态死在 BUILD ⇒ 之后所有注入 NAK "mb: busy"
        #   (通信域永久不可用, 只能 RESET/断电)。
        #   ★ 判据可失败: 旧代码下 inject 直接 NAK busy → ok=False, 本项必 FAIL。
        #   ★ 官方 ⑨ 只测了"超上限 (200>125)", 漏掉了这段**合法区** —— 那正是 M1 的盲区。
        m1_ok, m1_det = True, []
        for q in (62, 63, 100, 125):
            st, resp, rc = inject(mb_frame(1, 0x03, struct.pack(">HH", 40001, q)), wait=0.30)
            wl = 3 + q * 2 + 2
            ok = (resp is not None and len(resp) == wl
                  and resp[1] == 0x03 and resp[2] == q * 2 and mb_check(resp))
            m1_ok = m1_ok and ok
            m1_det.append("q%d=%s(len=%s)" % (
                q, "OK" if ok else "FAIL", len(resp) if resp is not None else "NAK"))
        record("⑨b ★M1: 合法大 qty 62/63/100/125 → 完整响应 + CRC 独立复核",
               m1_ok, " ".join(m1_det))

        # ⑨c ★ M1 的"未卡死"判据: 大 qty 之后通信域必须仍能正常应答一帧。
        #   没有这一条, ⑨b 可能被"状态残留恰好返回"骗过 —— 必须证明域是活的。
        st, resp, rc = inject(mb_frame(1, 0x03, struct.pack(">HH", 40065, 1)))
        record("⑨c ★M1: 大 qty 后通信域仍活 (再注入一帧正常响应)",
               resp is not None and len(resp) == 7 and mb_check(resp),
               "resp=%s" % (resp.hex() if resp else None))

        # ⑩ 统计对账 (frames_rx 应 = 注入次数, 且 err_crc/err_exc 与用例相符)
        st, r = L.xact(CMD_MB_RESP)
        if st == 0 and len(r) >= 2:
            tl = r[1]
            frx, ftx, ecrc, eexc = struct.unpack("<IIII", r[2 + tl:2 + tl + 16])
            record("⑩ 统计对账: frames_rx=15 (10 基础 + ⑨b×4 + ⑨c×1), err_crc≥1, err_exc≥4",
                   frx == 15 and ecrc >= 1 and eexc >= 4,
                   "rx=%d tx=%d ecrc=%d eexc=%d" % (frx, ftx, ecrc, eexc))
        else:
            record("⑩ 统计对账", False, "0x61 读取失败")

        # ══════════ 物理段 (可选) ══════════
        if a.la:
            print("\n── 物理段: 响应从 PA2 真发 (LA 抓 TTL 侧波形) ──")
            s, r = L.xact(CMD_MB_CFG, bytes([1, 1]))     # src=隧道, tx_uart=物理口
            record("⑪ 0x62 [1][1] 切到物理口发送", s == 0 and len(r) >= 3 and r[1] == 1,
                   "cfg=%s" % (r.hex() if r else None))
            s, _ = L.xact(CMD_MB_INJECT, mb_frame(1, 0x03, struct.pack(">HH", 40065, 2)))
            record("⑫ 注入后响应应经 PA2 发出 (需 LA 抓波形核对)", s == 0,
                   "★ 请用 LA 抓 PA2; 期望帧 = %s"
                   % mb_frame(1, 0x03, struct.pack(">HH", 40065, 2)).hex())
            L.xact(CMD_MB_CFG, bytes([1, 0]))            # 恢复缓冲模式

        L.xact(CMD_RESET)
        time.sleep(0.1)

    print("\n" + "=" * 74)
    npass = sum(1 for _, ok, _ in RESULTS if ok)
    nfail = len(RESULTS) - npass
    print("W4 Modbus 结果: %d PASS / %d FAIL" % (npass, nfail))
    print("=" * 74)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
