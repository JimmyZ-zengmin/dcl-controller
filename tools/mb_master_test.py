#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mb_master_test.py — PC 侧当 Modbus RTU 主站, 端到端测 H723 的 RS-485 通信。

链路: PC --USB--> [USB转485] --A/B 双绞--> [485转TTL] --TTL--> 板子 PA2(TX)/PA3(RX) + GND
板子: USART2, 115200 8N1, 从站地址 1, 轮询收发 (无 DE 控制脚 ⇒ 必须用**自动收发**模块)

★★ 两个必须先知道的事:
  ① **地址是"字面 40001 制"**: 固件直接判 `start >= 40001`
     (modbus.c 的 0x03/0x06/0x10 三处都是 `start - 40001`)。
     所以第一路寄存器发的是 **0x9C41**, 不是 0x0000。用别的上位机工具时务必注意。
  ② 固件默认 `tx_uart = 0`(响应只留内部缓冲), 要真从 PA2 发出来必须切 `tx_uart = 1`:
         python tools/inject_cmd.py 62 00 01
     (`src` 默认 0 = 走物理口, 这个不用改。)

寄存器映射 (modbus.h / modbus.c):
  40001-40064  读区 (只读): wire[0..63] 工程量 ×100 取整
  40065-40128  写区 (可读写): 上位机设定值 → DSL 的 SRC_HMI 源
  写读区 → 异常 02 (唯一写者语义)

判据设计 (每条都能失败):
  T0  链路活性   : 一条合法读请求**必须**有合法响应 —— 先证链路活, 再谈其它
  A   读 qty=10  : 功能码/字节数/CRC 全对
  B   写→回读    : 06 写 40065 后回读同值 (写真的落进去了)
  C   写只读区   : **必须**回异常 02 (不回 = "唯一写者语义"没了)
  D   非法 qty=0 : **必须**回异常 03
  E   越界地址   : 40000 → **必须**回异常 02
  F   坏 CRC     : **必须无响应** (T0 已证链路活, 所以"无响应"才有意义)
  G   错站号     : **必须无响应**

用法:
    python mb_master_test.py --port COM7
    python mb_master_test.py --port COM7 --counters   # 附带读固件侧通信计数 (需 pyocd)
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import serial
except ImportError:
    serial = None


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc


def frame(addr, pdu):
    body = bytes([addr]) + bytes(pdu)
    c = crc16(body)
    return body + bytes([c & 0xFF, (c >> 8) & 0xFF])


def scan_response(buf, addr):
    """在收到的字节流里找一条**CRC 合法**的响应 (容忍回显/半双工自收)。返回 (frame, pos)。"""
    for i in range(len(buf)):
        if buf[i] != addr or i + 4 > len(buf):
            continue
        f = buf[i + 1]
        if f & 0x80:
            n = 5
        elif f == 0x03:
            if i + 3 > len(buf):
                continue
            n = 3 + buf[i + 2] + 2
        elif f in (0x06, 0x10):
            n = 8
        else:
            continue
        if i + n > len(buf):
            continue
        body = bytes(buf[i:i + n - 2])
        c = crc16(body)
        if bytes(buf[i + n - 2:i + n]) == bytes([c & 0xFF, (c >> 8) & 0xFF]):
            return buf[i:i + n], i
    return None, -1


class Master:
    def __init__(self, port, baud, addr, timeout=0.15, gap=0.03):
        self.s = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=timeout)
        self.addr = addr
        self.gap = gap
        self.tx_n = 0

    def xfer(self, pdu, addr=None, crc_bad=False):
        """发一条请求, 收回所有字节。返回 (req, raw, resp_or_None)"""
        a = self.addr if addr is None else addr
        req = bytearray(frame(a, pdu))
        if crc_bad:
            req[-1] ^= 0xFF
        self.s.reset_input_buffer()
        self.s.write(bytes(req))
        self.s.flush()
        self.tx_n += 1
        deadline = time.time() + 0.25
        buf = bytearray()
        idle = time.time()
        while time.time() < deadline:
            n = self.s.in_waiting
            if n:
                buf += self.s.read(n)
                idle = time.time()
            else:
                if buf and (time.time() - idle) > 0.02:
                    break                     # 收了东西且静了 20ms ⇒ 帧结束
                time.sleep(0.002)
        time.sleep(self.gap)                  # 帧间静默 ≥ 3.5 字符 (固件用 4 拍=400us)
        rsp, _ = scan_response(buf, a)
        return bytes(req), bytes(buf), rsp


