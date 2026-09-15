#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_latch_sweep.py — MDMA 锁存链配置扫描 (P0-1 宽度 / P0-2 触发)

为什么要有这个脚本
------------------
`do.c` 原来把 `CTCR` 配成 `0x00020000`，注释说是"TRGM=BLOCK<<17"。
按官方 CMSIS `stm32h723xx.h` 解出来 bit17 是 `DBURST[17:15]`，真实配置是
**SSIZE/DSIZE=byte** ⇒ 4 字节搬运 = "把影子低字节往 ODR 低字节写 4 遍"
⇒ **PE8~PE15 锁存不到**，而 `DCL_DO_LATCH=1` 已关掉 CPU 直写通路。

★ 实测: **只把宽度改成 word 会直接 TEIF 传输错误**(CBRUR/TRGM/CLAR/TLEN 各种组合都试过)
  ⇒ 宽度不能单独改, 也可能 word 本身不是这条路该用的宽度。
  ⇒ 所以不要"猜一个值改一次宏烧一次片", 用 `0x39 op=15` 在**运行期**重配整条链，
    一次烧录扫完所有候选。

判据（每个都能失败）
--------------------
① **宽度**: 写影子 `0x0101` ⇒ 必须读回 `0x0101`（能排除"取值巧合"）；再验 `0xFFFF`。
② **TEIF**: `MDMA_CISR.TEIF` 置位 = 配置非法（硬件会把 `CCR.EN` 清掉）。
③ **触发/自由跑**: `op=14` 返回"从写影子到 ODR 生效"的延迟（DWT 周期, 2.5ns/cyc）。
   ★★ 必须用**确实会锁存**的值来测延迟（写 0x0000 → 再写 0x0001 制造真实跳变），
      否则宽度坏的档会一直等到超时，量出来的是超时值而不是锁存延迟 —— 第一版就踩了这个坑。
   - 延迟 ≲ 1 µs 且与 100 µs 拍长无关 ⇒ **MDMA 在自循环**（P0-2 未修）
   - 延迟落在 0~100 µs（平均约半拍）⇒ **真被拍请求驱动**（P0-2 已修）

★ 每次测量前必须 `op=7`（重新校时）—— 调试器会话会停掉 DWT_CYCCNT，
  那时 `lat` 会假报 0，而 0 恰好长得像"延迟极小"。op=14 返回 `dv` 位做自证。
