#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_traj_verify.py —— 实机验证**轨迹规划**（阶段三 A）

被测：`examples/h723_step_traj_tri.dcl`（三角波速度曲线）
      `examples/h723_step_traj_sine.dcl`（阶梯正弦速度曲线）

## 判据的设计原则：**形状判据 > 幅度判据**
单点幅度会被刻度误差吃掉（本会话刚吃过一次：`dab` 偏低 2.3 倍）；
而"**节点之间的比值**"与"**周期**"是形状量，与刻度无关 ⇒ 更硬。

  T1 峰值：实测峰值速率 / 理论峰值 ⇒ 三角波 ≈ A，正弦 ≈ 0.966A（容差 ±15%）
  T2 **形状**：
      · tri  每个半周期内"速率 vs 时间"应**线性** ⇒ 拟合 R² ≥ 0.90，且上下半周期**斜率反号**
      · sine 按段聚合平均速率 ⇒ 归一化序列与 `sin(π(k+0.5)/6)` 的**相关系数** ≥ 0.95
  T3 周期：段号翻转周期 = 2×300ms（三角）/ 6×100ms（正弦），容差 ±15%
  T4 全程无失速迹象（峰值速度与命令一致 ⇒ 没丢步；另看 `sub=24` 的 `mismatch_n` 不涨）
  R 反向：A=0 ⇒ 速率 ≈ 0（**不该红的不红**）

