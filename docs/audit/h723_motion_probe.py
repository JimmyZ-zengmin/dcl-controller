#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
H723 闭环伺服 · 运动品质审计探针 (证据生成器)
=============================================
本文件是 `H723-MOTION-QUALITY-AUDIT.md` 的**复现脚本**: 报告里每个数字都由
下面的子命令产出, 每个子命令都能独立复跑。

子命令:
  ena        失能机制解耦: 『限时到期』vs『手动 stop()』谁能拉低 ENA
  rate       速率线性度: 250/500/1000Hz 的实测 °/s (判脉冲时基)
  timebase   时基裁决: 脉冲数/真实秒 与 固件ms/真实秒 (10s 长窗)
  units      限时单位 → 实际转角 标定 (判『限时』能否作定时基准)
  bias       PC 定时通路的系统偏差与抖动 (判闭环定位精度天花板)
  repro      3 轮 × 10s 长脉冲复现性 (判速率与使能稳定性)

硬件: STM32H723 + TB6600(光耦输入) + AS5600(12bit 磁编码器), 串口 115200
接线前提: 光耦正端(PUL+/DIR+/ENA+) 接**板 3.3V** (不是 5V) —— 见 8.7 节

★ 结论速查 (实测, 2026-09-15):
  · 速率精确: 250/500/1000Hz → 偏差 ≤0.4%, 3轮复现 500.2/500.2/499.9 脉冲/s
  · 『限时 ms』不可作定时基准: 慢 ~26%, 且**随主机轮询密度变化 21%**
  · 限时**到期会自动失能** (ena=0, PE9=0); 手动 stop() 不会
  · 限时 ≤10 单位时**根本不生效** (跑到下一条命令才停)
  · PC 命令通路: 系统偏差 +55.3ms, 抖动 ±5.1ms
