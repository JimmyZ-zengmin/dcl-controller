#!/usr/bin/env python3
"""
h723_tick_determinism.py — 拍内执行时间的**可重复性**与**干扰通道**实验（E1 的系统级形态）

★★ 关键仪器发现（`main.c:3400-3404`）
      uint8_t was_run = SHM_U8(g_shm, OFF_CTRL_ENGINE_RUN);
      if (!was_run) { stats_reset(); }        /* STOP→START 转变时清统计 */
  ⇒ **`0x12 STOP` → `0x11 START` 清统计但不丢程序表。**
  这一条同时解决三个问题（第一版工具就是被它们污染的）:
    ① `EXEC_MAX` 被**部署那一拍**主导（热重载 memcpy）⇒ 先 deploy 再 STOP/START 即可排除
    ② `EXEC_MIN` 是**空引擎基线**（RESET 后、deploy 前的窗口）⇒ 同上
    ③ 重复测量**不需要重新 deploy** ⇒ R 个独立稳态窗口，成本极低
  ★ 副产品：**重载拍可以被精确测出来** = （deploy 后立刻读）−（STOP/START 后读），
    不需要 `g_reload_cyc`（它在 0x22 的地址范围之外，读不到）。

判据（三值，缺一不可；"测不出差异"绝不允许自动变成"通过"）
  R0 ★ 正对照（已知答案）：路数减半 ⇒ EXEC_MAX 必须明显变小。不成立 ⇒ 全部判**无效**
  R1 ★ **稳态可重复性**：同一配置 R 个窗口，`(EXEC_MIN, EXEC_MAX)` 必须逐窗口相同
  R1b ★★ **div0 程序: EXEC_MIN == EXEC_MAX**（每拍都跑全部路由 ⇒ 每拍应等长）
       —— 这是"拍级确定性"最直接的一条判据
  R2 MDMA 干涉通道（黑匣子开/关）；R3 铁律 0（主机是否轮询）
       ★ 三值判定：效应 > 窗口内散布 ⇒ 有影响；效应==0 且散布==0 ⇒ 无影响；
         否则 ⇒ **分辨率不足（SKIP，不是 PASS）**
  R4 拍周期极差
  R5 重载拍 = 统计包含部署 − 稳态

用法
  python tools/h723_tick_determinism.py --n 128 --reps 6
退出码: 0 = 无 FAIL / 1 = 有 FAIL / 2 = 前置或正对照不成立（判无效）
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

cmd_pinpat, cmd_deploy, cmd_status = 0x39, 0x10, 0x38
cmd_stop, cmd_start, cmd_reset, cmd_burst = 0x12, 0x11, 0x13, 0x22
OFF_T_SAMPLES, OFF_T_PMIN, OFF_T_PMAX = 0x18, 0x1C, 0x20
OFF_T_EMIN, OFF_T_EMAX = 0x24, 0x28
OFF_T_ESUM_LO, OFF_T_ESUM_HI, OFF_T_ESUM_N = 0x3860, 0x3864, 0x3868   # ★ 均值域(和+同口径分母)
OFF_T_OVERRUN = 0x3850
OP_PID, SRC_CONST, DST_WIRE, ACTIVE = 0x05, 2, 2, 1
TICK_TB = 20000           # 100 µs @ TIM5 5 ns


def mk(op, div, n):
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  ACTIVE, i, 1, 0, 0, div, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    return struct.pack("<HHH", n, n, 1) + routes + params + b"\x00" * 16


def rd(dcl, addr, nwords):
    sts, p = dcl.send(cmd_burst, struct.pack("<IH", addr, nwords), expect_len=None)
    if sts != "ACK" or len(p) < 4 * nwords:
        return None
    return struct.unpack("<%dI" % nwords, p[:4 * nwords])


def timing(dcl, shm):
    """★ 2026-09-18: 除 min/max 外再取**累积量(和 + 拍数)** ⇒ 得到**均值**。
    为什么必须补: `EXEC_MAX` 被黑匣子每 1024 拍的那次快照占住（`blackbox.c:310`），
    `EXEC_MIN` 会被空引擎窗口污染 ⇒ **单看 min/max 会得出"抖动 120 TB"的错结论**。
    均值才是"典型拍"的度量。（同族: §5.27/§5.28 —— 极值不免疫采样拍频。）"""
    v = rd(dcl, shm + OFF_T_SAMPLES, 5)
    if v is None:
        return None
    n = v[0]
    ov = rd(dcl, shm + OFF_T_OVERRUN, 1)
    # ★★ 分子分母必须**同一次突发**读出（§5.23）。
    #   分两次读 ⇒ 两次之间窗口前进几十拍 ⇒ 均值被系统性抬高
    #   （本工具第一版实测: 均值 8550.76 **大于** 同窗口 EXEC_MAX 8504 —— 不可能, 一算就露馅）。
    sm = rd(dcl, shm + OFF_T_ESUM_LO, 3)
    total = (sm[0] | (sm[1] << 32)) if sm else 0
    sn = sm[2] if sm else 0
    return dict(samples=n, pmin=v[1], pmax=v[2], emin=v[3], emax=v[4],
                sum=total, sum_n=sn, mean=(total / sn if sn else 0.0),
                ov=(ov[0] if ov else -1))


def bb(dcl, off):
    dcl.send(cmd_pinpat, struct.pack("<BB", 4, 1 if off else 0), expect_len=None)


def install(dcl, n, settle=1.2):
    """装好程序（会含一次重载拍），返回**含重载拍**的那次读数。"""
    dcl.send(cmd_stop); time.sleep(0.15)
    dcl.send(cmd_start); time.sleep(0.15)
    sts, p = dcl.send(cmd_deploy, mk(OP_PID, 0, n), expect_len=None)
    if sts != "ACK":
        return None
    time.sleep(settle)
    return timing(dcl, shm_glob[0])


def steady(dcl, settle=1.2):
    """★ STOP→START 清统计但保留程序 ⇒ 纯稳态窗口（无重载拍、无空引擎窗口）"""
    dcl.send(cmd_stop); time.sleep(0.15)
    dcl.send(cmd_start); time.sleep(0.15)
    time.sleep(settle)
    return timing(dcl, shm_glob[0])


shm_glob = [0]


def window(dcl, settle, poll=False):
    r = steady(dcl, settle)
    if poll:
        for _ in range(80):
            dcl.send(cmd_status, expect_len=None)
    return r


def stats(rows, key):
    xs = sorted(r[key] for r in rows)
    return xs[0], xs[-1], len(set(xs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("DCL_PORT"))
    ap.add_argument("--n", type=int, default=128)
    ap.add_argument("--reps", type=int, default=6)
    ap.add_argument("--settle", type=float, default=1.2)
    a = ap.parse_args()

    dcl = Dcl(a.port)
    print("端口 = %s" % dcl.port); time.sleep(1.0)
    res, skip = [], []
    try:
        sts, p = dcl.send(cmd_status, expect_len=51)
        if sts != "ACK" or len(p) < 51:
            print("!! 0x38 失败"); return 2
        shm_glob[0] = struct.unpack("<I", p[23:27])[0]
        print("SHM = 0x%08X" % shm_glob[0])
        if not shm_glob[0]:
            return 2
        bb(dcl, 0); time.sleep(0.3)

        # ── R0 正对照 ───────────────────────────────────────────────────
        print("\n[R0 正对照] 路数减半 ⇒ 稳态 EXEC_MAX 必须明显变小")
        install(dcl, a.n, a.settle)
        f = steady(dcl, a.settle)
        install(dcl, a.n // 2, a.settle)
        h = steady(dcl, a.settle)
        if not f or not h:
            print("  !! 读数失败 ⇒ 判无效"); return 2
        print("  n=%-4d 稳态 EXEC_MIN=%-6d EXEC_MAX=%-6d (%d / %d cyc)"
              % (a.n, f["emin"], f["emax"], f["emin"] * 2, f["emax"] * 2))
        print("  n=%-4d 稳态 EXEC_MIN=%-6d EXEC_MAX=%-6d" % (a.n // 2, h["emin"], h["emax"]))
        r0 = h["emax"] < f["emax"]
        res.append(("R0 正对照: 路数减半 ⇒ 稳态 EXEC_MAX 变小", r0))
        if not r0:
            print("  ✗ 正对照不成立 ⇒ 本次全部判无效"); return 2

        # ── install 一次，之后只做 STOP/START 窗口 ──────────────────────
        with_reload = install(dcl, a.n, a.settle)
        print("\n[重载拍] 含部署的窗口 EXEC_MAX=%d" % (with_reload["emax"] if with_reload else -1))

        groups = [("bb=on  poll=off", False, False), ("bb=off poll=off", True, False),
                  ("bb=on  poll=on ", False, True), ("bb=off poll=on ", True, True)]
        got = {}
        for name, off, poll in groups:
            bb(dcl, off); time.sleep(0.25)
            rows = []
            print("\n[%s] R=%d 个稳态窗口" % (name, a.reps))
            for i in range(a.reps):
                r = window(dcl, a.settle, poll)
                if not r:
                    print("  第 %d 个窗口读数失败" % (i + 1)); rows = None; break
                rows.append(r)
                print("  #%d  EXEC %d..%d  **均值 %.2f**  n=%d  PERIOD %d..%d  ov=%d"
                      % (i + 1, r["emin"], r["emax"], r["mean"], r["samples"],
                         r["pmin"], r["pmax"], r["ov"]))
            got[name] = rows

        # ── R1 可重复性 + R1b div0 等长 ─────────────────────────────────
        print()
        for name, off, poll in groups:
            rows = got.get(name)
            if not rows:
                res.append(("R1 可重复性 [%s]" % name.strip(), False)); continue
            lo_e, hi_e, k_e = stats(rows, "emin")
            lo_x, hi_x, k_x = stats(rows, "emax")
            means = [r["mean"] for r in rows]
            mlo, mhi = min(means), max(means)
            ok = (k_e == 1 and k_x == 1)
            res.append(("R1 稳态可重复性 [%s]" % name.strip(), ok))
            print("  R1 [%s] EXEC_MIN %d..%d (%d 种)  EXEC_MAX %d..%d (%d 种) %s"
                  % (name.strip(), lo_e, hi_e, k_e, lo_x, hi_x, k_x,
                     "✓ 逐窗口完全相同" if ok else "✗ 有抖动"))
            # ★★ 均值判据（新仪器的核心）: 典型拍的宽度。
            #   极值只说明"存在偏离"，均值才说明"**典型值有多集中**"。
            #   判据: 均值的窗口间极差 ≤ 1 TB tick（0.5 cyc 级别）⇒ 典型拍可复现。
            print("       ★ 均值 %d 个窗口: %.2f .. %.2f TB tick（跨窗口极差 %.2f）"
                  % (len(means), mlo, mhi, mhi - mlo))
            print("         典型拍与最小拍的差 = %.2f TB = %.1f cyc；与最大拍的差 = %.2f TB"
                  % (mlo - lo_e, (mlo - lo_e) * 2, hi_x - mlo))
            res.append(("R1m 典型拍（均值）跨窗口极差 ≤ 1 TB [%s]" % name.strip(),
                        (mhi - mlo) <= 1.0))
            res.append(("R1b div0 程序 EXEC_MIN==EXEC_MAX [%s]" % name.strip(), lo_x == hi_e and k_e == 1 and k_x == 1))

        # ── R2/R3 三值判定 ─────────────────────────────────────────────
        def spread(name, key):
            rows = got.get(name)
            if not rows:
                return None
            xs = [r[key] for r in rows]
            return max(xs) - min(xs), max(xs)

        # ★★ 2026-09-18: 干扰判据用**均值**而不是 `emax`。
        #   理由: 均值是"典型拍"的度量, 且实测跨窗口极差 **0.01 TB (0.02 cyc)**,
        #   而 `emax` 的跨窗口极差是 2~4 TB ⇒ **均值的分辨率高两个量级**。
        #   用 `emax` 会把 0.3 cyc 量级的效应淹没在噪声底里, 只能判 SKIP。
        print()
        for label, ka, kb, key in (("R2 MDMA 干涉通道（bb=on vs off, poll=off）",
                                    "bb=on  poll=off", "bb=off poll=off", "mean"),
                                   ("R3 铁律 0（poll=on vs off, bb=on）",
                                    "bb=on  poll=on ", "bb=on  poll=off", "mean")):
            ra, rb = got.get(ka) or [], got.get(kb) or []
            if not ra or not rb:
                res.append((label, False)); continue
            va = [r[key] for r in ra]
            vb = [r[key] for r in rb]
            noise = max(max(va) - min(va), max(vb) - min(vb))
            eff = abs(sum(va) / len(va) - sum(vb) / len(vb))
            verdict = ("有影响" if eff > max(noise, 0.05) else
                       ("无影响（可检测）" if noise <= 0.05 else "**分辨率不足**"))
            print("  %s\n     效应 = %.3f TB tick (%.2f cyc/拍)   窗口内散布 = %.3f TB"
                  % (label, eff, eff * 2, noise))
            if verdict == "有影响":
                print("     ⇒ **该通道到得了扫描体**，典型拍代价 ≈ %.2f cyc" % (eff * 2))
                res.append((label + " 判定为「有影响」", False))
            elif verdict == "无影响（可检测）":
                print("     ⇒ 无影响，且噪声底 %.3f TB ≤ 0.05 ⇒ **判定可检测**"
                      "（按 AMC 20-193 §5.3 note b，留证据即可，不必缓解）" % noise)
                res.append((label + " 判定为「无影响且可检测」", True))
            else:
                print("     ⇒ **判 SKIP：仪器分辨率不够，不许读成 PASS**")
                skip.append(label)

        # ── R4 拍周期 ──────────────────────────────────────────────────
        rows = got.get("bb=on  poll=off") or []
        if rows:
            r = rows[-1]
            span = r["pmax"] - r["pmin"]
            print("\n  R4 拍周期: MIN=%d MAX=%d ⇒ 极差 %d TB = %d cyc (%.3f%% 拍长)"
                  % (r["pmin"], r["pmax"], span, span * 2, 100.0 * span * 2 / TICK_TB))
            res.append(("R4 拍周期极差 < 1%% 拍长", 100.0 * span * 2 / TICK_TB < 1.0))

        # ── R5 重载拍 ──────────────────────────────────────────────────
        rows = got.get("bb=on  poll=off") or got.get("bb=on  poll=on ") or []
        if rows and with_reload:
            smax = max(r["emax"] for r in rows)
            print("\n  R5 重载拍: 含部署窗口 EXEC_MAX=%d, 稳态 EXEC_MAX=%d ⇒ 重载增量 **%d TB = %d cyc**"
                  % (with_reload["emax"], smax, with_reload["emax"] - smax,
                     (with_reload["emax"] - smax) * 2))
            print("     ★ 这就是此前只能给「上界 188 tick」的那个量 —— 现在是**精确值**")
    finally:
        bb(dcl, 0)
        dcl.send(cmd_stop); time.sleep(0.2); dcl.send(cmd_reset); time.sleep(0.3)
        dcl.send(cmd_start); time.sleep(0.3)
        dcl.close()

    print("\n=== 断言 ===")
    for k, v in res:
        print("  [%s] %s" % ("PASS" if v else "FAIL", k))
    for s in skip:
        print("  [SKIP] %s —— 分辨率不足，**不是 PASS**" % s)
    bad = [k for k, v in res if not v]
    print("\n%d 项: %d PASS / %d FAIL / %d SKIP" % (len(res), len(res) - len(bad), len(bad), len(skip)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
