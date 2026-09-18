#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-P: 转变代价 `k` 是否依赖 **op 对**?

## 为什么问这个
- E-L 测出：**全局** `k` 不是常数（每转变 8.50 → 5.56 TB，严格交替 1.29）
- `docs/exp-TCM-cycles` §A2.3 当年却测得 `k` **随 op 对而变**：
  `DIRECT+PID = 10.0 cyc`、`LPF+PID = 5.50 cyc`
⇒ 若"op 对"是真正的自变量，那么 E-L 看到的"随块大小下降"可能是
  **不同 op 对的 k 不同**被误读成块大小效应（两者在本轮设计里是**共线**的）。
⇒ **本步把每拍条数钉死，只改 op 对 ⇒ 把两个变量分开。**

## 模型（不重新拟合每条 op 的成本）
    di ≈ C + 64×c_a + 64×c_b + k_ab × 转变数
其中 `c_a`/`c_b` 取自 E-K 在本固件上**两点标定**的每 op 成本（`C` 与 `c_op` 同批）。

## 做法
每条程序 **n=128、div0、两种 op 各 64 条**，只用 4 种排列：
    block=64  ⇒ 转变数 1
    block=16  ⇒ 转变数 7
    block=4   ⇒ 转变数 31
    block=1   ⇒ 转变数 127（严格交替）
对每个 op 对, 用**前三点**拟直线 ⇒ 斜率 = `k_ab`；再用 block=1 **预测检验**
（★ 这样 block=1 的离群**不参与拟合**，可用来测"它是不是这类 op 对特有的"）。

## 判据（都能失败）
  Q-1 ★ `k_ab` 是否**随 op 对显著不同**（极差 > 30%）
  Q-2 ★ 用 block=1 做**留点检验**: 若它系统性低于拟合线 ⇒ "严格交替便宜"是**形状效应**
      （与 op 对无关）; 若只有某些 op 对低 ⇒ 是 **op 对效应**
  Q-3 反证: 对**同一 op 对**重复测 ⇒ `k_ab` 的重复性（应远小于 op 对之间的差）
