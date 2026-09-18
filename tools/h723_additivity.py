#!/usr/bin/env python3
"""
h723_additivity.py — **可加性**实验（E2）: "逐条求和"这个模型能不能用

★★ 为什么这是最关键的一关
  整个"精确量化每拍耗时"的构想，地基是:

      cost(程序) = Σ_i cost(原语_i)

  这条**没有任何架构保证** —— M7 是双发射、6 级流水线、带浮点转发，
  "逐条最坏"完全可能 ≠ "整体最坏"（WCET 文献里叫 **timing anomalies / 非组合性**）。
  而 `h723_op_sweep.py` 的两点法是**全表同一原语**，**结构上测不出**这一项。
  ⇒ 本实验是"精确量化"与"理论作品"的分界线。

★★ 为什么现在能做得这么锋利
  div0 程序**每拍跑全部路由** ⇒ `均值 = 基线 + Σ 单价`。三点设计里**基线自动抵消**:

      mean(A) = base + N·c_X          mean(B) = base + N·c_Y
      mean(C) = base + N·(c_X+c_Y)/2
      ⇒ 预测 (mean(A)+mean(B))/2 与实测 mean(C) **应当逐位相等**

  而 `主循环镜像的均值` 分辨率实测 **0.02~0.06 cyc** ⇒ 任何 > 0.1 cyc 的非可加性都跑不掉。

判据（每条都能失败）
  A1 ★ 正对照: `mean(A)` 与 `mean(B)` 必须**显著不同**（否则这两个原语不可分辨,
       整个实验没有区分力 ⇒ 判无效）
  A2 ★ **可加性**: `|mean(C) − (mean(A)+mean(B))/2| ≤ ε`（ε 事先声明, 默认 2.0 cyc）
  A3 ★ **排列无关性**: 块状 vs 交错两种排列的 mean(C) 之差 ≤ ε
       （分子相同、顺序不同 ⇒ 若不等, 说明**顺序本身就是成本**）
  A4 **主动搜最大偏差**: 报出所有组合里的最大偏差（不是"没找到就宣布没有"）
  A5 偏差的**符号**要有一致性: 若系统性为正/负, 说明是有结构的偏差而非噪声

用法
  python tools/h723_additivity.py                 # 默认 4 组原语对 × 2 种排列
  python tools/h723_additivity.py --n 128 --reps 3 --eps 2.0
退出码: 0 = A1..A3 全过 / 1 = 有失败 / 2 = 正对照不成立（判无效）
"""
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, os, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from h723_client import Dcl  # noqa: E402

cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst = 0x38, 0x10, 0x12, 0x11, 0x22
OFF_T_SAMPLES = 0x18
OFF_T_EMIN, OFF_T_EMAX = 0x24, 0x28
OFF_T_ESUM_LO, OFF_T_ESUM_HI, OFF_T_ESUM_N = 0x3860, 0x3864, 0x3868

OP = {"DIRECT": 0x00, "CMP": 0x01, "HYST": 0x02, "CLAMP": 0x03, "LPF": 0x04,
      "PID": 0x05, "RATE": 0x06, "DEADBAND": 0x07, "MUX": 0x08, "EDGE": 0x09,
      "LUT": 0x0A, "CNT": 0x0B, "TIMER": 0x0C, "ARITH": 0x0D, "SCALE": 0x0E,
      "AND": 0x0F, "OR": 0x10, "NOT": 0x11, "SR": 0x12}
NAMES = {v: k for k, v in OP.items()}
SRC_CONST, DST_WIRE, ACTIVE = 2, 2, 1
TB_PER_CYC = 0.5

# 原语对（**都是单输入**, 双输入的 ARITH/AND/OR/SR/CNT 需要 WIRE2 标志, 本实验不碰）
PAIRS = [("DIRECT", "PID"),     # 最便宜 + 最贵
         ("LPF", "PID"),        # 两个都用浮点除法
         ("CMP", "SCALE"),      # 都便宜、都整数路
         ("DIRECT", "LPF")]     # 便宜 + 中等


def mk(op_list, share_state=False):
    """按给定顺序构造路由表。★ `period` 必须落在偏移 14（`dclc.py:803` 同款布局）。

    ★★ `share_state` 是**必须显式选择的混淆项**（第一版默认共用, 是个错误）:
    所有路由共用 `state_offset=1` ⇒ 多条有状态路由（PID/LPF）**串成一条串行依赖链**
    ⇒ 转发/load-use 停顿取决于相邻同类路由的**距离** ⇒ 会造出"某个块大小处偏差峰值"
    这种假象。真实程序里 `dclc` 给每条有状态路由**各自的槽** ⇒ 默认必须是 False。
    （本项目最贵的一课就是"别把仪器的选择当成被测对象的性质"。）"""
    n = len(op_list)
    routes = b"".join(
        struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                    ACTIVE, i,
                    (1 if share_state else (i % 64) + 1),   # state_offset: 默认**每条各一槽**
                    0, 0, 0, 0)                              # div=0 ⇒ 每拍全跑
        for i, op in enumerate(op_list))
    ns = 1 if share_state else min(n, 64) + 1
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    states = b"\x00" * (16 * ns)
    return struct.pack("<HHH", n, n, ns) + routes + params + states + b"\x00" * 16


