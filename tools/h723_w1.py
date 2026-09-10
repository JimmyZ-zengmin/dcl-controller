#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_w1.py — W1 验证: 运行控制 (0x11/0x12/0x13) + SHM 读写 (0x20-0x23)

★ 设计原则 (同 h723_proto.py): **每个判据都必须能失败**。
  特别地:
    · "读回值 == 写入值" 无法证明写真的落到那个地址 —— 因为读可能是缓存/常量。
      所以 R2 用"写 A → 读 A → 写 B → 读 A 必须变" 三步, 排除"永远回同一个值"。
    · "STOP 后引擎停了" 无法只靠一次 0x38 判断 —— 那可能只是统计没更新。
      所以 S2 在 STOP 前后各读两次 routes_total, 要求**增量归零** (而不是值变小)。
    · "NaN 被拒" 必须带阳性对照 (合法值必须能写进去), 否则"全部拒绝"也能过。

★ 地址口径: PC 侧用 **SHM 偏移** 而不是绝对地址 —— 因为 SHM 基址由链接期决定
  (DTCM 0x20000000), 硬编码会随构建漂移。先用 0x38 读回 SHM 地址, 再算绝对地址。

用法:
    python tools/h723_w1.py                  # 自动找串口
    python tools/h723_w1.py --port COM7
    python tools/h723_w1.py -v               # 打印每帧 hex
