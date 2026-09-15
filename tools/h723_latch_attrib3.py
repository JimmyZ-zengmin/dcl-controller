#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_latch_attrib3.py — 用健全判据重做"桥"的排除实验

必须重做的原因
--------------
之前那轮"拆掉 DMAMUX/DIER/DMA2_S0 都不影响"的结论，用的是**坏判据**：
延迟探针只验了"写1 ⇒ ODR==1"，没验"写0 ⇒ ODR==0"。ODR 卡在 1 时会假通过。
⇒ 那批排除**全部作废，必须重做**。

已确证的事实（判据健全）:
  · MDMA 确实在做 CSAR→CDAR 的搬运（源重定向实验：源说了算）
  · `TSEL=8` 是唯一能触发的值；0/1/5/6/7/9/20/40/63 全部不搬
  · `CLAR=0` ⇒ 通道跑完一次自清 EN, 之后不再搬
  · 清 `DMA2_S0` 的 TCIF0 ⇒ 延迟不变（但这条要再确认一次）

本轮: 逐项拆桥, 全部用健全判据
"""
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from h723_client import Dcl   # noqa: E402

C = 0x39
SHADOW_SYM = 0x2000ABA8        # 仅作参考, 实际由固件自己算


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


def op10(d, idx, val):
    d.send(C, bytes([0x0A, idx]) + struct.pack("<I", val))
    time.sleep(0.04)


def op15(d):
    """恢复交付锁存配置 (CTCR=0x0A, BNDTR=2, CLAR=self, CTBR=0x10008, EN=1)"""
    d.send(C, bytes([0x0F]) + struct.pack("<7I", 0x0000000A, 0, 0x00010008, 2, 0x24003100, 0, 0))
    time.sleep(0.08)


def sample(d):
    """健全样本: 必须 写0⇒ODR==0 且 写1⇒ODR==1"""
    g0, _ = op14(d, 0x0000)
    if g0 != 0x0000:
        return None
    g1, lat = op14(d, 0x0001)
    if g1 != 0x0001:
        return None
    return lat


def run(d, n=6):
    L = [sample(d) for _ in range(n)]
    ok = [x for x in L if x is not None]
    return (sum(ok) // len(ok)) if ok else None, ["%d" % x if x is not None else "—" for x in L]


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM18"
    d = Dcl(port, wait=0.5)
    try:
        d.send(C, bytes([0x07]))
        d.send(C, bytes([0x00]))
        time.sleep(0.3)
        if op14(d, 1)[0] is None:
            print("✗ 无应答")
            return

        def report(name):
            avg, raw = run(d)
            print("  %-34s 延迟 %-10s 原始 %s"
                  % (name, ("%s cyc" % avg) if avg is not None else "**无**", raw))
            return avg

        print("=" * 88)
        print("逐项拆桥 (判据: 写0⇒ODR必须变0, 写1⇒ODR必须变1; 任一不成立该样本作废)")
        print("=" * 88)

        op15(d)
        w = op09(d)
        print("  基准现场: DMA2S0_CR=0x%X(EN=%d) DMAMUX1_C8=%d MDMA_CCR=%d CTCR=0x%X"
              % (w[0], w[0] & 1, w[10], w[2], w[5]))
        report("① 交付状态 (基准)")

        print()
        print("  ── 拆 DMAMUX 路由 ──")
        op09(d, 0x00000000)                       # DMAMUX1_C8 = 0
        op10(d, 1, 1)
        report("② DMAMUX1_C8 = 0")
        op09(d, 0x00000016)                       # 恢复 22
        op10(d, 1, 1)

        print()
        print("  ── 拆 DMA2_S0 ──")
        w = op09(d, 0x00005000)                   # EN=0
        print("     (DMA2_S0_CR 回读 = 0x%X, EN=%d)" % (w[0], w[0] & 1))
        op10(d, 1, 1)
        report("③ DMA2_S0_CR.EN = 0")
        op09(d, 0x00005111)

        print()
        print("  ── 拆定时器侧门控 (DIER) ──")
        op10(d, 1, 1)
        d.send(C, bytes([0x08, 22]) + struct.pack("<I", 0x00000001))   # C8=22 + DIER=UIE only
        time.sleep(0.05)
        report("④ DIER = UIE (无 UDE/CC4DE)")
        d.send(C, bytes([0x08, 22]) + struct.pack("<I", 0x00000101))   # UIE|UDE
        op09(d, 0x00005111)

        print()
        print("  ── 清 DMA2_S0 的 TC 标志 ──")
        op10(d, 1, 1)
        op10(d, 0, 0x3D)
        report("⑤ 刚清掉 TCIF0 之后 (立即)")
        time.sleep(0.002)
        report("⑥ 清掉 TCIF0 之后 2ms")

        print()
        print("=" * 88)
        print("判读")
        print("=" * 88)
        print("  · 若 ②③④ 仍能锁存且延迟量级不变 ⇒ **请求与整座桥无关** ⇒ TSEL=8 在本芯片上")
        print("    对应的源不是 DMA2_S0_TC, 或该请求在桥在场外就被拉高。")
        print("  · 若 ②③④ 中有一项变成'无'或延迟跳到 ~50µs ⇒ 那一项就是请求的来源。")

        op15(d)
        op09(d, 0x00005111)
        op10(d, 0, 0x3D)
        op14(d, 0)
        d.send(C, bytes([0x01]))
        print("\n已恢复交付配置")
    finally:
        d.close()


if __name__ == "__main__":
    main()