"""
import struct
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import serial
import serial.tools.list_ports as lp

PORT = "COM21" if "COM21" in [p.device for p in lp.comports()] else "COM14"

SPR = 1600.0                  # 8 细分: 1600 步/圈 (实测确认)
DEG_PER_STEP = 360.0 / SPR    # 0.225°
DEG_PER_LSB = 360.0 / 4096.0  # 0.0879° (AS5600 12bit 单圈)


def crc16(d, c=0xFFFF):
    for b in d:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


def fr(cmd, pl=b""):
    body = bytes([cmd, len(pl) & 0xFF, (len(pl) >> 8) & 0xFF]) + pl
    return bytes([0xC0]) + body + struct.pack("<H", crc16(body))


class Dut:
    """0x39 op=19 命令族; 应答 96 字节
       偏移 0/4/8/12/16/20/24/28 = hz/dir/ena/tleft/ccer/.../raw/.../enapol
       偏移 76/80/84/88/92 = GPIOA_IDR / GPIOE_IDR / TIM3_CCMR1 / TIM3_CCR1 / TIM3_ARR"""

    def __init__(self, port=PORT):
        self.ser = serial.Serial(port, 115200, timeout=0.02)

    def xchg(self, f, timeout=0.05):
        ser = self.ser
        ser.reset_input_buffer()
        ser.write(f)
        ser.flush()
        buf = b""
        t0 = time.time()
        while time.time() - t0 < timeout:
            b = ser.read(256)
            if b:
                buf += b
                if len(buf) >= 6 and buf[0] == 0xC1:
                    n = buf[2] | (buf[3] << 8)
                    if len(buf) >= 6 + n:
                        return buf[1], buf[4:4 + n]
            else:
                time.sleep(0.0005)
        return None, b""

    def op19(self, sub, arg=None):
        pl = bytes([19, sub]) + (struct.pack("<I", arg) if arg is not None else b"")
        return self.xchg(fr(0x39, pl))

    def st(self):
        s, p = self.op19(0)
        if s != 0 or not p or len(p) < 96:
            return None
        u = struct.unpack("<24I", p[:96])
        return dict(hz=u[0], dir=u[1], ena=u[2], tleft=u[3], ccer=u[4], raw=u[8],
                    enapol=u[12], pa_idr=u[19], pe_idr=u[20], cmr1=u[21],
                    pe9=(u[20] >> 9) & 1, ccr1=u[22], arr=u[23])

    def raw24(self):
        """应答的 24 个 u32 原始字 (用于找语义未知的字段)"""
        s, p = self.op19(0)
        if s != 0 or not p or len(p) < 96:
            return None
        return list(struct.unpack("<24I", p[:96]))

    def ena(self, v):    self.op19(3, v)
    def dirn(self, v):   self.op19(2, v)
    def rate(self, hz):  self.op19(1, hz)
    def limit(self, ms): self.op19(4, ms)
    def stop(self):      self.op19(1, 0)

    def close(self):
        try:
            self.stop()
            self.ena(0)
        except Exception:
            pass
        self.ser.close()


def serr(cur, tgt):
    """编码器计数 → 目标 的最短有符号误差, ±2048"""
    e = tgt - cur
    if e > 2048:
        e -= 4096
    elif e < -2048:
        e += 4096
    return e


def unwrap(seq):
    """raw 序列 → 累计角度(deg)"""
    out = [0.0]
    prev = seq[0]
    acc = 0
    for r in seq[1:]:
        d = r - prev
        if d > 2048:
            d -= 4096
        elif d < -2048:
            d += 4096
        acc += d
        out.append(acc * 360.0 / 4096.0)
        prev = r
    return out


def unwrap_dir(seq, fwd=True):
    """★ 单调解卷绕: 已知全程单向 ⇒ 每步取优势方向的模。
       `unwrap` 要求相邻两点位移 < 半圈, 本函数放宽到 < 整圈 (高速必用)。"""
    out = [0.0]
    acc = 0
    for i in range(1, len(seq)):
        d = (seq[i] - seq[i - 1]) & 0xFFF
        if not fwd:
            d = d - 4096
        acc += d
        out.append(acc * 360.0 / 4096.0)
    return out


def sstep(a, b):
    """最短弧单步位移 (计数)"""
    d = (b - a) & 0xFFF
    if d > 2048:
        d -= 4096
    return d


def occupied_arc(raws):
    """轴上占用角域(度) = 360 − 最大空缺。真转 ⇒ ≈360°; 冻结/原地 ⇒ ≈0°"""
    s = sorted(set(raws))
    if len(s) < 2:
        return 0.0, 0.0
    gaps = [(s[i + 1] - s[i]) for i in range(len(s) - 1)]
    gaps.append(s[0] + 4096 - s[-1])
    g = max(gaps)
    return 360.0 * (1 - g / 4096.0), g


def motion_shape(pts):
    """(t, raw) 序列 → 净速度/占用角域/反向步占比。
       两个速度估计量各有失效边界 ⇒ 用占用角域裁决 (详见运动套件里的同名函数)"""
    if len(pts) < 4:
        return None
    span = pts[-1][0] - pts[0][0]
    acc_arc = 0
    acc_cmd = 0
    back = 0
    wob = 0
    for i in range(1, len(pts)):
        a, b = pts[i - 1][1], pts[i][1]
        d = sstep(a, b)
        acc_arc += d
        wob += abs(d)
        if d < 0:
            back += 1
        acc_cmd += (b - a) & 0xFFF
    n = len(pts) - 1
    arc, gap = occupied_arc([p[1] for p in pts])
    stall = arc < 90.0
    return dict(span=span, n=n, back_frac=back / n, arc=arc, gap=gap,
                net_arc=acc_arc / span * DEG_PER_LSB,
                net_cmd=acc_cmd / span * DEG_PER_LSB,
                wob_s=wob / span * DEG_PER_LSB, stall=stall,
                net_s=0.0 if stall else acc_cmd / span * DEG_PER_LSB)


def head(t):
    d = Dut()
    s = d.st()
    if s is None:
        print("板子无响应 (端口 %s)" % PORT)
        return None, None
    print("基线: ena=%d 极性=%d PE9=%d raw=%d (%.2f°) 脉冲=%dHz"
          % (s["ena"], s["enapol"], s["pe9"], s["raw"], s["raw"] * 360 / 4096, s["hz"]))
    print("--- %s ---" % t)
    return d, s


# ------------------------------------------------------------------ 子命令
def cmd_ena():
    """解耦: 『限时到期』 vs 『手动 stop()』 谁拉低 ENA"""
    d, _ = head("A/B 解耦: 限时到期 vs 手动 stop()")
    r0 = d.st()["raw"]

    def snap(tag):
        s = d.st()
        print("  %-26s ena=%d PE9=%d hz=%4d tleft=%6d CC1E=%d raw=%d"
              % (tag, s["ena"], s["pe9"], s["hz"], s["tleft"], s["ccer"] & 1, s["raw"]))

    d.ena(1)
    time.sleep(0.25)
    snap("使能后")
    print("  A 组: 下发限时 500ms, **不发 stop**, 让它自己到期")
    d.dirn(0)
    d.limit(500)
    d.rate(500)
    snap("  A1 下发后")
    time.sleep(0.30)
    snap("  A2 +0.3s (限时内)")
    time.sleep(0.60)
    snap("  A3 +0.9s (限时已到期)")
    print("     ⇒ A3 若 PE9=0/ena=0 ⇒ 【限时到期会自动失能】")
    print()
    d.ena(1)
    time.sleep(0.25)
    snap("使能复位后")
    print("  B 组: 下发限时 3000ms, **提前**手动 stop()")
    d.dirn(0)
    d.limit(3000)
    d.rate(500)
    time.sleep(0.40)
    snap("  B2 +0.4s (仍在跑)")
    d.stop()
    snap("  B3 手动 stop() 后")
    time.sleep(0.20)
    snap("  B4 +0.2s")
    print("     ⇒ B3/B4 若 PE9=1/ena=1 ⇒ 【stop() 不会失能】, 责任在『限时到期』")
    d.ena(0)
    d.ser.close()


def cmd_rate():
    """速率线性度: 请求 Hz vs 实测 °/s (长窗真实时间戳)"""
    d, _ = head("速率线性度 (2.5s 窗, 真实时间戳)")
    print("  请求Hz  方向 | 实测deg/s | 相对理论 | 计数/真实秒 | 限时倒数/真实秒 | 末次CC1E")
    print("  ---------+----------+----------+-------------+-----------------+---------")
    for hz in (250, 500, 1000):
        for dr in (0, 1):
            d.ena(1)
            time.sleep(0.25)
            d.dirn(dr)
            d.limit(4000)          # 安全网, 2.5s 内不到期
            d.rate(hz)
            pts = []
            t0 = time.time()
            while time.time() - t0 < 2.5:
                s = d.st()
                if s:
                    pts.append((time.time() - t0, s["raw"], s["tleft"], s["ccer"] & 1))
            d.stop()
            time.sleep(0.15)
            w = [p for p in pts if p[0] > 0.4 and p[0] < 2.35]
            if len(w) < 5:
                print("  %6d  %3d  | 采样不足" % (hz, dr))
                continue
            ang = unwrap([p[1] for p in w])
            dts = w[-1][0] - w[0][0]
            dps = (ang[-1] - ang[0]) / dts
            theo = hz / SPR * 360.0
            print("  %6d  %3d  | %9.2f | %+7.1f%%  | %11.1f | %19.1f | %d"
                  % (hz, dr, dps, (abs(dps) - theo) / theo * 100,
                     abs(ang[-1] - ang[0]) / dts, (w[0][2] - w[-1][2]) / dts, w[-1][3]))
    print()
    print("★ |实测| 与理论成固定比例 ⇒ 速率标定错; 线性但比例≈1 ⇒ 速率正确")
    d.ena(0)
    d.ser.close()


def cmd_timebase():
    """时基裁决: 10s 长窗, 脉冲数 vs 固件 ms 消耗"""
    d, _ = head("时基裁决 (10s 长窗, 请求 500Hz)")
    d.ena(1)
    time.sleep(0.3)
    d.dirn(0)
    d.limit(60000)
    d.rate(500)
    pts = []
    t0 = time.time()
    while time.time() - t0 < 10.0:
        s = d.st()
        if s:
            pts.append((time.time() - t0, s["raw"], s["tleft"]))
    d.stop()
    ang = unwrap([p[1] for p in pts])
    real_s = pts[-1][0] - pts[0][0]
    deg = ang[-1] - ang[0]
    pulses = deg / 360.0 * SPR
    fw_ms = pts[0][2] - pts[-1][2]
    print("  真实耗时 %.3fs   编码器 %+.2f° ⇒ %.1f 脉冲 ⇒ %.2f 脉冲/s (期望 500)"
          % (real_s, deg, pulses, pulses / real_s))
    print("  固件限时消耗 %d ⇒ 固件 1 'ms' = %.4f 真实 ms (比值 %.3f)"
          % (fw_ms, real_s * 1000.0 / fw_ms, real_s * 1000.0 / fw_ms))
    print("  ⇒ 脉冲时基 %s ; ms 时基 %s"
          % ("✓ 准" if abs(pulses / real_s - 500) / 500 < 0.02 else "★ 错",
             "✓ 准" if abs(real_s * 1000.0 / fw_ms - 1) < 0.05
             else "★ 慢 %.0f%%" % ((real_s * 1000.0 / fw_ms - 1) * 100)))
    d.ena(0)
    d.ser.close()


def cmd_units():
    """限时单位 → 实际转角 标定"""
    d, _ = head("限时单位 → 实际转角 标定 (500Hz)")
    print("   units |  实测deg  | deg/单位 | 等效脉冲数 | 脉冲/单位")
    print("  -------+-----------+----------+------------+----------")
    for units in (2, 5, 10, 20, 50, 100, 200, 400, 800, 1000):
        base = None
        for _ in range(2):
            d.ena(1)
            d.dirn(0)
            d.limit(units)
            d.rate(500)
            time.sleep(units / 1000.0 * 1.3 + 0.25)
            d.ena(1)                        # ★ 到期会失能, 立刻补回
            time.sleep(0.30)
            r = d.st()["raw"]
            if base is None:
                base = r
                continue
            dg = serr(base, r) * DEG_PER_LSB
            pl = abs(dg) / 360.0 * SPR
            print("  %6d | %+9.3f | %8.4f | %10.1f | %8.3f"
                  % (units, dg, dg / units, pl, pl / units))
            break
    print()
    print("★ units ≤10 若 deg 远大于预期 ⇒ 『限时』小值不生效 (跑到下一条命令才停)")
    print("★ units ≥400 段 deg/单位 才是真斜率")
    d.ena(0)
    d.ser.close()


def cmd_bias():
    """PC 定时通路 偏差/抖动 标定"""
    d, _ = head("PC 定时通路 偏差/抖动 标定")
    HZ, SL, N = 500, 0.500, 8
    EXP = SL * HZ / SPR * 360.0
    print("  sleep=%.3fs @%dHz ⇒ 理论 %.3f°" % (SL, HZ, EXP))
    print("   次 |  实测deg  |  偏差deg  | 等效延时ms")
    vals = []
    for k in range(N):
        r0 = d.st()["raw"]
        d.ena(1)
        d.dirn(0)
        d.limit(int(SL * 1000 * 2.5) + 300)
        d.rate(HZ)
        time.sleep(SL + 0.02)
        d.stop()
        time.sleep(0.10)
        r1 = d.st()["raw"]
        dg = serr(r0, r1) * DEG_PER_LSB
        vals.append(dg)
        print("  %3d | %+9.3f | %+9.3f | %+9.2f"
              % (k + 1, dg, dg - EXP, (dg - EXP) / (HZ / SPR * 360.0) * 1000))
    m, mn, mx = sum(vals) / len(vals), min(vals), max(vals)
    dpm = HZ / SPR * 360.0
    print("  均值 %+.3f° ⇒ 系统偏差 %+.3f° = 等效 %+.2f ms (可标定补偿)"
          % (m, m - EXP, (m - EXP) / dpm * 1000))
    print("  极差 %.3f° ⇒ 抖动 ±%.3f° = ±%.2f ms (**不可补偿**) ⇒ 定位精度天花板"
          % (mx - mn, (mx - mn) / 2, (mx - mn) / 2 / dpm * 1000))
    d.ena(0)
    d.ser.close()


def cmd_repro():
    """3 轮 × 10s 长脉冲复现性"""
    d, _ = head("3 轮 × 10s 长脉冲复现性")
    HZ, WIN = 500, 10.0
    print("   轮 | 真实时长 | 累计deg | 等效脉冲/s | PE9集合 | CC1E集合 | 限时消耗/真实秒")
    for k in range(3):
        d.ena(1)
        time.sleep(0.35)
        d.dirn(0)
        d.limit(60000)
        d.rate(HZ)
        pts = []
        t0 = time.time()
        while time.time() - t0 < WIN:
            s = d.st()
            if s:
                pts.append((time.time() - t0, s["raw"], s["tleft"], s["ccer"] & 1, s["pe9"]))
        d.stop()
        time.sleep(0.3)
        ang = unwrap([p[1] for p in pts])
        real_s = pts[-1][0] - pts[0][0]
        deg = ang[-1] - ang[0]
        print("   %2d | %7.3fs | %+7.2f | %10.1f | %s | %s | %14.1f"
              % (k + 1, real_s, deg, deg / 360 * SPR / real_s,
                 sorted(set(p[4] for p in pts)), sorted(set(p[3] for p in pts)),
                 (pts[0][2] - pts[-1][2]) / real_s))
    d.ena(0)
    d.ser.close()


def cmd_startup():
    """起动剖面: 逐点看"从发命令到满速"用了多久 ⇒ 判固件有没有内部加减速。
       ★ 这是解释"突加上限"的前提: 若内部有斜坡, 突加上限 ≠ 电机 pull-in 上限。"""
    d, _ = head("起动剖面 (判固件有无内部加减速)")
    for hz in (5000, 15000):
        d.ena(1)
        time.sleep(0.3)
        d.dirn(0)
        d.limit(20000)
        t0 = time.time()
        d.rate(hz)
        pts = []
        while time.time() - t0 < 1.0:
            s = d.st()
            if s:
                pts.append((time.time() - t0, s["raw"]))
        d.stop()
        time.sleep(0.25)
        print("  请求 %d Hz (理论 %.0f °/s):" % (hz, hz / SPR * 360.0))
        print("      t(s) | 瞬时deg/s | 相对满速")
        ang = unwrap_dir([p[1] for p in pts], True)
        for i in range(1, min(len(pts), 22)):
            dtt = pts[i][0] - pts[i - 1][0]
            if dtt <= 1e-6:
                continue
            v = (ang[i] - ang[i - 1]) / dtt
            print("    %6.3f | %9.1f | %7.1f%%" % (pts[i][0], v, v / (hz / SPR * 360.0) * 100))
        print()
    print("★ 判读: 第 1~2 个采样点(≈35ms 间隔)就到 ~100%% ⇒ **无内部加减速** (硬突加);")
    print("        需要几百 ms 才爬上去 ⇒ 有内部斜坡, 此时『突加上限』其实是**斜坡能力上限**。")
    d.ena(0)
    d.ser.close()


def cmd_fields():
    """响应字段普查: 找出语义未知的字段里有没有 tick / 脉冲计数器。
       ★ 若找到**脉冲计数器**, 则所有"脉冲数不确定"的测量问题(限时时基/命令延时)一次性解决。"""
    d, _ = head("响应字段普查 (24 个 u32, 找 tick / 脉冲计数器)")

    def phase(tag, sec, hz=None):
        if hz:
            d.ena(1)
            time.sleep(0.25)
            d.dirn(0)
            d.limit(60000)
            d.rate(hz)
            time.sleep(0.3)
        a = d.raw24()
        t0 = time.time()
        time.sleep(sec)
        b = d.raw24()
        dt = time.time() - t0
        if hz:
            d.stop()
            d.ena(0)
            time.sleep(0.3)
        if a is None or b is None:
            print("  %s: 读失败" % tag)
            return
        ch = [(i, a[i], b[i], (b[i] - a[i]) & 0xFFFFFFFF) for i in range(24) if a[i] != b[i]]
        print("  ---- %s  (真实 %.2fs): %d/24 字段在变 ----" % (tag, dt, len(ch)))
        print("     序号 |        前值 |        后值 |         增量 | 增量/真实秒")
        for i, x, y, dz in ch:
            if dz > 0x7FFFFFFF:
                dz -= 0x100000000
            print("     %4d | %10d | %10d | %11d | %10.1f" % (i, x, y, dz, dz / dt))
        # 已知字段对照
        print("     (已知: 0=hz 1=dir 2=ena 3=tleft 4=ccer 8=raw 12=enapol "
              "19=GPIOA_IDR 20=GPIOE_IDR 21=CCMR1 22=CCR1 23=ARR)")
        print()

    phase("静置", 10.0)
    phase("500Hz 转动", 5.0, 500)
    phase("5000Hz 转动", 5.0, 5000)
    print("★ 判读:")
    print("  · 某字段增量/真实秒 ≈ 1000 / 10000 / 100000 ⇒ 那是 ms / 100µs / 1µs 计数 ⇒ 直接得到固件时基")
    print("  · 某字段在转动时的增量 ≈ 转动秒数×Hz ⇒ **那是脉冲计数器** ⇒ 可精确核对脉冲数")
    print("  · 若某字段是 100µs 扫描计数, 用它算出的扫描率能直接裁决 §D2 的 1.26x 慢时基")
    d.ser.close()


def cmd_bench():
    """命令通路容量: 最大命令率 / 延迟分布 / 长时稳定性
       ⇒ 决定"上位机在环"的控制环能跑多快 = 能承载多复杂的算法"""
    d, _ = head("命令通路容量 (决定上位机在环控制环的上限)")
    f = fr(0x39, bytes([19, 0]))
    N = 300
    lat = []
    fail = 0
    t0 = time.time()
    for _ in range(N):
        t1 = time.time()
        s, _p = d.xchg(f, timeout=0.25)
        dt = time.time() - t1
        if s is None:
            fail += 1
        else:
            lat.append(dt)
    tot = time.time() - t0
    if lat:
        lat.sort()
        print("  连续 %d 条命令: 用时 %.2fs ⇒ **%.1f 命令/s** (失败 %d)" % (N, tot, N / tot, fail))
        print("  单条往返延迟: p50 %.1fms  p90 %.1fms  p99 %.1fms  最大 %.1fms  最小 %.1fms"
              % (lat[len(lat) // 2] * 1000, lat[int(len(lat) * 0.9)] * 1000,
                 lat[int(len(lat) * 0.99)] * 1000, lat[-1] * 1000, lat[0] * 1000))
        print("  ⇒ 上位机在环控制环上限 ≈ %.1f Hz (单条查询) ; 抖动 p99-p50 = %.1fms"
              % (1.0 / (sum(lat) / len(lat)), (lat[int(len(lat) * 0.99)] - lat[len(lat) // 2]) * 1000))
    print()
    print("  ---- 30s 长时稳定性 ----")
    t0 = time.time()
    n = 0
    f2 = 0
    while time.time() - t0 < 30.0:
        s, _p = d.xchg(f, timeout=0.25)
        n += 1
        if s is None:
            f2 += 1
    el = time.time() - t0
    print("  30s 内 %d 条命令 ⇒ %.1f 命令/s, 失败 %d (%.2f%%)"
          % (n, n / el, f2, f2 / max(n, 1) * 100))
    print()
    print("★ 判读: 这个命令率就是**上位机在环闭环**的采样率上限。")
    print("        要跑更复杂的算法(更高的环频/更细的插补), 必须把这部分搬到固件里。")
    d.ser.close()


def cmd_liveness():
    """★ 编码器通路存活检查 —— 必须先做这个, 再谈任何运动结论。

    判据: 用"已知速度 × 已知时间 = 已知转角"核对 raw 是否在更新。
    血证: raw **完全冻结**时, 我一度判成"轴不转(占用角域 0.0°)",
          而现场清楚看到轴在转 ⇒ **是读路径挂了, 不是轴不动**。
    标志: 所有采样点 raw **完全相同**(唯一值=1) ⇒ 传感器不可能如此 ⇒ 读路径冻结。
          另一标志: tleft 倒数速率显著偏离 ~780/真实秒 ⇒ 固件时基/主循环异常。
    """
    d, _ = head("编码器通路存活检查 (raw 到底有没有在更新)")
    print("  判据: raw 唯一值=1 ⇒ 冻结;  理论转角 vs 实测转角;  tleft 倒数应 ≈780/真实秒")
    print()
    print("   请求Hz | 采样点 | raw唯一值 |  首raw |  末raw | 理论转角 | 实测净转角 |  tleft/s | 判定")
    print("  --------+--------+-----------+--------+--------+----------+------------+----------+------")
    for hz in (500, 5000, 16000):
        d.ena(1)
        time.sleep(0.3)
        d.dirn(0)
        d.limit(30000)
        pts = []
        t0 = time.time()
        d.rate(hz)
        while time.time() - t0 < 4.0:
            s = d.st()
            if s:
                pts.append((time.time() - t0, s["raw"], s["tleft"]))
        d.stop()
        time.sleep(0.15)
        if len(pts) < 5:
            print("  %6d | 采样不足" % hz)
            continue
        span = pts[-1][0] - pts[0][0]
        uniq = len(set(p[1] for p in pts))
        m = motion_shape([(p[0], p[1]) for p in pts])
        exp = hz / SPR * 360.0 * span
        tl = (pts[0][2] - pts[-1][2]) / span if span else 0
        if uniq == 1:
            verdict = "★ raw 冻结 ⇒ **读路径挂了**"
        elif abs(m["net_s"]) > 0.9 * hz / SPR * 360.0 and m["arc"] > 300:
            verdict = "✓ 正常(在更新)"
        elif m["arc"] < 90:
            verdict = "★ raw 几乎不动 ⇒ 读路径疑似不更新"
        else:
            verdict = "部分更新/丢步"
        print("  %6d | %6d | %9d | %6d | %6d | %8.1f | %10.1f | %8.1f | %s"
              % (hz, len(pts), uniq, pts[0][1], pts[-1][1], exp, m["net_s"], tl, verdict))
    print()
    print("★ 判读:")
    print("  · raw 唯一值=1 或 占用角域≈0 ⇒ **先查 AS5600 读路径(I2C)**,")
    print("    尤其是 bit-bang I2C 缺超时/总线恢复 ⇒ 一旦 NAK/时钟拉伸就永久卡死,")
    print("    固件会一直返回**最后一次的好值** ⇒ 数据看起来像'轴不动', 其实轴在转。")
    print("  · tleft 倒数显著偏离 780/真实秒 ⇒ 扫描节拍/时基异常, 该轮所有速度值都不可信。")
    d.ena(0)
    d.ser.close()


def cmd_asdiag():
    """AS5600 上电诊断 (命令 0x39 payload=[17]) —— **固件自己**扫 4 组引脚对 + 引脚级自检。

    ★ 为什么要用它而不是 SWD 直读: 本项目已记录"SWD 直读外设寄存器读不到真值"
      (main.c:716: GPIOA/E 读回 0xABFFFFFF/0xFFFFFFFF, RCC_AHB4ENR 读回 0)。
      固件在芯片内部读自己的 GPIO 才是可靠的。
    应答 64B = 16 word: [0..3]4×ACK [4..7]4×RAW [8]selftest2 [9]拉低读回 [10]释放读回
    组序: 0=PB10(SCL)/PB11(SDA)  1=接反  2=PB6/PB7  3=PB8/PB9
    """
    d, _ = head("AS5600 上电诊断 (0x39 op=17, 固件内自检)")
    s, p = d.xchg(fr(0x39, bytes([17])), timeout=0.6)
    if s is None or len(p) < 44:
        print("  ✗ 无应答或应答过短 (len=%s)" % (len(p) if p else 0))
        d.ser.close()
        return
    u = struct.unpack("<16I", p[:64])
    pairs = ["0: PB10=SCL PB11=SDA", "1: PB11=SCL PB10=SDA (接反)",
             "2: PB6 =SCL PB7 =SDA", "3: PB8 =SCL PB9 =SDA"]
    print("  ---- 引脚级自检 (决定 START 条件能不能成立) ----")
    print("    把 SCL/SDA 都拉低后读回 = 0x%03X   (期望 0x000)" % u[9])
    print("    释放(高)后读回         = 0x%03X   (期望 0x%03X = 两根线都被外部上拉)"
          % (u[10], 0x0C00))
    if u[9] != 0:
        print("    ⇒ ★★★ **拉不低** ⇒ 输出通路不通 ⇒ START 条件不成立 ⇒ 后面 ACK 全部无意义")
    elif u[10] != 0x0C00:
        print("    ⇒ ★★★ **释放后有脚不是高** ⇒ 没有外部上拉 / 线被拉住 / 器件未供电")
    else:
        print("    ⇒ ✓ 引脚级自检通过 (能拉低、能释放), 总线电气层看起来正常")
    print()
    print("  ---- 4 组候选引脚对的 I2C 探测 ----")
    print("    组 | 引脚            | 写地址 ACK | RAW_ANGLE")
    print("    ---+-----------------+------------+-----------")
    hit = []
    for k in range(4):
        ack = u[k]
        raw = u[4 + k]
        raws = ("%d (%.2f°)" % (raw, raw * 360.0 / 4096.0)) if raw <= 4095 else "—"
        print("    %d  | %-15s |     %d      | %s" % (k, pairs[k], ack, raws))
        if ack:
            hit.append(k)
    print()
    if hit:
        print("  ⇒ ✅ AS5600 在**组 %d** (%s) 应答, 且能读到 RAW" % (hit[0], pairs[hit[0]]))
    else:
        print("  ⇒ ✗ 四组**都没有 ACK** ⇒ 器件在这一层就不应答")
        if u[9] == 0 and u[10] == 0x0C00:
            print("     但引脚级自检通过 ⇒ 线上有上拉、也能驱动 ⇒ **器件侧问题**")
            print("     查: ① 器件供电(该 3.3V/5V 按模块) ② GND 是否真通 ③ 磁铁在不在")
            print("         ④ 器件是否被锁死(断电重启一次再测)")
    d.ser.close()


def main():
    a = sys.argv
    cmd = a[1] if len(a) > 1 else "ena"
    fn = dict(ena=cmd_ena, rate=cmd_rate, timebase=cmd_timebase,
              units=cmd_units, bias=cmd_bias, repro=cmd_repro,
              startup=cmd_startup, fields=cmd_fields, bench=cmd_bench,
              liveness=cmd_liveness, asdiag=cmd_asdiag).get(cmd)
    if fn is None:
        print(__doc__)
        return 2
    fn()
    return 0


if __name__ == "__main__":
    sys.exit(main())