"""
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

SYNC_PC2MCU = 0xC0
SYNC_MCU2PC = 0xC1
STS_ACK = 0x00
STS_NAK = 0xFF

CMD_GET_VERSION   = 0x01
CMD_DEPLOY        = 0x10
CMD_START         = 0x11
CMD_STOP          = 0x12
CMD_RESET         = 0x13
CMD_READ          = 0x20
CMD_WRITE         = 0x21
CMD_READ_BURST    = 0x22
CMD_WRITE_BURST   = 0x23
CMD_ENGINE_STATUS = 0x38

# ---- SHM 偏移 (必须与 src/engine.h 一致) ----
OFF_CTRL_MAGIC      = 0x00
OFF_CTRL_ENGINE_RUN = 0x0D
OFF_CTRL_GPIO_MASK  = 0x34
OFF_SENSOR_MAP      = 0x0040
OFF_WIRE_MAP        = 0x0240
OFF_PARAM_TABLE     = 0x1840
OFF_ROUTE_TABLE     = 0x0840

# 拒绝原因码 (必须与 src/main.c 的 NAKRH_* 一致)
NAKRH_ADDR, NAKRH_RANGE, NAKRH_COUNT, NAKRH_SHORT, NAKRH_NONFIN, NAKRH_BUDGET = range(1, 7)

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print("  [%s] %-52s %s" % (mark, name, detail))


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def build_frame(cmd, payload=b""):
    body = bytes([cmd, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    c = crc16(body)
    return bytes([SYNC_PC2MCU]) + body + bytes([c & 0xFF, c >> 8])


def hexdump(b):
    return " ".join("%02X" % x for x in b)


class Link:
    def __init__(self, ser, verbose=False):
        self.ser = ser
        self.verbose = verbose

    def xact(self, cmd, payload=b"", timeout=0.6):
        """发一帧, 等一个响应。返回 (sts, payload) 或 (None, b"") = 超时"""
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
            if buf[0] != SYNC_MCU2PC:
                # 前导垃圾 → 丢一个字节继续找 (与 h723_proto 同口径)
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
                if self.verbose:
                    print("      RX: %s" % hexdump(bytes(buf[:need])))
                return bytes(buf[1]), bytes(buf[4:need - 2])
            del buf[0]
        if self.verbose:
            print("      RX: (timeout)")
        return None, b""

    def rd32(self, addr):
        sts, p = self.xact(CMD_READ, struct.pack("<I", addr))
        if sts != STS_ACK or len(p) < 4:
            return None
        return struct.unpack("<I", p[:4])[0]


def find_port(explicit):
    if explicit:
        return explicit
    for p in list_ports.comports():
        d = (p.description or "") + (p.hwid or "")
        if "CH340" in d or "1A86" in d:
            return p.device
    ports = list(list_ports.comports())
    return ports[0].device if ports else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    port = find_port(a.port)
    if not port:
        print("!! 找不到串口")
        return 2
    print("端口: %s @ 115200\n" % port)

    with serial.Serial(port, 115200, timeout=0.05) as ser:
        L = Link(ser, a.verbose)
        time.sleep(0.3)

        # ───────── T0: 链路活性 (以下所有判据的前提) ─────────
        print("── T0 链路活性 ──")
        sts, p = L.xact(CMD_GET_VERSION)
        if sts != STS_ACK or len(p) < 4:
            print("  [FAIL] T0 链路不活 —— 后续全部无法判定")
            return 2
        fw, cap = struct.unpack("<HH", p[:4])
        record("T0 链路活性 (GET_VERSION→ACK)", True, "fw=0x%04X cap=0x%04X" % (fw, cap))

        # 拿 SHM 基址 (0x38 尾部第 23 字节起是 shm_addr)
        sts, st = L.xact(CMD_ENGINE_STATUS)
        if sts != STS_ACK or len(st) < 31:
            record("T0b 读 SHM 基址", False, "0x38 无响应/过短")
            return 2
        shm = struct.unpack("<I", st[23:27])[0]
        record("T0b 读 SHM 基址 (0x38)", shm >= 0x20000000 and shm < 0x20040000,
               "shm=0x%08X" % shm)

        # 读 routes_total 需要的辅助: 0x38 里没有, 改用 (samples, exec) 观察活性
        def samples():
            s, b = L.xact(CMD_ENGINE_STATUS)
            if s != STS_ACK or len(b) < 4:
                return None
            return struct.unpack("<I", b[:4])[0]

        # ───────── R1/R2: SHM 读写 ─────────
        print("\n── R1/R2 READ / WRITE ──")
        magic = L.rd32(shm + OFF_CTRL_MAGIC)
        record("R1 0x20 读 SHM 首字 (MAGIC 非零)", magic is not None and magic != 0,
               "magic=0x%08X" % (magic or 0))

        # ★ 写-读-再写-再读: 只做一次"写A读A"无法排除"永远回同一个值"
        w_addr = shm + OFF_WIRE_MAP          # WIRE_MAP[0], float 区
        V1, V2 = 0x40490FDB, 0x40C90FDB      # 3.14159f, 6.28318f
        sts, _ = L.xact(CMD_WRITE, struct.pack("<II", w_addr, V1))
        r1 = L.rd32(w_addr)
        sts2, _ = L.xact(CMD_WRITE, struct.pack("<II", w_addr, V2))
        r2 = L.rd32(w_addr)
        ok = (sts == STS_ACK and r1 == V1 and sts2 == STS_ACK and r2 == V2 and V1 != V2)
        record("R2 0x21 写→读 (两次不同值, 排除常量)", ok,
               "写0x%08X→读0x%08X, 写0x%08X→读0x%08X" % (V1, r1 or 0, V2, r2 or 0))

        # 阳性对照: 合法值必须能写 (否则"全拒"也能让下面的 NaN 判据通过)
        record("R2b 阳性对照 (合法值可写)", sts == STS_ACK and r1 == V1, "")

        # 非法地址 → NAK
        sts, p = L.xact(CMD_WRITE, struct.pack("<II", 0x58024400, 0x1234))  # RCC!
        record("R3 0x21 写 RCC 被拒 (禁区)", sts == STS_NAK, "载荷=%r" % p.decode("latin1"))

        sts, p = L.xact(CMD_READ, struct.pack("<I", 0x12345678))
        record("R4 0x20 读野地址被拒", sts == STS_NAK, "载荷=%r" % p.decode("latin1"))

        # 非对齐
        sts, p = L.xact(CMD_READ, struct.pack("<I", w_addr + 1))
        record("R5 0x20 非对齐地址被拒", sts == STS_NAK, "")

        # NaN / Inf 防护
        sts, p = L.xact(CMD_WRITE, struct.pack("<II", w_addr, 0x7F800000))  # +Inf
        record("R6 0x21 写 +Inf 到 float 区被拒", sts == STS_NAK, "载荷=%r" % p.decode("latin1"))
        sts, p = L.xact(CMD_WRITE, struct.pack("<II", w_addr, 0x7FC00000))  # NaN
        record("R7 0x21 写 NaN 到 float 区被拒", sts == STS_NAK, "")

        # 非 float 区 (控制块) 允许写任意位型 —— 这是刻意的语义区别
        sts, p = L.xact(CMD_WRITE, struct.pack("<II", shm + OFF_CTRL_GPIO_MASK, 0x7FC00000))
        record("R7b 非 float 区 (控制块) 不拦 NaN", sts == STS_ACK,
               "(控制块无浮点语义, 拦了反而阻碍测寄存器)")

        # ───────── R8/R9: BURST ─────────
        print("\n── R8/R9 READ_BURST / WRITE_BURST ──")
        sts, p = L.xact(CMD_READ_BURST, struct.pack("<IH", shm + OFF_SENSOR_MAP, 64))
        record("R8 0x22 burst 读 64 字 (256B)", sts == STS_ACK and len(p) == 256,
               "收到 %d 字节" % len(p))

        sts, p = L.xact(CMD_READ_BURST, struct.pack("<IH", shm + OFF_SENSOR_MAP, 256), timeout=1.2)
        record("R8b 0x22 burst 读 256 字 (1024B, T19 等价)",
               sts == STS_ACK and len(p) == 1024, "收到 %d 字节" % len(p))

        sts, p = L.xact(CMD_READ_BURST, struct.pack("<IH", shm + OFF_SENSOR_MAP, 0))
        record("R8c 0x22 count=0 被拒", sts == STS_NAK, "")

        sts, p = L.xact(CMD_READ_BURST, struct.pack("<IH", shm + OFF_SENSOR_MAP, 300))
        record("R8d 0x22 count>256 被拒", sts == STS_NAK, "")

        # burst 越界: 从 WIRE_MAP 末尾起读 256 字 (跨出 SHM)
        sts, p = L.xact(CMD_READ_BURST, struct.pack("<IH", shm + 0x7F00, 64))
        record("R9 0x22 burst 越界被拒", sts == STS_NAK, "")

        # burst 跨 RCC 禁区 (读 [RCC-8, RCC+8)) —— 这是最危险的一种越界
        sts, p = L.xact(CMD_READ_BURST, struct.pack("<IH", 0x58024400 - 8, 4))
        record("R9b 0x22 burst 跨 RCC 禁区被拒", sts == STS_NAK, "")

        # WRITE_BURST 合法 256 字
        data = b"".join(struct.pack("<I", 0x3F000000 + i) for i in range(256))  # 有限浮点
        sts, p = L.xact(CMD_WRITE_BURST,
                        struct.pack("<IH", shm + OFF_SENSOR_MAP, 256) + data, timeout=1.5)
        record("R10 0x23 burst 写 256 字 (T24 等价)", sts == STS_ACK, "")

        # ★ P1b: burst 中间夹 NaN → 整体拒绝, 且**前段未被写**
        marker = 0x40A00000  # 5.0f
        L.xact(CMD_WRITE, struct.pack("<II", shm + OFF_SENSOR_MAP, marker))
        bad = bytearray(data)
        bad[4 * 3:4 * 3 + 4] = struct.pack("<I", 0x7FC00000)   # 第 4 个字是 NaN
        sts, p = L.xact(CMD_WRITE_BURST,
                        struct.pack("<IH", shm + OFF_SENSOR_MAP, 256) + bytes(bad), timeout=1.5)
        still = L.rd32(shm + OFF_SENSOR_MAP)
        record("R11 0x23 含 NaN → NAK 且前段未被写 (P1b)",
               sts == STS_NAK and still == marker,
               "sts=%s word0=0x%08X (期望 0x%08X)" % (sts, still or 0, marker))

        # ───────── S1-S4: 运行控制 ─────────
        print("\n── S1-S4 START / STOP / RESET ──")

        # RESET 先归零
        sts, _ = L.xact(CMD_RESET)
        record("S1 0x13 RESET→ACK", sts == STS_ACK, "")
        time.sleep(0.2)
        run_after_reset = L.rd32(shm + OFF_CTRL_ENGINE_RUN)
        record("S1b RESET 后 ENGINE_RUN=0", run_after_reset == 0,
               "run=%s" % run_after_reset)

        # STOP 后统计必须**停止增长**
        sts, _ = L.xact(CMD_STOP)
        ok_stop = (sts == STS_ACK)
        time.sleep(0.3)
        s1 = samples()
        time.sleep(0.4)
        s2 = samples()
        record("S2 STOP 后 samples 停止增长", ok_stop and s1 is not None and s1 == s2,
               "s1=%s s2=%s" % (s1, s2))

        # START 后必须恢复增长
        sts, _ = L.xact(CMD_START)
        time.sleep(0.3)
        s3 = samples()
        time.sleep(0.4)
        s4 = samples()
        record("S3 START 后 samples 恢复增长", sts == STS_ACK and s3 is not None
               and s4 is not None and s4 > s3, "s3=%s s4=%s" % (s3, s4))

        # OA13 幂等: 二次 START 不清零统计 (samples 必须继续增长, 不能倒退)
        sts, _ = L.xact(CMD_START)
        time.sleep(0.1)
        s5 = samples()
        record("S4 二次 START 幂等 (samples 不回退, OA13)",
               s5 is not None and s5 >= (s4 or 0), "s4=%s → s5=%s" % (s4, s5))

        # ───────── S5: 毒药表兜底 (F11) ─────────
        print("\n── S5 F11 毒药表兜底 ──")
        # 造一个超预算程序: 128 条 PID (成本 140/条 → 远超 26000 预算)
        # 然后 STOP → 直接 START, 必须被拒
        # 注意: 这里**不 deploy** (deploy 自己就会拒), 走 persist 恢复的路径太复杂,
        #       所以直接测"budget 门"是否真的在 START 上生效 —— 用 deploy 已受理的最大程序。
        # 简化做法: 先 deploy 一个合法程序, 再手工把 N_ROUTES 改大 (模拟毒药表)
        L.xact(CMD_RESET)
        time.sleep(0.1)
        sts, p = L.xact(CMD_WRITE, struct.pack("<II", shm + 0x0E,
                                               struct.pack("<H", 128)[0] | 0))
        # ↑ 只改条数不改内容 → budget 会按 128 条算
        # 先把路由表填成 PID (成本 140) —— 用 burst 写 128 条路由的首字节 op=5
        route = bytearray(128 * 16)
        for i in range(128):
            route[i * 16 + 4] = 0x05      # op = PID
            route[i * 16 + 5] = 0x01      # flags ACTIVE
            route[i * 16 + 3] = i         # dst_channel (唯一, 避免 conflict)
        # 写路由表 (128×16 = 2048B = 512 字, 分两次 burst)
        for chunk in range(2):
            base = shm + OFF_ROUTE_TABLE + chunk * 1024
            seg = bytes(route[chunk * 1024:(chunk + 1) * 1024])
            words = struct.pack("<IH", base, 256) + seg
            L.xact(CMD_WRITE_BURST, words, timeout=1.5)
        L.xact(CMD_WRITE, struct.pack("<II", shm + 0x0E, 128))   # N_ROUTES = 128
        sts, p = L.xact(CMD_START)
        record("S5 超预算表 → START 被拒 (F11 兜底)", sts == STS_NAK,
               "载荷=%r" % p.decode("latin1") if sts == STS_NAK else "居然 ACK 了!")

        # 恢复
        L.xact(CMD_RESET)
        time.sleep(0.1)
        L.xact(CMD_START)

    # ───────── 汇总 ─────────
    npass = sum(1 for _, ok, _ in RESULTS if ok)
    nfail = len(RESULTS) - npass
    print("\n" + "=" * 74)
    print("W1 结果: %d PASS / %d FAIL" % (npass, nfail))
    if nfail:
        print("失败项:")
        for name, ok, d in RESULTS:
            if not ok:
                print("  ✗ %s  %s" % (name, d))
    print("=" * 74)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
