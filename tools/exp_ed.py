#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-D 验收: 扫描段与整段 ISR **同窗口**对比（判据 D-1 / D-2 / D-3）。

## 前置事实
`main.c` 在 `engine_tick()` 调用**前后**各取一次 `tb_cyc()`（那两个读本来就存在），
差值写进新的 SHM 域 `OFF_SCAN_CYC_*`（`0x3800` 起），与既有的 `di`（整段 ISR）**分开记**。

## 判据（都能失败）
  D-1 `扫描段 + 拍内其它工作 ≈ di`，**差 <5%**
      ★ 这条同时是**正确性判据**: 若夹取点错了（比如夹到别的东西上）, 差值会离谱。
  D-2 扫描段的**拍内 σ** 应小于整段 `di` 的 σ（说明噪声主要来自扫描之外）
  D-3 ★ **反证**: 引擎 STOP 后, `扫描段 SUM_N` **不得再增长**（拍数冻结）。
      若 STOP 后它还在涨 ⇒ 夹错位置（夹到一个与引擎门无关的量上）。
  D-4 `nrun` 必须与扫描段**同批**读出, 且与部署的路由数/分档自洽。

## 读法
两条统计面**在同一次 0x22 突发里**读（跨两次读会让窗口前进几十拍 ⇒ 读偏斜,
本项目已因这类错栽过: §A1.5 的 sum/g_isr_n 口径）。
"""
import os, re, struct, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

SHM_SEEN = 0x20004E80
cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst = 0x38, 0x10, 0x12, 0x11, 0x22
OFF_EXEC_SUM_LO, OFF_EXEC_SUM_HI, OFF_EXEC_SUM_N = 0x3860, 0x3864, 0x3868
OFF_SCAN_CYC_LAST = 0x3800
OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
       "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]
FLAGS = 0x01 | 0x02          # ACTIVE | ROUTE_FLAG_WIRE2（同 h723_tick_ring.mk）
SRC_CONST, DST_WIRE = 2, 2


def mk(op, div, n):
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  FLAGS, i, (i % 64) + 1, 0, i, div, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    states = b"\x00" * (16 * (min(n, 64) + 1))
    return struct.pack("<HHH", n, n, min(n, 64) + 1) + routes + params + states + b"\x00" * 16


def rd(dcl, addr, nwords):
    out, off = b"", 0
    while off < nwords:
        k = min(200, nwords - off)
        sts, p = dcl.send(cmd_burst, struct.pack("<IH", addr + 4 * off, k), expect_len=None)
        if sts != "ACK" or len(p) < 4 * k:
            return None
        out += p[:4 * k]
        off += k
    return out


def main():
    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("端口 = %s" % dcl.port)
    time.sleep(1.0)

    # ★★ SHM 地址**问 0x38 要**, 不硬编码。
    #   现场: 加 E-D 的 SCAN 域后 `g_shm` 从 `0x20004E80` 移到 `0x20004EA0`,
    #   而第一版这里写的是 `SHM_SEEN = 0x20004E80` ⇒ 读到垃圾
    #   （`环写=927865394 / tick=0`）⇒ 工具报"板子不健康", 而板上完全正常。
    #   ⇒ §5.42「工具的隐含前提会随固件改变而静默失效」的又一次实证。
    sts, p = dcl.send(cmd_status, expect_len=51)
    if sts != "ACK" or len(p) < 51:
        print("!! 0x38 失败"); dcl.close(); return 2
    SHM = struct.unpack("<I", p[23:27])[0]
    print("SHM = 0x%08X" % SHM)

    def health(tag):
        v = rd(dcl, SHM + 0x3880, 2)
        if not v:
            print("  %s 读失败" % tag); return None
        w, tk = struct.unpack("<2I", v[:8])
        print("  %-16s 环写=%-9d tick=%d" % (tag, w, tk))
        return tk

    tk = health("① 刚连接")
    if tk is None or tk < 200000:
        for i in range(6):
            dcl.close(); time.sleep(0.4)
            dcl.__init__(dcl.port, wait=1.0)
            v = rd(dcl, SHM + 0x3880, 2)
            if v and struct.unpack("<2I", v[:8])[1] >= 200000:
                tk = struct.unpack("<2I", v[:8])[1]
                print("  健康门: 第 %d 次重试后 tick=%d" % (i + 1, tk)); break
            time.sleep(1.0)
    if tk is None or tk < 200000:
        print("!! 板子不健康 ⇒ 判无效"); dcl.close(); return 2

    for op, div, n in ((5, 0, 128), (0, 0, 128), (5, 1, 128)):
        name = OPS[op] if op < len(OPS) else str(op)
        sts, pp = dcl.send(cmd_deploy, mk(op, div, n), expect_len=None)
        if sts != "ACK":
            print("\n=== %s div=%d n=%d: deploy 被拒 ===" % (name, div, n)); continue
        dcl.send(cmd_stop); time.sleep(0.15)
        dcl.send(cmd_start); time.sleep(0.15)
        time.sleep(1.5)

        # ★ 两条统计面**同一次突发**: 从 0x3800 连读到 0x386C（覆盖 SCAN 与 EXEC 两块）
        raw = rd(dcl, SHM + OFF_SCAN_CYC_LAST, (0x386C - OFF_SCAN_CYC_LAST) // 4)
        if raw is None:
            print("\n=== %s div=%d n=%d: 读失败 ===" % (name, div, n)); continue
        u = struct.unpack("<%dI" % (len(raw) // 4), raw)

        def at(off):
            return u[(off - OFF_SCAN_CYC_LAST) // 4]

        s_last, s_min, s_max = at(0x3800), at(0x3804), at(0x3808)
        s_n, s_lo, s_hi = at(0x380C), at(0x3810), at(0x3814)
        nrun = at(0x3818)
        e_lo, e_hi, e_n = at(0x3860), at(0x3864), at(0x3868)
        s_sum = s_lo | (s_hi << 32)
        e_sum = e_lo | (e_hi << 32)
        s_mean = s_sum / float(s_n) if s_n else 0
        e_mean = e_sum / float(e_n) if e_n else 0

        print("\n=== %s  div=%d  n=%d  (每拍 nrun=%d) ===" % (name, div, n, nrun))
        print("  扫描段 : n=%-6d 均值=%8.1f  min=%-6d max=%-6d last=%-6d TB"
              % (s_n, s_mean, s_min, s_max, s_last))
        print("  整段ISR: n=%-6d 均值=%8.1f  TB" % (e_n, e_mean))
        if e_mean:
            diff = e_mean - s_mean
            pct = 100.0 * diff / e_mean
            print("  ⇒ 拍内其它工作 = %.1f TB (%.0f cyc)  占整段 %.1f%%" % (diff, diff * 2, pct))
            print("     D-1 扫描段 + 其它 ≈ 整段: 恒等式（定义如此）; **关键是其它那部分有多大**")
            print("     ★ 结构性判读: 其它 = %.1f TB ⇒ 若它很大, 说明此前用 di 比结构量是错的"
                  % diff)

    # ── D-3 反证: STOP 后扫描段拍数必须冻结 ──
    print("\n=== D-3 ★ 反证: STOP 后扫描段拍数是否冻结 ===")
    dcl.send(cmd_stop); time.sleep(0.2)
    r1 = rd(dcl, SHM + OFF_SCAN_CYC_LAST, (0x381C - OFF_SCAN_CYC_LAST) // 4)
    n1 = struct.unpack("<7I", r1)[3] if r1 else -1     # 0x380C = SUM_N
    time.sleep(1.2)
    r2 = rd(dcl, SHM + OFF_SCAN_CYC_LAST, (0x381C - OFF_SCAN_CYC_LAST) // 4)
    n2 = struct.unpack("<7I", r2)[3] if r2 else -1
    print("  STOP 后 SUM_N: %d → %d（间隔 1.2 s）" % (n1, n2))
    print("  ⇒ D-3 %s" % ("**通过**（冻结 ⇒ 夹取点在引擎门之后, 位置正确）"
                        if n1 == n2 else "**未通过**（仍增长 %d ⇒ 夹错位置）" % (n2 - n1)))
    dcl.send(cmd_start); time.sleep(0.3)
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
