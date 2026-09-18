#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_stage1_ab_sine.py —— **阶段 1 判据 2**：命令通路滞后 A/B（v2：三角波 + 边沿穿越估计量）

════════════════════════════════════════════════════════════════════════════
这条判据量什么
════════════════════════════════════════════════════════════════════════════
`track sine` 一族量的是**开环前馈跟随**：轨迹生成器在哪，决定"命令到达电机"要多久。
  · PC 生成 ⇒ 命令要过**串口通路**（历史实测 **87 ms**）⇒ 位置滞后 = ω × 87 ms
  · 片内生成 ⇒ 生成器**就在拍里** ⇒ 滞后应 ≈ 0
⇒ 判据 = **两条臂的命令通路滞后之差**。

════════════════════════════════════════════════════════════════════════════
★★★ v2 为什么换波形 + 换估计量（v1 实测驱动，不是审美）
════════════════════════════════════════════════════════════════════════════
v1（6 级阶梯 + 斜坡 + LSQ `err≈−lag·ω`）跑到：残余峰峰 **36.6°** ≈ 滞后项 **36.2°**
⇒ 待测量被**斜坡限速暂态**淹没（每次跳变 Δv=1200 @3000 Hz/s ⇒ 爬升 400 ms ⇒ 暂态面积 ½Δv·t = **54°**），
  而该暂态**不是延时形状** ⇒ LSQ 报的是**混合值** ⇒ 两臂各 ~120-134 ms、差值只剩 **12 ms**（信号被共模吃掉）。

v2 的两处修：
  ① **波形**：请求只给 **HI/LO 两档**，让**斜坡自己画三角波**（Δv=1200 @480 Hz/s ⇒ 爬升 2.5 s）
     ⇒ **无限速暂态**；周期 2×2500 ms = **5000 ms 整**（2500/10 = 250 拍整，无量化残差）。
  ② **估计量**：改用**上升边沿的中点穿越时刻**（形状无关）。
     `delay_k = t(速度穿越中点) − t(请求 LO→HI 跳变) − 半坡时间`
     ★ 半坡时间 = (Δv/slope)/2 = 1.25 s ⇒ 若延时为 0，穿越恰好落在"跳变 + 1.25 s"。
     ★ 这个量**免疫**共模的斜坡滞后（它只与"跳变时刻"对齐，不比形状）。
     并且**逐边沿给一个样本** ⇒ 有真实的误差棒（不是单点结论）。

════════════════════════════════════════════════════════════════════════════
公平性（不满足即判无效）
════════════════════════════════════════════════════════════════════════════
| 项 | 值（两臂一致）|
|---|---|
| 请求波形 | HI=1500 / LO=300 方波（±480 Hz/s 斜坡 ⇒ 三角波）|
| 斜坡 | `sub=17 = 480 Hz/s`（★ 两侧同一斜率）|
| 周期 | 5000 ms（片内 2×DWELL 2500ms；PC 用同一 2.5 s 半周期）|
| 采样 | 两臂都把环周期补到 **80 ms** |
| 唯一变量 | **跳变由谁发出**（PC 的钟 vs 片的拍）|

臂：
  A   PC 在环（PC 写 `sub=1` 方波；跳变时刻**精确可知**）
  A+  同 A，但**命令延时 +300 ms** ← ★ **正对照**（证明估计量活着）
  B   片内（`sub=13 arg=1`；PC **只读**；跳变时刻由 `wire[56]` 段号跳变**检测**）

判据（都能失败；覆盖不到判 SKIP，退出码 2）：
  P2 ★正对照：`delay_A+ − delay_A ∈ [180, 420] ms`（注入 300 ms）—— 不过 ⇒ SKIP
  P1 主判据：`delay_A − delay_B ≥ 60 ms`（> 片内臂跳变检测的 ±36 ms 不确定度）
  R  样本一致性：每条臂的 `delay` 标准差 < 60 ms（否则均值无意义 ⇒ SKIP）

用法:
  python tools/h723_stage1_ab_sine.py [--no-deploy] [--dur 22] [--port COM22]
