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

SYNC_PC2MCU = 0xC0
SYNC_MCU2PC = 0xC1
STS_ACK = 0x00
STS_NAK = 0xFF

# ★ cap 期望值只有**一处**权威来源 (h723_proto) —— 固件改能力位时只改那里,
#   这里跟着变。T0 用它做"内容匹配", 避免"能收到字节就算链路活"那种空判据。
from h723_proto import EXPECT_CAP   # noqa: E402  (放在常量区, 便于与固件能力位对照)

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
OFF_CTRL_VERSION    = 0x04
OFF_CTRL_HEARTBEAT  = 0x08
OFF_CTRL_ENGINE_RUN = 0x0D
OFF_CTRL_GPIO_MASK  = 0x34
OFF_SENSOR_MAP      = 0x0040
OFF_WIRE_MAP        = 0x0240
OFF_PARAM_TABLE     = 0x1840
OFF_ROUTE_TABLE     = 0x0840

# ---- 预算模型常量 (必须与 src/engine.h 一致) ----
EXEC_DEPLOY_BUDGET  = 26000   # 拍预算门 (cyc)
OP_COST_MAX_MEASURED = 140    # 最贵原语 (PID) 实测成本

CTRL_MAGIC          = 0x44434C31   # 'DCL1' — 与固件同值

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
                # ★★ 审计发现 A (P2) 修复: 这里原本是 `bytes(buf[1])` —— buf[1] 是 **int**,
                #    而 `bytes(0x00)` = b"" (空), `bytes(0xFF)` = 255 个零字节。
                #    调用方判据是 `sts != STS_ACK` (STS_ACK = 0x00), 于是:
                #        ACK 帧  → bytes(0x00) = b""  →  b"" != 0  →  **恒真**
                #        NAK 帧  → bytes(0xFF) = 255 个 0x00 (也不是 0xFF)
                #    ⇒ **T0 链路活性判据恒 FAIL**, 工具直接 return 2, 后面 40 多项判据
                #      一行都没执行过 ⇒ "W1 六条命令已完成"这个声称**没有任何证据支撑**。
                #    ★ 危害不止于此: 失败信息是"T0 链路不活", 会把排查引向"串口/固件有问题"
                #      —— 与真因(工具自己)相反。与 BRR 事故同一个模式: 故障现象指向错误方向。
                #    ★ 修法: buf[1] 本身就是要的 int, **不要包 bytes()**。
                #    ★ 教训(审计建议, 已采纳): 验证工具自身也需要一道闸门 —— 见
                #      文件末尾 selftest_link() 的"故意断链必须失败"自检。
                return buf[1], bytes(buf[4:need - 2])
            del buf[0]
        if self.verbose:
            print("      RX: (timeout)")
        return None, b""

    def rd32(self, addr):
        sts, p = self.xact(CMD_READ, struct.pack("<I", addr))
        if sts != STS_ACK or len(p) < 4:
            return None
        return struct.unpack("<I", p[:4])[0]


