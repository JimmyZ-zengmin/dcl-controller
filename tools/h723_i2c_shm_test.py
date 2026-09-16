#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_i2c_shm_test.py —— **G6-3 验收**：I2C 事务区的"请求-完成"握手（SHM 数据总线路径）

## 这一层解决什么
G6-1/G6-2 之后，I2C 事务只能靠 `0x39 op=20/22` 这对**诊断命令**触发与读取。
而契约 §3.6 约束④要求给使用者一个**就绪门**，§3.7 要求请求/结果住在 SHM。
⇒ 本区（`OFF_I2C_XACT = 0x7200`, 64B）让"要用 I2C 的外设能力"走**数据总线**，不依赖诊断脚手架。

## 握手（这就是就绪门）
```
使用者:  写 IX_REQ / IX_DATA，然后 IX_REQ_SEQ = 新序号(单调、非 0)
服务方:  主循环看到 IX_REQ_SEQ != IX_DONE_SEQ ⇒ 发起状态机事务
完成时:  回写 IX_STATUS/IX_DATA，最后 IX_DONE_SEQ = 该序号
```
★ **`IX_DONE_SEQ == 你写的序号` 才代表结果有效**；禁止假设"写完就有值"（事务跨 ~8 拍）。

## 判据（每条都能失败）
| # | 判据 | 失败长什么样 |
|---|---|---|
| S1 | `IX_MAGIC == 'IXAC'`（区存在自证） | 读不到 ⇒ 布局假设错，**后面的判据都不该信** |
| S2 | 发起读 → `DONE_SEQ` 推进到该序号、`STATUS=OK`、`DATA` 与阻塞路径读数一致 | 超时 ⇒ 服务没跑；值不等 ⇒ 搬错数据 |
| S3 | ★ **就绪门**：`IX_TICKS == 6+n`（事务**跨拍**，不是"写完即得"） | 若为 0/1 ⇒ 该判据为空，说明它其实没跨拍 |
| S4 | ★ **序号幂等**：重复写同一个序号 ⇒ **不会**再发起一次（`IX_REQ_N` 不变） | 计数增长 ⇒ 同一个请求被跑了两次 |
| S5 | ping（op=0）也走通（证明不是只读一种） | — |
| S6 | 经 SHM 的事务**不污染**阻塞路径（AS5600 `nak` 不涨、`tx` 照涨） | nak 涨 ⇒ 两条路在互相踩 |

## 用法
    python tools/h723_i2c_shm_test.py [--port COM21]
