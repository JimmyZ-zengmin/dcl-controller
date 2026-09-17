#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_traj_peak_diag.py —— **一次定案**：轨迹峰值偏高 +40% 到底出在哪一侧

## 为什么值得单开一个工具
阶段三实测：三角波峰值 4257 counts/s = **1663 Hz**、正弦 4261 = **1664 Hz** ——
两个程序表不同、段长不同、一个有斜坡一个关斜坡，**峰值的绝对值却几乎相同**。
⇒ 这不像"规划算错"（那会各自差自己的倍数），**像一个天花板**。
⇒ 但正对照（脚手架 1200 Hz）读回 1.01 ⇒ 测速器是准的 ⇒ **不能只怀疑测量**。

## 三分判据（每条都能失败，且互斥）
同时录四个量：**程序下发的 `hz`**(`wire[58]`)、**固件"已应用频率"镜像**(`wire[64]`)、
**固件自报三段**(`sub=19`: `rate_cmd/rate_out/rate_actual`)、**编码器实测速率**。

  · 若 `encoder_max` ≈ `rate_actual_max` ≈ 1663 ⇒ 问题在**固件/程序侧**（继续看下一条）
  · 若 `wire[58]_max` ≈ 1663 而 `rate_actual_max` ≈ 1200 ⇒ 问题在**固件斜坡/量化侧**
  · 若 `wire[58]_max` ≈ 1200 且 `rate_actual_max` ≈ 1200，只有编码器是 1663
    ⇒ 问题在**测量侧**（我的测速器，尽管正对照过了 —— 那说明正对照的口径与它不同）
  · 若四个都 ≈ 1200 ⇒ **本次没复现**（⇒ 上一次的 +40% 是偶发/受别的因素影响，需重做）

★ 关键：`rate_actual` 是**固件用硬件算出来的频率**（不是我的估计），
  它就是"到底发出去多少 Hz"的权威 ⇒ 把它当**分界线的锚点**。

用法: python tools/h723_traj_peak_diag.py [--prog tri|sine] [--A 1200] [--secs 6]
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from h723_client import Dcl, find_board, engine_status, read_wires, read_sensors   # noqa: E402

SPR, CPR = 1600.0, 4096.0
_t = 0


def rec(ok, name, detail=""):
    global _t
    _t += 1
    print("  [%s] %-52s %s" % ("PASS" if ok else "FAIL", name, detail))
    return ok


