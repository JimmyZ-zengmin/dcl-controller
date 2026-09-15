#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_speed_profile.py — 速度变化曲线实测 (低速 → 极速 → 中速 → 低速)
====================================================================
★ 为什么用**双通道**:
  串口单通道要么发命令、要么读编码器, 一次环周期要 2 条命令 ⇒ 只有 ~13.5Hz,
  而 31000Hz 时轴速 6975 °/s, 13.5Hz 采样 ⇒ 每采样段 517° **远超 360° ⇒ 混叠**。
  ⇒ 本工具: **UART 专门发调速命令**, **编码器走 SWD 直读 `g_as_raw_v`(实测 ~500 采样/s)**
     500Hz 采样 ⇒ 每段最多 14° ⇒ **完全无混叠**, 曲线可信。

判据:
  · ω_cmd(t): 由我下发的频率 × 0.225 得到 (°/s)
  · ω_meas(t): 由 SWD 采到的 raw 做**强制单向**解卷绕 + 平滑得到
  · 比值 ω_meas/ω_cmd = 1 ⇒ 跟得上; < 1 ⇒ **丢步/滑移**(曲线下凹)
  · 滞后: 在 0~600ms 内搜索使 ω_meas(t) 与 ω_cmd(t−lag) 最贴合的 lag
  · 编码器健康: 同读 g_as_err_n / g_i2c_nak_n, 全程不得增长

用法: python h723_speed_profile.py
      (需用装了 pyocd 的 Python: C:/Users/min/AppData/Local/Programs/Python/Python313/python.exe)
