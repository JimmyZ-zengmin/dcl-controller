#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_i2c_gate_test.py —— **G6-2 验收**：I2C 总线独占门（契约 §3.6 约束③ / §3.7 G6-2）

## 这条判据在验什么
I2C 是**一条共享总线**，有两个驱动者：
  · **阻塞路径** `i2c_bb_*`（主循环，一次读 ≈250µs）
  · **状态机**   `i2c_sm` （拍 ISR 里推进，一次事务跨 ~8 拍 ≈800µs）
两者的时间窗**必然交叠** ⇒ 同一对引脚上两个写者（违反公理②）。本项目已因"一条线两个写者"栽过两次。

## 为什么需要 `0x39 op=22`
真实的交叠窗口只有 250~800 µs，**从串口根本抓不到** ⇒ 没有人工占用口，这条判据
**永远无法执行**（"判据不能失败 = 判据不存在"）。`op=22` 就是为此设的：
它**占住门**（有界, 到期自动放），让我们能稳定地复现"占用期再申请"。

## 判据（每条都能失败）
| # | 判据 | 失败长什么样 |
|---|---|---|
| G1 | `op=22 sub=0` 能占住（owner=BLOCKING, `g_i2c_bus_busy_n` 不变） | 占不住 ⇒ 门没接线 |
| G2 | ★ **占用期 `op=20 sub=0`（状态机请求）必须被拒**：`status=6(GATE_BUSY)` 且 `gate_n` +1 | 请求成功 ⇒ **门是空的**（这条最关键）|
| G3 | 释放后**状态机立刻可用**（status=OK 且读数与阻塞路径一致） | 放门没生效 ⇒ 总线被永久占住 |
| G4 | 整个过程中 AS5600 阻塞路径**不受影响**（tx 在涨、nak 不涨） | 门把正常业务也挡了 |
| G5 | 占用**有界**：不显式释放时，到期后 owner 自动回到 NONE | 忘了放就永久占死 |

## 用法
    python tools/h723_i2c_gate_test.py [--port COM21] [--hold 400]