def revive_if_dead(port, verbose=True):
    """M4 哨兵: 串口不响应时, 先判"核是不是被 pyocd 留在 halt", 并解卡。

    ★ 为什么需要它 (外部审计 M4 的第二半 —— 只有尾部 `go` 不够):
      pyocd 会话若以 halt 收尾, **或链条中途报错/超时以致尾部的 `go` 根本没执行到**,
      核就停在暂停; 此后所有串口工具一律"无响应"。更隐蔽的是: **用 pyocd 去查
      "为什么串口没响应"本身会让串口没响应** (观察者效应)。
      ⇒ 每个串口套件在 T0 之前先过这道哨兵, 就不会再把"核被暂停"误判成"固件挂了"。
    返回 True = 链路现在活着 (可能刚解卡); False = 仍不通 (那是真故障: 查接线/端口占用)。
    """
    import subprocess, time
    import serial

    def ping():
        try:
            with serial.Serial(port, 115200, timeout=0.4) as s:
                time.sleep(0.15); s.reset_input_buffer()
                s.write(build_frame(CMD_GET_VERSION)); s.flush()
                return bool(s.read(64))
        except Exception:
            return False

    if ping():
        return True
    if verbose:
        print("  [M4 哨兵] 串口无响应 → 先按[核被 pyocd 留 halt]处理, 尝试解卡 …")
    try:
        subprocess.run(["pyocd", "cmd", "-t", "stm32h723xx",
                        "-O", "connect_mode=under-reset",
                        "-c", "reset", "-c", "go", "-c", "sleep", "400"],
                       capture_output=True, text=True, timeout=120)
    except Exception:
        pass
    time.sleep(0.3)
    ok = ping()
    if verbose:
        print("  [M4 哨兵] %s" % ("已解卡, 链路活" if ok else
                                 "仍无响应 ⇒ 不是 halt 问题 (查接线 / 端口被占用)"))
    return ok


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

        # ───────── T0: 链路活性 (以下所有判据的前提) ─────────
        print("── T0 链路活性 ──")
        sts, p = L.xact(CMD_GET_VERSION)
        # ★★ 审计轴1 修复 (2026-09-11): 原实现是"上面 if 不通过就 return 2, 然后无条件
        #   `record(..., True, ...)`" —— 判据本身不参与判定, 形式上就是**恒真**。
        #   机械审计抓到了它, 而且抓得对: 只要将来有人把那个 guard 改成 print 而不 return,
        #   这行会继续报 PASS ⇒ 假 PASS。
        #   现在把**解析结果**当判据: ACK + 载荷够长 + **cap 含全部必备位**
        #   (必备位还给"对端就是这台固件"背书 —— 避免"别的设备在回话"被判成链路活)。
        #   ★★ 2026-09-16 改为**必备位掩码**语义 (`cap & EXPECT_CAP == EXPECT_CAP`):
        #     原来是等值。等值下固件**新增一个能力位**就会让 T0 判否 ⇒
        #     整个套件在 T0 处 return 2, 打印"T0 链路不活 —— 后续全部无法判定"
        #     —— 把一个"工具期望值没跟上固件"的问题，伪装成**链路/接线**故障
        #     (与 BRR 事故引向同一方向)。掩码下"新增能力位不破坏 T0"。
        #     ★ 分工: "宣称 = 实现"(多一位也算错) 由 h723_proto.py 的 T1.4 用**等值**审;
        #       本处只审"必备能力在不在", 两处语义不同、刻意不合并。
        fw = cap = None
        if sts == STS_ACK and len(p) >= 4:
            fw, cap = struct.unpack("<HH", p[:4])
        t0_ok = (fw is not None) and ((cap & EXPECT_CAP) == EXPECT_CAP)
        record("T0 链路活性 (0x01 → ACK + cap 必备位齐全)", t0_ok,
               ("fw=0x%04X cap=0x%04X (必备位 0x%04X)" % (fw, cap, EXPECT_CAP)) if fw is not None
               else "无有效应答帧 (sts=%s len=%d)" % (sts, len(p)))
        if not t0_ok:
            print("  [FAIL] T0 链路不活 —— 后续全部无法判定")
            return 2

        # ★★★ 工具自检闸门 (审计发现 A 的直接对策, 采纳审计建议) ★★★
        #   审计发现 A 的形态是: **工具自己的解析器坏了**, 导致 `sts != STS_ACK`
        #   这个判据**恒真**, 于是 T0 永远 FAIL、后续 40 多项判据一行都没跑过 ——
        #   而失败信息却指向"链路不活", 把人引向串口/固件 (方向完全相反)。
        #   ⇒ 按审计建议: 给工具自己也配一个阳性对照 ——
        #     发一条**未实现**的命令, 必须得到 NAK。
        #     · 若返回 ACK  ⇒ 说明"sts 判据"已经失效 (永远是 ACK 或永远是同一个值);
        #     · 若返回 NAK  ⇒ 证明 sts 通道**双向可辨**(ACK/NAK 能区分) ⇒ T0 可信。
        #   这一步同时覆盖了 A 那一族 (bytes(int) 类型 bug) 会造出的所有症状:
        #   只要 sts 的解析退化, 这里就会当场失败, 而不是等到 40 条判据白跑。
        s_bad, p_bad = L.xact(0x7F)          # 0x7F 未实现 → 固件应回 NAK "bad cmd"
        record("T0c ★工具自检: 未实现命令(0x7F)必须回 NAK (证明 sts 判据能失败)",
               s_bad == STS_NAK,
               "sts=%s (期望 0x%02X=NAK)" % (("0x%02X" % s_bad) if s_bad is not None else "None",
                                             STS_NAK))
        if s_bad != STS_NAK:
            print("\n  !! sts 判据不可信 —— 后续判据的 FAIL 不能归因到固件。提前终止。")
            return 2

        # 拿 SHM 基址 (0x38 尾部第 23 字节起是 shm_addr)
        sts, st = L.xact(CMD_ENGINE_STATUS)
        if sts != STS_ACK or len(st) < 31:
            record("T0b 读 SHM 基址", False, "0x38 无响应/过短")
            return 2
        shm = struct.unpack("<I", st[23:27])[0]
        record("T0b 读 SHM 基址 (0x38)", shm >= 0x20000000 and shm < 0x20040000,
               "shm=0x%08X" % shm)

        # ★★ 审计发现 B/S2 的核心: "引擎停了没"需要两个语义**不同**的量, 缺一不可。
        def samples():
            """**本次 RUN 段的拍数** (0x38 r[0:4] = g_isr_n → SHM 0x18 = OFF_TIMING_SAMPLES)。
            ★ 契约与 S3 范本**一字不差**: 范本把 `SAMPLES += 1` 写在 `if (!run) return`
              **之后** (`core0_isr.c:358`, OA13 "统计只反映本次 RUN 段"), 且范本 0x38
              的 samples 就取自它 (`main.c:242`) ⇒ 所以 STOP 之后它**冻结**。
            ★★ #2 修复前 H723 是**每拍都加** —— 与 0x08 的语义完全对调; 本工具原来的
              S2/S2b 判据正建立在那个反义上, 现在一并改正 (判据跟着契约走, 不是反过来)。"""
            s, b = L.xact(CMD_ENGINE_STATUS)
            if s != STS_ACK or len(b) < 4:
                return None
            return struct.unpack("<I", b[:4])[0]

        def heartbeat():
            """**每拍无条件**递增的存活计数 (SHM 0x08 = OFF_CTRL_HEARTBEAT)。
            ★ 契约与 S3 范本**一字不差**: 范本把 `HEARTBEAT += 1` 写在 `if (!run) return`
              **之前** (`core0_isr.c:355`), 注释原文 "OA14 (P2, 审计): 心跳必须在 run 门外
              无条件翻 — 停机也翻 (外部可观测 CPU+定时器存活)"。
            ★ 所以它在 STOP 之后**仍然增长** —— 它回答的是"CPU + 定时器活着吗",
              不是"引擎在推进吗"(那是 samples 的问题)。
              成对读可区分三态: 0x08↑0x18↑ 在跑 / 0x08↑0x18=停 停机态 / 0x08=停 固件死。"""
            return L.rd32(shm + OFF_CTRL_HEARTBEAT)

        # ───────── R1/R2: SHM 读写 ─────────
        print("\n── R1/R2 READ / WRITE ──")
        magic = L.rd32(shm + OFF_CTRL_MAGIC)
        ver = L.rd32(shm + OFF_CTRL_VERSION)
        # ★ 审计发现 C: 固件已修 (cold_start_reset 里写 MAGIC+VERSION)。
        #   判据同时覆盖"就绪标志"与"布局版本"两个字段 —— 后者是 W3 新加的,
        #   它回答的是"我认不认得这块 SHM 的字段语义", 与 MAGIC 的"是否就绪"互补。
        record("R1 0x20 读 SHM MAGIC == 'DCL1' (审计发现 C 修复)",
               magic == 0x44434C31, "magic=0x%08X" % (magic if magic is not None else 0))
        record("R1b 0x20 读 SHM LAYOUT_VERSION 非 0 (字段语义版本)",
               ver is not None and ver != 0, "version=0x%08X" % (ver if ver is not None else 0))

        # ★★ 审计发现 B/R2 修复: 原判据直接在引擎 RUN 态写 WIRE_MAP[0] —— 而那是
        #    **引擎每拍覆写**的地址 (BOOT_GATE=1 时 128 条路由每拍写各自 dst)。
        #    写完立刻被下一拍覆盖, 于是"写 3.14159 → 读回 0"看起来像
        #    "固件不响应写", 实际是**测试选址错了** (选了个有别的写者的地址)。
        #    ⇒ 测 SHM **写路径**必须先 STOP: 引擎停 = 无写者竞争, 此时"写进去读得回"
        #      才真的只反映 0x21 的行为。这是"一次只验证一个东西"的基本要求。
        L.xact(CMD_STOP)
        time.sleep(0.25)

        # ★ 写-读-再写-再读: 只做一次"写A读A"无法排除"永远回同一个值"
        w_addr = shm + OFF_WIRE_MAP          # WIRE_MAP[0], float 区
        V1, V2 = 0x40490FDB, 0x40C90FDB      # 3.14159f, 6.28318f
        sts, _ = L.xact(CMD_WRITE, struct.pack("<II", w_addr, V1))
        time.sleep(0.05)
        r1 = L.rd32(w_addr)
        sts2, _ = L.xact(CMD_WRITE, struct.pack("<II", w_addr, V2))
        time.sleep(0.05)
        r2 = L.rd32(w_addr)
        ok = (sts == STS_ACK and r1 == V1 and sts2 == STS_ACK and r2 == V2 and V1 != V2)
        record("R2 0x21 写→读 (STOP 态, 两次不同值, 排除常量+覆写)", ok,
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

        # ★★ 审计发现 B/R9: 原判据从 `shm+0x7F00` 读 64 字 = [0x7F00, 0x8000) ——
        #    **恰好落在 SHM_SIZE=0x8000 之内**, 根本不是越界, 固件 ACK 是正确的。
        #    这不是"判据失败", 是"判据的前提写错了" —— 它从来没测到越界这件事。
        #    ⇒ 改为读 128 字: [0x7F00, 0x8100), **跨出 SHM 末尾 0x100 字节**。
        sts, p = L.xact(CMD_READ_BURST, struct.pack("<IH", shm + 0x7F00, 128))
        record("R9 0x22 burst 越界被拒 ([0x7F00,0x8100) 跨出 SHM 末尾)",
               sts == STS_NAK, "载荷=%r" % p.decode("latin1") if sts == STS_NAK else "居然 ACK")

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
        # ★★ 审计发现 B/S1b: 原来读 `shm + OFF_CTRL_ENGINE_RUN` (= 0x0D) —— 那是 **u8**,
        #    地址 **非 4 字节对齐**。固件的地址守卫**正确地拒绝**了它 (rd32 返回 None),
        #    于是判据报 `run=None` FAIL —— 但那是**工具用了非法地址**, 不是固件错。
        #    (对照: SHM 控制块按 u8/u16 紧凑排布是**协议的一部分**, 改不了。)
        #    ⇒ 正确姿势: 读对齐的 0x0C 整字, 再取其中的 RELOAD/RUN/N_ROUTES 位段。
        w = L.rd32(shm + 0x0C)
        run_after_reset = None if w is None else ((w >> 8) & 0xFF)
        record("S1b RESET 后 ENGINE_RUN=0 (读对齐字 0x0C 取 bit8 — 审计 B/S1b)",
               run_after_reset == 0, "0x0C=0x%08X → run=%s" % (w if w is not None else 0, run_after_reset))

        # ★★ #2 的核心判据: STOP 之后 **samples 必须冻结** (本次 RUN 段已结束),
        #   而 **HEARTBEAT 必须继续增长** (CPU + 定时器存活)。两者在**同一时刻**给出
        #   **相反**的结论 —— 这是"两个量语义不同"最硬的可失败证据: 只测其中一个的话,
        #   就算两者被对调了也看不出来。(修复前本工具正是这么被蒙过去的。)
        sts, _ = L.xact(CMD_STOP)
        ok_stop = (sts == STS_ACK)
        time.sleep(0.3)
        h1, s1 = heartbeat(), samples()
        time.sleep(0.4)
        h2, s2 = heartbeat(), samples()
        record("S2 ★STOP 后 samples 冻结 (仅 RUN 拍 — 范本 OA13 语义)",
               ok_stop and s1 is not None and s1 == s2, "samples %s→%s" % (s1, s2))
        record("S2b ★对照: 同时刻 HEARTBEAT 仍在增长 (每拍无条件 — 范本 OA14 语义)",
               h1 is not None and h2 is not None and h2 > h1, "HEARTBEAT %s→%s" % (h1, h2))

        # START 后 samples 必须恢复增长
        sts, _ = L.xact(CMD_START)
        time.sleep(0.3)
        s3 = samples()
        time.sleep(0.4)
        s4 = samples()
        record("S3 START 后 samples 恢复增长 (引擎在推进)", sts == STS_ACK and s3 is not None
               and s4 is not None and s4 > s3, "samples %s→%s" % (s3, s4))

        # OA13 幂等: 二次 START 不重置统计 (samples 必须继续增长/不回退)
        sts, _ = L.xact(CMD_START)
        time.sleep(0.1)
        s5 = samples()
        record("S4 二次 START 幂等 (samples 不回退, OA13)",
               s5 is not None and s4 is not None and s5 >= s4, "s4=%s → s5=%s" % (s4, s5))

        # ───────── S5: 毒药表兜底 (F11) ─────────
        print("\n── S5 F11 毒药表兜底 ──")
        # ★★★ 审计发现 B/S5 的处置 (2026-09-11)。
        #   这条判据原本**不可能失败**: 注释写"128 条 PID (成本 140/条 → 远超 26000
        #   预算)", 期望 START 被拒。但实际是
        #       128 × 140 = **17920 < 26000**
        #   ⇒ 预算门**根本不会触发** ⇒ 期望"被拒"= 永远 FAIL。而这不是固件的问题,
        #     是这条判据测的是**一件不会发生的事** (不具备可失败性)。
        #   ★ 更严重: 同一个项目的 h723_persist.py T24 **明确记录过同一个算式**
        #     ("128×140 = 17920 < 26000 —— 全 PID 也不超预算!")。
        #     两个工具对同一件事给出**相反的期望** —— 工具之间自相矛盾,
        #     而审计时会先信哪个都是错的。
        #   ⇒ 处置: 把判据**反过来写**, 让它重新具备可失败性:
        #       已知 worst < budget ⇒ 门不该触发 ⇒ START 应当 ACK。
        #       若实测被拒, 说明我的算式或预算模型与固件不一致 —— 那才是要查的。
        #     这样"我知道门不触发"从一句断言变成一条**能被推翻的判据**。
        #   (与 persist T24 的"如实标注不具约束力"同源, 但更进一步: T24 只标注,
        #    这里把标注变成了一个正向可失败判据。)
        NPID, COST_PID, BUDGET = 128, 140, EXEC_DEPLOY_BUDGET
        worst = NPID * COST_PID
        print("  最贵可装程序: %d 条 × %d cyc = %d cyc  vs  预算门 %d"
              % (NPID, COST_PID, worst, BUDGET))
        print("  ⇒ %d %s %d ⇒ 预算门%s被触发"
              % (worst, "<" if worst < BUDGET else ">=", BUDGET, "不" if worst < BUDGET else "会"))

        # 造"最贵程序" (128 条 PID) —— 同时验证"最贵程序确实装得下"
        L.xact(CMD_RESET)
        time.sleep(0.1)
        route = bytearray(128 * 16)
        for i in range(128):
            route[i * 16 + 4] = 0x05      # op = PID
            route[i * 16 + 5] = 0x01      # flags ACTIVE
            route[i * 16 + 3] = i         # dst_channel (唯一, 避免 dst 冲突)
        for chunk in range(2):
            base = shm + OFF_ROUTE_TABLE + chunk * 1024
            seg = bytes(route[chunk * 1024:(chunk + 1) * 1024])
            L.xact(CMD_WRITE_BURST, struct.pack("<IH", base, 256) + seg, timeout=1.5)
        L.xact(CMD_WRITE, struct.pack("<II", shm + 0x0E, 128))   # N_ROUTES = 128
        sts, p = L.xact(CMD_START)
        record("S5 ★F11 预算门不具约束力: %d < %d ⇒ START 应 ACK (可失败)" % (worst, BUDGET),
               sts == STS_ACK,
               "START=%s; 若被拒则说明预算模型算错了, 需重查"
               % ("ACK" if sts == STS_ACK else "NAK %r" % p.decode("latin1")))

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
