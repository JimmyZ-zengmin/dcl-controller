#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_as5600_bind.py —— 把 AS5600 写进**具名设备绑定表**，让 `sensor[0]/[1]` 复活。

## 为什么需要它（2026-09-17 现场的根因）
③层（DCL 程序）读的是 `sensor[i]`。而 **`sensor[0]/[1]`（AS5600 的 raw / 角度）
不是硬连线的 —— 它靠"具名设备绑定表"（G6-4）里的一条
"每 N 拍读 addr7=0x36/reg=0x0C/len=2 → 写 SENSOR[0]" **在每个拍上回填**。

★★★ 实测：**这张表默认是空的**（`DB_N_VALID = 0`），而且**它随程序包持久化（GAP-11）**
⇒ 上传一个"只带算法、不带绑定"的 DCL 程序，**会把反馈源一起删掉** ⇒
`sensor[1]` 变成**陈旧的常数**、AS5600 状态机 `ok_n` **恒 0 且不再增长** —— 而**不报任何错**。

⇒ 后果：**闭环的反馈是死的，而现象看起来像"电机不转"**（我在这上面误判过一次）。
★ 区分手段：`asdiag`（**阻塞路径**，不依赖绑定表）**照样读到值** ⇒ 两条路一条活一条死。

## 用法
    python tools/h723_as5600_bind.py --port COM21          # 写入并验证状态机复活
    python tools/h723_as5600_bind.py --port COM21 --dry    # 只看当前表，不写

★ 判据（能失败）：写入后 1.5 s 内 `ok_n` **必须增长**；`SENSOR[0]` **必须与阻塞路径读数一致**（±1 LSB）。
"""
import os
import struct
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h723_dev_bind_test as D          # noqa: E402  ← **复用**它的 FNV/entry/submit/wait，不重写

AS5600_ADDR7 = 0x36
AS5600_RAW_REG = 0x0C          # RAW_ANGLE（12 位在 [11:0]）
SENSOR_RAW_SLOT = 0            # src/as5600.h: AS5600_SENSOR_RAW = 0


def main():
    port = None
    if "--port" in sys.argv:
        port = sys.argv[sys.argv.index("--port") + 1]
    dry = "--dry" in sys.argv
    b = D.Board(port or "COM21")
    try:
        if not b.locate_shm():
            print("✗ 读不到 g_shm（0x38 无应答）⇒ 无法定位 SHM")
            return 1
        n0 = b.rd(D.DB_N_VALID, 1)[0]
        e0 = b.rd(D.DB_ENTRIES, 1)[0]
        print("表状态: N_VALID=%d  ENTRIES[0]=0x%08X  PERIOD=%d" % (n0, e0, b.rd(D.DB_PERIOD, 1)[0]))
        s18 = lambda: struct.unpack("<10I", b.send(0x39, bytes([19, 18]))[1][:40])
        a = s18()
        print("AS5600 状态机: ok_n=%d err_n=%d raw=%d" % (a[4], a[5], a[1]))
        if dry:
            return 0

        ents = [0] * D.DB_SLOTS
        ents[0] = D.entry(1, SENSOR_RAW_SLOT, 2, AS5600_RAW_REG, AS5600_ADDR7)
        crc = D.db_crc_words(ents)
        seq = (b.rd(D.DB_REQ_SEQ, 1)[0] or 0) + 1
        print("写入: ENTRIES[0]=0x%08X  CRC=0x%08X  SEQ=%d  PERIOD=%d"
              % (ents[0], crc, seq, D.DB_PERIOD_DEF))
        ok, err = D.submit(b, ents, seq, mode="words", period=D.DB_PERIOD_DEF)
        if err:
            print("✗ 提交被拒: %s" % err)
            return 1
        if not D.wait_done(b, seq):
            print("✗ 等 done_seq 超时")
            return 1
        n1 = b.rd(D.DB_N_VALID, 1)[0]
        print("提交后: N_VALID=%d  REJECT=%d" % (n1, b.rd(D.DB_REJECT, 1)[0]))
        print("  [%s] N_VALID 从 0 变为非 0（表被受理）" % ("PASS" if n1 > 0 else "FAIL"))

        # ★★ 判据：状态机必须在 ~1.5 s 内开始成功（每 N 拍一次）
        t = s18()
        time.sleep(1.5)
        u = s18()
        grew = u[4] > t[4]
        print("  [%s] AS5600 状态机复活: ok_n %d → %d (err_n %d → %d)"
              % ("PASS" if grew else "FAIL", t[4], u[4], t[5], u[5]))
        # ★ 与阻塞路径交叉核对（两条独立路径）
        blk = struct.unpack("<24I", b.send(0x39, bytes([19, 0]) + struct.pack("<I", 0))[1][:96])[8]
        smraw = (u[1] & 0xFFF)
        dd = (smraw - blk) & 0xFFF
        if dd > 2048:
            dd -= 4096
        print("  [%s] 两条路径一致: 状态机 raw=%d vs 阻塞 raw=%d (Δ%+d, 允许 ±1 LSB 噪声底)"
              % ("PASS" if abs(dd) <= 1 else "FAIL", smraw, blk, dd))
        print()
        print("★ 现在 `sensor[0]`（raw）/`sensor[1]`（角度）**每 N 拍自动回填** ⇒ "
              "DCL 程序的 `SENSOR name FROM sensor[1]` 才有活的反馈。")
        print("★★ 记住：**新程序包必须自带这条绑定**（否则部署动作会把反馈源删掉）。")
        return 0 if (n1 > 0 and grew and abs(dd) <= 1) else 1
    finally:
        b.close()


if __name__ == "__main__":
    sys.exit(main())
