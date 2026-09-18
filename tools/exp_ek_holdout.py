#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-K: **端到端留点验证** —— 模型能不能算一个程序的执行时间?

## 为什么这一步是必要的
E-A 到 E-J 每一步都在**标定**（测常数）。**模型本身从未被"留点预测"验证过。**
本步就是那条验证: 拿**没参与标定**的程序, 用模型算, 再实测。

## 模型（全部来自前序实测, 本脚本**不重新拟合**）
    di(t) = C_other + C_scan + Σ_ops ( m_op × 该 op 本拍条数 )
            474 TB     op 相关     m_op: 19 个常数（E-C/E-I 两次构建复现 ≤0.6%）
                      19×N 两参数拟合 ⇒ (C_scan + C_other) 与 m_op
    ★ 均值: E[di] = C + m_op × E[nrun]      （单 op 程序）

## 做法（严格留点）
1. **标定集**: 单 op、div0、n ∈ {16, 32}     ⇒ 两参数 (C, m_op)
2. **验证集（留点）**: 同 op、div0、n ∈ {64, 128} ⇒ 模型预测 vs 实测
3. **交叉验证**: 对多个 op 重复 ⇒ 看 (C, m_op) 是否**跨 op 一致**
   （★ 这条同时检验"m_op 是原语属性"在均值口径下是否成立）

## 判据（都能失败）
  K-1 ★ 留点偏差 ≤ **3%**（对 64/128 两个点）
  K-2 ★ `m_op` 由**两点标定**得到, 应与 E-C/E-I 的独立测量**同量级**（差 ≤10%）
  K-3 `C` 应**跨 op 一致**（极差 ≤ 40 TB）—— 若随 op 变, 说明模型缺一项
  K-4 ★ 反证: **必须同时报 div0 与 div1** —— 若模型只在 div0 成立,
      那它是"按 division 标定的", 不是"程序无关的"
