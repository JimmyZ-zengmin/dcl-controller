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


def main():
    a = sys.argv
    cmd = a[1] if len(a) > 1 else "ena"
    fn = dict(ena=cmd_ena, rate=cmd_rate, timebase=cmd_timebase,
              units=cmd_units, bias=cmd_bias, repro=cmd_repro,
              startup=cmd_startup).get(cmd)
    if fn is None:
        print(__doc__)
        return 2
    fn()
    return 0


if __name__ == "__main__":
    sys.exit(main())