"""
import os, re, struct, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst = 0x38, 0x10, 0x12, 0x11, 0x22
S_N, S_LO, S_HI, S_NRUN = 0x380C, 0x3810, 0x3814, 0x3818
E_LO, E_HI, E_N = 0x3860, 0x3864, 0x3868
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02
N = 128
HALF = N // 2

OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
       "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]
# ★ 每 op 成本：E-K 在本固件上两点标定的 m_op（TB/条）—— 本步不重新拟合
M = {"DIRECT": 29.51, "CMP": 37.51, "HYST": 39.01, "CLAMP": 38.51, "LPF": 49.53,
     "PID": 64.02, "RATE": 35.01, "DEADBAND": 42.02, "MUX": 36.49, "EDGE": 45.01,
     "LUT": 46.01, "CNT": 41.00, "TIMER": 44.75, "ARITH": 37.01, "SCALE": 35.00,
     "AND": 39.01, "OR": 35.50, "NOT": 35.01, "SR": 39.09}
IDX = {n: i for i, n in enumerate(OPS)}
C_BASE = 578.0     # E-K 的 C（每拍常数, TB）—— 与 m_op 同批标定


def build(block, a, b):
    """前 HALF 条 a、后 HALF 条 b, 按 block 交错 ⇒ 转变数随 block 变。"""
    ops, cur, la, lb = [], a, HALF, HALF
    while la > 0 or lb > 0:
        k = min(block, la if cur == a else lb)
        if k <= 0:
            cur = b if cur == a else a
            continue
        ops.extend([cur] * k)
        if cur == a:
            la -= k
        else:
            lb -= k
        cur = b if cur == a else a
    return ops[:N]


def transitions(ops):
    return sum(1 for i in range(len(ops) - 1) if ops[i] != ops[i + 1])


def mk_seq(ops):
    fl = FLAG_ACTIVE | FLAG_WIRE2
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, ops[i],
                                  fl, i, (i % 64) + 1, 0, i, 0, 0) for i in range(N))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(N))
    states = b"\x00" * (16 * (min(N, 64) + 1))
    return struct.pack("<HHH", N, N, min(N, 64) + 1) + routes + params + states + b"\x00" * 16


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
    pairs = []
    if len(sys.argv) > 1:
        pairs = [(sys.argv[1], sys.argv[2])]
    else:
        pairs = [("DIRECT", "PID"), ("LPF", "PID"),          # §A2.3 当年测的那两对
                 ("LPF", "MUX"), ("RATE", "SR"),             # 成本接近的
                 ("DIRECT", "CMP"), ("NOT", "OR")]           # 又一堆对照

    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("端口 = %s" % dcl.port); time.sleep(1.0)
    sts, p = dcl.send(cmd_status, expect_len=51)
    if sts != "ACK" or len(p) < 51:
        print("!! 0x38 失败"); return 2
    SHM = struct.unpack("<I", p[23:27])[0]
    print("SHM = 0x%08X  cap=0x%04X" % (SHM, p[2] | (p[3] << 8)))
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

    def meas(ops):
        sts, pp = dcl.send(cmd_deploy, mk_seq(ops), expect_len=None)
        if sts != "ACK":
            return None, (pp.decode('utf-8', 'replace') if sts == 'NAK' else sts)
        dcl.send(cmd_stop); time.sleep(0.10)
        dcl.send(cmd_start); time.sleep(0.10)
        time.sleep(1.1)
        raw = rd(dcl, SHM + S_N, (E_N + 4 - S_N) // 4)
        if raw is None:
            return None, "读失败"
        u = struct.unpack("<%dI" % (len(raw) // 4), raw)

        def at(o):
            return u[(o - S_N) // 4]

        sn, slo, shi = at(S_N), at(S_LO), at(S_HI)
        elo, ehi, en = at(E_LO), at(E_HI), at(E_N)
        if not sn or not en:
            return None, "无样本"
        return dict(di=(elo | (ehi << 32)) / float(en), nrun=at(S_NRUN)), None

    print("\n=== E-P 逐 op 对的 k_ab（n=128, div0, 两 op 各 64 条）===")
    print("%-16s %-8s %-10s %-10s %-10s"
          % ("op 对", "转变数", "实测 di", "纯可加", "每转变 TB"))
    print("-" * 62)
    out = {}
    for a, b in pairs:
        pure = C_BASE + HALF * M[a] + HALF * M[b]
        rows = []
        for block in (64, 16, 4, 1):
            ops = build(block, IDX[a], IDX[b])
            nt = transitions(ops)
            r, err = meas(ops)
            if r is None:
                print("%-16s block=%-4d **%s**" % ("%s+%s" % (a, b), block, err)); continue
            rows.append((block, nt, r["di"]))
            print("%-16s %-8d %-10.1f %-10.1f %+.3f"
                  % ("%s+%s" % (a, b) if block == 64 else "",
                     nt, r["di"], pure, (r["di"] - pure) / nt if nt else float('nan')))
        out[(a, b)] = (pure, rows)

    print("\n=== 拟合（只用 block=64/16/4 三点; block=1 留点检验）===")
    print("%-16s %-10s %-12s %s" % ("op 对", "k_ab(cyc)", "block=1 实测", "留点残差(cyc)"))
    print("-" * 64)
    ks = []
    for (a, b), (pure, rows) in out.items():
        fit = [r for r in rows if r[0] != 1]
        test = [r for r in rows if r[0] == 1]
        if len(fit) < 2:
            continue
        xs = [r[1] for r in fit]
        ys = [r[2] - pure for r in fit]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        k = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / den if den else 0.0
        ks.append((("%s+%s" % (a, b)), k * 2))
        extra = ""
        if test:
            _, nt1, di1 = test[0]
            res = (di1 - pure) - k * nt1
            extra = "block=1 实测 %+.1f TB/转变, 拟合预测 %.2f ⇒ 残差 %+.1f cyc" % (
                (di1 - pure) / nt1, k, res * 2)
        print("%-16s %-10.2f %-12s %s"
              % ("%s+%s" % (a, b), k * 2,
                 "%.2f TB/转变" % ((test[0][2] - pure) / test[0][1]) if test else "-",
                 extra))

    print("\n=== 判据 ===")
    if len(ks) >= 2:
        vals = [v for _, v in ks]
        lo, hi = min(vals), max(vals)
        print("  Q-1 ★ k_ab 极差: %.2f ~ %.2f cyc/转变 ⇒ 比值 %.2f×"
              % (lo, hi, hi / max(lo, 1e-9)))
        print("      ⇒ %s" % ("**随 op 对显著变化（>30%）**" if hi > 1.3 * lo
                            else "**不随 op 对显著变化**（全局常数够用）"))
        print("  逐对: %s" % ", ".join("%s=%.2f" % (n, v) for n, v in ks))
    print("\n  ★ 注意: 若所有 op 对的 block=1 残差**同号且同量级** ⇒ 那是**形状效应**"
          "\n     （严格交替的可预测分支模式），与 op 对无关。")
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
