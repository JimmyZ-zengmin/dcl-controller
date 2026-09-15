#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_latch_attrib2.py — 用**判据健全**的探针重做 P0-2 归因

为什么要有 v2
-------------
v1（以及之前几轮）用的延迟判据是：
    写影子=0 → 再写影子=1 → 量"ODR 变成 1"的耗时
**它漏了一步校验**：如果"写 0"那一步根本没生效（ODR 卡在 1），
那么"写 1"会立刻看到 ODR==1 ⇒ **假通过**（还给出一个很小很漂亮的延迟）。

★ 这正是本项目最恨的那类错误：**判据不能失败 —— 它实际不存在**。
  修法：每一步都验值。`写0 ⇒ OD==0` **且** `写1 ⇒ OD==1`，两者都成立才算这个样本有效。
  ⇒ 结论：**"清掉 TCIF0 不影响锁存"这条排除，是基于坏判据的，必须重做。**

本轮要答的问题
--------------
1. TSEL 到底有没有选择性？（v1 说"全都不影响"，与 v2 复核矛盾）
2. **MDMA 的请求是不是 `DMA2_S0` 的 TC 标志电平**（该标志从来没人清）
   · 清掉 TCIF0 后：锁存变得"要等很久"（等下一拍）⇒ **成立**（根因找到）
   · 清掉 TCIF0 后：延迟不变 ⇒ 不成立
3. `CLAR=0` 时通道是否真的自停（EN=0）
"""
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from h723_client import Dcl   # noqa: E402

C = 0x39
CT = 0x0000000A
CTBR = 0x00010008
CLAR_SELF = 0x24003100


def op14(d, v):
    sts, p = d.send(C, bytes([0x0E]) + struct.pack("<I", v))
    if sts != "ACK" or len(p) < 24:
        return None, None, None
    w = struct.unpack("<6I", p[:24])
    return w[2], w[4], w[5]          # got, lat, dwt_valid


def op15(d, ctbr=CTBR, clar=CLAR_SELF, ctcr=CT, bndt=2, csar=0, cdar=0):
    d.send(C, bytes([0x0F]) + struct.pack("<7I", ctcr, 0, ctbr, bndt, clar, csar, cdar))
    time.sleep(0.08)
    w = struct.unpack("<12I", d.send(C, bytes([0x09]))[1][:48])
    return dict(mccr=w[2], mcisr=w[3], cbndtr=w[4], ctcr=w[5], ctbr=w[6], clar=w[9])


def sample(d):
    """判据健全的锁存延迟样本。

    ★ 必须先确认 `写0 ⇒ ODR==0`；否则 ODR 卡在 1 会让后面一步假通过。
    返回 (lat, 说明)：lat=None 表示这个样本无效/没锁存。
    """
    g0, _, dv0 = op14(d, 0x0000)
    if dv0 == 0:
        return None, "DWT无效"
    if g0 != 0x0000:
        return None, "写0无效(ODR=0x%04X, 通道没在搬)" % (g0 or 0)
    g1, lat, _ = op14(d, 0x0001)
    if g1 != 0x0001:
        return None, "写1没锁存"
    return lat, "ok"


def run(d, n=5):
    out = []
    for _ in range(n):
        lat, why = sample(d)
        out.append(lat if lat is not None else -1)
        time.sleep(0.002)
    ok = [x for x in out if x >= 0]
    return (sum(ok) // len(ok) if ok else None), out


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM18"
    d = Dcl(port, wait=0.5)
    try:
        d.send(C, bytes([0x07]))
        d.send(C, bytes([0x00]))
        time.sleep(0.3)
        _, _, dv = op14(d, 1)
        if dv == 0:
            print("✗ DWT 未在走")
            return

        print("=" * 84)
        print("测试 1  TSEL 的选择性（判据健全版）")
        print("=" * 84)
        print("  %-10s %-10s %-14s %s" % ("写入TSEL", "读回TSEL", "锁存延迟", "判定"))
        for t in (0, 1, 5, 6, 7, 8, 9, 20, 40, 63):
            s = op15(d, ctbr=0x00010000 | t)
            avg, raw = run(d, 4)
            print("  %-10d %-10d %-14s %s"
                  % (t, s["ctbr"] & 0xFF,
                     ("%d cyc" % avg) if avg is not None else "无",
                     ("✓ 会搬 (自循环 %d ns)" % (avg * 2.5 / 1000)) if avg is not None
                     else "✗ 不搬"))

        print()
        print("=" * 84)
        print("测试 2  ★ 清掉 DMA2_S0 的 TCIF0 之后, 锁存还跟得上吗?")
        print("=" * 84)
        op15(d)
        avg0, raw0 = run(d, 6)
        print("  清标志之前: 延迟均值 %-10s  原始 %s" % (
            ("%s cyc" % avg0) if avg0 is not None else "无", raw0))
        for k in range(3):
            d.send(C, bytes([0x0A, 0]) + struct.pack("<I", 0x3D))   # 清 DMA2_S0 全部标志
            time.sleep(0.01)
            avg, raw = run(d, 6)
            print("  第%d次清标志后: 延迟均值 %-10s  原始 %s"
                  % (k + 1, ("%s cyc" % avg) if avg is not None else "无", raw))
        print()
        print("  ⇒ 若延迟跳到 ~50µs 量级(等下一拍) ⇒ **请求就是 TCIF0 电平, 根因确定**")
        print("  ⇒ 若延迟仍是 ~85cyc        ⇒ 不是它")

        print()
        print("=" * 84)
        print("测试 3  CLAR=0 时通道是否自停 (EN 是否被硬件清 0)")
        print("=" * 84)
        s = op15(d, clar=0)
        time.sleep(0.3)
        w = struct.unpack("<12I", d.send(C, bytes([0x09]))[1][:48])
        avg, raw = run(d, 4)
        print("  CLAR=0: EN=%d CBNDTR=%d CLAR=0x%X" % (w[2] & 1, w[4], w[9]))
        print("          判据健全的样本: %s" % (("均值 %d cyc" % avg) if avg is not None else "全部无效"))
        print("          ⇒ 若全部无效 ⇒ 通道真自停了 (EN=0 后不搬) ⇒ 与 'EN=0 也照搬' 的旧结论矛盾")
        print("             (旧结论正是坏判据造成的假象)")

        op15(d)
        op14(d, 0)
        d.send(C, bytes([0x01]))
        print("\n已恢复交付配置")
    finally:
        d.close()


if __name__ == "__main__":
    main()