"""
import math
import os
import struct
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from h723_client import Dcl, find_board, engine_status, OFF_WIRE_MAP   # noqa: E402

HI_HZ = 1500.0
LO_HZ = 300.0
RAMP = 480.0                      # ★ 两侧同一斜率（也决定"半坡时间"）
HALF_T = (HI_HZ - LO_HZ) / RAMP    # 半周期 = 爬完 Δv 所需时间 = 1200/480 = **2.5 s**
#  ★★ 单位纪律：这是**时间**（Δv / 斜率），不是"频率差 ÷2"。
#     v2 首跑我把这里写成 `(HI_HZ-LO_HZ)/2.0 = 600` ⇒ `k = int(tt/600)` 恒为 0
#     ⇒ PC 永远发 LO ⇒ 轴降到下限就不动 ⇒ "跳变 0 / 穿越 0"（整条 A 臂静默失效）
PERIOD = 2.0 * HALF_T             # 5.0 s
MID = (HI_HZ + LO_HZ) / 2.0       # 900 Hz ← 穿越判据用
SPR = 1600.0                      # 与套件同口径（标定前）
K = 360.0 / SPR                   # Hz → °/s
MID_DPS = MID * K                 # ★ 单位对齐：`local_vel` 给的是 °/s，别拿 Hz 去比（v1 差点又栽）
HALF_RAMP = (HI_HZ - LO_HZ) / RAMP / 2.0   # 1.25 s
LOOP_S = 0.080
DCL_FILE = os.path.join(ROOT, "examples", "h723_step_traj_tri_5s.dcl")
PRED_DELAY = 0.300
SKIP = 2
RESULTS = []


def record(ok, name, detail=""):
    """★ 判据发射器（E 类闸门按这个名字找）。`ok=None` = SKIP（无效，不是通过）。"""
    tag = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    RESULTS.append((tag, name, detail))
    print("   [%s] %-32s %s" % (tag, name, detail))
    return ok


def unwrap(raws):
    out = [0.0]
    acc = 0.0
    prev = raws[0]
    for r in raws[1:]:
        d = (r - prev) % 4096
        if d > 2048:
            d -= 4096
        acc += d
        out.append(acc * 360.0 / 4096.0)
        prev = r
    return out


def local_vel(ts, ang, half=0.4):
    """滑动窗内最小二乘斜率 ⇒ 实际角速度(°/s)。★ 形状无关地只取"局部斜率"。"""
    n = len(ts)
    v = [0.0] * n
    for i in range(n):
        lo, hi = i, i
        while lo > 0 and ts[i] - ts[lo] < half:
            lo -= 1
        while hi < n - 1 and ts[hi] - ts[i] < half:
            hi += 1
        if hi - lo < 4:
            v[i] = float("nan")
            continue
        xs = ts[lo:hi + 1]
        ys = ang[lo:hi + 1]
        m = len(xs)
        mx = sum(xs) / m
        my = sum(ys) / m
        den = sum((x - mx) ** 2 for x in xs)
        v[i] = sum((xs[j] - mx) * (ys[j] - my) for j in range(m)) / den if den > 1e-12 else float("nan")
    return v


def crossings(ts, vel, level, rising=True):
    """vel 穿越 level 的时刻（线性插值）。"""
    out = []
    for i in range(1, len(vel)):
        a, b = vel[i - 1], vel[i]
        if math.isnan(a) or math.isnan(b):
            continue
        if rising and a < level <= b:
            out.append(ts[i - 1] + (ts[i] - ts[i - 1]) * (level - a) / (b - a))
        elif (not rising) and a > level >= b:
            out.append(ts[i - 1] + (ts[i] - ts[i - 1]) * (level - a) / (b - a))
    return out


def delays_from_edges(t_cross, t_rise, half_ramp=HALF_RAMP):
    """每条上升穿越配一个**最近的前置上升跳变** ⇒ 逐边沿一个 delay 样本。"""
    ds = []
    for tc in t_cross:
        cands = [tr for tr in t_rise if 0.0 < tc - tr < PERIOD * 0.9]
        if not cands:
            continue
        tr = max(cands)
        ds.append(tc - tr - half_ramp)
    return ds


def main():
    argv = sys.argv[1:]
    no_deploy = "--no-deploy" in argv
    dur = float(argv[argv.index("--dur") + 1]) if "--dur" in argv else 42.0
    #  ★ 42 s ≈ 8 个周期 ⇒ 8 个上升穿越。★ 为什么不用 20 s：边沿检测的**采样相位**每半周期滑
    #    0.25 个样本（80ms 环 vs 2500ms 半周期）⇒ 估计量有 ~10 s 周期的**锯齿**；窗太短均值有偏。
    port = argv[argv.index("--port") + 1] if "--port" in argv else None

    if not no_deploy:
        print("=== 部署片内臂程序（三角波 5s）===")
        for script, args in (("dclc.py", [DCL_FILE]), ("h723_as5600_bind.py", [])):
            r = subprocess.run([sys.executable, os.path.join(HERE, script)] + args,
                               capture_output=True, text=True, cwd=ROOT)
            tail = [l for l in ((r.stdout or "") + (r.stderr or "")).splitlines() if l.strip()][-1:]
            print("   %-22s rc=%d  %s" % (script, r.returncode, (tail or [""])[0][:64]))
            if r.returncode != 0:
                print("   ✗ 部署失败 ⇒ 中止（不许在旧程序上跑判据）")
                return 1
        time.sleep(0.5)

    d = Dcl(port or find_board())
    shm = engine_status(d)["shm"]

    def sx(n, v):
        return d.send(0x39, bytes([19, n]) + struct.pack("<I", int(v)))[0] == "ACK"

    def rd_wire(n):
        s, p = d.send(0x20, struct.pack("<I", shm + OFF_WIRE_MAP + n * 4))
        return struct.unpack("<f", p[:4])[0] if (s == "ACK" and len(p) >= 4) else None

    def wr_wire(n, v):
        return d.send(0x21, struct.pack("<II", shm + OFF_WIRE_MAP + n * 4,
                                        struct.unpack("<I", struct.pack("<f", v))[0]))[0] == "ACK"

    def snap():
        s, p = d.send(0x39, bytes([19, 0]) + struct.pack("<I", 0))
        if s == "ACK" and len(p) >= 96:
            u = struct.unpack("<24I", p[:96])
            return u[8], (u[20] >> 9) & 1
        return None, None

    def prep(program_face):
        sx(1, 0); sx(6, 0)
        sx(17, int(RAMP))              # ★★ 两侧同一斜率
        sx(13, 1 if program_face else 0)
        sx(5, 1)                       # ★ 极性：PE9=1 才是使能
        sx(2, 0); sx(4, 60000)
        sx(22, 1); sx(20, 10)          # 阶段 0：拍内反馈 ~1 kHz
        wr_wire(10, 0.0)
        wr_wire(11, HI_HZ)
        time.sleep(0.3)

    def pe9():
        return snap()[1]

    # ── 采样：三臂共用（★ 都把环周期补到 80 ms ⇒ 采样率一致）──
    def run_pc(cmd_delay=0.0):
        """PC 臂：自己发方波跳变。返回 (ts, raws, 上升跳变时刻 list)。"""
        # 预充：先把速度顶到 HI（三角波稳态需要先到顶）
        prep(program_face=False)
        sx(3, 1)
        t_ch = time.time()
        while time.time() - t_ch < 6.0:
            raw, _ = snap()
            if raw is not None:
                break
            time.sleep(0.05)
        sx(1, int(HI_HZ))
        t_settle = time.time()
        while time.time() - t_settle < 4.0:      # 爬 1500/480 = 3.1 s
            time.sleep(0.05)
        ts, raws, t_rise = [], [], []
        hist = []
        t0 = time.time()
        last_req = None
        while True:
            it0 = time.time()
            tt = it0 - t0
            if tt >= dur:
                break
            # 方波：第 k 个半周期 k=0 → LO（从顶上开始下降）
            k = int(tt / HALF_T)
            req = HI_HZ if (k % 2 == 1) else LO_HZ
            send = req
            if cmd_delay > 0.0:
                hist.append((tt, req))
                cut = tt - cmd_delay
                send = LO_HZ
                for (th, hh) in hist:
                    if th <= cut:
                        send = hh
                    else:
                        break
            if req != last_req:
                if req == HI_HZ:
                    t_rise.append(time.time() - t0)     # ★ 意图跳变时刻（写之前）
                last_req = req
            sx(1, int(send))
            raw, _ = snap()
            if raw is not None:
                ts.append(time.time() - t0); raws.append(raw)
            rem = LOOP_S - (time.time() - it0)
            if rem > 0:
                time.sleep(rem)
        sx(1, 0); sx(3, 0); time.sleep(0.4)
        return ts, raws, t_rise

    def run_chip():
        """片内臂：PC **只读**。跳变时刻从 `wire[56]` 段号跳变**检测**（±36 ms）。"""
        prep(program_face=True)
        wr_wire(10, 1.0)
        sx(3, 1)
        time.sleep(8.0)                  # 让片内三角波进稳态（不复位它的相位）
        ts, raws, t_rise = [], [], []
        prev_seg = None
        prev_t = None
        t0 = time.time()
        while True:
            it0 = time.time()
            if it0 - t0 >= dur:
                break
            raw, _ = snap()
            seg = rd_wire(56)
            now = time.time() - t0
            if raw is not None:
                ts.append(now); raws.append(raw)
            if seg is not None:
                seg = int(round(seg))
                if prev_seg is not None and seg != prev_seg:
                    # LO→HI = 段号 2→1 ⇒ 上升跳变。真跳变在两次采样之间 ⇒ 取中点
                    if prev_seg == 2 and seg == 1 and prev_t is not None:
                        t_rise.append(0.5 * (prev_t + now))
                prev_seg, prev_t = seg, now
            rem = LOOP_S - (time.time() - it0)
            if rem > 0:
                time.sleep(rem)
        wr_wire(10, 0.0); time.sleep(0.3); sx(3, 0)
        return ts, raws, t_rise

    def analyse(tag, ts, raws, t_rise):
        ang = unwrap(raws)
        vel = local_vel(ts, ang)
        cr = crossings(ts, vel, MID_DPS, rising=True)
        ds = delays_from_edges(cr, t_rise)
        if len(ds) < 3:
            print("   %-4s ✗ 边沿样本不足（穿越 %d / 跳变 %d）" % (tag, len(cr), len(t_rise)))
            return None
        m = sum(ds) / len(ds)
        var = sum((x - m) ** 2 for x in ds) / max(1, len(ds) - 1)
        sd = var ** 0.5
        print("   %-4s 环频 %.1f Hz  跳变 %d  穿越 %d  ⇒ **delay %+.1f ms** (n=%d, sd %.1f ms)"
              "  %s" % (tag, len(ts) / max(1e-9, ts[-1] - ts[0]), len(t_rise), len(cr),
                        m * 1000, len(ds), sd * 1000,
                        " ".join("%+.0f" % (x * 1000) for x in ds)))
        return dict(delay=m, sd=sd, n=len(ds))

    try:
        if pe9() is None:
            record(None, "前置：链路", "读不到快照 ⇒ SKIP")
            return SKIP

        print("\n=== A  PC 在环（PC 发方波跳变）===")
        a = analyse("A", *run_pc())
        print("\n=== A+ 正对照（PC 在环 + 命令延时 %.0f ms）===" % (PRED_DELAY * 1000))
        ap = analyse("A+", *run_pc(cmd_delay=PRED_DELAY))
        print("\n=== B  片内（sub=13 arg=1，PC 只读）===")
        b = analyse("B", *run_chip())

        print("\n=== 判据 ===")
        if not (a and ap and b):
            record(None, "P1 主判据", "有臂样本不足 ⇒ 覆盖不到 ⇒ SKIP")
            return SKIP

        # R：样本一致性（先判，否则均值无意义）
        r_ok = record(max(a["sd"], b["sd"]) < 0.060, "R 边沿样本一致性 (sd<60ms)",
                      "sd: A %.0f ms / B %.0f ms" % (a["sd"] * 1000, b["sd"] * 1000))
        if not r_ok:
            record(None, "P1 主判据", "样本太散 ⇒ 均值无意义 ⇒ SKIP")
            return SKIP

        # P2 正对照
        d_ctrl = (ap["delay"] - a["delay"]) * 1000
        p2 = record(180 <= d_ctrl <= 420, "P2 正对照 +300ms ∈[180,420]",
                    "实测增量 %+.0f ms" % d_ctrl)
        if not p2:
            print("   ⇒ ★ 正对照不过 ⇒ 估计量对已知延时无响应 ⇒ 判据无效 ⇒ SKIP（不是 B 通过）")
            return SKIP

        # P1
        gain = (a["delay"] - b["delay"]) * 1000
        p1 = record(gain >= 60.0, "P1 delay_A − delay_B ≥ 60 ms",
                    "A %+.0f − B %+.0f = **%+.0f ms**" % (a["delay"] * 1000, b["delay"] * 1000, gain))

        print("\n   ⇒ 结论：PC 命令通路滞后 **%+.0f ms**；片内 **%+.0f ms**（差 **%+.0f ms** = 收益）"
              % (a["delay"] * 1000, b["delay"] * 1000, gain))
        print("     （片内臂跳变检测不确定度 ±36 ms；两臂各自 sd 见上）")
        return 0 if p1 else 1
    finally:
        try:
            wr_wire(10, 0.0); sx(1, 0); sx(17, 0); sx(13, 0); sx(22, 0); sx(20, 100); sx(3, 0)
        except Exception:
            pass
        d.close()


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)
    rc = main()
    print("\nSummary: %d PASS / %d FAIL / %d SKIP ⇒ rc=%d"
          % (sum(1 for t, _, _ in RESULTS if t == "PASS"),
             sum(1 for t, _, _ in RESULTS if t == "FAIL"),
             sum(1 for t, _, _ in RESULTS if t == "SKIP"), rc))
    sys.exit(rc)