"""
import os, struct, sys, time, statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst = 0x38, 0x10, 0x12, 0x11, 0x22
S_N, S_LO, S_HI, S_NRUN = 0x380C, 0x3810, 0x3814, 0x3818
E_LO, E_HI, E_N = 0x3860, 0x3864, 0x3868
E_LAST = 0x3818 + 4          # 读到 NRUN 之后即可
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02
OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
       "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]
# E-C / E-I 独立测得的 m_op（TB/条）—— 本脚本**不参与标定, 只作对照**
REF_M = {"DIRECT": 29.50, "CMP": 37.50, "HYST": 39.00, "CLAMP": 38.50, "LPF": 49.50,
         "PID": 64.00, "RATE": 35.00, "DEADBAND": 42.00, "MUX": 36.50, "EDGE": 45.00,
         "LUT": 46.00, "CNT": 41.00, "TIMER": 44.75, "ARITH": 37.00, "SCALE": 35.00,
         "AND": 39.00, "OR": 35.50, "NOT": 35.00, "SR": 39.09}


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
    nops = int(sys.argv[1]) if len(sys.argv) > 1 else 6      # 只测前 N 个 op 以控时间
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

    def meas(op, div, n):
        sts, pp = dcl.send(cmd_deploy, mk(op, div, n), expect_len=None)
        if sts != "ACK":
            return None
        dcl.send(cmd_stop); time.sleep(0.10)
        dcl.send(cmd_start); time.sleep(0.10)
        time.sleep(1.1)
        # ★ 一次突发读 0x380C..0x386B（含 E_N 那个字）。
        #   ★ 第一版写 `E_LAST = 0x3818+4` —— 那只到 NRUN, **没覆盖 EXEC_SUM 那块**,
        #     于是 at(E_LO) 越界崩。区间是**半开**的, 终点要写到"最后一个字 + 4"。
        raw = rd(dcl, SHM + S_N, (E_N + 4 - S_N) // 4)
        if raw is None:
            return None
        u = struct.unpack("<%dI" % (len(raw) // 4), raw)

        def at(o):
            return u[(o - S_N) // 4]

        sn, slo, shi = at(S_N), at(S_LO), at(S_HI)
        elo, ehi, en = at(E_LO), at(E_HI), at(E_N)
        if not sn or not en:
            return None
        return dict(scan=(slo | (shi << 32)) / float(sn),
                    di=(elo | (ehi << 32)) / float(en), nrun=at(S_NRUN))

    print("=== E-K 留点验证（标定 n=16/32 · 验证 n=64/128, div0）===")
    print("%-9s %-8s %-9s %-9s %-9s %-8s %s"
          % ("op", "C(标定)", "m(标定)", "REF m", "差%", "留点偏差", "判定"))
    print("-" * 78)
    Cs, rows = [], []
    for op, name in list(enumerate(OPS))[:nops]:
        a = meas(op, 0, 16)
        b = meas(op, 0, 32)
        if not (a and b) or a["nrun"] == b["nrun"]:
            print("%-9s **标定点取数失败**" % name); continue
        # 两点标定: di = C + m × nrun
        m = (b["di"] - a["di"]) / float(b["nrun"] - a["nrun"])
        C = a["di"] - m * a["nrun"]
        Cs.append((name, C))
        ref = REF_M[name]
        dref = (m - ref) / ref * 100
        # 留点预测
        worst = 0.0
        det = []
        for n in (64, 128):
            q = meas(op, 0, n)
            if not q:
                continue
            pred = C + m * q["nrun"]
            dev = (q["di"] - pred) / pred * 100
            worst = max(worst, abs(dev))
            det.append("n=%d 预测%.0f/实测%.0f(%+.1f%%)" % (n, pred, q["di"], dev))
        rows.append((name, C, m, dref, worst))
        print("%-9s %-8.1f %-9.2f %-9.2f %+8.1f %-8.2f%% %s"
              % (name, C, m, ref, dref, worst, " ".join(det)))
        print("           K-1 %s   K-2 %s"
              % ("通过" if worst <= 3.0 else "**未通过**",
                 "通过" if abs(dref) <= 10 else "**未通过**"))

    print("\n=== 判据汇总 ===")
    if not rows:
        print("  无有效行"); dcl.close(); return 2
    k1 = all(r[4] <= 3.0 for r in rows)
    k2 = all(abs(r[3]) <= 10 for r in rows)
    cvals = [c for _, c in Cs]
    k3 = (max(cvals) - min(cvals)) <= 40
    print("  K-1 ★ 留点偏差 ≤3%%: 最大 %.2f%% ⇒ %s"
          % (max(r[4] for r in rows), "**通过**" if k1 else "**未通过**"))
    print("  K-2 ★ 两点法 m 与独立测量差 ≤10%%: 最大 %.1f%% ⇒ %s"
          % (max(abs(r[3]) for r in rows), "**通过**" if k2 else "**未通过**"))
    print("  K-3 ★ C 跨 op 一致（极差 ≤40 TB）: 极差 %.1f（%.1f ~ %.1f）⇒ %s"
          % (max(cvals) - min(cvals), min(cvals), max(cvals),
             "**通过**" if k3 else "**未通过 ⇒ 模型缺一项**"))
    print("\n  C 逐 op: %s" % ", ".join("%s=%.0f" % (n, c) for n, c in Cs))

    # ── K-4 ★ 反证: 模型是否只在 div0 成立? ──
    #   用 **div0 标定的** (C, m) 去预测 div1 / div2 的同 op 程序。
    #   ★ 这是本脚本最强的一条: 若它不过, 说明模型"按 division 标定", 不是程序无关的。
    print("\n=== K-4 ★ 反证: 用 div0 的 (C, m) 预测 div1 / div2 ===")
    print("%-9s %-8s %-9s %-10s %s" % ("op", "div", "nrun", "预测", "实测(偏差)"))
    print("-" * 62)
    k4rows = []
    for op, name in list(enumerate(OPS))[:nops]:
        # 重新用 div0 n=16/32 标定（与上面同口径）
        a = meas(op, 0, 16)
        b = meas(op, 0, 32)
        if not (a and b) or a["nrun"] == b["nrun"]:
            continue
        m = (b["di"] - a["di"]) / float(b["nrun"] - a["nrun"])
        C = a["di"] - m * a["nrun"]
        for div in (1, 2):
            q = meas(op, div, 128)
            if not q:
                continue
            pred = C + m * q["nrun"]
            dev = (q["di"] - pred) / pred * 100
            k4rows.append((name, div, dev))
            mark = "" if abs(dev) <= 10 else "  ★超差"
            print("%-9s %-8d %-9d %-10.0f %.0f (%+.1f%%)%s"
                  % (name, div, q["nrun"], pred, q["di"], dev, mark))
    if k4rows:
        worst = max(abs(d) for _, _, d in k4rows)
        nbad = sum(1 for _, _, d in k4rows if abs(d) > 10)
        print("\n  K-4 最大偏差 %.1f%%, 超 10%% 的 %d/%d ⇒ %s"
              % (worst, nbad, len(k4rows),
                 "**通过**（模型跨 division 成立 ⇒ 程序无关）" if nbad == 0
                 else "**未通过** ⇒ 模型只在 div0 成立, 是「按档标定」而非程序无关"))
    else:
        print("  K-4 无有效行")
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
