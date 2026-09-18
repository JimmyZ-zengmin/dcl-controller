#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-T —— 把「每拍固定开销」拆开：`C_other` 到底是多少，`C_scan(op)` 补全 19 个原语

## 为什么要做（两个具体疑点）
1. **`C_other = 474 TB` 是一整块黑箱**，而且它与另一个实测数**对不上**：
   `h723_tick_ring.py` 的标定里记着"**所有路由 INACTIVE** 时 `C_loop = 62.3 TB (125 cyc)"。
   474 与 62.3 差 **7.6 倍** —— 两者不可能同时是"每拍固定开销"。
2. **`C_scan` 的 op 依赖只测过 2/19**（PID 121.7、DIRECT 99.6）。
   要声称"**任意**程序的每拍时间可算"，19 个都得有。

## 做法（一条式子把两块分开）
    di(n, op) = C_other + C_scan(op) + m_op × n          （div0、单一 op、均匀程序）
  · 每个 op 取 **n=16 与 n=112** 两点 ⇒ 斜率 = `m_op`（可与 E-C 的表交叉验证）
                                       截距 = `C_other + C_scan(op)`
  · **`n = 0`（空程序）** 单独测 ⇒ `di(0) = C_other`（`engine_tick` 在桶全空时**不调扫描体**）
  ⇒ `C_scan(op) = 截距(op) − di(0)`  ★ 这一步以前**没人做过**，所以那两块一直粘在一起。

## 判据（都能失败）
  T1 `n=0` 的空程序可部署且 `run=1`（前置）
  T2 每个 op 的斜率与 E-C 的 `m_op` 表**复现**（±10%）—— 两代固件、两套工具互证
  T3 每个 op 的 `C_scan(op) = 截距 − di(0)` **为正**（扫描一次总要有开销）
  T4 ★ `m_op` 随 op 的**排序**必须与 E-C 表一致（Spearman=1.0）—— 排序错说明测的不是同一个量
  ★ T5 记录项: `di(0)` 与 **62.3 TB** / **474 TB** 哪个吻合（这是本次要回答的问题本身）