退出码：0 全过；2 有判据失败；1 环境/协议错误。
"""
import argparse
import struct
import sys
import time

sys.path.insert(0, "tools")
from h723_client import Dcl                                     # noqa: E402

OP22 = 22          # 0x39 op=22: 总线门诊断
SUB_HOLD, SUB_REL, SUB_QUERY = 0, 1, 2
OP20 = 20          # 0x39 op=20: I2C 状态机
SUB_SM_FIRE, SUB_SM_QUERY = 0, 1
OP18 = 18          # 0x39 op=18: AS5600 运行态（阻塞路径）


def op22(d, sub, arg=0):
    sts, p = d.send(0x39, struct.pack("<BBI", OP22, sub, arg))
    if sts != "ACK" or len(p) < 36:
        return None
    f = struct.unpack("<9I", p[:36])
    return dict(got=f[0], owner=f[1], busy_n=f[2], sm_gate_n=f[3], refs=f[8],
                sm_status=f[4], sm_phase=f[5], bb_tx=f[6], bb_nak=f[7])


def sm(d, sub):
    sts, _ = d.send(0x39, struct.pack("<BBI", OP20, sub, 0))
    if sts != "ACK":
        return None
    sts, p = d.send(0x39, struct.pack("<BBI", OP20, SUB_SM_QUERY, 0))
    if sts != "ACK" or len(p) < 48:
        return None
    f = struct.unpack("<12I", p[:48])
    return dict(status=f[0], phase=f[1], pcnt=f[2], tcnt=f[3], req=f[4], ok=f[5],
                nak=f[6], stuck=f[7], gate=f[8], ln=f[10], data=f[11])


def as5600(d):
    sts, p = d.send(0x39, struct.pack("<BBI", OP18, 0, 0))
    if sts != "ACK" or len(p) < 40:
        return None
    f = struct.unpack("<10I", p[:40])
    return dict(raw=f[0], ok=f[4], err=f[5], tx=f[7], i2c_ok=f[8], nak=f[9])


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                            # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--hold", type=int, default=400, help="占用拍数（默认 400 = 40ms）")
    a = ap.parse_args()

    d = Dcl(a.port)
    print(f"端口 = {d.port}")
    fails = []

    blk0 = as5600(d)
    q0 = op22(d, SUB_QUERY)
    print(f"起点: owner={q0['owner']} busy_n={q0['busy_n']}  AS5600 tx={blk0['tx']} nak={blk0['nak']}")

    # ── G1 占住 ──
    h = op22(d, SUB_HOLD, a.hold)
    ok_g1 = (h is not None and h["got"] == 1 and h["owner"] == 1)
    print(f"[{'PASS' if ok_g1 else 'FAIL'}] G1 占住总线: got={h['got']} owner={h['owner']}(应 1=BLOCKING)")
    if not ok_g1:
        fails.append("G1 占不住")

    # ── G2 ★ 占用期状态机请求必须被拒 ──
    r_sm = sm(d, SUB_SM_FIRE)
    q1 = op22(d, SUB_QUERY)
    ok_g2 = (r_sm is not None and r_sm["status"] == 6
             and q1["sm_gate_n"] > q0["sm_gate_n"] and q1["busy_n"] > q0["busy_n"])
    print(f"[{'PASS' if ok_g2 else 'FAIL'}] G2 ★占用期 SM 请求被拒: "
          f"status={r_sm['status']}(应 6=GATE_BUSY) gate_n {q0['sm_gate_n']}→{q1['sm_gate_n']} "
          f"busy_n {q0['busy_n']}→{q1['busy_n']}")
    if not ok_g2:
        fails.append("G2 门没拦住状态机")

    # ── G4 阻塞路径不受影响（占用是"给阻塞路径预留", 同 owner 可重入）──
    time.sleep(0.05)
    blk1 = as5600(d)
    ok_g4 = (blk1["tx"] > blk0["tx"] and blk1["nak"] == blk0["nak"])
    print(f"[{'PASS' if ok_g4 else 'FAIL'}] G4 阻塞路径照常: tx {blk0['tx']}→{blk1['tx']} "
          f"nak {blk0['nak']}→{blk1['nak']}")
    if not ok_g4:
        fails.append("G4 门把正常业务也挡了")

    # ── G3 释放后状态机立刻可用 ──
    op22(d, SUB_REL)
    sm(d, SUB_SM_FIRE)          # 发起（事务跨 8 拍 ≈800µs）
    time.sleep(0.02)            # 等 20ms ≫ 800µs
    r_sm2 = sm(d, SUB_SM_QUERY)
    blk2 = as5600(d)
    sm_raw = ((r_sm2["data"] & 0xFF) << 8) | ((r_sm2["data"] >> 8) & 0xFF)
    ok_g3 = (r_sm2["status"] == 2 and r_sm2["ln"] == 2 and sm_raw == blk2["raw"])
    print(f"[{'PASS' if ok_g3 else 'FAIL'}] G3 释放后 SM 恢复: status={r_sm2['status']}(应 2) "
          f"len={r_sm2['ln']} 读数={sm_raw} vs 阻塞={blk2['raw']}")
    if not ok_g3:
        fails.append("G3 放门没生效")

    # ── G5 占用有界：再占一次, 不显式释放, 等到期后必须自动回 NONE ──
    op22(d, SUB_HOLD, 100)                      # 100 拍 = 10ms
    q2 = op22(d, SUB_QUERY)
    time.sleep(0.4)                             # ≫ 主循环一圈
    q3 = op22(d, SUB_QUERY)
    ok_g5 = (q2["owner"] == 1 and q3["owner"] == 0)
    print(f"[{'PASS' if ok_g5 else 'FAIL'}] G5 占用有界(自动放): 立刻 owner={q2['owner']} "
          f"→ 0.4s 后 owner={q3['owner']}(应 0)")
    if not ok_g5:
        fails.append("G5 占用没有自动释放")

    d.close()
    print()
    if fails:
        print("[FAIL] G6-2 未通过: " + "; ".join(fails))
        return 2
    print("[PASS] G6-2 通过: 占用期状态机被拒 + 阻塞路径不受影响 + 释放即恢复 + 占用有界")
    return 0


if __name__ == "__main__":
    sys.exit(main())