"""
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from h723_client import Dcl   # noqa: E402

C = 0x39
CTBR_DEF = 0x00010008          # TSEL=8(DMA2_S0_TC) + SBUS=1(源在 TCM)
CLAR_SELF = 0x24003100         # 链表节点自身 ⇒ 自循环

SZ_BYTE, SZ_HW, SZ_W = 0, 1, 2
INC_DIS, INC_BYTE, INC_HW, INC_W = 0b00, 0b10, 0b10, 0b10


def ctcr(ssize, dsize, sinc, dinc, sincos=0, dincos=0, tlen=0, trgm=0, bursts=0):
    return ((ssize << 4) | (dsize << 6) | (sinc << 0) | (dinc << 2)
            | (sincos << 8) | (dincos << 10) | (bursts << 12) | (bursts << 15)
            | (tlen << 18) | (trgm << 28))


CTCR_ORIG = 0x00020000                                    # 原交付: byte + DBURST=4拍

CONFIGS = [
    ("① 原交付值(byte)",        CTCR_ORIG,                                0, 4, CLAR_SELF),
    ("② byte + 字节递增 BNDT=2", ctcr(SZ_BYTE, SZ_BYTE, INC_BYTE, INC_BYTE),  0, 2, CLAR_SELF),
    ("③ halfword 固定 BNDT=2",   ctcr(SZ_HW, SZ_HW, 0, 0),                 0, 2, CLAR_SELF),
    ("④ halfword 固定 BNDT=4",   ctcr(SZ_HW, SZ_HW, 0, 0),                 0, 4, CLAR_SELF),
    ("⑤ halfword + 半字递增",    ctcr(SZ_HW, SZ_HW, INC_HW, INC_HW, 0b01, 0b01), 0, 2, CLAR_SELF),
    ("⑥ word 固定 BNDT=4",       ctcr(SZ_W, SZ_W, 0, 0),                   0, 4, CLAR_SELF),
    ("⑦ word + 字递增 BNDT=4",   ctcr(SZ_W, SZ_W, INC_W, INC_W, 0b10, 0b10), 0, 4, CLAR_SELF),
    ("⑧ halfword + DBURST=2拍",  ctcr(SZ_HW, SZ_HW, 0, 0, bursts=1),       0, 2, CLAR_SELF),
]


def op14(d, v):
    sts, p = d.send(C, bytes([0x0E]) + struct.pack("<I", v))
    if sts != "ACK" or len(p) < 24:
        return None
    w = struct.unpack("<6I", p[:24])
    return dict(v=w[0], odr0=w[1], got=w[2], it=w[3], lat=w[4], dv=w[5])


def op15(d, c, cbrur, bndt, clar):
    pl = bytes([0x0F]) + struct.pack("<5I", c, cbrur, CTBR_DEF, bndt, clar)
    sts, p = d.send(C, pl)
    if sts != "ACK" or len(p) < 48:
        return None
    w = struct.unpack("<12I", p[:48])
    return dict(mccr=w[2], mcisr=w[3], cbndtr=w[4], ctcr=w[5], cbrur=w[10])


def measure_lat(d, samples=3):
    """写 0x0000 再写 0x0001 制造真实跳变, 量锁存延迟 (只取成功锁存到的样本)。"""
    lats = []
    for _ in range(samples):
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
        d.send(C, bytes([0x07]))       # 重新校时
        d.send(C, bytes([0x00]))       # 关引脚码型诊断 (别让它覆盖影子)
        time.sleep(0.2)
        probe = op14(d, 0x0001)
        if not probe or probe["dv"] == 0:
            print("✗ DWT 未在走 ⇒ 延迟判据不可用 (先确认 op=7)")
            return

        print("MDMA 锁存链配置扫描  (CTBR=0x%X)" % CTBR_DEF)
        print("%-24s %-26s %-8s %-6s %-14s %s"
              % ("配置", "CTCR 解码", "16位宽", "TEIF", "锁存延迟", "判定"))
        for name, c, cbrur, bndt, clar in CONFIGS:
            s = op15(d, c, cbrur, bndt, clar)
            if s is None:
                print("%-24s ✗ 无应答" % name)
                continue
            time.sleep(0.05)
            w1 = op14(d, 0x0101)
            w2 = op14(d, 0xFFFF)
            wide = bool(w1 and w1["got"] == 0x0101 and w2 and w2["got"] == 0xFFFF)
            teif = s["mcisr"] & 1
            lats = measure_lat(d)
            lat_s = ("%d cyc" % (sum(lats) // len(lats))) if lats else "—"
            dec = "SSIZE=%d DSIZE=%d SINC=%d DINC=%d" % (
                (s["ctcr"] >> 4) & 3, (s["ctcr"] >> 6) & 3,
                s["ctcr"] & 3, (s["ctcr"] >> 2) & 3)
            if teif:
                v = "✗ TEIF (配置非法)"
            elif not wide:
                v = "✗ 宽度不够"
            elif not lats:
                v = "✗ 压根没锁存"
            else:
                avg = sum(lats) // len(lats)
                if avg < 400:
                    v = "△ 宽度OK; 延迟 %d cyc ⇒ 仍自循环 (P0-2 未修)" % avg
                else:
                    v = "✓✓ 宽度OK + 延迟 %.1f µs ⇒ 拍驱动!" % (avg * 2.5 / 1000)
            print("%-24s %-26s %-8s %-6s %-14s %s"
                  % (name, dec, "✓" if wide else "✗", "1" if teif else "0", lat_s, v))

        op15(d, CTCR_ORIG, 0, 4, CLAR_SELF)      # 回交付默认
        op14(d, 0x0000)
        d.send(C, bytes([0x01]))
        print("\n已恢复: 原 CTCR / CBRUR=0 / BNDT=4 / 自循环, 影子=0, 诊断开")
    finally:
        d.close()


if __name__ == "__main__":
    main()
