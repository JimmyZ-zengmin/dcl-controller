#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_client.py — S3 `test_dcl.Dcl` / `engine_status` 的 **H723 侧等价实现**

★ 为什么需要这个文件:
  S3 的 PC 工程工具 (`dclc.py` 文本组态编译器 / `dclmon.py` 监控台) 依赖
  `esp32-core0/tools/test_dcl.py` 里的客户端类。迁到 H723 线时**不应该把 test_dcl.py
  整份搬过来** —— 那是 S3 的验收脚本体, 搬过来会造成"两份 test_dcl 各自漂移"。
  正确做法是: 让工具依赖**一个稳定的客户端接口**, 由各平台各自实现。

★ 语义等价性怎么保证 (不是"我觉得一样"):
  · 帧格式: `0xC0` 请求 / `0xC1` 响应 + CRC16-CCITT —— H723 与 S3 **逐字相同**
    (`transport.h` 照搬), 判据 = S3 的 `test_dcl.py` **一行不改** 能打 H723 (22/30)。
  · 0x38 ENGINE_STATUS 的字段偏移: 前 31 字节与 S3 同布局**且同语义** ——
    特别是 `p[22] = run` (H723 刻意保留 S3 语义, 自己的 gate 挪到了尾部 `p[37]`)。
    见 `src/transport.h` 的说明。
  · 本文件只做"同一协议上的接口形状适配": S3 侧 `send()` 返回 `('ACK'|'NAK'|'TIMEOUT', bytes)`,
    H723 侧 `h723_modbus.Link.xact()` 返回 `(int|None, bytes)` ⇒ 这里把后者包成前者。
  ⇒ 所以 `dclc` / `dclmon` 迁过来是**换实现、不换语义**, 唯一行为差异是默认串口。

★ 与 `h723_t26.py` 里那几个同名函数的关系: 那边是本套件自用的局部副本 (先跑起来再说);
  这里是给**工程工具**用的公共件。后续两边应合并到本文件, 避免漂移。