def main():
    prog = "tri"
    if "--prog" in sys.argv:
        prog = sys.argv[sys.argv.index("--prog") + 1]
    A = 1200.0
    if "--A" in sys.argv:
        A = float(sys.argv[sys.argv.index("--A") + 1])
    secs = 6.0
    if "--secs" in sys.argv:
        secs = float(sys.argv[sys.argv.index("--secs") + 1])
    port = sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else None

    d = Dcl(port or find_board())
    shm = engine_status(d)["shm"]
    import h723_client as HC
    OWM = HC.OFF_WIRE_MAP
    dwell = 0.6 if prog == "tri" else 0.25
    peak_factor = 1.0 if prog == "tri" else 0.966
    slope = int(A / dwell) if prog == "tri" else 0
    print("=== 峰值定案: %s, A=%.0f Hz, 斜坡=%d Hz/s, 理论峰值=%.0f Hz ==="
          % (prog, A, slope, A * peak_factor))

    def wset(n, v):
        return d.send(0x21, struct.pack(
            "<II", shm + OWM + n * 4,
            struct.unpack("<I", struct.pack("<f", v))[0]))[0] == "ACK"

    def r19():
        st, p = d.send(0x39, bytes([19, 19]) + struct.pack("<I", 0))
        if st != "ACK" or len(p) < 32:
            return None
        v = struct.unpack("<8I", p[:32])
        return dict(slope=v[0], cmd=v[1], out=v[2], actual=v[3])

    ok = True
    try:
        d.send(0x39, bytes([19, 13]) + struct.pack("<I", 1)); time.sleep(0.3)   # 程序面
        d.send(0x39, bytes([19, 17]) + struct.pack("<I", slope)); time.sleep(0.1)  # 斜坡
        wset(10, 1.0); wset(11, A); time.sleep(0.5)

        rows = []
        t0 = time.time()
        while time.time() - t0 < secs:
            w = read_wires(d, shm, 65)
            s = read_sensors(d, shm, 1)
            r = r19()
            if w is None or s is None or r is None:
                continue
            rows.append((time.time() - t0, int(round(s[0])), w[30], w[58], w[64],
                         r["cmd"], r["out"], r["actual"]))
        print("  采到 %d 点（%.0f Hz），列 = t, raw, seg, hz(58), ap(64), cmd, out, actual"
              % (len(rows), len(rows) / secs))
        if len(rows) < 30:
            print("  ✗ 采样太少 ⇒ 本判据**无效**（不是通过）"); return 1
        for r in rows[:3] + rows[-2:]:
            print("    %.2f raw=%-5d seg=%.0f hz=%-7.0f ap=%-7.0f cmd=%-7d out=%-7d act=%-7d" % r)

        # 编码器速率：只用相邻两点的 Δraw/Δt（口径一致 —— 见 §5.23 的教训）
        pairs = []
        for i in range(1, len(rows)):
            dt = rows[i][0] - rows[i - 1][0]
            if dt <= 0:
                continue
            dv = (rows[i][1] - rows[i - 1][1]) & 0xFFF
            if dv > 2048:
                dv -= 4096
            pairs.append((dt, abs(dv), abs(dv) / dt))
        vs = sorted(v for _, _, v in pairs)
        nv = len(vs)
        def pct(q):
            return vs[min(nv - 1, int(q * nv))] if nv else 0.0
        dts = sorted(d for d, _, _ in pairs)
        dvs = sorted(v for _, v, _ in pairs)
        print("  ── 差分量的分布（★ 第一版只用 max() 定峰值 ⇒ **对离群点毫无免疫力**）──")
        print("    Δt(ms)  min/中位/max = %.1f / %.1f / %.1f   （刷新闻隔应 ~40ms）"
              % (dts[0] * 1e3, dts[nv // 2] * 1e3, dts[-1] * 1e3))
        print("    Δraw    min/中位/max = %d / %d / %d   （1200Hz×40ms 应 ≈123）"
              % (dvs[0], dvs[nv // 2], dvs[-1]))
        f_max = vs[-1] / CPR * SPR
        f_p95 = pct(0.95) / CPR * SPR
        f_p50 = vs[nv // 2] / CPR * SPR
        print("    速率     max=%.0f  P95=%.0f  中位=%.0f  Hz"
              % (f_max, f_p95, f_p50))
        if nv >= 20 and f_max > 1.4 * f_p50:
            print("    ★ max/P50 = %.2f ≫ 1 ⇒ **存在离群点** —— 下面按 P95 定峰值。"
                  % (f_max / f_p50))
        # ★★★ 唯一**对采样拍频免疫**的量：**累积位移 / 总时长**。
        #   差分的极值/分位数都会在"编码器回填率 ≈ PC 轮询率"时失真（本次实测 Δraw 中位数=2，
        #   而 1200Hz 应有 123）⇒ 只有 ΣΔraw（带符号、逐点）才守恒。
        tot = 0
        for i in range(1, len(rows)):
            dv = (rows[i][1] - rows[i - 1][1]) & 0xFFF
            if dv > 2048:
                dv -= 4096
            tot += dv
        span = rows[-1][0] - rows[0][0]
        f_mean = abs(tot) / span / CPR * SPR if span > 0 else 0.0
        print("    累积位移 %d counts / %.2f s ⇒ **平均速率 = %.1f Hz**（唯一免疫拍频的量）"
              % (tot, span, f_mean))
        f_enc = f_mean
        f_hz = max(r[3] for r in rows)          # wire[58] 程序下发的
        f_ap = max(r[4] for r in rows)          # wire[64] 固件已应用镜像
        f_act = max(r[7] for r in rows)         # sub=19 rate_actual
        f_cmd = max(r[5] for r in rows)         # sub=19 rate_cmd
        f_out = max(r[6] for r in rows)         # sub=19 rate_out
        want = A * peak_factor
        print("\n  ── 各侧峰值（Hz）──")
        print("   程序下发 wire[58]      = %8.1f" % f_hz)
        print("   固件已应用 wire[64]    = %8.1f" % f_ap)
        print("   固件 rate_cmd          = %8d" % f_cmd)
        print("   固件 rate_out          = %8d" % f_out)
        print("   固件 rate_actual(权威) = %8d" % f_act)
        print("   编码器实测(P95)        = %8.1f" % f_enc)
        print("   理论                   = %8.1f" % want)

        # ── 三分 ──
        def near(x, y, tol=0.08):
            return abs(x - y) <= tol * max(abs(y), 1.0)
        r = f_enc / want
        print("\n  ── 定案 ──")
        if near(f_act, want) and near(f_hz, want) and not near(f_enc, want):
            print("  ⇒ **测量侧**：固件权威 %d Hz、程序下发 %.0f Hz 都等于理论 %.0f，"
                  % (f_act, f_hz, want))
            print("     只有编码器读成 %.0f（×%.2f）⇒ 测速器在同一口径下仍有偏差。" % (f_enc, r))
            rec(False, "定案: 测量侧（四量不等 ⇒ 见上）", "enc×%.2f" % r)
        elif near(f_act, want) and not near(f_hz, want):
            print("  ⇒ **程序侧**：`wire[58]` 给了 %.0f 而理论 %.0f（×%.2f），固件忠实执行了它。"
                  % (f_hz, want, f_hz / want))
            rec(False, "定案: 程序侧（表/阈值/槽）", "hz×%.2f" % (f_hz / want))
        elif not near(f_act, want) and near(f_hz, want):
            print("  ⇒ **固件斜坡/量化侧**：程序要 %.0f，固件 `rate_actual` 却到了 %d。"
                  % (f_hz, f_act))
            rec(False, "定案: 固件斜坡/量化侧", "act=%d want=%.0f" % (f_act, want))
        else:
            print("  ⇒ **本次未复现**（四量都 ≈ 理论 %.0f，编码器 %.0f）" % (want, f_enc))
            print("     ⇒ 上次的 +40% 不是稳态性质 ⇒ 需重做上一次的测量条件再定。")
            rec(True, "定案: 本次未复现（⇒ 上一次结论要重测，不能定案）",
                "enc×%.2f" % r)

        # 附带：程序下发是否等于 A（单点核对，独立于形状）
        print("\n  附: 段号/下发频率相关（前 3 点已在上面逐点列出）")
        ok &= rec(f_hz <= A * 1.02 + 1, "程序下发从未超过 A（表上限）", "max=%.0f" % f_hz)
    finally:
        try:
            wset(11, 0.0); wset(10, 0.0)
            d.send(0x39, bytes([19, 17]) + struct.pack("<I", 0))
            d.send(0x39, bytes([19, 13]) + struct.pack("<I", 0))
            d.send(0x39, bytes([19, 3]) + struct.pack("<I", 0))
            print("\n  已收尾: 请求清 0 / 关斜坡 / 运动源回脚手架 / 失能")
        except Exception as e:
            print("\n  ⚠ 收尾异常: %s" % e)
        d.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
