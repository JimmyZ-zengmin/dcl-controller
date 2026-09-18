#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-L: **混合 op 程序的留点验证** —— 模型的 `Σ_ops` 求和部分从未验过。

## 为什么这是唯一的关键缺口
E-K 验的是**单 op、均匀**程序, 所以只验了"每项常数对不对", **没验求和**。
而模型写的是 `di = C + Σ_ops ( m_op × 条数 )`。

★★ 而且"可加性"在本项目**历史上失败过一次**:
   `docs/exp-TCM-cycles` §A2.3 记着 `cost = Σc + k × 转变数` ——
   **相邻 op 之间有转变代价**（DIRECT+PID `k=10.0`、LPF+PID `k=5.50` 个周期）。
⇒ 混合 op 程序的偏差**很可能不是 0, 而是重现那个 k**。

## 设计（一次实验同时回答两个问题）
固定 n=128、div0（每拍全跑）, 只改**相邻 op 的排列**:

    block=128 : 全 PID                      ⇒ 转变数 0
    block= 64 : 64×PID + 64×DIRECT          ⇒ 转变数 1
    block= 32 : 32×PID + 32×DIRECT 交替      ⇒ 转变数 3
    block= 16/8/4/2/1 : 更细的交错           ⇒ 转变数 7/15/31/63/127

★ 关键: **每种 op 的总条数恒为 64**, 所以 `Σ_ops` 那一项**恒定**
   ⇒ 任何随 block 变小的**增量**, 就**只能**来自"邻接 op 的改变"。

## 判据（都能失败）
  L-1 ★ **可加性**: block=64（只有 1 次转变）的实测应 ≈ 预测（Σ_ops 那项）
  L-2 ★★ **转变项**: 若偏差随转变数增长 ⇒ 拟出 `k`, 并检验**残差 ≤1 个周期**
      （与 §A2.3 当年对 LPF+PID 做到的 0.12 cyc 同口径）
  L-3 **反证**: `k` 必须**远大于**测量噪声（block=128 两点重复测量的散布）
  L-4 若 block=1（127 次转变）的偏差**反而更小** ⇒ 说明不是单调的转变效应,
     而是别的东西（要重新解释）