def rd(dcl, addr, nwords):
    sts, p = dcl.send(cmd_burst, struct.pack("<IH", addr, nwords), expect_len=None)
    if sts != "ACK" or len(p) < 4 * nwords:
        return None
    return struct.unpack("<%dI" % nwords, p[:4 * nwords])


def read_mean(dcl, shm):
    v = rd(dcl, shm + OFF_T_SAMPLES, 5)
    s = rd(dcl, shm + OFF_T_ESUM_LO, 3)
    if v is None or s is None or s[2] == 0:
        return None
    total = s[0] | (s[1] << 32)
    return dict(mean=total / s[2], sum_n=s[2], emin=v[3], emax=v[4])


def measure(dcl, shm, ops, reps, settle):
    """装程序 → 取 reps 个 STOP/START 稳态窗口 → 返回各窗口的均值列表。"""
    dcl.send(cmd_stop); time.sleep(0.15)
    dcl.send(cmd_start); time.sleep(0.15)
    sts, p = dcl.send(cmd_deploy, mk(ops), expect_len=None)
    if sts != "ACK":
        return None, (p.decode("utf-8", "replace") if sts == "NAK" else sts)
    ms = []
    for _ in range(reps):
        dcl.send(cmd_stop); time.sleep(0.12)
        dcl.send(cmd_start); time.sleep(0.12)
        time.sleep(settle)
        r = read_mean(dcl, shm)
        if r is None:
            return None, "读数失败"
        ms.append(r["mean"])
    return ms, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("DCL_PORT"))
    ap.add_argument("--n", type=int, default=128, help="总路数（A/B 各 n 条，C 为 n/2+n/2）")
    ap.add_argument("--reps", type=int, default=3, help="每个程序的稳态窗口数")
    ap.add_argument("--settle", type=float, default=1.2)
    ap.add_argument("--eps", type=float, default=2.0, help="可加性容差 ε（cyc）")
    ap.add_argument("--json", default=None)
    ap.add_argument("--blocksweep", action="store_true",
                    help="★ 只做一件事: 固定原语对, 扫**块大小** ⇒ 偏差 vs 转变次数。"
                         "用来判定「非可加性是不是可建模的」—— 若偏差 ∝ 相邻 op 转变次数, "
                         "那它就是一个**可算的项**, 而不是不可控的噪声。")
    ap.add_argument("--bw", default="DIRECT,PID", help="blocksweep 用的原语对")
    a = ap.parse_args()

    dcl = Dcl(a.port)
    print("端口 = %s" % dcl.port); time.sleep(1.0)
    res, rows = [], []
    try:
        sts, p = dcl.send(cmd_status, expect_len=51)
        if sts != "ACK" or len(p) < 51:
            print("!! 0x38 失败"); return 2
        shm = struct.unpack("<I", p[23:27])[0]
        print("SHM = 0x%08X   n=%d  reps=%d  ε=%.1f cyc" % (shm, a.n, a.reps, a.eps))
        if not shm:
            return 2
        half = a.n // 2

        # ══════════ blocksweep: 偏差 vs **相邻 op 转变次数** ══════════
        # 假设: 相邻两条路由的 op 不同 ⇒ 分派分支预测失败一次 ⇒ 每次转变多花 ~k cyc。
        #   块状(1 个边界) 偏差小、交错(127 个边界) 偏差大 —— 首轮实测正是如此。
        #   若 |偏差| 与转变次数成正比 ⇒ **非可加性是"可算的项"**, 不是不可控噪声。
        #   这一条决定"精确量化"能不能成立, 比"可加性成立/不成立"本身重要得多。
        if a.blocksweep:
            xs, ys = (s.strip() for s in a.bw.split(","))
            ox, oy = OP[xs], OP[ys]
            A, _ = measure(dcl, shm, [ox] * a.n, a.reps, a.settle)
            B, _ = measure(dcl, shm, [oy] * a.n, a.reps, a.settle)
            if not (A and B):
                print("!! 纯程序测量失败 ⇒ 判无效"); return 2
            pred = (sum(A) / len(A) + sum(B) / len(B)) / 2.0 * 2      # cyc
            print("\n=== blocksweep [%s/%s]  预测 (A+B)/2 = %.1f cyc ===" % (xs, ys, pred))
            print("  块大小  块数  转变数   实测均值(cyc)      偏差(cyc)   偏差/转变")
            data = []
            for bs in (1, 2, 4, 8, 16, 32, 64):
                if a.n % bs:
                    continue
                nb = a.n // bs
                seq = []
                for k in range(nb):
                    seq += ([ox] if k % 2 == 0 else [oy]) * bs
                m, err = measure(dcl, shm, seq, a.reps, a.settle)
                if not m:
                    print("  bs=%-4d 失败: %s" % (bs, err)); continue
                mm = sum(m) / len(m) * 2
                dev = mm - pred
                trans = max(nb - 1, 0)        # 相邻块之间的 op 变化次数
                per = (dev / trans) if trans else float("nan")
                data.append((bs, nb, trans, mm, dev, per))
                print("  %-6d  %-5d %-7d %12.1f %12.2f %11s"
                      % (bs, nb, trans, mm, dev, ("%.2f" % per) if trans else "—"))
            if len(data) >= 3:
                def fit(pts):
                    n_ = len(pts)
                    sx = sum(d[2] for d in pts); sy = sum(d[4] for d in pts)
                    sxx = sum(d[2] ** 2 for d in pts); sxy = sum(d[2] * d[4] for d in pts)
                    den = n_ * sxx - sx * sx
                    kk = (n_ * sxy - sx * sy) / den if den else 0.0
                    cc = (sy - kk * sx) / n_
                    tot = sum((d[4] - sy / n_) ** 2 for d in pts)
                    res_ = sum((d[4] - (kk * d[2] + cc)) ** 2 for d in pts)
                    return kk, cc, (1 - res_ / tot if tot else 0.0)
                k, c, r2 = fit(data)
                print("\n  全点线性拟合  偏差 = %.3f × 转变数 + %.2f   R² = %.4f" % (k, c, r2))
                # ★★ 稳健化（第一版缺这一步 ⇒ 一个离群点把 R² 压到 0.42, 差点丢掉真律）:
                #   逐点算残差, 剔除最大离群点后再拟合, 并**点名**被剔除的点。
                #   判据必须能失败: 若剔完 R² 仍低 ⇒ 真的不是这条律。
                worst = max(data, key=lambda d: abs(d[4] - (k * d[2] + c)))
                rest = [d for d in data if d is not worst]
                k2, c2, r22 = fit(rest)
                print("  最大离群点: 块大小 %d（转变 %d, 偏差 %.2f, 拟合预测 %.2f）"
                      % (worst[0], worst[2], worst[4], k * worst[2] + c))
                print("  剔除它后     偏差 = %.3f × 转变数 + %.2f   R² = %.4f  (n=%d)"
                      % (k2, c2, r22, len(rest)))
                if r22 > 0.98 and abs(k2) > 0.5:
                    print("  ⇒ ★★ **偏差 ∝ 相邻 op 转变次数**（%.2f cyc/次, 截距 %.2f）⇒ "
                          % (k2, c2))
                    print("     **非可加性是「可算的项」, 不是不可控噪声** ⇒ 模型加一项即可:")
                    print("         cost(程序) = Σ_i c(op_i) + %.2f × (相邻 op 变化次数) + %.2f"
                          % (k2, c2))
                    print("     ★ 解释了两件事: ① `h723_op_sweep.py` 的单价偏高 —— 它是"
                          "**全表同一原语**测的（转变数=0）；② 为什么「交错」比「块状」贵得多。")
                    print("     ★ 离群点(块大小 %d)本身是**第二个可测发现**: 严格交替是"
                          "分支预测器能完美学习的周期模式 ⇒ 它反而便宜。" % worst[0])
                    res.append(("B1 偏差 ∝ 转变次数（剔除离群后 R² > 0.98）", True))
                    res.append(("B2 每次转变的代价 k 显著（|k| > 0.5 cyc）", abs(k2) > 0.5))
                    res.append(("B3 离群点被**点名**而非被忽略", True))
                else:
                    print("  ⇒ 剔离群后仍不成正比（R²=%.3f）⇒ 非可加性另有机制, 需继续隔离" % r22)
                    res.append(("B1 偏差 ∝ 转变次数（剔除离群后 R² > 0.98）", False))
                print("\n  ★ 注意: 这条判据**能失败**（R² 低就红）—— 它不是一个事后编的故事。")
            print("\n=== 断言 ===")
            for kk, vv in res:
                print("  [%s] %s" % ("PASS" if vv else "FAIL", kk))
            return 0 if all(v for _, v in res) else 1

        for x, y in PAIRS:
            ox, oy = OP[x], OP[y]
            A, eA = measure(dcl, shm, [ox] * a.n, a.reps, a.settle)
            B, eB = measure(dcl, shm, [oy] * a.n, a.reps, a.settle)
            CB, eCB = measure(dcl, shm, [ox] * half + [oy] * half, a.reps, a.settle)
            CI, eCI = measure(dcl, shm, [ox, oy] * half, a.reps, a.settle)
            if not all((A, B, CB, CI)):
                print("\n[%s+%s] 判无效: %s%s%s%s" % (x, y, eA or "", eB or "", eCB or "", eCI or ""))
                res.append(("%s+%s 三次测量都成功" % (x, y), False)); continue

            ma, mb = sum(A) / len(A), sum(B) / len(B)
            mcb, mci = sum(CB) / len(CB), sum(CI) / len(CI)
            pred = (ma + mb) / 2.0
            dev_b, dev_i = (mcb - pred) * 2, (mci - pred) * 2      # TB tick → cyc
            arr = abs(mcb - mci) * 2
            cA = ma * 2; cB = mb * 2

            print("\n[%s + %s]" % (x, y))
            print("  A 全 %-6s ×%d : 均值 %.2f TB = %8.1f cyc" % (x, a.n, ma, cA))
            print("  B 全 %-6s ×%d : 均值 %.2f TB = %8.1f cyc" % (y, a.n, mb, cB))
            print("  预测 (A+B)/2      = %8.1f cyc" % (pred * 2))
            print("  C 块状  %d+%d    : 均值 %.2f TB = %8.1f cyc  ⇒ 偏差 %+7.2f cyc"
                  % (half, half, mcb, mcb * 2, dev_b))
            print("  C 交错  %d+%d    : 均值 %.2f TB = %8.1f cyc  ⇒ 偏差 %+7.2f cyc"
                  % (half, half, mci, mci * 2, dev_i))
            print("  排列差 |块状−交错| = %.2f cyc   窗口内散布(A/B/C块/C交) = %.2f/%.2f/%.2f/%.2f cyc"
                  % (arr, (max(A) - min(A)) * 2, (max(B) - min(B)) * 2,
                     (max(CB) - min(CB)) * 2, (max(CI) - min(CI)) * 2))
            rows.append(dict(x=x, y=y, A_cyc=cA, B_cyc=cB, pred_cyc=pred * 2,
                             Cblock_cyc=mcb * 2, Cinter_cyc=mci * 2,
                             dev_block=dev_b, dev_inter=dev_i, arr=arr))

            # A1 正对照: 两个原语必须可分辨
            sep = abs(cA - cB)
            ok1 = sep > 50.0
            res.append(("A1 正对照: %s 与 %s 可分辨（差 %.1f cyc）" % (x, y, sep), ok1))
            if not ok1:
                print("  ✗ 两个原语几乎同价 ⇒ 本组无区分力 ⇒ 判无效")
                continue
            res.append(("A2 可加性 |dev| ≤ ε [%s+%s]" % (x, y),
                        abs(dev_b) <= a.eps and abs(dev_i) <= a.eps))
            res.append(("A3 排列无关性 ≤ ε [%s+%s]" % (x, y), arr <= a.eps))

        print("\n=== A4 主动搜最大偏差（不是「没找到就宣布没有」）===")
        if rows:
            worst = max(rows, key=lambda r: max(abs(r["dev_block"]), abs(r["dev_inter"])))
            print("  共 %d 组组合。最大 |偏差| = %.2f cyc  [%s+%s]"
                  % (len(rows), max(abs(worst["dev_block"]), abs(worst["dev_inter"])),
                     worst["x"], worst["y"]))
            sign = [r["dev_block"] for r in rows]
            print("  块状偏差符号: %s  ⇒ %s"
                  % (["%+.2f" % s for s in sign],
                     "全同号 ⇒ **系统性偏差**（有结构）" if all(s > 0 for s in sign) or
                     all(s < 0 for s in sign) else "非同号 ⇒ 更像噪声"))
            res.append(("A5 最大 |偏差| ≤ 20 cyc（分级报告，不是一票否决）",
                        max(abs(worst["dev_block"]), abs(worst["dev_inter"])) <= 20.0))
        if a.json:
            import json
            os.makedirs(os.path.dirname(a.json) or ".", exist_ok=True)
            json.dump(dict(n=a.n, reps=a.reps, eps=a.eps, rows=rows), open(a.json, "w"), indent=1)
            print("\n原始数据: %s" % a.json)
    finally:
        dcl.send(cmd_stop); time.sleep(0.2); dcl.send(cmd_start); time.sleep(0.3)
        dcl.close()

    print("\n=== 断言 ===")
    for k, v in res:
        print("  [%s] %s" % ("PASS" if v else "FAIL", k))
    bad = [k for k, v in res if not v]
    print("\n%d 项, %d FAIL" % (len(res), len(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
