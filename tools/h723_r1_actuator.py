#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_r1_actuator.py — R1 子项 ④ 的判据: `actuator_idx` 越界必须在**下载期**被拒

背景 (docs/audit/REVIEW-MIGRATION-FIDELITY.md 一级 #1 子项 ④)
------------------------------------------------------------
范本 `core0` 的 `engine_route_validate()` 里有两条 actuator 校验
(`actuator_idx >= 32` 拒绝 / 受保护引脚拒绝)。H723 迁移时按"本平台没有 GPIO 执行器"
**整段略掉**了 ⇒ 越界索引被"配置接受、物理无输出":
ISR 侧是 `if (ai && ai < MAX_ACTUATORS) ac[ai] = res;` ⇒ **>=64 静默丢弃**。
"配置接受了、就是没效果"是最难查的一类问题。

★ 为什么不是照搬范本的 32
  范本是**单端口 u32 位图**(位 = 引脚) ⇒ 上界 32。
  H723 **没有 GPIO 执行器面**, `actuator_idx` 的语义是 **SHM 浮点槽索引**
  (`ACTUATOR_STATUS[0..63]`) ⇒ 上界 `MAX_ACTUATORS` = **64**。
  **照搬 32 会把合法的 32..63 槽一起误杀** —— 这不是"更严格", 是换个错法。

判据设计 (两侧都要有, 否则是空判据)
----------------------------------
  T0  链路活性 (GET_VERSION → ACK + 期望 cap)
  T1  **阳性对照**: actuator_idx = 63 (合法上界) → deploy **ACK**
      ★ 没有这一条, "64 被拒" 无法区分"校验正确" 与 "deploy 一律失败"。
  T2  **★ 负例**: actuator_idx = 64 (= MAX_ACTUATORS) → deploy **NAK**,
      且原因串含 "actuator_idx"
  T3  **阳性对照 2**: actuator_idx = 0 (= "不驱动执行器") → ACK
      ★ 证明拦的是 `>= MAX_ACTUATORS`, 不是把 0 也拦了。
  T4  工具自检: 同一条路由改一个**必然非法**的字段 (dst_channel=200) → NAK
      ★ 证明"N个ACK里夹一个NAK"这件事本身是可疑的 (NAK 通道真的是通的)。

用法: python tools/h723_r1_actuator.py [--port COM14]
"""

# ★ Windows 控制台默认 GBK: 脚本 print 非 ASCII 字符会 UnicodeEncodeError 直接崩掉,
#   症状看起来像"脚本坏了"而不是"编码问题"。入口统一改成"永不抛"。
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse
import os
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from h723_client import Dcl, CMD_DEPLOY, CMD_RESET, CMD_GET_VERSION  # noqa: E402

# 常量 (必须与 src/engine.h 一致)
SRC_CONST, DST_WIRE, OP_DIRECT = 2, 2, 0x00
ROUTE_FLAG_ACTIVE = 0x01
MAX_ACTUATORS = 64

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok))
    print("  [%s] %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


def route_payload(actuator_idx, dst_channel=5):
    """1 条 CONST→wire 的 DIRECT 路由 + 1 个有限参数"""
    r = struct.pack('<BBBBBBHHHHB',
                    SRC_CONST, 0,          # src_type, src_index(=param 索引)
                    DST_WIRE, dst_channel,
                    OP_DIRECT, ROUTE_FLAG_ACTIVE,
                    0, 0,                  # param_idx, state_offset
                    actuator_idx, 0, 0) + b'\x00'
    header = struct.pack('<HHH', 1, 1, 0)          # nr, np, ns
    param = struct.pack('<ffff', 1.0, 0.0, 0.0, 0.0)
    return header + r + param


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    a = ap.parse_args()

    d = Dcl(a.port, wait=0.5)
    print("端口: %s @ 115200" % d.port)
    print("=== R1 子项 ④: actuator_idx 边界 (下载期拒绝) ===")

    sts, p = d.send(CMD_GET_VERSION)
    cap = (p[2] | (p[3] << 8)) if (sts == "ACK" and len(p) >= 4) else -1
    record("T0 链路活性 (GET_VERSION→ACK)", sts == "ACK" and cap >= 0, "cap=0x%04X" % cap)
    if sts != "ACK":
        d.close()
        return 2

    d.send(CMD_RESET); import time; time.sleep(0.2)

    # T1 阳性对照: 合法上界 63 必须被接受
    s1, r1 = d.send(CMD_DEPLOY, route_payload(63))
    record("T1 阳性对照: actuator_idx=63 (合法上界) → ACK",
           s1 == "ACK", "sts=%s resp=%s" % (s1, r1[:24]))

    # T2 ★负例: 越界 64 必须被拒, 且原因串可读
    s2, r2 = d.send(CMD_DEPLOY, route_payload(64))
    ok2 = (s2 == "NAK" and b"actuator_idx" in r2)
    record("T2 ★actuator_idx=64 (=MAX_ACTUATORS) → NAK 'actuator_idx out of range'",
           ok2, "sts=%s resp=%s" % (s2, r2[:40]))

    # T3 阳性对照 2: 0 表示"不驱动执行器", 必须放行
    s3, r3 = d.send(CMD_DEPLOY, route_payload(0))
    record("T3 阳性对照: actuator_idx=0 (不驱动执行器) → ACK",
           s3 == "ACK", "sts=%s resp=%s" % (s3, r3[:24]))

    # T4 工具自检: NAK 通道本身是通的 (改一个必然非法的字段)
    s4, r4 = d.send(CMD_DEPLOY, route_payload(0, dst_channel=200))
    record("T4 工具自检: dst_channel=200 (必然非法) → NAK",
           s4 == "NAK", "sts=%s resp=%s" % (s4, r4[:40]))

    d.send(CMD_RESET); time.sleep(0.1)
    d.close()

    n = sum(1 for _, ok in RESULTS if ok)
    print("\n=== R1-④ actuator 边界: %d/%d PASS ===" % (n, len(RESULTS)))
    return 0 if n == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
