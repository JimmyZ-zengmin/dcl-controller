#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-E1: 用**扫描段**（而不是整段 ISR）重标模型。

## 为什么必须重标
E-D 证明 `di = 扫描段 + 474 TB`（每拍固定、与程序无关）。
⇒ E-A~E-C 的常数**全部是在"多算了 474 TB"的口径下标定的**。
斜率不受影响（常数相减约掉），但**截距与 c0_op 会被抬高 474 TB**。

## 本脚本做什么
直接把扫描段统计（`OFF_SCAN_CYC_SUM_LO/HI/N` @0x3810/0x3814/0x380C）
当成"每拍成本"来读，重做 E-A 的两参数拟合。

## 判据（都能失败）
  E-E1-1 用 div0+div1 标定 ⇒ 预测 div2，偏差 ≤ **40 TB（20 cyc）**
         （比 E-A 的判据严一倍 —— 口径干净了, 没有 474 TB 干扰）
  E-E1-2 ★ 自洽: `C_scan` 应 ≈ **583 TB**（E-A 在 di 口径下的截距）
         ★ 这条**反验**"474 TB 是常数"：若 C_scan 与 583 差 >100 TB ⇒ 474 不是常数
  E-E1-3 `m_op` 与 E-C 同量级（差 ≤15%）
"""
import os, re, struct, sys, time
import statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst = 0x38, 0x10, 0x12, 0x11, 0x22
# 扫描段域（engine.h: 0x3800 起）
S_LAST, S_MIN, S_MAX, S_N, S_LO, S_HI, S_NRUN = (0x3800, 0x3804, 0x3808,
                                                 0x380C, 0x3810, 0x3814, 0x3818)
E_LO, E_HI, E_N = 0x3860, 0x3864, 0x3868          # 整段 ISR（对照）
FLAGS = 0x01 | 0x02
SRC_CONST, DST_WIRE = 2, 2


def mk(op, div, n):
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  FLAGS, i, (i % 64) + 1, 0, i, div, 0) for i in range(n))
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

    # 健康门（同 h723_tick_ring）
    ok = 0
    for _ in range(6):
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
    print("健康门通过")

    pts = []
    print("\n%-9s %-4s %-5s %-8s %-11s %-11s %s"
          % ("op", "div", "n", "nrun", "扫描段(TB)", "整段(TB)", "其它"))
    for op, name in ((5, "PID"), (0, "DIRECT")):
        for div in (0, 1, 2):
            for n in (16, 32, 64, 128):
                sts, pp = dcl.send(cmd_deploy, mk(op, div, n), expect_len=None)
                if sts != "ACK":
                    print("%-9s %-4d %-5d deploy 被拒" % (name, div, n)); continue
                dcl.send(cmd_stop); time.sleep(0.12)
                dcl.send(cmd_start); time.sleep(0.12)
                time.sleep(1.2)
                # ★ 扫描段与整段**同一次突发**读（0x3800 .. 0x386B 含 E_N 那个字）
                #   ★ 第一版写成 `(E_N - S_LAST)//4` —— 差一个字: 区间是**半开**的,
                #     要读到 `E_N` 所在的那个 u32, 字数必须是 `(E_N + 4 - S_LAST)//4`。
                #     症状是 `IndexError: tuple index out of range`, 直接崩而不是给错数
                #     （算幸运 —— 少读多读在别处会**静默**给错值）。
                raw = rd(dcl, SHM + S_LAST, (E_N + 4 - S_LAST) // 4)
                if raw is None:
                    print("%-9s %-4d %-5d 读失败" % (name, div, n)); continue
                u = struct.unpack("<%dI" % (len(raw) // 4), raw)

                def at(o):
                    return u[(o - S_LAST) // 4]

                sn, slo, shi = at(S_N), at(S_LO), at(S_HI)
                elo, ehi, en = at(E_LO), at(E_HI), at(E_N)
                nrun = at(S_NRUN)
                if not sn or not en:
                    print("%-9s %-4s %-5s 无样本" % (name, div, n)); continue
                s_mean = (slo | (shi << 32)) / float(sn)
                e_mean = (elo | (ehi << 32)) / float(en)
                pts.append(dict(op=name, div=div, n=n, nrun=nrun,
                                scan=s_mean, isr=e_mean))
                print("%-9s %-4d %-5d %-8d %-11.1f %-11.1f %.1f"
                      % (name, div, n, nrun, s_mean, e_mean, e_mean - s_mean))

    if len(pts) < 8:
        print("\n点不足 ⇒ 判无效"); dcl.close(); return 2

    print("\n=== 用扫描段重标（逐 op, cost = C_scan + m × nrun）===")
    per = {}
    for name in ("PID", "DIRECT"):
        sub = [q for q in pts if q["op"] == name]
        if len(sub) < 3:
            continue
        A = [[1.0, float(q["nrun"])] for q in sub]
        y = [q["scan"] for q in sub]
        c = ols(A, y)
        res = [y[i] - (c[0] + c[1] * A[i][1]) for i in range(len(y))]
        per[name] = c
        ae = sorted(abs(x) for x in res)
        print("  %-7s C_scan=%7.1f TB (%.0f cyc)  m=%6.2f TB/条 (%.1f cyc/条)  "
              "残差 中位 %.1f / 最大 %.1f TB"
              % (name, c[0], c[0] * 2, c[1], c[1] * 2, statistics.median(ae), max(ae)))

    print("\n=== E-E1-2 ★ 自洽: C_scan 是否 ≈ 583 TB（E-A 在 di 口径下的截距）===")
    for name, c in per.items():
        dev = abs(c[0] - 583.0)
        print("  %-7s C_scan=%.1f  与 583 差 %.1f TB ⇒ %s"
              % (name, c[0], dev,
                 "**一致**（⇒ 474 TB 是常数的结论被反验）" if dev <= 100
                 else "**不一致** ⇒ 474 TB 非常数, 需复查"))

    print("\n=== E-E1-3 m_op 与 E-C 对比（差 ≤15%）===")
    EC = {"PID": 64.00, "DIRECT": 29.51}
    for name, c in per.items():
        ref = EC.get(name)
        if not ref:
            continue
        d = abs(c[1] - ref) / ref * 100
        print("  %-7s 本次 m=%.2f  E-C m=%.2f  差 %.1f%% ⇒ %s"
              % (name, c[1], ref, d, "**同量级**" if d <= 15 else "**变了, 需复查**"))

    # E-E1-1 留点预测
    print("\n=== E-E1-1 div0+div1 标定 ⇒ 预测 div2 ===")
    for name in ("PID", "DIRECT"):
        tr = [q for q in pts if q["op"] == name and q["div"] in (0, 1)]
        te = [q for q in pts if q["op"] == name and q["div"] == 2]
        if len(tr) < 3 or not te:
            continue
        A = [[1.0, float(q["nrun"])] for q in tr]
        y = [q["scan"] for q in tr]
        c = ols(A, y)
        print("  %s: 标定 C=%.1f m=%.2f" % (name, c[0], c[1]))
        bad = 0
        for q in te:
            pred = c[0] + c[1] * q["nrun"]
            dev = q["scan"] - pred
            if abs(dev) > 40:
                bad += 1
            print("     n=%-4d nrun=%6.2f 预测%8.1f 实测%8.1f 偏差%+8.1f %s"
                  % (q["n"], q["nrun"], pred, q["scan"], dev,
                     "" if abs(dev) <= 40 else "★超差"))
        print("     ⇒ %s（%d/%d 超 40 TB）"
              % ("**通过**" if bad == 0 else "**未通过**", bad, len(te)))
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
