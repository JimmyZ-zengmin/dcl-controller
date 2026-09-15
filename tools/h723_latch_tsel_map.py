#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_latch_tsel_map.py — 扫出 TSEL ↔ MDMA 请求线的真实映射

依据（RM0468 §14.5.12 MDMA_CxTBR, 第 610 页，逐字）:
    "Bits 5:0 TSEL[5:0]: Trigger selection.
     This field selects the hardware trigger (RQ) input for channel x.
     **The ACK is sent on the ACK output having the same index value.**
     When SWRM bit is set, this bit field is ignored."

⇒ TSEL = `mdma_str[n]` 的**线号**，1:1。
   而 `do.c` 注释说 TSEL=8 = `DMA2_Stream0_TC` —— 本轮实测已否掉：
   把 DMAMUX 路由 / DIER 门控 / `DMA2_S0_CR.EN` / TCIF0 全部拆掉，TSEL=8 照样触发。

本脚本的判据（能失败）
----------------------
对每个 TSEL，分别测 **DMA2_S0 使能 / 禁用** 两种状态下的锁存延迟：
  · 该 TSEL 若真是 `DMA2_S0_TC` ⇒ 禁用 DMA2_S0 后应该**停止**搬运
  · 若两种状态都照样搬 ⇒ 是别的（常被拉高的）请求线
  · 若被 DMA2_S0 驱动且**只在使能时**偶尔搬 ⇒ 这才是真正的 DMA2_S0_TC 线号

★ 延迟判据必须健全: 写0⇒ODR==0 **且** 写1⇒ODR==1, 否则该样本作废。
"""
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from h723_client import Dcl   # noqa: E402

C = 0x39


def op14(d, v):
    sts, p = d.send(C, bytes([0x0E]) + struct.pack("<I", v))
    if sts != "ACK" or len(p) < 24:
        return None, None
    w = struct.unpack("<6I", p[:24])
    return w[2], w[4]


def op09(d, cr=None):
    pl = bytes([0x09]) + (struct.pack("<I", cr) if cr is not None else b"")
    sts, p = d.send(C, pl)
    return struct.unpack("<12I", p[:48])


def set_ctbr(d, tsel):
    d.send(C, bytes([0x0F]) + struct.pack("<7I", 0x0000000A, 0, 0x00010000 | tsel,
                                          2, 0x24003100, 0, 0))
    time.sleep(0.05)


def sample(d):
    g0, _ = op14(d, 0x0000)
    if g0 != 0x0000:
        return None
    g1, lat = op14(d, 0x0001)
    return lat if g1 == 0x0001 else None


def works(d, n=3):
    L = [sample(d) for _ in range(n)]
    ok = [x for x in L if x is not None]
    return (sum(ok) // len(ok)) if ok else None


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM18"
    d = Dcl(port, wait=0.5)
    try:
        d.send(C, bytes([0x07]))
        d.send(C, bytes([0x00]))
        time.sleep(0.3)

        print("TSEL ↔ 请求线映射扫描 (判据: 写0⇒ODR=0 且 写1⇒ODR=1)")
        print("  对每个 TSEL 测两次: DMA2_S0 使能 / 禁用")
        print()
        print("  %-6s %-18s %-18s %s" % ("TSEL", "DMA2_S0 使能", "DMA2_S0 禁用", "判读"))
        found = []
        for tsel in range(0, 34):
            op09(d, 0x00005111)
            set_ctbr(d, tsel)
            a = works(d)
            op09(d, 0x00005000)          # DMA2_S0 EN=0
            b = works(d)
            op09(d, 0x00005111)
            if a is None and b is None:
                continue                  # 该线无触发, 不打印 (34 行太多)
            la = ("%d cyc" % a) if a is not None else "—"
            lb = ("%d cyc" % b) if b is not None else "—"
            if a is not None and b is not None:
                v = "两种状态都搬 ⇒ **常高线**, 与 DMA2_S0 无关"
            elif a is not None and b is None:
                v = "★ 只在其使能时搬 ⇒ **这才是 DMA2_S0_TC!**"
                found.append(tsel)
            else:
                v = "?(使能时反而没搬)"
            print("  %-6d %-18s %-18s %s" % (tsel, la, lb, v))

        print()
        if found:
            print("⇒ 候选 = DMA2_S0_TC 的线号: %s" % found)
        else:
            print("⇒ 没有找到'只在DMA2_S0使能时搬'的线 ⇒ 当前的桥接思路不可行,")
            print("   需要换请求源 (或改回 CPU 直写)。")

        set_ctbr(d, 8)
        op09(d, 0x00005111)
        op14(d, 0)
        d.send(C, bytes([0x01]))
        print("\n已恢复交付配置")
    finally:
        d.close()


if __name__ == "__main__":
    main()