退出码：0 全过；2 有判据失败；1 环境/协议错误。
"""
import argparse
import struct
import sys
import time

sys.path.insert(0, "tools")
from h723_client import Dcl, engine_status                        # noqa: E402

IX = 0x7200           # OFF_I2C_XACT
O_MAGIC, O_REQ_SEQ, O_DONE_SEQ, O_STATUS, O_PHASE, O_TICKS = 0, 4, 8, 12, 16, 20
O_LAST_OK, O_REQ, O_DATA, O_REQ_N, O_OK_N, O_NAK_N = 24, 28, 32, 40, 44, 48

OP_PING, OP_READ = 0, 1
AS5600 = 0x36
RAW_REG = 0x0C


def rd(d, shm, off, n=1):
    sts, p = d.send(0x22, struct.pack("<IH", shm + IX + off, n))
    if sts != "ACK":
        return None
    return p


def rd32(d, shm, off):
    p = rd(d, shm, off, 1)
    return struct.unpack("<I", p[:4])[0] if p else None


def wr32(d, shm, off, val):
    sts, _ = d.send(0x21, struct.pack("<II", shm + IX + off, val))
    return sts == "ACK"


def as5600(d):
    sts, p = d.send(0x39, struct.pack("<BBI", 18, 0, 0))
    if sts != "ACK" or len(p) < 40:
        return None
    f = struct.unpack("<10I", p[:40])
    return dict(raw=f[0], err=f[5], tx=f[7], nak=f[9])


def do_xact(d, shm, seq, addr, op, reg, length, txdata=b""):
    """按握手发起一次事务并等完成。→ (status, data_bytes, ticks, timed_out)"""
    for i, b in enumerate(txdata):
        if not wr32(d, shm, O_DATA + i, b):
            return None
    req = (addr & 0xFF) | ((op & 0xFF) << 8) | ((reg & 0xFF) << 16) | ((length & 0xFF) << 24)
    if not wr32(d, shm, O_REQ, req):
        return None
    if not wr32(d, shm, O_REQ_SEQ, seq):
        return None
    for _ in range(60):                      # ≤0.6s, 远大于 800µs
        time.sleep(0.01)
        if rd32(d, shm, O_DONE_SEQ) == seq:
            st = rd32(d, shm, O_STATUS)
            tk = rd32(d, shm, O_TICKS)
            nbytes = length if op != OP_PING else 0
            blob = rd(d, shm, O_DATA, 2) or b"\x00\x00"
            return (st, blob[:nbytes] if nbytes else b"", tk, False)
    return (None, b"", None, True)


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                             # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    a = ap.parse_args()
    d = Dcl(a.port)
    s = engine_status(d)
    shm = s["shm"]
    print(f"端口 = {d.port}  SHM=0x{shm:08X}  IX@0x{shm + IX:08X}")

    fails = []

    # ── S1 magic ──
    mg = rd32(d, shm, O_MAGIC)
    ok_s1 = (mg == 0x43415849)
    print(f"[{'PASS' if ok_s1 else 'FAIL'}] S1 区存在自证: MAGIC=0x{mg:08X} (应 0x43415849 'IXAC')")
    if not ok_s1:
        print("  ⇒ MAGIC 不对就**不该继续相信后面的判据**（布局假设可能整个是错的）")
        return 2

    # ── S6 基线 ──
    b0 = as5600(d)

    # ── S2/S3 读一次 RAW ANGLE ──
    seq = (rd32(d, shm, O_REQ_SEQ) or 0) + 1
    st, blob, ticks, to = do_xact(d, shm, seq, AS5600, OP_READ, RAW_REG, 2)
    blk = as5600(d)
    val = ((blob[0]) << 8) | blob[1] if len(blob) == 2 else None
    ok_s2 = (not to and st == 2 and val == blk["raw"])
    print(f"[{'PASS' if ok_s2 else 'FAIL'}] S2 经 SHM 读: STATUS={st}(应2) DATA={val} "
          f"vs 阻塞路径={blk['raw']} done_seq={seq}")
    if not ok_s2:
        fails.append("S2 SHM 路径读数不符/超时")

    # ── S3 ★ 就绪门：事务必须**跨拍**（IX_TICKS 记录耗拍）──
    ok_s3 = (ticks == 6 + 2)
    print(f"[{'PASS' if ok_s3 else 'FAIL'}] S3 ★就绪门: IX_TICKS={ticks} (应 6+n={8}) "
          f"⇒ 事务跨 {ticks} 拍, 不是'写完即得值'")
    if not ok_s3:
        fails.append("S3 没跨拍(就绪门判据为空)")

    # ── S4 ★ 序号幂等：同一序号再写一次, 不应再发起 ──
    n0 = rd32(d, shm, O_REQ_N)
    wr32(d, shm, O_REQ_SEQ, seq)             # 重复同一个序号
    time.sleep(0.1)
    n1 = rd32(d, shm, O_REQ_N)
    ok_s4 = (n1 == n0)
    print(f"[{'PASS' if ok_s4 else 'FAIL'}] S4 ★序号幂等: 重写同一 seq ⇒ REQ_N {n0}→{n1}(应不变)")
    if not ok_s4:
        fails.append("S4 同一请求被跑了两次")

    # ── S5 ping 也走通 ──
    seq2 = seq + 1
    st2, _b, tk2, to2 = do_xact(d, shm, seq2, AS5600, OP_PING, 0, 0)
    ok_s5 = (not to2 and st2 == 2 and tk2 == 3)
    print(f"[{'PASS' if ok_s5 else 'FAIL'}] S5 ping 经 SHM: STATUS={st2} TICKS={tk2}(应3=START+TX+STOP)")
    if not ok_s5:
        fails.append("S5 ping 未走通")

    # ── S6 阻塞路径不被污染 ──
    b1 = as5600(d)
    ok_s6 = (b1["tx"] > b0["tx"] and b1["nak"] == b0["nak"] and b1["err"] == 0)
    print(f"[{'PASS' if ok_s6 else 'FAIL'}] S6 阻塞路径不受影响: tx {b0['tx']}→{b1['tx']} "
          f"nak {b0['nak']}→{b1['nak']} err={b1['err']}")
    if not ok_s6:
        fails.append("S6 两条路互相踩了")

    d.close()
    print()
    if fails:
        print("[FAIL] G6-3 未通过: " + "; ".join(fails))
        return 2
    print("[PASS] G6-3 通过: 数据总线路径可用 + 就绪门可判 + 序号幂等 + 阻塞路径无污染")
    return 0


if __name__ == "__main__":
    sys.exit(main())
