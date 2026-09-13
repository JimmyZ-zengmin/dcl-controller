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

    def send(self, cmd, payload=b""):
        """→ ('ACK'|'NAK'|'TIMEOUT', payload_bytes) —— 与 S3 同形状"""
        sts, p = self.L.xact(cmd, payload, timeout=self.timeout)
        if sts is None:
            return ("TIMEOUT", b"")
        return ("ACK" if sts == STS_ACK else "NAK", bytes(p))

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
    判据: 逐个开、发 0x01、看能力字是否等于 EXPECT_CAP。"""
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
      (+ 可选: cap 与期望一致, 说明对端确实是这台固件)。

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
                if expect_cap is None or cap == expect_cap:
                    return True
            time.sleep(0.15)
        return False
    finally:
        d.close()