"""
import os, struct, sys, time, statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst = 0x38, 0x10, 0x12, 0x11, 0x22
S_N, S_LO, S_HI, S_NRUN = 0x380C, 0x3810, 0x3814, 0x3818
E_LO, E_HI, E_N = 0x3860, 0x3864, 0x3868
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02

OP_PID, OP_DIRECT = 5, 0
M_PID, M_DIRECT, C = 64.00, 29.50, 596.4      # E-K 两点标定值（不参与本步拟合）


def build_ops(n, block, a, b):
    """长为 n 的 op 序列: 前 half 条 a、后 half 条 b? 不 —— 按 block 交错。"""
    half = n // 2
    ops = []
    # 前 half 用 a, 后 half 用 b; 但在各自内部按 block 分组交错以控制转变数
    # 更简单可控的构造: 交替 block 块
    cur = a
    left_a, left_b = half, n - half
    while left_a > 0 or left_b > 0:
        k = min(block, left_a if cur == a else left_b)
        if k <= 0:
            cur = b if cur == a else a
            continue
        ops.extend([cur] * k)
        if cur == a:
            left_a -= k
        else:
            left_b -= k
        cur = b if cur == a else a
    return ops[:n]


def transitions(ops):
    return sum(1 for i in range(len(ops) - 1) if ops[i] != ops[i + 1])


def mk_seq(ops, n):
    fl = FLAG_ACTIVE | FLAG_WIRE2
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, ops[i],
                                  fl, i, (i % 64) + 1, 0, i, 0, 0) for i in range(n))
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

    N = 128
    def meas(ops):
        sts, pp = dcl.send(cmd_deploy, mk_seq(ops, N), expect_len=None)
        if sts != "ACK":
            print("   deploy 被拒"); return None
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
        elo, ehi, en = at(E_LO), at(E_HI), at(E_N)
        if not sn or not en:
            return None
        return dict(scan=(slo | (shi << 32)) / float(sn),
                    di=(elo | (ehi << 32)) / float(en), nrun=at(S_NRUN))

    # 预测: 每种 op 各 64 条 ⇒ C + 64×M_PID + 64×M_DIRECT
    pred = C + 64 * M_PID + 64 * M_DIRECT
    print("=== E-L 混合 op 留点验证（n=128, div0, 两种 op 各 64 条）===")
    print("  模型预测（不含转变项）= C + 64×%.2f + 64×%.2f = **%.0f TB**"
          % (M_PID, M_DIRECT, pred))
    print()
    print("%-8s %-10s %-11s %-11s %s" % ("block", "转变数", "实测 di", "偏差", "每转变"))
    print("-" * 60)
    rows = []
    # ★ 先重复测 block=128（全 PID）拿噪声底
    print("  -- 噪声底（同一程序重复测）--")
    nz = []
    for _ in range(3):
        q = meas([OP_PID] * N)
        if q:
            nz.append(q["di"])
            print("     全 PID 实测 = %.1f TB" % q["di"])
    noise = (max(nz) - min(nz)) if len(nz) >= 2 else 0.0
    print("     ⇒ 噪声底（极差）= %.1f TB = %.1f cyc" % (noise, noise * 2))
    if nz:
        print("     ★ 注意: 这不是上面的 pred（pred 含 DIRECT）, 只用来量散布")

    for block in (64, 32, 16, 8, 4, 2, 1):
        ops = build_ops(N, block, OP_PID, OP_DIRECT)
        nt = transitions(ops)
        q = meas(ops)
        if not q:
            continue
        dev = q["di"] - pred
        per = dev / nt if nt else float('nan')
        rows.append((block, nt, q["di"], dev, per))
        print("%-8d %-10d %-11.1f %+11.1f %+.3f TB/转变"
              % (block, nt, q["di"], dev, per))

    print("\n=== 判据 ===")
    if not rows:
        print("  无有效行"); dcl.close(); return 2
    d1 = [r for r in rows if r[1] == 1]
    if d1:
        b, nt, got, dev, _ = d1[0]
        print("  L-1 ★ 可加性（block=64, 只 1 次转变）:")
        print("      预测 %.0f  实测 %.0f  偏差 %+.1f TB = %+.1f cyc"
              % (pred, got, dev, dev * 2))
        print("      ⇒ %s" % ("**可加性成立（偏差 ≤ 噪声底）**"
                            if abs(dev) <= max(noise, 10) else
                            "**可加性不成立**（偏差 %.0f TB 远超噪声底 %.0f）" % (dev, noise)))
    # L-2 转变项
    if len(rows) >= 3:
        xs = [r[1] for r in rows]
        ys = [r[3] for r in rows]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        k = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / den if den else 0.0
        c0 = my - k * mx
        res = [ys[i] - (c0 + k * xs[i]) for i in range(n)]
        print("\n  L-2 ★★ 转变项拟合: 偏差 ≈ %.1f + %.3f × 转变数  (TB)" % (c0, k))
        print("      ⇒ k = %.3f TB/转变 = **%.1f cyc/转变**" % (k, k * 2))
        print("      残差: 最大 %.1f TB = %.1f cyc" % (max(abs(r) for r in res),
                                                    max(abs(r) for r in res) * 2))
        print("      ⇒ %s" % ("**单调转变效应成立**（k 显著 > 噪声）"
                            if abs(k) * 1 > noise else
                            "**k 被噪声淹没 ⇒ 不能声称转变项**"))
        if abs(k) > 0.01:
            print("      ⇒ 修正模型: di = C + Σ_ops(m_op×条数) + **%.1f cyc × 邻接op改变次数**"
                  % (k * 2))
    # L-4 单调性
    if len(rows) >= 2:
        ys2 = [r[3] for r in rows]
        mono = all(ys2[i] <= ys2[i + 1] + noise for i in range(len(ys2) - 1))
        print("\n  L-4 偏差随转变数单调（容噪声）: %s" % ("**是**" if mono else "**否**"))
        print("      偏差序列: %s" % ", ".join("%+.0f" % y for y in ys2))
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
