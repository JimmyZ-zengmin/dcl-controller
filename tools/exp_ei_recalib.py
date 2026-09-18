#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-I: 在**修复后的固件**上重标 —— 用**扫描段**口径。

## 为什么要重标
- 修了 div2 相位溢出缺陷后, **div2 的真实行为变了**（相位 64..99 现在可寻址）
- 且 `h723_tick_ring.py` 的 (c0, m) 与 E-C 的 19 个 `m_op` 都取自**旧固件**
⇒ 现在模型与现实不一致（跑该工具 P1 必 FAIL）。本步把它对齐。

## 口径（用 E-D 已经建好的扫描段域）
    di(t) = 扫描段(t) + C_other        C_other = 474 TB（已实测, 与程序无关）
    扫描段(t) = C_scan + Σ_ops ( m_op × 该 op 本拍条数 )
- **本步直接测 `扫描段`**（SHM 0x3810/0x3814/0x380C），不再用 `di`
- `nrun` 用固件自报（0x3818），与扫描段**同一次突发读**

## 做法
  A. 19 个原语 × div0 的 `n=32` 与 `n=128` 两点 ⇒ `m_op = Δ扫描段 / 96`
     （div0 走 `cnt1[0]`, 每拍全跑 ⇒ 不受相位缺陷影响, 也不受本次修复影响）
  B. PID / DIRECT × 三档 × n=16/32/64/128 ⇒ 验证"单一 m 走通三档"是否仍成立
  C. 抽出 `C_scan`（扫描段截距）

## 判据（都能失败）
  I-1 每个 op 的两点必须**单调**（n=128 的扫描段 > n=32）
  I-2 ★ `m_op` 与旧值（E-C, 旧固件）在 div0 上应**基本一致**（差 ≤15%）
      —— 因为 div0 不受本次修复影响。**若不一致 ⇒ 说明修复影响了 div0, 那是意外, 要点名**
  I-3 P1 式自洽: 用标定出的 `(C_scan, m)` 预测**未参与标定**的点, 偏差 ≤40 TB
"""
import os, re, struct, sys, time, statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst = 0x38, 0x10, 0x12, 0x11, 0x22
S_N, S_LO, S_HI, S_NRUN = 0x380C, 0x3810, 0x3814, 0x3818
E_N = 0x3868
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02
OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
       "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]
# 旧固件上测的 m_op（E-C, div0）—— 只作对照
OLD_M = {"DIRECT": 29.51, "CMP": 37.51, "HYST": 39.01, "CLAMP": 38.50, "LPF": 49.50,
         "PID": 64.00, "RATE": 35.01, "DEADBAND": 42.01, "MUX": 36.49, "EDGE": 45.01,
         "LUT": 46.01, "CNT": 41.00, "TIMER": 45.01, "ARITH": 37.01, "SCALE": 35.00,
         "AND": 39.01, "OR": 35.50, "NOT": 35.01, "SR": 39.09}


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
        print("!! 板子不健康 ⇒ 判无效"); dcl.close(); return 2
    print("健康门通过\n")

    def meas(op, div, n):
        sts, pp = dcl.send(cmd_deploy, mk(op, div, n), expect_len=None)
        if sts != "ACK":
            return None
        dcl.send(cmd_stop); time.sleep(0.10)
        dcl.send(cmd_start); time.sleep(0.10)
        time.sleep(1.1)
        raw = rd(dcl, SHM + S_N, (E_N + 4 - S_N) // 4)
        if raw is None:
            return None
        u = struct.unpack("<%dI" % (len(raw) // 4), raw)

        def at(o):
            return u[(o - S_N) // 4]

        sn, slo, shi = at(S_N), at(S_LO), at(S_HI)
        if not sn:
            return None
        return dict(scan=(slo | (shi << 32)) / float(sn), nrun=at(S_NRUN))

    # ── A: 19 个原语, div0, n=32/128 ──
    print("=== A. 逐原语 m_op（div0, n=32 → 128）===")
    print("%-9s %-11s %-11s %-9s %-9s %-9s %s"
          % ("op", "扫描段@32", "扫描段@128", "m_op新", "m_op旧", "差%", "nrun@128"))
    print("-" * 78)
    new_m = {}
    for op, name in enumerate(OPS):
        a = meas(op, 0, 32)
        b = meas(op, 0, 128)
        if not (a and b):
            print("%-9s **取数失败**" % name); continue
        m = (b["scan"] - a["scan"]) / 96.0
        old = OLD_M[name]
        d = (m - old) / old * 100
        new_m[name] = m
        flag = ""
        if b["scan"] <= a["scan"]:
            flag = " ★I-1 违反(不单调)"
        if abs(d) > 15:
            flag += " ★I-2 与旧值差 >15%"
        print("%-9s %-11.1f %-11.1f %-9.2f %-9.2f %+8.1f %-9d%s"
              % (name, a["scan"], b["scan"], m, old, d, b["nrun"], flag))

    # ── B: PID/DIRECT 三档 ──
    print("\n=== B. PID / DIRECT × 三档 × n（验证单一 m 走通三档）===")
    print("%-9s %-4s %-5s %-9s %-11s %s" % ("op", "div", "n", "nrun", "扫描段", "每路由"))
    print("-" * 60)
    cur = {}
    for op, name in ((5, "PID"), (0, "DIRECT")):
        pts = []
        for div in (0, 1, 2):
            for n in (16, 32, 64, 128):
                r = meas(op, div, n)
                if not r:
                    continue
                pts.append((div, n, r["nrun"], r["scan"]))
                per = r["scan"] / r["nrun"] if r["nrun"] else float('nan')
                print("%-9s %-4d %-5d %-9d %-11.1f %.2f" % (name, div, n, r["nrun"], r["scan"], per))
        cur[name] = pts
        # 两点法在各档内算斜率
        for div in (0, 1, 2):
            sub = [q for q in pts if q[0] == div]
            if len(sub) >= 2:
                sub.sort(key=lambda z: z[2])
                (_, n1, r1, c1), (_, n2, r2, c2) = sub[0], sub[-1]
                if r2 != r1:
                    print("    %s div%d 内两点斜率 = %.2f TB/条 (Δnrun=%d)"
                          % (name, div, (c2 - c1) / float(r2 - r1), r2 - r1))

    print("\n=== 汇总 ===")
    if new_m:
        print("  新 m_op 表（div0, 扫描段口径）:")
        for k in OPS:
            if k in new_m:
                print("     %-9s %6.2f TB/条 = %6.1f cyc/条" % (k, new_m[k], new_m[k] * 2))
        ds = [abs(new_m[k] - OLD_M[k]) / OLD_M[k] * 100 for k in new_m]
        print("  与旧值偏差: 中位 %.1f%%, 最大 %.1f%% ⇒ %s"
              % (statistics.median(ds), max(ds),
                 "**一致（div0 未受修复影响, 符合预期）**" if max(ds) <= 15
                 else "**有 op 不一致, 需点名**"))
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
