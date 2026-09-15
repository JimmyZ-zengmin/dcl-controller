#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_latch_trig.py — 判定"MDMA 为什么不停地在搬"（P0-2）

已确认的事实（前两轮实测）:
  · 尺寸 > byte（halfword/word）⇒ **TEIF**（这条路只吃字节传输）
  · `byte + 字节递增 + BNDT=2` ⇒ 宽度正确覆盖 PE0~PE15（P0-1 的解）
  · 但锁存延迟恒为 **114 cyc ≈ 285 ns**，与 100 µs 拍长无关 ⇒ **仍在自循环**

本脚本固定"已修好宽度"的那套配置，只改**触发相关**的字段，看哪一个能让
延迟从 ~285 ns 跳到"0~100 µs 区间"（= 真被拍请求驱动）。

嫌疑: `CLAR`(链表指回自身) / `TRGM`(触发模式) / `CTBR.TSEL`(请求源)
"""
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from h723_client import Dcl   # noqa: E402

C = 0x39
CLAR_SELF = 0x24003100
CLAR_NONE = 0

CTCR_BYTEINC = 0x0000000A                      # SSIZE=DSIZE=byte + SINC/DINC=字节递增
TRGM = {0: "buffer", 1: "block", 2: "rep-blk", 3: "full"}
CTBR_SBUS_TSEL8 = 0x00010008                   # TSEL=8 = DMA2_S0_TC (交付值)
CTBR_SBUS_TSEL0 = 0x00010000                   # TSEL=0 = 软件请求

CASES = [
    ("A 基线(交付触发方式)",      CTCR_BYTEINC,                       CTBR_SBUS_TSEL8, CLAR_SELF),
    ("B TRGM=block",          CTCR_BYTEINC | (1 << 28),           CTBR_SBUS_TSEL8, CLAR_SELF),
    ("C TRGM=rep-block",      CTCR_BYTEINC | (2 << 28),           CTBR_SBUS_TSEL8, CLAR_SELF),
    ("D TRGM=full",           CTCR_BYTEINC | (3 << 28),           CTBR_SBUS_TSEL8, CLAR_SELF),
    ("E CLAR=0 (不接链表)",      CTCR_BYTEINC,                       CTBR_SBUS_TSEL8, CLAR_NONE),
    ("F CLAR=0 + TRGM=block", CTCR_BYTEINC | (1 << 28),           CTBR_SBUS_TSEL8, CLAR_NONE),
    ("G TSEL=0 (软件请求)",      CTCR_BYTEINC,                       CTBR_SBUS_TSEL0, CLAR_SELF),
    ("H TSEL=0 + CLAR=0",     CTCR_BYTEINC,                       CTBR_SBUS_TSEL0, CLAR_NONE),
]


def op14(d, v):
    sts, p = d.send(C, bytes([0x0E]) + struct.pack("<I", v))
    if sts != "ACK" or len(p) < 24:
        return None
    w = struct.unpack("<6I", p[:24])
    return dict(v=w[0], odr0=w[1], got=w[2], it=w[3], lat=w[4], dv=w[5])


def op15(d, c, cbrur, bndt, clar, ctbr):
    sts, p = d.send(C, bytes([0x0F]) + struct.pack("<5I", c, cbrur, ctbr, bndt, clar))
    if sts != "ACK" or len(p) < 48:
        return None
    w = struct.unpack("<12I", p[:48])
    return dict(mccr=w[2], mcisr=w[3], cbndtr=w[4], ctcr=w[5], ctbr=w[6], clar=w[9])


def lat_samples(d, n=4):
    lats = []
    for _ in range(n):
        op14(d, 0x0000)
        time.sleep(0.002)
        r = op14(d, 0x0001)
        if r and r["got"] == 0x0001:
            lats.append(r["lat"])
    return lats


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM18"
    d = Dcl(port, wait=0.5)
    try:
        d.send(C, bytes([0x07]))
        d.send(C, bytes([0x00]))
        time.sleep(0.2)
        if not op14(d, 0x0001)["dv"]:
            print("✗ DWT 未在走 ⇒ 先修 op=7")
            return

        print("触发方式判定 (固定 SSIZE=DSIZE=byte + 字节递增 + BNDT=2)")
        print("%-22s %-9s %-7s %-6s %-16s %s"
              % ("方案", "TRGM", "CLAR", "TEIF", "锁存延迟", "判定"))
        for name, c, ctbr, clar in CASES:
            s = op15(d, c, 0, 2, clar, ctbr)
            if s is None:
                print("%-22s ✗ 无应答" % name)
                continue
            time.sleep(0.05)
            teif = s["mcisr"] & 1
            lats = lat_samples(d)
            if not lats:
                print("%-22s %-9s %-7s %-6s %-16s %s"
                      % (name, TRGM[(s["ctcr"] >> 28) & 3],
                         "self" if s["clar"] else "0", "1" if teif else "0", "—",
                         "✗ 压根不搬 (配置无效/一次性)"))
                continue
            avg = sum(lats) // len(lats)
            mn, mx = min(lats), max(lats)
            if avg < 400:
                verdict = "△ 仍自循环 (延迟 285ns 量级)"
            else:
                verdict = "✓✓ 拍驱动! 延迟 %.1f µs" % (avg * 2.5 / 1000)
            print("%-22s %-9s %-7s %-6s %-16s %s"
                  % (name, TRGM[(s["ctcr"] >> 28) & 3], "self" if s["clar"] else "0",
                     "1" if teif else "0", "%d..%d cyc" % (mn, mx), verdict))

        op15(d, 0x00020000, 0, 4, CLAR_SELF, CTBR_SBUS_TSEL8)
        op14(d, 0x0000)
        d.send(C, bytes([0x01]))
        print("\n已恢复交付默认")
    finally:
        d.close()


if __name__ == "__main__":
    main()