用法: python tools/h723_traj_verify.py --prog tri|sine [--port COM22] [--A 1200]
"""
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from h723_client import Dcl, find_board, engine_status, read_wires, read_sensors   # noqa: E402

CMD_WRITE = 0x21
SPR, CPR = 1600.0, 4096.0
PROG = {"tri":  ("examples/h723_step_traj_tri.dcl",  0.600, 0.0),
        "sine": ("examples/h723_step_traj_sine.dcl", 0.250,
                 [0.259, 0.707, 0.966, 0.966, 0.707, 0.259])}
_t = 0


def rec(ok, name, detail=""):
    global _t
    _t += 1
    print("  [%s] %-46s %s" % ("PASS" if ok else "FAIL", name, detail))
    return ok


def sub(d, n, arg=0):
    st, p = d.send(0x39, bytes([19, n]) + struct.pack("<I", arg))
    return (st, p)


def main():
    prog = "tri"
    if "--prog" in sys.argv:
        prog = sys.argv[sys.argv.index("--prog") + 1]
    A = 1200.0
    if "--A" in sys.argv:
        A = float(sys.argv[sys.argv.index("--A") + 1])
    port = sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else None
    f, dwell, tab = PROG[prog]
    d = Dcl(port or find_board())
    shm = engine_status(d)["shm"]
    print("=== 轨迹规划验证: %s  (A=%.0f Hz, 段长 %dms) ===" % (prog, A, dwell * 1000))

    def wset(n, v):
        return d.send(CMD_WRITE, struct.pack(
            "<II", shm + _OWM + n * 4,
            struct.unpack("<I", struct.pack("<f", v))[0]))[0] == "ACK"

    import h723_client as HC
    global _OWM
    _OWM = HC.OFF_WIRE_MAP

    def sample(secs):
        """按 (t, raw, seg, tgt, hz) 采样。
        ★ 第一版每点读两帧(61 个 wire + 1 个 sensor) ⇒ 实测只有 **32 Hz**，
          对 100ms 的阶梯只有 3 点、对被测形状来说太粗。
        ⇒ 改成：wire 一次读完(65 个, 含 `wire[64]` 应用频率镜像)，**编码器每 3 次才读一次**
          （raw 是位置量，可插值/就近取），采样率提到 ~2 倍。
        ★ 编码器的 raw 用"最近一次读到的值"填充 ⇒ 速率估计的分辨率由 sensor 那一帧决定，
          这对**形状**判据够用（节点比值与周期都与刻度无关）。"""
        t0, out, last_raw = time.time(), [], None
        k = 0
        while time.time() - t0 < secs:
            w = read_wires(d, shm, 65)
            if w is None:
                continue
            fresh = False
            if k % 3 == 0:
                sv = read_sensors(d, shm, 1)
                if sv is not None:
                    last_raw = int(round(sv[0])); fresh = True
            k += 1
            if last_raw is None:
                continue
            # ★ 记下"这一点的编码器值是不是**刚重读的**" —— 见 vel() 的说明
            out.append((time.time() - t0, last_raw, w[30], w[57], w[58], fresh))
        return out

    def vel(rows):
        """按最短弧算速率 (counts/s)。
        ★★★ 2026-09-17 修一个真 bug（**被新加的"正对照"当场抓出来**）：
          第一版把"**每 3 次才重读的编码器值**"配上"**相邻两点的 Δt**" ⇒
          分子跨 3 个采样、分母只有 1 个 ⇒ **速率恒定偏高约 3 倍**。
          证据：正对照（脚手架 1200 Hz，阶段 2 已证准确）实测比值 **3.21**。
          ⇒ 现在**只用"编码器刚重读"的点**算 Δraw，Δt 也取相邻两次重读之间 ⇒ 口径一致。
        ★ 一般化：**差分量的分子与分母必须同口径**；而"正对照"就是专门抓这个的。"""
        idx = [i for i in range(len(rows)) if rows[i][5]]
        v = []
        for a, b in zip(idx, idx[1:]):
            dt = rows[b][0] - rows[a][0]
            if dt <= 0:
                continue
            dv = (rows[b][1] - rows[a][1]) & 0xFFF
            if dv > 2048:
                dv -= 4096
            v.append(abs(dv) / dt)
        return v, idx

    ok = True
    try:
        # ── 前置：运动源 + 斜坡 ──
        sub(d, 13, 1); time.sleep(0.3)
        st, p = sub(d, 14)
        src = struct.unpack("<8I", p[:32])[0] if (st == "ACK" and len(p) >= 32) else None
        ok &= rec(src == 1, "前置: 运动源 = 程序面", "src=%s" % src)
        if prog == "tri":
            slope = int(A / dwell)          # ★ 峰值 = 斜率 × 半周期
        else:
            slope = 0                       # ★ 阶梯模式：斜率 0 才能看到"表"
        sub(d, 17, slope)
        print("  斜坡斜率 = %d Hz/s（%s）" % (slope, "三角: 峰值=斜率×半周期" if prog == "tri" else "阶梯: 关斜坡看表"))

        # ══ 正对照：先用**脚手架**证明"这条测量链能看见运动" ══
        #   ★★ 为什么必须先做：第一版没有它，而"轴完全不动"时 R 判据**必然通过**
        #     （0 < 阈值）—— 那是个**空判据**，会给出假绿。任何"期望为 0/小"的判据
        #     都必须先配一条"期望为大"的正对照，否则你不知道仪器是瞎了还是真安静。
        sub(d, 13, 0)                       # 运动源 = 脚手架
        sub(d, 3, 1); sub(d, 1, int(A))     # 使能 + 直接给 A
        time.sleep(0.6)
        pc = sample(0.6)
        pv = max(vel(pc)[0]) if len(pc) > 3 else 0.0
        pv_th = A / SPR * CPR
        print("  正对照(脚手架 %.0f Hz): 实测峰值 %.0f / 理论 %.0f counts/s (比值 %.2f)"
              % (A, pv, pv_th, pv / pv_th if pv_th else 0))
        ok &= rec(pv >= 0.5 * pv_th, "正对照: 测量链确实看得见运动（否则下面全是空判据）",
                  "比值 %.2f" % (pv / pv_th if pv_th else 0))
        sub(d, 1, 0); sub(d, 3, 0)          # 停 + 失能
        time.sleep(0.4)
        sub(d, 13, 1); time.sleep(0.3)      # 切回程序面

        # ── R 反向：A=0 ⇒ 不该动 ──
        wset(10, 1.0); wset(11, 0.0); time.sleep(0.8)
        r0 = sample(0.6)
        rv = max(vel(r0)[0]) if len(r0) > 3 else 0.0
        ok &= rec(rv < 0.05 * A / SPR * CPR, "R A=0 时速率≈0（不该红的不红）",
                  "实测 %.0f counts/s" % rv)

        # ── 正题 ──
        wset(11, A)
        time.sleep(0.4)                     # 起步
        rows = sample(5.0)
        print("  采到 %d 点（%.0f Hz 采样率）" % (len(rows), len(rows) / 3.0))
        if len(rows) < 60:
            print("  ✗ 采样太少 ⇒ 本判据**无效**（不是通过）"); return 1
        v, idx = vel(rows)
        # 用 (t, seg) 配速率
        pts = [(rows[idx[i + 1]][0], rows[idx[i + 1]][2], v[i]) for i in range(len(v))]

        # T1 峰值
        vmax = max(p[2] for p in pts)
        v_theory = (A if prog == "tri" else A * 0.966) / SPR * CPR
        print("  T1 峰值: 实测 %.0f / 理论 %.0f counts/s (比值 %.3f)"
              % (vmax, v_theory, vmax / v_theory))
        ok &= rec(0.85 <= vmax / v_theory <= 1.15, "T1 峰值与设定一致（±15%）",
                  "比值 %.3f" % (vmax / v_theory))

        # T3 周期（段号翻转周期）
        period = 2 * dwell if prog == "tri" else len(tab) * dwell
        flips = [pts[i][0] for i in range(1, len(pts))
                 if pts[i][1] != pts[i - 1][1] and pts[i][1] == 0.0]
        if len(flips) >= 3:
            per = (flips[-1] - flips[0]) / (len(flips) - 1)
            print("  T3 周期: 实测 %.3f s / 设定 %.3f s" % (per, period))
            ok &= rec(abs(per - period) / period <= 0.15, "T3 周期与设定一致（±15%）",
                      "%.3f vs %.3f" % (per, period))
        else:
            print("  T3 周期: 段号翻转不足 3 次 ⇒ 本判据**无效**（不是通过）")

        if prog == "sine":
            # T2 形状：按段聚合 ⇒ 归一化序列 vs sin(π(k+0.5)/6)
            segs = {}
            for t, s, vv in pts:
                segs.setdefault(int(s), []).append(vv)
            got = [sum(segs.get(k, [0])) / max(1, len(segs.get(k, [1])))
                   for k in range(len(tab))]
            gmax = max(got) or 1.0
            g = [x / gmax for x in got]
            th = [x / max(tab) for x in tab]
            n = len(g)
            mg, mt = sum(g) / n, sum(th) / n
            num = sum((g[i] - mg) * (th[i] - mt) for i in range(n))
            den = math.sqrt(sum((g[i] - mg) ** 2 for i in range(n)) *
                            sum((th[i] - mt) ** 2 for i in range(n)))
            r = num / den if den else 0.0
            print("  T2 形状: 各段归一化速率 %s" % " ".join("%.2f" % x for x in g))
            print("           理想半正弦表   %s" % " ".join("%.2f" % x for x in th))
            print("           相关系数 r = %.4f" % r)
            ok &= rec(r >= 0.95, "T2 形状（相关系数 ≥0.95）", "r=%.4f" % r)
        else:
            # T2 形状：半周期内线性（两端各取一半样本分别拟合）
            half = []
            cur, acc = pts[0][1], []
            for t, s, vv in pts:
                if s != cur:
                    half.append(acc); acc, cur = [], s
                acc.append((t, vv))
            half.append(acc)
            rs = []
            for h in half:
                if len(h) < 6:
                    continue
                ts = [x[0] for x in h]; vs = [x[1] for x in h]
                mt2, mv = sum(ts) / len(ts), sum(vs) / len(vs)
                sxy = sum((ts[i] - mt2) * (vs[i] - mv) for i in range(len(ts)))
                sxx = sum((t - mt2) ** 2 for t in ts)
                syy = sum((v - mv) ** 2 for v in vs)
                k = sxy / sxx if sxx else 0.0
                rr = (sxy * sxy / (sxx * syy)) if (sxx and syy) else 0.0
                rs.append((k, rr, len(h)))
            lin = [x for x in rs if x[1] >= 0.90]
            signs = set(1 if x[0] > 0 else -1 for x in rs)
            print("  T2 形状: 各半周期斜率/ R² / 样本数 = %s"
                  % ", ".join("k=%.3g R²=%.3f n=%d" % x for x in rs[:6]))
            ok &= rec(len(lin) >= max(2, len(rs) // 2), "T2 形状（过半半周期线性 R²≥0.90）",
                      "%d/%d 段线性" % (len(lin), len(rs)))
            ok &= rec(len(signs) >= 2, "T2b 上下半周期**斜率反号**（真的是三角波，不是单调爬）",
                      "斜率符号集 %s" % sorted(signs))

        # T4 无失步（峰值速度与命令一致已隐含）；再看 mismatch 不涨
        st, p = sub(d, 24)
        if st == "ACK" and len(p) == 32:
            mm = struct.unpack("<8I", p[:32])[0]
            print("  T4 sub=24: mismatch_n=%d（本次会话内）" % mm)
            ok &= rec(mm == 0, "T4 使能一致性无失配（mismatch_n==0）", "n=%d" % mm)
    finally:
        try:
            wset(11, 0.0); wset(10, 0.0)
            sub(d, 17, 0)          # 关斜坡（恢复默认）
            sub(d, 13, 0)          # 运动源回脚手架
            sub(d, 3, 0)           # 失能
            print("\n  已收尾: 请求清 0 / 关斜坡 / 运动源回脚手架 / 失能")
        except Exception as e:
            print("\n  ⚠ 收尾异常: %s" % e)
        d.close()
    print("=== %s ===" % ("全部通过" if ok else "有 FAIL —— 见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
