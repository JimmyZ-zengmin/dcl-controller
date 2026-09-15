#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_microstep.py — 实测"每一脉冲让轴走多少"，即**驱动的实际分辨力**
====================================================================
为什么这么测:
  固件**没有"走 N 个脉冲"的命令** (0x39 op=19 的 sub 只到 10, 全是设频率/方向/使能/限时),
  所以没法"精确发 1 个脉冲"。但:
    · 脉冲率是**精确的** (实测偏差 ≤0.4%)
    · 位移用编码器量, 不需要知道脉冲数
  ⇒ **平均每脉冲位移 = 总位移 / (频率 × 时长)** —— 这个量不需要精确计数, 且可以测得很准。
  ⇒ 同时看**位移的分布**: 是"每脉冲一小步"还是"攒几步跳一下"(齿槽/静摩擦的尺寸效应)。

用法: python h723_microstep.py [最高Hz]
"""
import re
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAP = r"D:\STM\8.29 AIAutoFactior\9.10 H723newest\build\dcl_h723.map"
PORT = "COM21"
SPR = 1600.0                       # 8 细分 ⇒ 1600 步/圈
STEP_DEG = 360.0 / SPR             # 0.225°
LSB = 360.0 / 4096.0               # 0.0879°


def crc16(d, c=0xFFFF):
    for b in d:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


def fr(cmd, pl=b""):
    body = bytes([cmd, len(pl) & 0xFF, (len(pl) >> 8) & 0xFF]) + pl
    return bytes([0xC0]) + body + struct.pack("<H", crc16(body))


def sym(name):
    txt = open(MAP, encoding="utf-8", errors="replace").read()
    m = re.search(r"0x([0-9a-fA-F]{8,16})\s+" + re.escape(name) + r"\b", txt)
    return int(m.group(1), 16) if m else None


def main():
    import serial
    from pyocd.core.helpers import ConnectHelper

    a_raw = sym("g_as_raw_v")
    if not a_raw:
        print("✗ 找不到 g_as_raw_v")
        return 1
    rates = [4, 10, 25, 60, 150, 400]
    print("=" * 84)
    print("微步分辨力实测: 每脉冲让轴走多少?  (理论 %.3f° = 1/%d 圈)" % (STEP_DEG, int(SPR)))
    print("=" * 84)
    print("  编码器 1 LSB = %.4f°  ⇒ 理论 1 微步 = %.2f LSB" % (LSB, STEP_DEG / LSB))

    ser = serial.Serial(PORT, 115200, timeout=0.02)

    def cmd(payload, wait=0.06, tag=""):
        ser.reset_input_buffer()
        ser.write(fr(0x39, payload))
        ser.flush()
        buf = b""
        t0 = time.time()
        while time.time() - t0 < wait:
            b = ser.read(256)
            if b:
                buf += b
                if len(buf) >= 6 and buf[0] == 0xC1:
                    n = buf[2] | (buf[3] << 8)
                    if len(buf) >= 6 + n:
                        return buf[1], buf[4:4 + n]
            else:
                time.sleep(0.0005)
        if tag:
            print("  ⚠ 命令 %s 无应答" % tag)
        return None, b""

    def st():
        s, p = cmd(bytes([19, 0]), tag="st")
        if s is None or len(p) < 96:
            return None
        u = struct.unpack("<24I", p[:96])
        return dict(hz=u[0], ena=u[2], raw=u[8], enapol=u[12], pe9=(u[20] >> 9) & 1)

    sess = ConnectHelper.session_with_chosen_probe(
        target_override="stm32h723xx",
        options={"connect_mode": "halt", "frequency": 2000000}, blocking=False)
    if sess is None:
        print("✗ 找不到探针")
        return 1

    with sess:
        t = sess.target
        t.halt()
        t.resume()
        try:
            got = None
            for _ in range(12):
                got = st()
                if got:
                    break
                time.sleep(0.4)
            if got is None:
                print("✗ 板子无应答")
                return 1
            cmd(bytes([19, 5, 1, 0, 0, 0]))
            time.sleep(0.2)
            cmd(bytes([19, 3, 1, 0, 0, 0]))
            time.sleep(0.25)
            cmd(bytes([19, 2, 0, 0, 0, 0]))
            cmd(bytes([19, 4]) + (200000).to_bytes(4, "little"))
            got = st()
            if got is None or got["ena"] != 1 or got["pe9"] != 1:
                print("✗ 前置条件不成立 (ena/PE9)")
                return 1
            print("  前置: 极性=%d ena=%d PE9=%d ✓" % (got["enapol"], got["ena"], got["pe9"]))
            print()
            print("  频率Hz | 时长s | 脉冲数(名义) | 实测位移° | **每脉冲°** | 与理论比 | 位移分布(LSB)")
            print("  -------+-------+--------------+-----------+-------------+----------+-------------")
            for f in rates:
                sec = max(4.0, 240.0 / f)          # 保证够多脉冲
                cmd(bytes([19, 1]) + int(f).to_bytes(4, "little"), tag="rate")
                time.sleep(0.25)                   # 等它稳下来再开始计数
                rec = []
                t0 = time.perf_counter()
                while time.perf_counter() - t0 < sec:
                    tic = time.perf_counter()
                    rec.append((time.perf_counter() - t0, t.read32(a_raw)))
                    while time.perf_counter() - tic < 0.0025:
                        pass
                cmd(bytes([19, 1]) + (0).to_bytes(4, "little"))
                time.sleep(0.2)
                if len(rec) < 20:
                    print("  %6d | 采样不足" % f)
                    continue
                span = rec[-1][0] - rec[0][0]
                ang = 0
                dist = {}
                back = 0
                for i in range(1, len(rec)):
                    # ★★ 必须用**最短弧**, 不能用强制单向:
                    #   低速时每采样段位移 << 1 LSB, 编码器 ±1LSB 噪声会造出"向后退一步",
                    #   强制单向会把 -1 读成 +4095 ⇒ **每步虚增一整圈**
                    #   (实测 4Hz 时 4095 出现 621 次, 把位移虚增到 249686°, 真值只 ~54°)
                    d = (rec[i][1] - rec[i - 1][1]) & 0xFFF
                    if d > 2048:
                        d -= 4096
                    if d < 0:
                        back += 1
                    ang += d
                    dist[d] = dist.get(d, 0) + 1
                deg = ang * LSB
                npulse = f * span
                per = deg / npulse if npulse else 0
                top = sorted(dist.items(), key=lambda kv: -kv[1])[:4]
                tops = " ".join("%+d×%d" % (k, v) for k, v in top)
                print("  %6d | %5.1f | %12.0f | %9.2f | **%10.4f** | %7.3f | %s (反向%4.1f%%)"
                      % (f, span, npulse, deg, per, per / STEP_DEG, tops,
                         back / max(len(rec) - 1, 1) * 100))
            print()
            print("★ 判读:")
            print("   · 『每脉冲°』与理论 0.225° 的比值 ⇒ 微步标定是否正确")
            print("   · 位移分布若是『1×N』(每个采样段固定一个小步) ⇒ 平滑运动")
            print("     若出现『0×N 与 3×N 交替』 ⇒ **攒几步跳一下** = 齿槽/静摩擦造成的尺寸效应")
            print("     ⇒ 后者意味着**实际分辨力比 0.225° 差**")
            print("   · 低速(4Hz)下若 1 个脉冲走不满 0.225° ⇒ 微步在低速下不可分辨")
        finally:
            cmd(bytes([19, 1]) + (0).to_bytes(4, "little"))
            cmd(bytes([19, 3, 0, 0, 0, 0]))
            ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
