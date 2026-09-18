#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-I 的 B 段: PID / DIRECT × 三档 × n —— 用**扫描段**验证"单一 m 走通三档"。

## 为什么这段单独跑
主脚本 A 段（19 原语 × 2 点 = 38 次测量）已跑完, 结果: 新 m_op 与旧值差 **≤0.6%**。
B 段是另一组 24 次测量, 为免单次调用过长而拆开（上次日志被输出上限截断, 不是脚本崩）。

## 判据
  I-3 ★ 用 div0 的点标定 `(C_scan, m)`, 再**预测** div1/div2 的点, 偏差 ≤40 TB
      —— 这是"单一 m 走通三档"的直接检验
  I-4 `C_scan` 应与 C_scan 分解一小节实测的 **62.3 TB** 同量级（差 ≤40 TB）
  I-5 ★★ div2 的 `nrun` 分布应出现 **0**（修复前恒 ≥1）
      —— 这是修复在扫描段口径下的指纹
"""
import os, struct, sys, time, statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst = 0x38, 0x10, 0x12, 0x11, 0x22
S_N, S_LO, S_HI, S_NRUN = 0x380C, 0x3810, 0x3814, 0x3818
E_N = 0x3868
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


def ols(A, y):
    k = len(A[0])
    M = [[sum(A[i][a] * A[i][b] for i in range(len(A))) for b in range(k)]
         + [sum(A[i][a] * y[i] for i in range(len(A)))] for a in range(k)]
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-9:
            return None
        for r in range(k):
            if r != c:
                f = M[r][c] / M[c][c]
                for j in range(c, k + 1):
                    M[r][j] -= f * M[c][j]
    return [M[i][k] / M[i][i] for i in range(k)]


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

    data = {}
    print("\n%-9s %-4s %-5s %-8s %-11s %s" % ("op", "div", "n", "nrun", "扫描段", "每路由"))
    print("-" * 58)
    for op, name in ((5, "PID"), (0, "DIRECT")):
        data[name] = []
        for div in (0, 1, 2):
            for n in (16, 32, 64, 128):
                r = meas(op, div, n)
                if not r:
                    continue
                data[name].append((div, n, r["nrun"], r["scan"]))
                per = r["scan"] / r["nrun"] if r["nrun"] else float('nan')
                print("%-9s %-4d %-5d %-8d %-11.1f %.2f"
                      % (name, div, n, r["nrun"], r["scan"], per))

    print("\n=== 判据 ===")
    for name in ("PID", "DIRECT"):
        pts = data[name]
        if len(pts) < 6:
            print("  %s: 点不足" % name); continue
        div0 = [q for q in pts if q[0] == 0]
        rest = [q for q in pts if q[0] != 0]
        A = [[1.0, float(q[2])] for q in div0]
        y = [q[3] for q in div0]
        c = ols(A, y)
        print("\n  %s: 用 div0 标定 ⇒ C_scan=%.1f TB  m=%.2f TB/条" % (name, c[0], c[1]))
        bad = 0
        for div, n, nrun, scan in rest:
            pred = c[0] + c[1] * nrun
            dev = scan - pred
            if abs(dev) > 40:
                bad += 1
            print("     div%d n=%-4d nrun=%-4d 预测%9.1f 实测%9.1f 偏差%+9.1f%s"
                  % (div, n, nrun, pred, scan, dev, "" if abs(dev) <= 40 else "  ★超差"))
        print("     ⇒ I-3 %s（%d/%d 超 40 TB）"
              % ("**通过**" if bad == 0 else "**未通过**", bad, len(rest)))
        print("     I-4 C_scan=%.1f vs 62.3（C_scan 分解实测）⇒ 差 %.1f ⇒ %s"
              % (c[0], abs(c[0] - 62.3),
                 "同量级" if abs(c[0] - 62.3) <= 40 else "**不同量级, 要点名**"))
        zeros = [q for q in pts if q[2] == 0]
        print("     I-5 div2 出现 nrun=0 的配置数 = %d ⇒ %s"
              % (len(zeros),
                 "**通过**（修复前恒 ≥1）" if zeros else "**未观察到 0**"))
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