"""
import struct
import time

import serial

from h723_modbus import Link, find_port, open_serial

STS_ACK = 0x00

# 与 S3 test_dcl.py / dclmon.py 保持一致的 SHM 偏移 (H723 刻意同址)
OFF_SENSOR_MAP = 0x0040
OFF_WIRE_MAP = 0x0240
MAX_WIRES = 128

CMD_GET_VERSION = 0x01
CMD_DEPLOY = 0x10
CMD_START = 0x11
CMD_STOP = 0x12
CMD_RESET = 0x13
CMD_READ = 0x20
CMD_READ_BURST = 0x22
CMD_FORCE = 0x24
CMD_ENGINE_STATUS = 0x38
CMD_SEQ_DEPLOY = 0x44


class Dcl:
    """S3 `test_dcl.Dcl` 的等价物。

    ★ 与原版的一处**有意差异**: 端口可省略 (None / 'auto') ⇒ 自动找 CH340。
      原版默认写死 'COM7' (S3 机器的口), 照搬会让 H723 用户以为"要改代码才能用"。
    """

    def __init__(self, port=None, wait=0.3, timeout=2.0):
        # ★ 用 find_board() 而**不是** find_port(None): 后者取"第一个 CH340",
        #   而本机插着两个 ⇒ 会挑错口, 现象是"板子没响应"(极像板子坏了)。
        #   2026-09-13 实测踩过。
        self.port = port or find_board()
        self.timeout = timeout
        if not self.port:
            raise RuntimeError("找不到串口 —— 检查 CH340 是否插好, 或用 --port 指定")
        # ★ 用 open_serial: 打开后立刻释放 DTR/RTS (防"CH340 RTS 接 NRST 时把板子按在
        #   复位上"导致整条链路假死 —— 见 h723_modbus.open_serial 的事故记录)
        self.ser = open_serial(self.port)
        self.L = Link(self.ser)
        self.ser.reset_input_buffer()
        if wait:
            time.sleep(wait)      # 打开串口常触发 DTR/RTS 复位 —— 等设备启动

    def send(self, cmd, payload=b"", expect_len=None):
        """→ ('ACK'|'NAK'|'TIMEOUT', payload_bytes) —— 与 S3 同形状

        ★★★ `expect_len`（2026-09-16 新增，**现场教训**）：给定时校验**应答载荷长度**，
        不符直接返回 `("TIMEOUT", b"")`（调用方按现有语义重试/判失败）。

        **它挡的是什么**：协议应答里**没有命令码回显、也没有序号**（契约 GAP-12），
        所有 ACK 帧的 cmd 都是 `0x00` ⇒ 客户端**无法判断这条应答是不是自己刚发出的请求**。
        于是任何杂帧（开机 banner、上一条的迟到应答、**另一个进程的应答**）都会被当成本次应答。
        **实测现场（不容置疑）**：密轮询 `0x22 READ_BURST` 读 1 个字时，读回值位型
        `0x1DF70200` = **低16位 `fw_ver 0x0200` + 高16位 `cap 0x1DF7`** ——
        那是一次 **`0x01 GET_VERSION` 的应答**被吃掉了。★ 最坏巧合：`0x22` 读 **1 个字**
        的应答**也是 4 字节**，与 `0x01` 的**完全相同** ⇒ 在那一档上**长度判据也救不了**。
        ⇒ 实践建议（三层，按代价从低到高）：
          ① 能读**两个字**就别读一个字（帧长 8 即挡掉 4 字节杂帧）—— `expect_len=8`；
          ② 关键观测**连续两次读到同值**才算命中；
          ③ 命中时**打印原始 u32**（不要只打解码后的 float —— 定性靠位型）。
        ★ 根治在协议层（应答带命令码回显或序号），属 v1 首选。
        ★ 说明：**旧调用点仍是弱判据**（不传 `expect_len` 时行为与从前逐字节相同），
          这里只提供能力，不在全仓库强行改造（那是 v1 量级的线上格式变更）。
        """
        # ★ 重试的意义：串帧是**瞬时**现象（杂帧被吃掉一次），重发通常就能拿对。
        #   与"连续两次同值"配合，才能把"偶然串帧"与"真实数值变化"分开。
        for _ in range(3 if expect_len is not None else 1):
            sts, p = self.L.xact(cmd, payload, timeout=self.timeout)
            if sts is None:
                return ("TIMEOUT", b"")
            if expect_len is not None and len(p) != expect_len:
                continue          # ★ 长度不符 ⇒ **这条应答不属于本次请求**，丢掉重发
            return ("ACK" if sts == STS_ACK else "NAK", bytes(p))
        return ("TIMEOUT", b"")   # 重试后仍拿不到长度正确的应答

    def close(self):
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    def __del__(self):
        self.close()


def find_board():
    """自动找协议口 —— ★ 认**能力字**, 不认"第一个 CH340" (本机就插着两个 CH340,
    第一版 Dcl(None) 因此挑错口 ⇒ 现象是"板子没响应", 极像板子坏了)。
    判据: 逐个开、发 0x01、看能力字是否**包含** EXPECT_CAP 的每一个必备位
    (★ 必备位掩码, **不是**等值 —— 理由见 link_alive 的 docstring)。"""
    from serial.tools import list_ports
    cands = [p.device for p in list_ports.comports()]
    for dev in cands:
        try:
            # ★ 用 link_alive (它自带重试与合适的等待) —— 第一版用 `Dcl(dev, wait=0.05)`
            #   自己探, 等待太短 ⇒ **板子明明在也应答, 却判"找不到板子"** ✗
            if link_alive(dev, tries=2):
                return dev
        except Exception:
            pass
    raise RuntimeError("自动找板子失败 (试过 %s)" % cands)


def engine_status(dcl):
    """0x38 → dict。字段名与 S3 `test_dcl.engine_status` 完全一致 (工具依赖它)。"""
    sts, p = dcl.send(CMD_ENGINE_STATUS)
    if sts != "ACK" or len(p) < 27:
        return None
    samples, pmin, pmax, emin, emax = struct.unpack("<IIIII", p[:20])
    n_routes, = struct.unpack("<H", p[20:22])
    shm, = struct.unpack("<I", p[23:27])
    ov = struct.unpack("<I", p[27:31])[0] if len(p) >= 31 else 0
    return dict(samples=samples, pmin=pmin, pmax=pmax, emin=emin, emax=emax,
                n_routes=n_routes, run=p[22], shm=shm, ov=ov)


def read_wires(dcl, shm, count=4):
    """0x22 读 WIRE 区连续 count 个 float"""
    sts, p = dcl.send(CMD_READ_BURST, struct.pack("<IH", shm + OFF_WIRE_MAP, count))
    if sts != "ACK" or len(p) < count * 4:
        return None
    return struct.unpack("<%df" % count, p[:count * 4])


def read_sensors(dcl, shm, count=4):
    sts, p = dcl.send(CMD_READ_BURST, struct.pack("<IH", shm + OFF_SENSOR_MAP, count))
    if sts != "ACK" or len(p) < count * 4:
        return None
    return struct.unpack("<%df" % count, p[:count * 4])


def persist_flags(dcl):
    """0x43 查询 → (dirty, persisting) 或 None"""
    sts, p = dcl.send(0x43)
    if sts == "ACK" and len(p) >= 8:
        return (p[7] & 1) != 0, (p[7] & 2) != 0
    return None


def link_alive(port=None, tries=3, expect_cap=0x0DF7):
    """链路活性判据 —— **内容匹配**，不是"有没有字节"。

    ★★★ 为什么不能是 `if d: return True` (这是一次审计发现):
      噪声、波特率不匹配、打开端口的瞬态**都会产生字节**，但**产生不出**
      "状态字节 = ACK + 长度合法 + CRC 通过"的帧。用字节数判活性的后果是:
      一条时基全错的链路会被判成"活" ⇒ 后续所有失败被错误归因到别处。
      (本项目 BRR 事故就是这么被带偏一整轮的: "AB 两端都收不到" 与 "时基全错"
       在 PC 侧表现完全一样。)
    ⇒ 判据 = 解析出帧 + 状态字节是 ACK + 载荷长度 ≥4 + **独立实现**的 CRC 通过
      (+ 可选: cap 含期望位, 说明对端确实是这台固件)。

    ★★★ `expect_cap` 的语义 = **必备位掩码 (required bits)**，**不是等值比较** ——
      判据是 `(cap & expect_cap) == expect_cap`（缺任何一位即判否）。

      · 为什么**必须**是掩码 (这是本函数第二版踩的坑):
        固件新增一个能力位时实现字会**变大**（例: 加 `DCL_CAP_DEVBIND=0x1000`
        后 0x0DF7 → 0x1DF7）。若写成 `cap == expect_cap`，一个新增位就让
        **所有**调用本函数的工具同时认不到板子，而症状是"**找不到板子**"
        —— 会把人引向接线/驱动/端口方向，与真实原因(固件的能力字多了一位)
        完全相反。掩码语义下"新增能力位不破坏认口"。
      · 为什么不干脆 `expect_cap=None`（只看帧合法）:
        那样"别的设备在回话"也会被判成这台固件活着 —— 判据退化成空判据。
        必备位是"对端身份"这一层的最弱但**仍可失败**的判据。
      · 语义边界（有意为之）: 本函数只问"必备位在不在"，**不问**"有没有多余位"。
        要审"宣称 = 实现"（多一位/少一位都算错）请用 `h723_proto.py` 的 T1.4
        —— 那里是**等值**比较，与本函数的分工不同、刻意不合并。
      · 调用方若需要更严的必备集: 传自己的 `expect_cap`（掩码，或起来即可）。
        想要求"某位**不存在**"则超出本函数语义（会给认口加脆性，故不提供）。

    返回 True/False；不抛异常（调用方通常在"准备阶段"用它）。
    """
    try:
        d = Dcl(port)
    except Exception:
        return False
    try:
        for _ in range(tries):
            sts, p = d.send(CMD_GET_VERSION)
            if sts == "ACK" and len(p) >= 4:
                cap = p[2] | (p[3] << 8)
                # ★ 必备位掩码 (不是等值) —— 见 docstring: 等值语义会让"固件新增
                #   能力位"这一个动作把**所有**工具一起打成"找不到板子"。
                if expect_cap is None or (cap & expect_cap) == expect_cap:
                    return True
            time.sleep(0.15)
        return False
    finally:
        d.close()
