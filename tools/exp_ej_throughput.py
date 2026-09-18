#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-J: **行为判据** —— 分档真的把每条路由都轮到了吗?

## 为什么必须有这一步（结构对 ≠ 行为对）
修 `div2 相位溢出` 缺陷时, 我证明了**结构**判据:
  路由 period 的相位落在 0..63 · 桶表 cnt2[64..99] 全 0 · 扫描器选 tick%64
但**结构正确不等于引擎真的把每条路由都跑了**。
本项目纪律: 每个结论都要配一个**能失败的**判据。本脚本就是那个判据。

## 判据（用累计吞吐, 不是均值）
读 `OFF_ENG_ROUTES_LO`(0x381C) 与 `OFF_ENG_TICKS`(0x3820), 在**同一窗口**内取两次差:

    实测吞吐 = Δroutes_total / Δticks
    预测吞吐 = (Σ_t nrun(t)) / PERIOD      （由桶表算, 结构侧）

★★ 2026-09-18 修正（第一版判据写错了周期）
  第一版把 div2 的预测写成 **1.28 = 128/100** —— **错**。
  错在: 扫描器选相位用的是 `tick % BUCKET_DIV2_PHASES_USED` = **`tick % 64`**
        ⇒ **一个完整周期是 64 拍, 不是 100 拍**。
  实测桶表: 64 个相位 × 每个 **恰好 2** 条 = 128 ⇒ 每拍 `nrun = 2`
        ⇒ 吞吐 = 128/64 = **2.00**, 与实测 **2.0000** 吻合。
  ⇒ 正确的预测式: `吞吐 = (每相位条数 × 相位数) / 相位数 = 每相位条数`
     对"条数 = 相位数 × k"的整齐配置, 吞吐直接 = **k**。

  J-1 ★ |实测 − 预测| / 预测 ≤ **5%**
  J-2 **反证（关键）**: div0 n=128 的吞吐必须**恰好 = 128.00**
  J-3 div1 n=128 吞吐 = 128/10 = **12.80**
  J-4 ★ 三条**同时**成立才算通过

## 为什么用"累计量"而不是"瞬时 nrun 均值"
本项目 §5.27/§5.28: **极值/分位数不免疫采样拍频, 累积量才免疫**。
累计吞吐是累积量 ⇒ 不受读数时刻影响。
"""
import os, re, struct, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst = 0x38, 0x10, 0x12, 0x11, 0x22
OFF_ROUTES_LO, OFF_TICKS = 0x381C, 0x3820
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02


def mk(op, div, n):
    fl = FLAG_ACTIVE | FLAG_WIRE2
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  fl, i, (i % 64) + 1, 0, i, div, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    states = b"\x00" * (16 * (min(n, 64) + 1))
    return struct.pack("<HHH", n, n, min(n, 64) + 1) + routes + params + states + b"\x00" * 16


def rd(dcl, addr, nwords):
    out, off = b"", 0
    while off < nwords:
        k = min(200, nwords - off)
        sts, p = dcl.send(cmd_burst, struct.pack("<IH", addr + 4 * off, k), expect_len=None)
        if sts != "ACK" or len(p) < 4 * k:
            return None
        out += p[:4 * k]
        off += k
    return out


def main():
    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("端口 = %s" % dcl.port); time.sleep(1.0)
    sts, p = dcl.send(cmd_status, expect_len=51)
    if sts != "ACK" or len(p) < 51:
        print("!! 0x38 失败"); return 2
    SHM = struct.unpack("<I", p[23:27])[0]
    print("SHM = 0x%08X" % SHM)
    ok = 0
    for _ in range(10):
        v = rd(dcl, SHM + 0x3880, 2)
        if v and struct.unpack("<2I", v[:8])[1] >= 200000:
            ok += 1
            if ok >= 3:
                break
        else:
            ok = 0
        time.sleep(0.25)
    if ok < 3:
        print("!! 板子不健康"); dcl.close(); return 2
    print("健康门通过\n")

    def throughput(op, div, n, expect, tag):
        sts, pp = dcl.send(cmd_deploy, mk(op, div, n), expect_len=None)
        if sts != "ACK":
            print("  %-20s deploy 被拒" % tag); return None
        dcl.send(cmd_stop); time.sleep(0.10)
        dcl.send(cmd_start); time.sleep(0.10)
        time.sleep(0.8)
        a = rd(dcl, SHM + OFF_ROUTES_LO, (OFF_TICKS + 4 - OFF_ROUTES_LO) // 4)
        time.sleep(1.5)
        b = rd(dcl, SHM + OFF_ROUTES_LO, (OFF_TICKS + 4 - OFF_ROUTES_LO) // 4)
        if not (a and b):
            print("  %-20s 读失败" % tag); return None
        r1, t1 = struct.unpack("<2I", a[:8])
        r2, t2 = struct.unpack("<2I", b[:8])
        dr, dt = r2 - r1, t2 - t1
        if dt <= 0:
            print("  %-20s Δticks=%d ⇒ 判无效" % (tag, dt)); return None
        got = dr / float(dt)
        dev = (got - expect) / expect * 100
        flag = "✅" if abs(dev) <= 5 else "★超差"
        print("  %-20s Δroutes=%-9d Δticks=%-7d 吞吐=**%.4f**  预测 %.2f  偏差 %+.2f%% %s"
              % (tag, dr, dt, got, expect, dev, flag))
        return got

    print("=== E-J 行为判据: 实测吞吐 vs 结构预测 ===")
    print("    预测: div0 = n/1 ; div1 = n/10 ; div2 = n/64  （周期 = 相位数, 不是 100）")
    g0 = throughput(0, 0, 128, 128.00, "div0 n=128 (反证)")
    g1 = throughput(0, 1, 128, 12.80, "div1 n=128")
    g2 = throughput(0, 2, 128, 2.00, "div2 n=128 (修复目标)")
    g2b = throughput(5, 2, 128, 2.00, "div2 n=128 PID")
    g2c = throughput(0, 2, 64, 1.00, "div2 n=64 (各相位 1 条)")

    print("\n=== 判定 ===")
    res = []
    if g0 is not None:
        res.append(("J-2 ★ 反证 div0 恰好 128.00（±5%）", abs(g0 - 128.0) / 128.0 <= 0.05))
    if g1 is not None:
        res.append(("J-3 div1 = 12.80（±5%）", abs(g1 - 12.8) / 12.8 <= 0.05))
    if g2 is not None:
        res.append(("J-1 ★ div2 = 2.00（±5%）", abs(g2 - 2.0) / 2.0 <= 0.05))
    if g2c is not None:
        res.append(("J-5 div2 n=64 = 1.00（每条相位恰好 1 条）", abs(g2c - 1.0) / 1.0 <= 0.05))
    for k, v in res:
        print("  [%s] %s" % ("PASS" if v else "FAIL", k))
    if res and all(v for _, v in res):
        print("\n  ⇒ **全过**: 分档确实把每条路由都轮到了 —— 修复在**行为**上也成立。")
    else:
        print("\n  ⇒ 有项未过。★ 先看 J-2: 若连 div0 都不是 128.00, 说明**判据本身**坏了,"
              "而不是引擎坏了（§5.41 那类: 判据口径错会伪装成被测对象坏）。")
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
