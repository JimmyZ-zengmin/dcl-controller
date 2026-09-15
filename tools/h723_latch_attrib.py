#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_latch_attrib.py — 锁存链的**结构归因**：ODR 的写入者到底是谁？

背景
----
已实测：改动 SHADOW(DTCM) 后，`GPIOE_ODR` 会在 ~285 ns 内跟着变，且与
DMAMUX 路由 / DIER 门控 / `DMA2_S0_CR.EN` / MDMA `TSEL` / `TRGM` / `CLAR` / 标志电平均无关。
⇒ 要说"是 MDMA 在搬"，就必须证明 **目的值由 CSAR 决定**，而不是由影子决定。

本脚本做两件事
--------------
M1 **源重定向**：把 `CSAR` 从影子改到另一个 DTCM 常量（`g_trig_ccr4`，可用 `op=5` 设值）。
   - ODR 改成跟随那块常量 ⇒ 确实是"源→目的"的搬运，MDMA 在搬（源说了算）
   - ODR 仍跟随影子       ⇒ 写 ODR 的另有其人，前提错了
M2 **目的重定向**：把 `CDAR` 从 `GPIOE_ODR` 改到 `GPIOB_ODR`（另一个可读回的口），
   再看 `GPIOE_ODR` 是否还会变 —— 若还会变，说明写它的人不看 CDAR。

★ 每次改完都用 `op=9` 读回 CSAR/CDAR, 确认**写真的进去了**（本项目"写不进去"是常态）。
"""
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from h723_client import Dcl   # noqa: E402

C = 0x39
CT = 0x0000000A            # 已修好的宽度配置: byte + 字节递增
CTBR = 0x00010008          # TSEL=8 + SBUS=1
BNDT = 2
CLAR_SELF = 0x24003100
G_TRIG_CCR4 = 0x2000002C   # DTCM: 常量, 可用 op=5 设值 (来自 .map)
GPIOE_ODR = 0x58021014
GPIOB_ODR = 0x58020414


def op14(d, v):
    sts, p = d.send(C, bytes([0x0E]) + struct.pack("<I", v))
    if sts != "ACK" or len(p) < 24:
        return None
    return struct.unpack("<6I", p[:24])[2]      # got


def op15(d, csar=0, cdar=0, ctcr=CT, ctbr=CTBR, bndt=BNDT, clar=CLAR_SELF):
    d.send(C, bytes([0x0F]) + struct.pack("<7I", ctcr, 0, ctbr, bndt, clar, csar, cdar))
    time.sleep(0.08)
    sts, p = d.send(C, bytes([0x09]))
    w = struct.unpack("<12I", p[:48])
    return dict(mccr=w[2], mcisr=w[3], ctcr=w[5], ctbr=w[6], csar=w[7], cdar=w[8], clar=w[9], odr=w[11])


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM18"
    d = Dcl(port, wait=0.5)
    try:
        d.send(C, bytes([0x07]))
        d.send(C, bytes([0x00]))
        time.sleep(0.3)

        print("=" * 78)
        print("M1  源重定向: ODR 跟的是「影子」还是「CSAR 指的地方」?")
        print("=" * 78)
        s = op15(d)
        print("  ① 源 = 影子         CSAR=0x%08X  写影子 0x0101 → ODR=0x%04X"
              % (s["csar"], op14(d, 0x0101)))

        d.send(C, bytes([0x05, 0]) + struct.pack("<I", 0x1234))
        time.sleep(0.15)
        print("  ② 把 g_trig_ccr4(0x%08X) 设为 0x1234" % G_TRIG_CCR4)

        s = op15(d, csar=G_TRIG_CCR4)
        a = op14(d, 0x0000)
        b = op14(d, 0xFFFF)
        print("  ③ 源 = g_trig_ccr4  CSAR=0x%08X  写影子 0x0000 → ODR=0x%04X"
              % (s["csar"], a))
        print("                      写影子 0xFFFF → ODR=0x%04X" % b)
        if a == 0x1234 and b == 0x1234:
            print("  ⇒ ✓✓ **源说了算** —— 目的值由 CSAR 决定 ⇒ 确实是 MDMA 在做 源→ODR 的搬运")
        elif b == 0xFFFF or a == 0x0000:
            print("  ⇒ ✗ **仍跟随影子** ⇒ 写 ODR 的另有其人, 前提错, 要换方向查")
        else:
            print("  ⇒ ? 既不是常量也不是影子, 需要单独看")

        print()
        print("=" * 78)
        print("M2  目的重定向: 把 CDAR 改到 GPIOB_ODR, GPIOE_ODR 还会变吗?")
        print("=" * 78)
        s = op15(d, csar=0, cdar=GPIOB_ODR)
        a = op14(d, 0x0000)
        b = op14(d, 0xFFFF)
        print("  源=影子, CDAR=0x%08X(读回 0x%08X)" % (GPIOB_ODR, s["cdar"]))
        print("  写影子 0x0000 → GPIOE_ODR=0x%04X ; 写影子 0xFFFF → GPIOE_ODR=0x%04X" % (a, b))
        if b == 0xFFFF:
            print("  ⇒ ✗ GPIOE_ODR **仍在变** ⇒ 写它的人不看 CDAR ⇒ 不是 MDMA!")
        else:
            print("  ⇒ ✓ CDAR 改了之后 GPIOE_ODR 不再跟随 ⇒ 写入者确实走 CDAR (MDMA)")

        op15(d, csar=0, cdar=0)
        op14(d, 0)
        d.send(C, bytes([0x01]))
        print("\n已恢复交付配置 (源=影子, 目的=GPIOE_ODR, 影子=0, 诊断开)")
    finally:
        d.close()


if __name__ == "__main__":
    main()