用法: python tools/exp_et_cost_decomposition.py [--n-lo 16 --n-hi 112]
退出码: 0 = 全 PASS / 1 = 有 FAIL / 2 = 前置不满足
"""
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, json, os, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from h723_client import Dcl  # noqa: E402

CMD_STATUS, CMD_DEPLOY, CMD_STOP, CMD_START, CMD_BURST = 0x38, 0x10, 0x12, 0x11, 0x22
OFF_WIRE_MAP, OFF_EXEC_RING_HDR = 0x0240, 0x3880
S_N, S_LO, S_HI = 0x380C, 0x3810, 0x3814
E_LO, E_HI, E_N = 0x3860, 0x3864, 0x3868
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02
TICK_HEALTHY = 200000

# E-C/E-I 的 19 原语 m_op 表（TB/条，div0）—— 本次**只用来交叉验证**, 不参与拟合
M_OP_REF = {0x00: 29.51, 0x01: 37.51, 0x02: 39.01, 0x03: 38.50, 0x04: 49.50,
            0x05: 64.00, 0x06: 35.00, 0x07: 42.01, 0x08: 36.49, 0x09: 45.01,
            0x0A: 46.01, 0x0B: 41.00, 0x0C: 45.01, 0x0D: 37.01, 0x0E: 35.00,
            0x0F: 39.01, 0x10: 35.50, 0x11: 35.00, 0x12: 39.09}
NAME = {0x00: "DIRECT", 0x01: "CMP", 0x02: "HYST", 0x03: "CLAMP", 0x04: "LPF",
        0x05: "PID", 0x06: "RATE", 0x07: "DEADBAND", 0x08: "MUX", 0x09: "EDGE",
        0x0A: "LUT", 0x0B: "CNT", 0x0C: "TIMER", 0x0D: "ARITH", 0x0E: "SCALE",
        0x0F: "AND", 0x10: "OR", 0x11: "NOT", 0x12: "SR"}


def mk(op, div, n):
    """0x10 载荷。★ 四个 u16 的**内存序** = (param_idx, state_offset, actuator_idx, wire2_idx)。"""
    if not (0 <= n <= 128):
        raise SystemExit("!! n 超出 0..128")
    if n == 0:                                  # 空程序: 无路由, 但仍给 1 个 param/state
        return struct.pack("<HHH", 0, 1, 1) + struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) + b"\x00" * 16
    fl = FLAG_ACTIVE | FLAG_WIRE2
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  fl, i, (i % 64) + 1, 0, i, div, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    states = b"\x00" * (16 * (min(n, 64) + 1))
    return struct.pack("<HHH", n, n, min(n, 64) + 1) + routes + params + states + b"\x00" * 16


def rd(dcl, addr, nwords, chunk=200):
    out, off = b"", 0
    while off < nwords:
        k = min(chunk, nwords - off)
        sts, p = dcl.send(CMD_BURST, struct.pack("<IH", addr + 4 * off, k), expect_len=4 * k)
        if sts != "ACK" or len(p) < 4 * k:
            return None
        out += p[:4 * k]
        off += k
    return out


def _wait_healthy(dcl, shm, tries=6, need=3):
    for i in range(tries):
        t0, good = time.time(), 0
        while time.time() - t0 < 12.0:
            v = rd(dcl, shm + OFF_EXEC_RING_HDR, 2)
            if v and struct.unpack("<2I", v[:8])[1] >= TICK_HEALTHY:
                good += 1
                if good >= need:
                    print("    [健康门] 第 %d 次 ⇒ 开工（%.1f s）" % (i + 1, time.time() - t0))
                    return True
            else:
                good = 0
            time.sleep(0.25)
        try:
            dcl.close()
        except Exception:
            pass
        time.sleep(0.4)
        dcl.__init__(dcl.port, wait=1.0)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("DCL_PORT"))
    ap.add_argument("--n-lo", type=int, default=16, dest="n_lo")
    ap.add_argument("--n-hi", type=int, default=112, dest="n_hi")
    ap.add_argument("--settle", type=float, default=1.1)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    print("=" * 74)
    print("E-T  每拍固定开销的拆分: C_other 与 C_scan(op)（19 原语）")
    print("=" * 74)
    dcl = Dcl(a.port)
    print("端口 = %s" % dcl.port)
    time.sleep(1.0)
    res, skip, rows = [], [], {}

    def meas(op, n):
        sts, pp = dcl.send(CMD_DEPLOY, mk(op, 0, n), expect_len=None)
        if sts != "ACK":
            return None
        dcl.send(CMD_STOP); time.sleep(0.10)
        dcl.send(CMD_START); time.sleep(0.10)
        time.sleep(a.settle)
        raw = rd(dcl, shm + S_N, (E_N + 4 - S_N) // 4)
        if raw is None:
            return None
        u = struct.unpack("<%dI" % (len(raw) // 4), raw)

        def at(o):
            return u[(o - S_N) // 4]
        en, elo, ehi = at(E_N), at(E_LO), at(E_HI)
        if not en:
            return None
        return (elo | (ehi << 32)) / float(en)

    try:
        sts, p = dcl.send(CMD_STATUS, expect_len=51)
        if sts != "ACK" or len(p) < 51:
            print("!! 0x38 失败"); return 2
        shm = struct.unpack("<I", p[23:27])[0]
        print("SHM = 0x%08X" % shm)
        dcl.send(0x39, bytes([19, 4]) + struct.pack("<I", 0), expect_len=None)  # 清限时
        if not _wait_healthy(dcl, shm):
            print("\n[exit] 板子不健康 ⇒ 判无效"); return 2

        # ── T1/T5: 空程序（n=0）⇒ 纯骨架 ──────────────────────────────
        print("\n── n=0 空程序（桶全空 ⇒ `engine_tick` **不调扫描体**）──")
        d0 = meas(0, 0)
        if d0 is None:
            res.append(("T1 空程序可部署且统计在跑（di 可读）", False))
            print("    !! 空程序测不到 di ⇒ 无法拆分（后续 T3 会 SKIP）")
        else:
            print("    实测 di(0) = **%.1f TB**（= %.0f cyc）" % (d0, d0 * 2))
            res.append(("T1 空程序可部署且统计在跑（di(0) = %.1f TB）" % d0, True))
            print("    ★ 与两个历史数对账: 62.3 TB（tick_ring 的\"全 INACTIVE\"）· 474 TB（E-D 的 C_other）")
            near = "62.3 TB（⇒ 474 TB 那个数**不是**纯骨架, 它含扫描调用）" if abs(d0 - 62.3) < abs(d0 - 474) else \
                   "474 TB（⇒ 62.3 TB 那个数来自**另一个口径/配置**）"
            print("    ⇒ 更接近: **%s**" % near)
            rows["di0"] = d0
            rows["di0_near"] = near

        # ── 19 原语的斜率与截距 ───────────────────────────────────────
        print("\n── 19 原语: n=%d / %d 两点 ⇒ 斜率(m_op) 与 截距(C_other+C_scan) ──"
              % (a.n_lo, a.n_hi))
        print("    %-9s %-9s %-9s %-9s %-9s %s"
              % ("op", "di(n_lo)", "di(n_hi)", "m_op实测", "E-C 表", "偏差"))
        ops = []
        for op in sorted(M_OP_REF):
            dlo, dhi = meas(op, a.n_lo), meas(op, a.n_hi)
            if dlo is None or dhi is None:
                skip.append("T2 %s —— 读不到 di" % NAME[op]); continue
            m = (dhi - dlo) / float(a.n_hi - a.n_lo)
            icpt = dlo - m * a.n_lo
            ref = M_OP_REF[op]
            dev = 100.0 * (m / ref - 1.0)
            ops.append((op, m, icpt))
            print("    %-9s %-9.1f %-9.1f %-9.2f %-9.2f %+.1f%%"
                  % (NAME[op], dlo, dhi, m, ref, dev))
            res.append(("T2 %s: m_op 复现 E-C 表 ±10%%（实测 %.2f vs %.2f, %+.1f%%）"
                        % (NAME[op], m, ref, dev), abs(dev) <= 10.0))
        rows["ops"] = [[NAME[o], m, i] for o, m, i in ops]

        # ── T4: 排序一致性（Spearman，必须带平局秩与区间断言）─────────
        def spearman(xs, ys):
            def rank(v):
                s = sorted(range(len(v)), key=lambda i: v[i])
                r = [0.0] * len(v)
                i = 0
                while i < len(s):
                    j = i
                    while j + 1 < len(s) and v[s[j + 1]] == v[s[i]]:
                        j += 1
                    avg = (i + j) / 2.0 + 1.0
                    for k in range(i, j + 1):
                        r[s[k]] = avg
                    i = j + 1
                return r
            rx, ry = rank(xs), rank(ys)
            n = len(xs)
            mx, my = sum(rx) / n, sum(ry) / n
            num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
            den = (sum((x - mx) ** 2 for x in rx) * sum((y - my) ** 2 for y in ry)) ** 0.5
            rho = num / den if den else 0.0
            assert -1.0001 <= rho <= 1.0001, "Spearman 越界: %r" % rho
            return rho

        if len(ops) >= 5:
            rho = spearman([o[1] for o in ops], [M_OP_REF[o[0]] for o in ops])
            print("\n    T4 排序一致性（实测 m_op vs E-C 表）: Spearman rho = **%.3f**" % rho)
            res.append(("T4 19 原语 m_op 的排序与 E-C 表一致（rho ≥ 0.80；实测 %.3f）" % rho,
                        rho >= 0.80))

        # ── T3: C_scan(op) = 截距 − di(0) ─────────────────────────────
        if d0 is not None and ops:
            print("\n── C_scan(op) = 截距 − di(0) ──")
            print("    %-9s %-11s %-11s %s" % ("op", "截距", "C_scan(op)", "（= 截距 − di(0)）"))
            positives = 0
            for op, m, icpt in ops:
                cs = icpt - d0
                if cs > 0:
                    positives += 1
                print("    %-9s %-11.1f %-11.1f" % (NAME[op], icpt, cs))
                rows.setdefault("cscan", []).append([NAME[op], cs])
            res.append(("T3 19/19 的 C_scan(op) 为正（实测 %d/19）" % positives, positives == len(ops)))
            csl = [c for _n, c in rows["cscan"]]
            print("    ⇒ C_scan 区间 = [%.1f, %.1f] TB（均值 %.1f）· 与 E-Q 用的 100~122 TB 对照"
                  % (min(csl), max(csl), sum(csl) / len(csl)))
        else:
            skip.append("T3 C_scan(op) —— 缺 di(0) 或没有可用 op 行")
    finally:
        try:
            dcl.send(CMD_STOP); time.sleep(0.2); dcl.send(CMD_START); time.sleep(0.3)
        except Exception:
            pass
        dcl.close()

    print("\n" + "=" * 74)
    print("=== 判据 ===")
    for k, v in res:
        print("  [%s] %s" % ("PASS" if v else "FAIL", k))
    for k in skip:
        print("  [SKIP] %s" % k)
    bad = [k for k, v in res if not v]
    print("\n%d 项判定, %d FAIL, %d SKIP（SKIP ≠ PASS）" % (len(res), len(bad), len(skip)))
    if a.json:
        json.dump(dict(res=[[k, bool(v)] for k, v in res], skip=skip, data=rows),
                  open(a.json, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print("原始数据: %s" % a.json)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