"""
import os
import re
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAP = r"D:\STM\8.29 AIAutoFactior\9.10 H723newest\build\dcl_h723.map"
PORT = "COM21"
SPR = 1600.0
DEG_PER_LSB = 360.0 / 4096.0
K = 360.0 / SPR                     # Hz → °/s  (0.225)
# ★ SWD 采样限速: 实测 ~500 次/s 时读数可信; 冲到 1900 次/s 会**读到陈旧值**
#   (曲线出现 "0 / 合理值" 交替)。这与 i2c_diag 里验证过的速率一致。
SWD_HZ = 500.0


def crc16(d, c=0xFFFF):
    for b in d:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


def fr(cmd, pl=b""):
    body = bytes([cmd, len(pl) & 0xFF, (len(pl) >> 8) & 0xFF]) + pl
    return bytes([0xC0]) + body + struct.pack("<H", crc16(body))


def sym(name):
    txt = open(MAP, encoding="utf-8", errors="replace").read()
    m = re.search(r"0x([0-9a-fA-F]{8,16})\s+" + re.escape(name) + r"\b", txt)
    return int(m.group(1), 16) if m else None


# ----------------------------------------------------------------- 速度曲线
# (时刻, 频率Hz) 关键点 —— 线性插值
def build_profile(low=500, mx=31000, mid=8000,
                  t_low=3.0, t_up=10.0, t_hold=3.0, t_dn1=6.0, t_dn2=4.0):
    """★ 关键点必须**无重复时间戳** —— 早期版本写成
       `ks.append((t+t_up, mx)); t += t_up; ks.append((t, mx))` ⇒ 同一时刻两个点,
       平台宽度为 0 ⇒ 三个平台段只识别出一个 (真实踩过)。"""
    ks = [(0.0, low), (t_low, low)]
    t = t_low + t_up
    ks.append((t, mx))
    t += t_hold
    ks.append((t, mx))
    t += t_dn1
    ks.append((t, mid))
    t += t_hold
    ks.append((t, mid))
    t += t_dn2
    ks.append((t, low))
    t += t_hold
    ks.append((t, low))
    return ks, t


def omega_cmd(ks, t):
    if t <= ks[0][0]:
        return ks[0][1] * K
    for i in range(1, len(ks)):
        t0, f0 = ks[i - 1]
        t1, f1 = ks[i]
        if t <= t1:
            if t1 <= t0:
                return f1 * K
            return (f0 + (f1 - f0) * (t - t0) / (t1 - t0)) * K
    return ks[-1][1] * K


def main():
    import serial
    from pyocd.core.helpers import ConnectHelper

    a_raw = sym("g_as_raw_v")
    a_err = sym("g_as_err_n")
    a_nak = sym("g_i2c_nak_n")
    if not a_raw:
        print("✗ .map 里找不到 g_as_raw_v")
        return 1

    ks, total = build_profile()
    print("=" * 82)
    print("速度变化曲线: 500 → 31000 → 8000 → 500 Hz   (%.0fs)" % total)
    print("=" * 82)
    print("  UART 发命令 + SWD 采编码器(约500Hz)   ⇒ 无混叠")

    ser = serial.Serial(PORT, 115200, timeout=0.02)

    def cmd(payload, wait=0.06, tag=""):
        """★ 返回 (status, payload); 失败要能被看见 —— 血证: 不检查返回值时,
           复位后固件还没起来 ⇒ ena/enapol 全没生效 ⇒ 我在"轴没使能"下跑完整条曲线,
           实测速度全 0 却毫无提示。"""
        ser.reset_input_buffer()
        ser.write(fr(0x39, payload))
        ser.flush()
        buf = b""
        t0 = time.time()
        while time.time() - t0 < wait:
            b = ser.read(256)
            if b:
                buf += b
                if len(buf) >= 6 and buf[0] == 0xC1:
                    n = buf[2] | (buf[3] << 8)
                    if len(buf) >= 6 + n:
                        return buf[1], buf[4:4 + n]
            else:
                time.sleep(0.0005)
        if tag:
            print("  ⚠ 命令 %s 无应答 (%d 字节)" % (tag, len(buf)))
        return None, b""

    def st():
        s, p = cmd(bytes([19, 0]), tag="st")
        if s is None or len(p) < 96:
            return None
        u = struct.unpack("<24I", p[:96])
        return dict(hz=u[0], dir=u[1], ena=u[2], raw=u[8], enapol=u[12],
                    pe9=(u[20] >> 9) & 1, ccr1=u[22], arr=u[23])

    sess = ConnectHelper.session_with_chosen_probe(
        target_override="stm32h723xx",
        options={"connect_mode": "halt", "frequency": 2000000}, blocking=False)
    if sess is None:
        print("✗ 找不到探针")
        return 1

    with sess:
        t = sess.target
        t.halt()
        t.resume()                       # ★ 必须 resume, 否则固件不跑
        rec = []
        e0 = n0 = None
        try:
            # ---------- 前置条件: 必须逐条确认成立, 否则后面全是空跑 ----------
            got = None
            for k in range(12):          # 复位后固件可能还没起来, 最多等 ~5s
                got = st()
                if got:
                    break
                time.sleep(0.4)
            if got is None:
                print("✗ 板子 5s 内无应答 (复位后没起来? 端口? )")
                return 1
            print("  基线: 极性=%d ena=%d PE9=%d raw=%d" % (got["enapol"], got["ena"], got["pe9"], got["raw"]))
            cmd(bytes([19, 5, 1, 0, 0, 0]), tag="enapol=1")
            time.sleep(0.2)
            cmd(bytes([19, 3, 1, 0, 0, 0]), tag="ena=1")
            time.sleep(0.25)
            cmd(bytes([19, 2, 0, 0, 0, 0]), tag="dir=0")
            cmd(bytes([19, 4]) + (200000).to_bytes(4, "little"), tag="limit")
            got = st()
            if got is None:
                print("✗ 设置后读不到状态")
                return 1
            print("  设置后: 极性=%d ena=%d PE9=%d  (期望 极性=1 ena=1 PE9=1)"
                  % (got["enapol"], got["ena"], got["pe9"]))
            if got["ena"] != 1 or got["pe9"] != 1 or got["enapol"] != 1:
                print("  ✗✗ **前置条件不成立 ⇒ 中止** (在'轴没使能'下跑曲线毫无意义)")
                return 1
            # ★ 编码器存活: **必须一边转一边看** ——
            #   "读数不变" 有两种含义: ① 真的静止  ② 读路径冻结。
            #   只有"主动制造一个已知动作"才能分开 (静止时读数本来就该是常数)。
            cmd(bytes([19, 1]) + (200).to_bytes(4, "little"), tag="rate=200")
            time.sleep(0.1)
            probe = [t.read32(a_raw) for _ in range(200)]
            cmd(bytes([19, 1]) + (0).to_bytes(4, "little"))
            time.sleep(0.15)
            print("  编码器存活(转动中): raw 唯一值 %d, %d..%d  %s"
                  % (len(set(probe)), min(probe), max(probe),
                     "✓ 在更新" if len(set(probe)) > 3 else "✗ **冻结**(轴动但读数不变)"))
            if len(set(probe)) <= 3:
                print("  ✗✗ 编码器值冻结 ⇒ 先修观测面 (见 h723_motion_probe.py asdiag/liveness)")
                return 1

            e0 = t.read32(a_err)
            n0 = t.read32(a_nak)
            print("  开始跑曲线…  (SWD 采样限速 %d/s —— 更快会读到陈旧值)" % SWD_HZ)
            t0 = time.perf_counter()
            last_f = None
            last_tx = 0.0
            ncmd = 0
            nstale = 0
            period = 1.0 / SWD_HZ
            while True:
                tic = time.perf_counter()
                now = tic - t0
                if now >= total:
                    break
                f = omega_cmd(ks, now) / K
                if f < 30:
                    f = 30
                if last_f is None or abs(f - last_f) >= 120 or (tic - last_tx) > 0.25:
                    cmd(bytes([19, 1]) + int(round(f)).to_bytes(4, "little"))
                    last_f = f
                    last_tx = tic
                    ncmd += 1
                v = t.read32(a_raw)
                if rec and rec[-1][1] == v:
                    nstale += 1
                rec.append((time.perf_counter() - t0, v))
                # ★ 混合等待: sleep 大部分 + 忙等到点。
                #   纯 sleep 会被 Windows 的 ~15ms 粒度拖慢 (500/s 曾变成 184/s);
                #   纯忙等又浪费。目标: **时间戳间隔均匀** (解卷绕对间隔敏感)。
                sl = period - (time.perf_counter() - tic)
                if sl > 0.002:
                    time.sleep(sl - 0.001)
                while time.perf_counter() - tic < period:
                    pass
            print("  曲线上共下发 %d 条调速命令 (平均 %.1f 条/s) ; 连续同值比例 %.0f%%"
                  % (ncmd, ncmd / max(total, 1e-6), nstale / max(len(rec) - 1, 1) * 100))
        finally:
            cmd(bytes([19, 1]) + (0).to_bytes(4, "little"))
            cmd(bytes([19, 3, 0, 0, 0, 0]))
            if e0 is not None:
                e1 = t.read32(a_err)
                n1 = t.read32(a_nak)
                print()
                print("  编码器健康: g_as_err_n %d → %d (%+d) ; g_i2c_nak_n %d → %d (%+d)"
                      % (e0, e1, e1 - e0, n0, n1, n1 - n0))
            ser.close()

    if len(rec) < 50:
        print("✗ SWD 采样点太少 (%d)" % len(rec))
        return 1
    span = rec[-1][0] - rec[0][0]
    print("  SWD 采样: %d 点 / %.2fs = **%.0f 采样/s**" % (len(rec), span, len(rec) / span))

    # ---- 解卷绕 (强制单向) + 滑窗斜率 ----
    acc = 0
    ang = []
    for i in range(len(rec)):
        if i:
            acc += (rec[i][1] - rec[i - 1][1]) & 0xFFF
        ang.append(acc * DEG_PER_LSB)
    W = 9                                  # ≈20ms 平滑窗 (仅用于报表)
    vel = [None] * len(rec)
    for i in range(W, len(rec)):
        dt = rec[i][0] - rec[i - W][0]
        if dt > 1e-6:
            vel[i] = (ang[i] - ang[i - W]) / dt

    # ★★ 曲线用**滑窗斜率**: v(t) = [ang(t+H) − ang(t−H)] / 2H
    #    理由: SWD 读数仍有陈旧样本 ⇒ **瞬时值会抖**;
    #    而滑窗斜率只用"两端的总位移", 对个别陈旧样本不敏感 ⇒ 形状可信。
    HW = 0.25
    def vel_win(tt):
        lo = hi = None
        for i in range(len(rec)):
            if rec[i][0] <= tt - HW:
                lo = i
            if rec[i][0] <= tt + HW:
                hi = i
        if lo is None or hi is None or hi <= lo:
            return None
        dt = rec[hi][0] - rec[lo][0]
        return (ang[hi] - ang[lo]) / dt if dt > 1e-6 else None

    def v_at(tt):
        return vel_win(tt)

    # ---- 滞后搜索 ----
    def rms_for(lag):
        s = 0.0
        n = 0
        for i in range(0, len(rec), 7):
            tt = rec[i][0] - lag
            if tt < 0:
                continue
            v = vel_win(rec[i][0])
            if v is None:
                continue
            s += (v - omega_cmd(ks, tt)) ** 2
            n += 1
        return (s / n) ** 0.5 if n else 1e9

    best_lag = min((round(x * 0.02, 3) for x in range(0, 31)), key=rms_for)
    print("  ★ 等效滞后 = %.0f ms  (RMS 残差 %.1f °/s)" % (best_lag * 1000, rms_for(best_lag)))

    # ---- 各平台段 ----
    plats = []
    for i in range(1, len(ks)):
        if abs(ks[i][1] - ks[i - 1][1]) < 1e-6:
            plats.append((ks[i - 1][0], ks[i][0], ks[i][1]))
    print()
    print("  平台段 | 时刻(s)     | 指令Hz | 指令°/s | 实测°/s | 比值 | 判读")
    print("  -------+-------------+--------+---------+---------+------+------")
    for (p0, p1, f) in plats:
        vs = [vel_win(rec[i][0]) for i in range(len(rec))
              if p0 + 0.5 < rec[i][0] < p1 - 0.2]
        vs = [v for v in vs if v is not None]
        if not vs:
            continue
        mv = sum(vs) / len(vs)
        cmdv = f * K
        r = mv / cmdv if cmdv else 0
        print("  %6.0f | %5.1f~%5.1f | %6d | %7.0f | %7.0f | %.3f | %s"
              % (f, p0, p1, f, cmdv, mv, r,
                 "跟得上 ✓" if 0.97 <= r <= 1.03 else ("★ 丢步" if r < 0.9 else "临界")))

    # ---- 变速段: 实测加速度 ----
    print()
    print("  变速段 | 时刻(s)     | 频率变化        | 指令加速度  | 实测加速度")
    print("  -------+-------------+-----------------+-------------+-----------")
    for i in range(1, len(ks)):
        f0, f1 = ks[i - 1][1], ks[i][1]
        if abs(f1 - f0) < 1e-6:
            continue
        p0, p1 = ks[i - 1][0], ks[i][0]
        # ★ 只用斜坡**内部**: 滑窗 ±HW 在两端会跨出斜坡范围 ⇒ 端点值被拉平
        #   ⇒ 用端点差算斜率会系统性偏小 (升速段曾算出 379 而真值 ~700)。
        #   改: 对内部点做**最小二乘**拟合斜率。
        HWm = HW + 0.30
        vs = [(rec[j][0], vel_win(rec[j][0])) for j in range(len(rec))
              if p0 + HWm < rec[j][0] < p1 - HWm]
        vs = [(a, b) for (a, b) in vs if b is not None]
        if len(vs) < 8:
            continue
        n = len(vs)
        sx = sum(a for a, _ in vs)
        sy = sum(b for _, b in vs)
        sxx = sum(a * a for a, _ in vs)
        sxy = sum(a * b for a, b in vs)
        den = n * sxx - sx * sx
        a_meas = (n * sxy - sx * sy) / den if abs(den) > 1e-9 else 0.0
        a_cmd = (f1 - f0) * K / (p1 - p0)
        print("  %6d | %5.1f~%5.1f | %5d→%-6d Hz | %7.0f°/s² | %7.0f°/s²  %s"
              % (i, p0, p1, f0, f1, a_cmd, a_meas,
                 "✓" if abs(a_meas - a_cmd) < 0.15 * abs(a_cmd) else "差 %.0f%%"
                 % (abs(a_meas - a_cmd) / abs(a_cmd) * 100)))

    # ---- 曲线 (ASCII): # = 实测, · = 只有指令(没跟上) ----
    print()
    print("  速度曲线 (滑窗 ±%.2fs, 每 %.2fs 一点;  # = 实测  · = 指令未被跟上)"
          % (HW, span / 40))
    print("      t(s) |  %-44s | 指令°/s | 实测°/s" % "曲线")
    wmax = 1.0
    for x in rec:
        wmax = max(wmax, omega_cmd(ks, x[0]))
    step = max(1, len(rec) // 40)
    for i in range(0, len(rec), step):
        tt = rec[i][0]
        c = omega_cmd(ks, tt)
        m = vel_win(tt)
        if m is None:
            continue
        n1 = max(0, min(22, int(c / wmax * 22)))
        n2 = max(0, min(22, int(m / wmax * 22)))
        line = ["·" if k < n1 else " " for k in range(22)]
        for k in range(n2):
            line[k] = "#"
        print("  %7.2f |  %-44s | %7.0f | %7.0f"
              % (tt, "".join(line), c, m))
    print()
    print("★ 判读: 实测曲线若在高频段整体下凹 ⇒ 跟不住(丢步);")
    print("        与指令曲线水平错位 = 命令通路滞后; 形状一致只错位 ⇒ 能力够、接口慢。")
    print("        ⚠ 平台均值可信(用整段总位移); 瞬时曲线已用 ±%.2fs 滑窗抗 SWD 陈旧样本。" % HW)
    return 0


if __name__ == "__main__":
    sys.exit(main())