def show(tag, req, raw, rsp):
    print("  %-22s TX %s" % (tag, req.hex(" ")))
    if raw:
        print("  %-22s RX %s" % ("", raw.hex(" ")))
    else:
        print("  %-22s RX (无字节)" % "")


def main():
    argv = sys.argv[1:]
    if serial is None:
        print("[X] 缺 pyserial (用装了 pyocd 的系统 Python 跑)")
        return 2
    port = argv[argv.index("--port") + 1] if "--port" in argv else None
    baud = int(argv[argv.index("--baud") + 1]) if "--baud" in argv else 115200
    addr = int(argv[argv.index("--addr") + 1]) if "--addr" in argv else 1
    want_cnt = "--counters" in argv
    if not port:
        print(__doc__)
        return 2

    m = Master(port, baud, addr)
    fails = []
    print("=== 485 通信端到端测试: %s @%d 8N1, 从站 %d ===" % (port, baud, addr))
    print("地址按**字面 40001 制** (0x9C41 起), 见文件头说明\n")

    # ---- T0 链路活性: 一条合法读请求必须有合法响应 ----
    req, raw, rsp = m.xfer([0x03, 0x9C, 0x41, 0x00, 0x01])
    show("T0 读 40001 (qty=1)", req, raw, rsp)
    if rsp is None:
        print("  ==> [X] 链路没活: 没有任何合法响应。**先别急着判固件** ——")
        print("      按这个顺序查: ① 共地了吗 ② A/B 对调试 ③ TX/RX 交叉是否接对")
        print("      ④ 485 模块是自动收发吗 ⑤ 板子上 PA2/PA3 接对了吗")
        print("      ⑥ 固件 tx_uart 切换了吗: python tools/inject_cmd.py 62 00 01")
        return 1
    print("  ==> T0 OK (链路活)\n")

    # ---- A 读 10 路 (读区 40001..40010) ----
    req, raw, rsp = m.xfer([0x03, 0x9C, 0x41, 0x00, 0x0A])
    show("A 读 40001..40010", req, raw, rsp)
    ok = rsp and rsp[1] == 0x03 and rsp[2] == 20
    if ok:
        vals = [int.from_bytes(rsp[3 + 2 * i:5 + 2 * i], "big") for i in range(10)]
        print("  ==> A OK  值 = %s  (wire[0..9]×100)" % vals)
    else:
        fails.append("A")
        print("  ==> A FAIL")

    # ---- B 写 40065 再回读 ----
    req, raw, rsp = m.xfer([0x06, 0x9C, 0x80, 0x12, 0x34])
    show("B1 写 40065=0x1234", req, raw, rsp)
    ok_w = rsp and rsp[1] == 0x06 and rsp[2:6] == bytes([0x9C, 0x80, 0x12, 0x34])
    req, raw, rsp = m.xfer([0x03, 0x9C, 0x80, 0x00, 0x01])
    show("B2 回读 40065", req, raw, rsp)
    got = int.from_bytes(rsp[3:5], "big") if (rsp and rsp[1] == 0x03 and len(rsp) >= 7) else None
    if ok_w and got == 0x1234:
        print("  ==> B OK  写进去并读回来了 (40065 = 0x1234)")
    else:
        fails.append("B")
        print("  ==> B FAIL  回读 = %s" % (hex(got) if got is not None else None))

    # ---- C 写只读区必须异常 02 ----
    req, raw, rsp = m.xfer([0x06, 0x9C, 0x41, 0x00, 0x01])
    show("C 写只读 40001", req, raw, rsp)
    if rsp and rsp[1] == 0x83 and rsp[2] == 0x02:
        print("  ==> C OK  异常 02 (唯一写者语义在)")
    else:
        fails.append("C")
        print("  ==> C FAIL  期望异常 02, 实际 %s" % (rsp.hex(" ") if rsp else "无响应"))

    # ---- D qty=0 必须异常 03 ----
    req, raw, rsp = m.xfer([0x03, 0x9C, 0x41, 0x00, 0x00])
    show("D qty=0", req, raw, rsp)
    if rsp and rsp[1] == 0x83 and rsp[2] == 0x03:
        print("  ==> D OK  异常 03")
    else:
        fails.append("D")
        print("  ==> D FAIL  期望异常 03, 实际 %s" % (rsp.hex(" ") if rsp else "无响应"))

    # ---- E 越界地址 40000 必须异常 02 ----
    req, raw, rsp = m.xfer([0x03, 0x9C, 0x40, 0x00, 0x01])
    show("E 读 40000 (越界)", req, raw, rsp)
    if rsp and rsp[1] == 0x83 and rsp[2] == 0x02:
        print("  ==> E OK  异常 02")
    else:
        fails.append("E")
        print("  ==> E FAIL  期望异常 02, 实际 %s" % (rsp.hex(" ") if rsp else "无响应"))

    # ---- F 坏 CRC 必须无响应 ----
    req, raw, rsp = m.xfer([0x03, 0x9C, 0x41, 0x00, 0x01], crc_bad=True)
    show("F 坏 CRC", req, raw, rsp)
    if rsp is None:
        print("  ==> F OK  正确丢弃 (T0 已证链路活 ⇒ 这个'无响应'有意义)")
    else:
        fails.append("F")
        print("  ==> F FAIL  坏帧竟然被响应了")

    # ---- G 错站号必须无响应 ----
    req, raw, rsp = m.xfer([0x03, 0x9C, 0x41, 0x00, 0x01], addr=(addr % 247) + 1)
    show("G 错站号", req, raw, rsp)
    if rsp is None:
        print("  ==> G OK  非本站不响应")
    else:
        fails.append("G")
        print("  ==> G FAIL  别的站号也响应了")

    if want_cnt:
        print("\n=== 固件侧通信计数 (对端视角: 帧真的走过物理口吗) ===")
        try:
            from pyocd.core.helpers import ConnectHelper
            import re
            mp = {}
            for ln in open("build/dcl_h723.map", encoding="utf-8", errors="replace"):
                mm = re.match(r"\s+0x([0-9a-fA-F]+)\s+(g_shm_addr|g_mb_ticks)\s*$", ln)
                if mm:
                    mp[mm.group(2)] = int(mm.group(1), 16)
            sess = ConnectHelper.session_with_chosen_probe(
                target_override="stm32h723xx", options={"connect_mode": "halt"})
            sess.open()
            try:
                t = sess.target
                shm = t.read32(mp["g_shm_addr"])
                base = shm + 0x4B20            # OFF_MB_CTRL
                cb = bytes(t.read_memory_block8(base, 40))
                print("  MbCtrl: state=%d slave=%d rx_len=%d tx_len=%d enabled=%d "
                      "budget=%d src=%d tx_uart=%d"
                      % (cb[0], cb[1], cb[2], cb[4], cb[7], cb[8], cb[25], cb[26]))
                print("  frames_rx=%d frames_tx=%d err_crc=%d err_exc=%d"
                      % (int.from_bytes(cb[9:13], "little"), int.from_bytes(cb[13:17], "little"),
                         int.from_bytes(cb[17:21], "little"), int.from_bytes(cb[21:25], "little")))
            finally:
                try:
                    sess.target.resume()
                except Exception:
                    pass
                sess.close()
        except Exception as e:
            print("  [!] 读不到固件计数 (需要 pyocd + 系统 Python): %s" % e)

    print("\n=== 小结: TX %d 帧, %s ===" % (m.tx_n, "全部 PASS" if not fails else "FAIL: %s" % fails))
    m.s.close()
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
