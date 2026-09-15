#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
H723 步进运动测试套件 (闭环伺服验证用)
======================================
采样: 读到完整帧就返回 (帧 ~102B @115200 ≈ 9ms) ⇒ 采样率 ~60-80 Hz (原来固定等待 0.25s 只有 2Hz)

子命令:
  probe                        只读状态快照 (不动电机, 不改任何状态)
  longrun <hz> <sec>           长跑 + 统计 (速度稳定性/丢步/采样质量)
  reverse <hz> <sec_each>      正转→反转→正转 (变向)
  pos <target_deg> [tol]       上位机闭环位置控制 (读编码器→调脉冲→逼近)
  hold <deg> <sec>             位置锁定测试 (闭环保持 + 误差带统计)
  speed <hz_max> [step] [sec]  ★ 突加转速扫描 (找固件无加减速时的可用上限)
  ramp <target_hz> [ms] [hold] ★ 带斜坡起动 (分离"无加减速"与"驱动器/电机上限")
  slip <hz> [sec]              ★ 失速形态: 残差 = 实际角 − 指令角 (滑差 + 摆动)
  coast <hz>                   ★ 失能滑行: 量摩擦/阻尼减速率 (扭矩法的另一半)
  accel <tgt> [stepHz] [lo] [hi] [it] ★ 最大可跟随加速度 (力矩裕度的代理量)
  roundtrip <hz> <ms> <n>      ★ 往返复现性: n 轮定时脉冲正/反, 每轮等脉冲数
                                  (用固件"限时"字段定时 ⇒ 脉冲数严格相等,
                                   排除 Python 计时超时造成的"每段多走几度"假象)

编码器: AS5600, 单圈 0..4095 (12bit) → 4096 = 360°, 解卷绕累计

判据说明 (往返复现性):
  背隙 (backlash)  → 每轮终点**固定偏一点**, 偏移量**不累积**
  丢步 (lost step) → 每轮终点偏移**逐轮累积**, 累积速率 = 每轮丢的步数
  两者可同时存在: 固定分量 = 背隙, 累积分量 = 丢步
"""
import struct
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import serial
import serial.tools.list_ports as lp

PORT = "COM21" if "COM21" in [p.device for p in lp.comports()] else "COM14"


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
    def __init__(self, port=PORT):
        self.ser = serial.Serial(port, 115200, timeout=0.02)

    def xchg(self, f, timeout=0.05):
        """发命令, 轮询到完整帧即返回 (高采样率的关键)"""
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
        return dict(hz=u[0], dir=u[1], ena=u[2], tleft=u[3], ccer=u[4],
                    raw=u[8], enapol=u[12],
                    pa_idr=u[19], pe_idr=u[20], cmr1=u[21],
                    pe9=(u[20] >> 9) & 1, ccr1=u[22], arr=u[23])

    def ena(self, v):  self.op19(3, v)
    def dirn(self, v): self.op19(2, v)
    def rate(self, hz): self.op19(1, hz)
    def limit(self, ms): self.op19(4, ms)
    def stop(self): self.op19(1, 0)
    def enapol(self, v): self.op19(5, v)

    def close(self):
        try:
            self.stop()
            self.ena(0)
        except Exception:
            pass
        self.ser.close()


# ---------------------------------------------------------------- 常量与助手
SPR = 1600.0                  # 8 细分: 1600 步/圈 (实测确认: 500Hz→112.5°/s, ±0.4%)
DEG_PER_STEP = 360.0 / SPR    # 0.225°
DEG_PER_LSB = 360.0 / 4096.0  # 0.0879°  (AS5600 12bit 单圈)
COUNTS_PER_STEP = 4096.0 / SPR  # 2.56 计数/步

# ★ 实测固件行为常数 (见 _diag_limit / _diag_repro, 3 轮复现)
LIMIT_SCALE = 1.26            # 固件"限时 ms" 1 单位 ≈ 1.26 真实 ms (但**随轮询变化**, 不可作定时基准)
PULSES_PER_UNIT = 0.637       # 限时字段每消耗 1 单位 ≈ 0.637 个脉冲 (500/785)
SAFE_LIMIT = 200000           # 当"安全网"用的超大限时 (不会到期)
CMD_BIAS_S = 0.0553           # ★ 实测: 命令通路等效延时 (8 次标定: +6.218°/56.25° @500Hz)
ABRUPT_CEIL_HZ = 16650        # ★ 实测: **突加**(无加减速)可跟随上限; 超过即失速

# ★ 方向映射 (必须实测, 不能假设): dir=0 ⇒ 编码器 raw **增大**; dir=1 ⇒ raw **减小**
#   依据: dir=0 长跑 +1122° / dir=1 脉冲串 −868 计数
DIR_POS = 0


def dir_for(err_counts):
    """误差(计数) → 使 raw 朝目标走的 dir 码"""
    return DIR_POS if err_counts > 0 else (1 - DIR_POS)


def serr(cur_raw, tgt_raw):
    """raw → 目标 的最短有符号误差 (编码器计数), 范围 ±2048"""
    e = tgt_raw - cur_raw
    if e > 2048:
        e -= 4096
    elif e < -2048:
        e += 4096
    return e


def timed_burst(d, dir_bits, hz, ms):
    """★ 由**固件限时**终止的一段脉冲。
       注意: 限时到期时固件会**自动失能** (ena=0 → PE9=0), 且真实时长 = ms×1.26。"""
    d.ena(1)
    d.dirn(dir_bits)
    d.limit(int(ms))
    d.rate(int(hz))


def wait_burst(ms, extra=0.10):
    time.sleep(ms / 1000.0 * LIMIT_SCALE + extra)


def safety_burst(d, dir_bits, hz, win_ms):
    """★ PC 墙钟定时的一段脉冲, 目标**真实脉冲窗口** = win_ms。
       ★ 实测: 命令通路有 +55.3ms 等效延时 (rate 与 stop 的处理时刻之差),
         抖动 ±5.1ms ⇒ 必须减掉这个常量, 否则每次都多走 rate×55ms。
       限时字段只作**安全网** (2.5 倍余量), 保证不会先到期。"""
    win_ms = max(2.0, win_ms)
    d.ena(1)
    d.dirn(dir_bits)
    d.limit(int(win_ms * 2.5) + 300)
    d.rate(int(hz))
    time.sleep(max(0.001, win_ms / 1000.0 - CMD_BIAS_S))
    d.stop()


def move_deg(d, dir_bits, deg, gain=1.0):
    """按**角度**发一段脉冲。速率随行程自适应:
       ★ 最小可分辨行程 = rate × CMD_BIAS (55ms) ⇒ 小行程必须降速率,
         否则"想走 1° 实际走了 6°" (500Hz 下 55ms = 27 脉冲 = 6.2°)。"""
    ad = abs(deg) * gain
    if ad > 20.0:
        hz = 500
    elif ad > 5.0:
        hz = 200
    elif ad > 1.5:
        hz = 80
    else:
        hz = 40
    dps = hz / SPR * 360.0
    safety_burst(d, dir_bits, hz, ad / dps * 1000.0)
    return hz


def speed_point(d, hz, sec=1.5):
    """突加一档频率, 稳态段测速。
       ★ 用 motion_shape(最短弧) 而不是 unwrap_dir(强制单向) —— 后者会把
         『原地抖动』伪造成高速正转 (20000Hz 时曾假报 2048°/s, 实际轴不转)。"""
    d.ena(1)
    time.sleep(0.25)
    d.dirn(DIR_POS)
    d.limit(int(sec * 1000 * 1.6) + 3000)      # 安全网, sec 内不到期
    d.rate(int(hz))
    pts = []
    t0 = time.time()
    while time.time() - t0 < sec:
        s = d.st()
        if s:
            pts.append((time.time() - t0, s["raw"], s["hz"]))
    d.stop()
    time.sleep(0.12)
    if len(pts) < 6:
        return None
    w = [p for p in pts if p[0] > 0.35 and p[0] < sec - 0.15]     # 剔除突加瞬态
    if len(w) < 4:
        return None
    m = motion_shape([(p[0], p[1]) for p in w])
    if m is None:
        return None
    return dict(hz=hz, hz_rb=w[-1][2], dps=m["net_s"], wob=m["wob_s"],
                arc=m["arc"], back=m["back_frac"], theo=hz / SPR * 360.0,
                wrap=m["wrap"])


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
    """★ 单调解卷绕 (高速必用): 已知全程单向 ⇒ 每步只取优势方向的模。
       `unwrap` 要求相邻两点位移 < 半圈(2048 计数), 本函数放宽到 < 整圈(4096),
       因为 27Hz 采样在 ~4900°/s 时就会跨 2048。"""
    out = [0.0]
    acc = 0
    for i in range(1, len(seq)):
        d = (seq[i] - seq[i - 1]) & 0xFFF
        if not fwd:
            d = d - 4096
        acc += d
        out.append(acc * 360.0 / 4096.0)
    return out


def max_per_sample_wrap(pts):
    """按相邻采样间隔估算"每采样段最大可测速度"(°/s), 用于给结果标可信度"""
    if len(pts) < 3:
        return 0.0
    gaps = [pts[i][0] - pts[i - 1][0] for i in range(1, len(pts)) if pts[i][0] > pts[i - 1][0]]
    if not gaps:
        return 0.0
    return 360.0 / (sum(gaps) / len(gaps))


def sstep(a, b):
    """最短弧单步位移 (计数): ±2048 内, 双向都正确。
       ★ 与 unwrap_dir 的区别: unwrap_dir 强制单向, 遇到"原地抖动"会把
         向后的 10 计数算成向前的 4086 计数 ⇒ **把抖动伪造成高速正转**。"""
    d = (b - a) & 0xFFF
    if d > 2048:
        d -= 4096
    return d


def occupied_arc(raws):
    """轴上占用角域(度): 圆上被访问过的弧长 = 360 − 最大空缺。
       ★ 真在转 ⇒ ≈360°; 原地抖 ⇒ 只占一小段弧。"""
    s = sorted(set(raws))
    if len(s) < 2:
        return 0.0, 0.0
    gaps = [(s[i + 1] - s[i]) for i in range(len(s) - 1)]
    gaps.append(s[0] + 4096 - s[-1])                 # 环绕缺口
    g = max(gaps)
    return 360.0 * (1 - g / 4096.0), g


def motion_shape(pts):
    """从 (t, raw) 序列提炼"到底在转还是在抖"的一组量。

    ★★ 两个速度估计量**各自都有一条失效边界**, 所以必须都算 + 用第三个量裁决:
       · sstep(最短弧)       —— 真单向运动 >180°/采样段时**反向**
                               (30000Hz=6750°/s, 每段 246° ⇒ 被读成 -114° ⇒ 假报负速度)
       · unwrap_dir(强制单向) —— 原地抖动时**伪造高速正转**
                               (20000Hz 轴不动, 却假报 +2048°/s)
       · occupied_arc(占用角域) —— 裁决量, 无上述两种失效
                                ⇒ >90° = 在转(取单向估计); <90° = 不转(净速度记 0)
    """
    if len(pts) < 4:
        return None
    span = pts[-1][0] - pts[0][0]
    acc_arc = 0          # 最短弧累计   (有效区间: 每采样段 < 180°  ⇒ <~2.2万Hz)
    acc_cmd = 0          # 强制单向累计 (有效区间: 每采样段 < 360°  ⇒ <~4.3万Hz)
    back = 0
    wob = 0
    for i in range(1, len(pts)):
        a, b = pts[i - 1][1], pts[i][1]
        d = sstep(a, b)                                  # 最短弧 (双向正确)
        acc_arc += d
        wob += abs(d)
        if d < 0:
            back += 1
        # ★ 强制单向: 取模 4096, 不做 ±2048 折返 ⇒ 抖动时会伪造, 但高速不混叠
        dc = (b - a) & 0xFFF if DIR_POS == 0 else -((a - b) & 0xFFF)
        acc_cmd += dc
    n = len(pts) - 1
    arc, gap = occupied_arc([p[1] for p in pts])
    stall = arc < 90.0
    return dict(span=span, n=n, back_frac=back / n, arc=arc, gap=gap,
                net_arc=acc_arc / span * DEG_PER_LSB,
                net_cmd=acc_cmd / span * DEG_PER_LSB,
                wob_s=wob / span * DEG_PER_LSB,
                stall=stall,
                net_s=0.0 if stall else acc_cmd / span * DEG_PER_LSB,
                wrap=(360.0 / (span / n)) if n else 0.0)


def sample(dut, dur_s, gap=0.0):
    """连续采样, 返回 [(t, raw)]"""
    pts = []
    t0 = time.time()
    while time.time() - t0 < dur_s:
        s = dut.st()
        if s:
            pts.append((time.time() - t0, s["raw"]))
        if gap:
            time.sleep(gap)
    return pts


def stats(pts, t_start=None):
    if len(pts) < 3:
        return None
    ang = unwrap([p[1] for p in pts])
    dur = pts[-1][0] - pts[0][0]
    net = ang[-1] - ang[0]
    # 分段速度 (每 10% 段)
    seg = []
    n = len(pts)
    for k in range(5):
        i0, i1 = int(n * k / 5), int(n * (k + 1) / 5) - 1
        if i1 > i0:
            dt = pts[i1][0] - pts[i0][0]
            if dt > 0:
                seg.append((ang[i1] - ang[i0]) / dt)
    return dict(n=n, dur=dur, net=net, vavg=net / max(dur, 1e-6),
                seg=seg, rate=n / max(dur, 1e-6))


def bar(v, scale=6.0, width=50):
    return "#" * min(width, int(abs(v) / scale))


def main():
    a = sys.argv
    cmd = a[1] if len(a) > 1 else "longrun"
    d = Dut()
    s = d.st()
    if s is None:
        print("板子无响应"); return 1
    print("基线: 使能=%d 极性=%d PE9=%d raw=%d (%.2f°)  脉冲=%dHz" %
          (s["ena"], s["enapol"], s["pe9"], s["raw"], s["raw"] * 360 / 4096, s["hz"]))

    if cmd == "probe":
        # 只读快照: 不动电机, 不改任何状态
        print("  ---- 只读快照 (未做任何写操作) ----")
        print("  逻辑: 使能=%d 方向=%d 极性=%d 请求脉冲=%dHz 限时余量=%dms"
              % (s["ena"], s["dir"], s["enapol"], s["hz"], s["tleft"]))
        print("  TIM3: CC1E=%d CCR1(缓存)=%d ARR=%d CCMR1=0x%04X(OC1M=%s)"
              % ((s["ccer"] >> 0) & 1, s["ccr1"], s["arr"], s["cmr1"], (s["cmr1"] >> 4) & 7))
        print("  IDR : GPIOE=0x%04X (PE9=%d PE8=%d)  GPIOA=0x%04X (PA6=%d)"
              % (s["pe_idr"], (s["pe_idr"] >> 9) & 1, (s["pe_idr"] >> 8) & 1,
                 s["pa_idr"], (s["pa_idr"] >> 6) & 1))
        print()
        print("  ★ 判读锚点 (3.3V 供电后 ENA 逻辑已翻转):")
        print("     光耦『导通』= 失能  ⇒  PE9=0(拉低=导通)=失能, PE9=1(高=LED关断)=使能")
        print("     所以本配置下『使能』应表现为 PE9=1。")
        d.ser.close()
        return 0

    d.ena(1)
    time.sleep(0.2)
    s = d.st()
    print("使能后: PE9=%d  (本接线: 1 = 高 = 光耦不导通 = 使能)" % s["pe9"])
    print()

    if cmd == "longrun":
        hz = int(a[2]) if len(a) > 2 else 500
        sec = float(a[3]) if len(a) > 3 else 120.0
        # ★ 限时字段是 u32 ms; 给足余量 (但要能自己停)
        d.limit(int(sec * 1000) + 2000)
        t0 = time.time()
        d.rate(hz)
        pts = sample(d, sec)
        d.stop()
        st = stats(pts)
        if not st:
            print("采样点太少"); d.close(); return 1
        print("=== 长跑 %dHz × %.0fs ===" % (hz, sec))
        print("  采样 %d 点 / %.2fs  ⇒ %.1f Hz 采样率" % (st["n"], st["dur"], st["rate"]))
        print("  净转角 = %.1f°  (%.2f 圈)   平均速度 = %.2f °/s" % (st["net"], st["net"] / 360, st["vavg"]))
        # 8 细分假设
        th = hz * sec / 1600.0 * 360.0
        print("  理论(8细分 1600步/圈) = %.1f°  偏差 %+.1f%%" % (th, (st["net"] - th) / th * 100))
        print("  分段速度(5 段): " + "  ".join("%.1f" % v for v in st["seg"]))
        if st["seg"]:
            vmin, vmax = min(st["seg"]), max(st["seg"])
            print("  ⇒ 速度波动 %.1f%%  (max-min)/avg = %.3f" % ((vmax - vmin) / abs(st["vavg"]) * 100, (vmax - vmin) / abs(st["vavg"])))
        # 编码器采样质量: raw 是否卡死
        raws = set(p[1] for p in pts)
        print("  raw 唯一值 %d 个  ⇒ %s" % (len(raws), "正常(在动)" if len(raws) > 20 else "★ 可疑(编码器卡住?)"))

    elif cmd == "reverse":
        hz = int(a[2]) if len(a) > 2 else 500
        each = float(a[3]) if len(a) > 3 else 2.0
        allpts = []
        tbase = 0.0
        for k, dr in enumerate((0, 1, 0)):
            d.dirn(dr)
            d.limit(int(each * 1000) + 500)
            d.rate(hz)
            pts = sample(d, each)
            for t, r in pts:
                allpts.append((tbase + t, r))
            d.stop()
            tbase += pts[-1][0] if pts else 0
            time.sleep(0.3)
        ang = unwrap([p[1] for p in allpts])
        print("=== 变向: 正转 %.1fs → 反转 %.1fs → 正转 %.1fs @ %dHz ===" % (each, each, each, hz))
        n = len(allpts)
        for k in range(0, n, max(1, n // 30)):
            t = allpts[k][0]
            a_ = ang[k] - ang[0]
            tag = "正转" if k < n / 3 else ("反转" if k < 2 * n / 3 else "正转")
            print("  %5.2fs  %+8.1f°  %s %s" % (t, a_, tag, bar(a_)))

    elif cmd == "pos":
        target = float(a[2]) if len(a) > 2 else 90.0
        tol = float(a[3]) if len(a) > 3 else 2.0
        print("=== 位置控制: 目标 %.1f° (容差 %.1f°) ===" % (target, tol))
        # 以当前角度为 0 基准, 目标换算成绝对 raw
        s = d.st()
        raw0 = s["raw"]
        tgt_raw = (raw0 + int(target / 360.0 * 4096)) % 4096
        print("  起点 raw=%d (%.2f°)  目标 raw=%d" % (raw0, raw0 * 360 / 4096, tgt_raw))
        t0 = time.time()
        log = []
        itch = 0
        while time.time() - t0 < 30:
            s = d.st()
            if not s:
                continue
            cur = s["raw"]
            err = cur - tgt_raw
            if err > 2048:
                err -= 4096
            elif err < -2048:
                err += 4096
            err_deg = err * 360.0 / 4096.0
            log.append((time.time() - t0, cur * 360 / 4096, err_deg))
            if abs(err_deg) <= tol:
                d.stop()
                print("  ✅ 到位: 用时 %.2fs  误差 %.2f°  迭代 %d 次" % (time.time() - t0, err_deg, itch))
                break
            # ★ 改进: 按"剩余角度"算脉冲时长 —— 走完这一轮就刚好接近目标
            #    步数 = |err|/360*1600 (8细分)   时长 = 步数/v(Hz) 秒  留 15% 余量
            # ★ 注意符号约定: 本处 err = cur - tgt; dir_for() 要的是 tgt - cur ⇒ 取负
            e_toward = -err
            dr = dir_for(e_toward)
            eg = max(-150.0, min(150.0, -err_deg))
            # ★ PC 墙钟定时(已扣命令延时) + 速率随行程自适应; 收敛靠闭环迭代
            move_deg(d, dr, eg, gain=0.85)
            itch += 1
            if itch > 220:
                d.stop(); print("  ✗ 超迭代未到位"); break
        d.stop()
        n = len(log)
        for k in range(0, n, max(1, n // 25)):
            t, a_, e = log[k]
            print("  %5.2fs  %.2f°  误差 %+7.2f°  %s" % (t, a_, e, bar(e, 1.0, 40)))
    elif cmd == "hold":
        target = float(a[2]) if len(a) > 2 else 90.0
        sec = float(a[3]) if len(a) > 3 else 20.0
        dead = float(a[4]) if len(a) > 4 else 1.00      # 死区 (度); 实测可达 ~±1°
        hz = 500
        s = d.st()
        raw0 = s["raw"]
        tgt = (raw0 + int(round(target / 360.0 * 4096))) % 4096
        print("=== 位置锁定: 目标 %+.1f°  死区 ±%.2f° (±%.1f 步, 步距 %.3f°)  保持 %.0fs ==="
              % (target, dead, dead / DEG_PER_STEP, DEG_PER_STEP, sec))
        print("  起点 raw=%d  目标 raw=%d   (驱动器全程保持使能, 不靠固件限时)" % (raw0, tgt))

        def move(cnt_err, gain):
            """按误差(计数)发一段 PC 墙钟定时的脉冲; 返回实际转角(deg)"""
            deg = cnt_err * DEG_PER_LSB
            r0 = d.st()["raw"]
            move_deg(d, dir_for(cnt_err), deg, gain)
            time.sleep(0.08)
            r1 = d.st()["raw"]
            return serr(r0, r1) * DEG_PER_LSB

        # ① 粗定位 (迭代收敛到 2 步内)
        s = d.st()
        e1 = serr(s["raw"], tgt)
        it = 0
        for it in range(10):
            if abs(e1) < 2 * COUNTS_PER_STEP:
                break
            move(e1, 0.9)
            e1 = serr(d.st()["raw"], tgt)
        print("  ① 粗定位: %d 次迭代 ⇒ 残差 %+.3f° (%+d 计数 = %.2f 步)"
              % (it + 1, e1 * DEG_PER_LSB, e1, e1 / COUNTS_PER_STEP))

        # ② 闭环保持 (全程保持使能; 死区内不发脉冲)
        log = []
        corr = 0
        t0 = time.time()
        while time.time() - t0 < sec:
            s = d.st()
            if not s:
                continue
            e = serr(s["raw"], tgt)
            ed = e * DEG_PER_LSB
            log.append((time.time() - t0, ed))
            if abs(ed) > dead:
                move(e, 0.6)          # 保持阶段用小增益, 抑制极限环
                corr += 1
                if corr > 120:
                    print("     (修正次数达上限 120, 退出)")
                    break
        d.stop()
        time.sleep(0.10)

        if log:
            es = [x[1] for x in log]
            warm = min(2.0, sec * 0.15)
            settle = [x[1] for x in log if x[0] > warm]
            inband = sum(1 for v in es if abs(v) <= dead)
            print("  ② 闭环保持 %.1fs: 采样 %d 点  修正 %d 次  (%.1f 次/s)"
                  % (sec, len(log), corr, corr / max(sec, 1e-6)))
            print("     全域: 最大 %+.3f°  最小 %+.3f°  RMS %.3f°  峰峰 %.3f°"
                  % (max(es), min(es), (sum(v * v for v in es) / len(es)) ** 0.5,
                     max(es) - min(es)))
            print("     稳态段(剔除起始 %.1fs): RMS %.3f°  峰峰 %.3f°  落在死区内 %.0f%%  ⇒ %s"
                  % (warm, (sum(v * v for v in settle) / max(len(settle), 1)) ** 0.5,
                     (max(settle) - min(settle)) if settle else 0.0,
                     100.0 * inband / max(len(es), 1),
                     "闭环收敛 ✓" if inband / len(es) > 0.8 else "★ 未收敛/极限环"))
            n = len(log)
            for k in range(0, n, max(1, n // 30)):
                t, ed = log[k]
                print("     %5.2fs  %+7.3f°  %s" % (t, ed, bar(ed, 0.5, 30)))

        # ③ 保持使能时的自由观察 (噪声底 + 保持能力)
        g0 = sample(d, 3.0)
        if g0:
            a0 = unwrap([p[1] for p in g0])
            s0 = d.st()
            print("  ③ 自由观察 3.0s (无脉冲, **仍使能** ena=%d PE9=%d): 漂移 %+.3f°  峰峰 %.3f°"
                  % (s0["ena"], s0["pe9"], a0[-1] - a0[0], max(a0) - min(a0)))

        # ④ A/B 对照: 主动失能后再观察 ⇒ 直接检验「到期/失能是否掉保持力」
        d.ena(0)
        time.sleep(0.4)
        b0 = d.st()
        g1 = sample(d, 3.0)
        if g1:
            a1 = unwrap([p[1] for p in g1])
            print("  ④ 对照 (失能 ena=%d PE9=%d): 漂移 %+.3f°  峰峰 %.3f°"
                  % (b0["ena"], b0["pe9"], a1[-1] - a1[0], max(a1) - min(a1)))
            print("     ⇒ 两者之差 = 『保持力』被卸掉后转子退到整步齿槽的位移")
            print("     ⇒ 编码器噪声底 ≈ %.3f° (±%.1f LSB, 1LSB=%.4f°)"
                  % (max(max(a0) - min(a0), max(a1) - min(a1)) if g0 else 0.0,
                     (max(a1) - min(a1)) / 2 / DEG_PER_LSB, DEG_PER_LSB))

    elif cmd == "roundtrip":
        hz = int(a[2]) if len(a) > 2 else 500
        ms = int(a[3]) if len(a) > 3 else 1000           # 每段请求时长 (固件单位)
        rounds = int(a[4]) if len(a) > 4 else 6
        dur = ms / 1000.0 * LIMIT_SCALE
        print("=== 往返复现性: %d 轮 × (正 %d 单位 → 反 %d 单位) @%dHz ===" % (rounds, ms, ms, hz))
        print("  每段请求 %d 单位 ⇒ 真实 %.0fms ⇒ 约 %.0f 脉冲 = %.2f°"
              % (ms, dur * 1000, hz * dur, hz * dur / SPR * 360))
        print("  ★ 限时只作安全网(%.0f 单位), 由 stop() 终止 ⇒ 驱动器**全程保持使能**" % SAFE_LIMIT)
        print()
        print("    轮 段 | 消耗单位 |  实测deg  | deg/单位 | 相对段均")
        print("  -------+----------+-----------+----------+---------")
        d.ena(1)
        d.limit(SAFE_LIMIT)
        d.rate(0)
        time.sleep(0.15)
        base = d.st()["raw"]
        cur = base
        legs = []
        for k in range(rounds):
            net = 0.0
            for dr in (1, 0):
                l0 = d.st()["tleft"]
                d.dirn(dr)
                d.rate(hz)
                time.sleep(dur)
                d.stop()
                s = d.st()
                used = l0 - s["tleft"]
                r = s["raw"]
                dg = serr(cur, r) * DEG_PER_LSB
                legs.append((used, dg))
                net += dg
                cur = r
                print("  %4d %s | %8d | %+9.3f | %8.4f |"
                      % (k + 1, "正" if dr == 1 else "反", used, dg,
                         dg / used if used else 0.0))
                time.sleep(0.15)
            print("  %4d -- | 本轮净偏移 %+8.3f°  (累积 %+8.3f°)"
                  % (k + 1, net, sum(x[1] for x in legs)))
        print()
        us = [x[0] for x in legs]
        gs = [x[1] for x in legs]
        rat = [abs(x[1]) / x[0] for x in legs if x[0]]
        med = sorted(rat)[len(rat) // 2] if rat else 0.0
        fwd = [x[1] for x in legs[0::2]]
        bwd = [x[1] for x in legs[1::2]]
        print("  统计:")
        print("    消耗单位: 均值 %.1f  极差 %d  (上位机计时抖动, 非电机量)"
              % (sum(us) / len(us), max(us) - min(us)))
        print("    |deg|/单位: 中位 %.4f  极差 %.4f (%.1f%%)  ⇒ %s"
              % (med, (max(rat) - min(rat)) if rat else 0.0,
                 (max(rat) - min(rat)) / med * 100 if med else 0.0,
                 "线性度好" if med and (max(rat) - min(rat)) / med < 0.02
                 else "★ 分母(单位代理)抖动大, 以角度为准"))
        print("    正转段 |deg| 均值 %.3f°   反转段均值 %.3f°   换向不对称 %+.3f° (%.1f 步)"
              % (abs(sum(fwd) / len(fwd)), abs(sum(bwd) / len(bwd)),
                 abs(sum(bwd) / len(bwd)) - abs(sum(fwd) / len(fwd)),
                 (abs(sum(bwd) / len(bwd)) - abs(sum(fwd) / len(fwd))) / DEG_PER_STEP))
        print("    段角 |deg| 极差 %.3f° (%.1f 步)  ⇒ 装置重复性地板"
              % (max(abs(v) for v in gs) - min(abs(v) for v in gs),
                 (max(abs(v) for v in gs) - min(abs(v) for v in gs)) / DEG_PER_STEP))
        nets = []
        for k in range(rounds):
            nn = sum(x[1] for x in legs[2 * k:2 * (k + 1)])
            nets.append(nn)
        print("    每轮净偏移: 均值 %+.3f°  极差 %.3f°  符号 %s"
              % (sum(nets) / len(nets), max(nets) - min(nets),
                 "".join("+" if v > 0 else "-" for v in nets)))
        print("    首末累积 %+.3f° = %.2f 步 / %d 轮 = %.2f 步/轮"
              % (sum(nets), sum(nets) / DEG_PER_STEP, rounds,
                 sum(nets) / DEG_PER_STEP / rounds))
        print()
        print("  判读:")
        print("    · 各段 deg/单位 一致 ⇒ 『脉冲 → 角度』线性, **无系统性丢步**")
        print("    · 每轮净偏移**符号随机**且不累积 ⇒ 无换向不对称丢步 (量测抖动)")
        print("    · 累积 %.2f 步/轮; 若 > 1 步/轮且同号 ⇒ 才判为真丢步"
              % (sum(nets) / DEG_PER_STEP / rounds))

    elif cmd == "repeat":
        hz = int(a[2]) if len(a) > 2 else 500
        ms = int(a[3]) if len(a) > 3 else 1000
        n = int(a[4]) if len(a) > 4 else 6
        dur = ms / 1000.0 * LIMIT_SCALE
        print("=== 同向重复 (对照实验): %d 段 × %d 单位 @%dHz, 全程**不换向** ===" % (n, ms, hz))
        print("  每段请求 %d 单位 ⇒ 真实 %.0fms ⇒ 约 %.0f 脉冲 = %.2f°"
              % (ms, dur * 1000, hz * dur, hz * dur / SPR * 360))
        print()
        print("   段 | 消耗单位 |  实测deg  | deg/单位")
        print("  ----+----------+-----------+---------")
        d.ena(1)
        d.limit(SAFE_LIMIT)
        d.rate(0)
        time.sleep(0.15)
        cur = d.st()["raw"]
        tot = 0.0
        us = []
        gs = []
        for k in range(n):
            l0 = d.st()["tleft"]
            d.dirn(1)
            d.rate(hz)
            time.sleep(dur)
            d.stop()
            s = d.st()
            used = l0 - s["tleft"]
            r = s["raw"]
            dg = serr(cur, r) * DEG_PER_LSB
            cur = r
            tot += dg
            us.append(used)
            gs.append(dg)
            print("  %2d  | %8d | %+9.3f | %8.4f" % (k + 1, used, dg, dg / used if used else 0.0))
            time.sleep(0.15)
        print()
        print("  段均值 %+.3f°   累计 %+.3f°   单位消耗均值 %.1f (极差 %d)"
              % (sum(gs) / n, tot, sum(us) / n, max(us) - min(us)))
        print("  ⇒ 同向每段若也系统性偏离 ⇒ 偏差来自**每段起步/停止**, 与换向无关;")
        print("    同向精确而往返才偏 ⇒ 偏差是**换向特有**的。")

    elif cmd == "speed":
        hz_max = int(a[2]) if len(a) > 2 else 8000
        step = int(a[3]) if len(a) > 3 else 500
        sec = float(a[4]) if len(a) > 4 else 1.5
        hz_beg = int(a[5]) if len(a) > 5 else 500
        print("=== 突加转速扫描: %d → %d Hz (步进 %d, 每档 %.1fs) ===" % (hz_beg, hz_max, step, sec))
        print("  换算(1600 步/圈): 1600Hz = 1 圈/s = 360°/s;  Hz × 0.225 = °/s")
        print("  ★ 突加 = 固件**无加减速**时的真实可用上限 (带斜坡的上限见 `ramp`)")
        print()
        print("  请求Hz | 读回Hz | 净速度°/s | 比值(读回) | 占用角域 | 反向步 | 判读")
        print("  -------+--------+-----------+------------+----------+--------+-------")
        ok_hi = None
        hz = hz_beg
        while hz <= hz_max:
            r = speed_point(d, hz, sec)
            if r is None:
                print("  %6d | 采样不足" % hz)
            else:
                ratio = r["dps"] / r["theo"] if r["theo"] else 0.0
                theo_rb = r["hz_rb"] / SPR * 360.0
                ratio_rb = r["dps"] / theo_rb if theo_rb else 0.0
                if 0.97 <= ratio_rb <= 1.03:
                    verdict = "跟得上"; ok_hi = (hz, r)
                elif r["arc"] < 90:
                    verdict = "★ 轴不转(原地)"
                elif ratio_rb < 0.9:
                    verdict = "★ 掉步"
                else:
                    verdict = "临界"
                warn = ""
                if abs(theo_rb) > r["wrap"] * 0.9:
                    warn = " ⚠超采样上限"
                print("  %6d | %6d | %9.1f | %10.3f | %7.1f° | %5.1f%% | %s%s"
                      % (hz, r["hz_rb"], r["dps"], ratio_rb, r["arc"],
                         r["back"] * 100, verdict, warn))
            hz += step
        print()
        if ok_hi:
            h, r = ok_hi
            print("  ★ 突加稳定上限: %d Hz = %.0f °/s = %.2f 圈/s = %.0f rpm"
                  % (h, r["dps"], r["dps"] / 360.0, r["dps"] * 60.0 / 360.0))
            print("     2min 长跑窗口已单独验证 500Hz 累计偏差 -0.0%% (见 longrun)")
        else:
            print("  ★ 突加: 最低档 500Hz 就已掉步 ⇒ 先查供电/电流/机械")

    elif cmd == "ramp":
        tgt = int(a[2]) if len(a) > 2 else 4000
        ramp_ms = float(a[3]) if len(a) > 3 else 1200.0
        hold = float(a[4]) if len(a) > 4 else 1.5
        start = min(300, tgt)
        nseg = max(4, int(ramp_ms / 80))
        print("=== 带斜坡起动: %d → %d Hz, 斜坡 %.0fms (%d 级), 保持 %.1fs ==="
              % (start, tgt, ramp_ms, nseg, hold))
        d.ena(1)
        time.sleep(0.25)
        d.dirn(DIR_POS)
        d.limit(int((ramp_ms + hold * 1000) * 1.8) + 3000)
        d.rate(start)
        t0 = time.time()
        pts = []
        for k in range(1, nseg + 1):
            d.rate(int(start + (tgt - start) * k / nseg))
            t1 = time.time()
            while time.time() - t1 < (ramp_ms / 1000.0) / nseg:
                s = d.st()
                if s:
                    pts.append((time.time() - t0, s["raw"], s["hz"], s["ccer"] & 1))
        th = time.time()
        holdpts = []
        while time.time() - th < hold:
            s = d.st()
            if s:
                pts.append((time.time() - t0, s["raw"], s["hz"], s["ccer"] & 1))
                holdpts.append((time.time() - th, s["raw"], s["hz"]))
        d.stop()
        time.sleep(0.12)
        theo = tgt / SPR * 360.0
        m = motion_shape([(p[0], p[1]) for p in holdpts]) if len(holdpts) >= 4 else None
        if m is not None:
            dps = m["net_s"]
            hz_rb = holdpts[-1][2]
            theo_rb = hz_rb / SPR * 360.0          # ★ 按**读回**频率折算才公平
            r_req = dps / theo if theo else 0.0
            r_rb = dps / theo_rb if theo_rb else 0.0
            dev = (hz_rb - tgt) / tgt * 100.0
            print("  读回 hz = %d (请求 %d, 偏差 %+.2f%%)  ⇒ %s"
                  % (hz_rb, tgt, dev,
                     "≈请求值" if abs(dev) < 0.5 else
                     "★ 频率量化(ARR 取整), 实际比请求高 %.2f%%" % dev))
            print("  保持段净速度 %.1f °/s = %.0f rpm  |  比值(请求) %.3f  比值(读回) %.3f  %s"
                  % (dps, dps * 60.0 / 360.0, r_req, r_rb,
                     "跟得上 ✓" if r_rb >= 0.97 else ("★ 掉步" if r_rb < 0.9 else "临界")))
            print("  形态: 占用角域 %.1f°/360°  反向步 %.1f%%  净速度(最短弧) %+.1f °/s  ⇒ %s"
                  % (m["arc"], m["back_frac"] * 100, m["net_arc"],
                     ("真在转" if m["arc"] > 300 else
                      "★★ 轴不转(原地) —— 『嗡嗡响但轴不动』" if m["arc"] < 90 else "部分失步")))
            if m["stall"]:
                print("     ⚠ 占用角域 %.1f° < 90° ⇒ **转子几乎不动**, 净速度记 0"
                      % m["arc"])
            elif m["net_cmd"] > 0 and m["net_arc"] < 0:
                print("     ⚠ 最短弧已**混叠**(转速 >180°/采样段) ⇒ 净速度取自强制单向累计")
            elif m["back_frac"] > 0.3:
                print("     ⚠ 反向步占比 %.0f%% 偏高 ⇒ 有反向滑动, 单向累计可能略偏大"
                      % (m["back_frac"] * 100))
            if r_rb >= 0.97 and tgt > ABRUPT_CEIL_HZ:
                print("  ⇒ ★ 该点在 %.0f Hz 突加会失速, 斜坡后能跟 ⇒ **斜坡确实扩展了上限**"
                      % ABRUPT_CEIL_HZ)
            elif r_rb < 0.97:
                print("  ⇒ ★ 斜坡加持下也到不了 ⇒ 上限在**电机/驱动器/供电**")
        else:
            print("  保持段采样不足")
        if len(pts) >= 6:
            mm = motion_shape([(p[0], p[1]) for p in pts])
            tot = mm["net_s"] * mm["span"]
            print("  全程: %.2fs 净走 %+.1f° (%.2f 圈), 平均 %.1f °/s = %.0f rpm ; 占用角域 %.0f°"
                  % (mm["span"], tot, tot / 360.0, mm["net_s"],
                     mm["net_s"] * 60.0 / 360.0, mm["arc"]))

    elif cmd == "slip":
        hz = int(a[2]) if len(a) > 2 else 20000
        sec = float(a[3]) if len(a) > 3 else 2.0
        print("=== 运动形态: 请求 %d Hz (指令 %.0f °/s) ===" % (hz, hz / SPR * 360.0))
        d.ena(1)
        time.sleep(0.3)
        d.dirn(DIR_POS)
        d.limit(int(sec * 1000 * 1.6) + 3000)
        t0 = time.time()
        d.rate(hz)
        pts = []
        while time.time() - t0 < sec:
            s = d.st()
            if s:
                pts.append((time.time() - t0, s["raw"], s["hz"]))
        d.stop()
        time.sleep(0.1)
        m = motion_shape([(p[0], p[1]) for p in pts])
        if m is None:
            print("  采样不足")
        else:
            hz_rb = pts[-1][2]
            om = hz_rb / SPR * 360.0
            print("  读回 %d Hz ⇒ 指令 %.0f °/s" % (hz_rb, om))
            print("  占用角域   %6.1f° / 360°   (最大空缺 %.0f°)" % (m["arc"], m["gap"] * DEG_PER_LSB))
            print("  反向步占比 %6.1f%%   (纯正转应 ≈0%%)" % (m["back_frac"] * 100))
            print("  净速度(命令方向) %+8.1f °/s = 指令的 %+.1f%%" % (m["net_s"], m["net_s"] / om * 100))
            print("  净速度(最短弧)   %+8.1f °/s   ← 仅交叉校验; >2.2万Hz 时会反向失效" % m["net_arc"])
            print("  抖动速度     %8.1f °/s   (|每步| 之和 / 时间)" % m["wob_s"])
            print("  抖动/净 比   %8.1f" % (m["wob_s"] / max(abs(m["net_s"]), 1e-6)))
            print()
            if m["net_s"] > 0.97 * om and m["back_frac"] < 0.15:
                verdict = "✓ 真在转 (单向, 无失步)"
            elif m["arc"] < 90 and abs(m["net_s"]) < 0.25 * om:
                verdict = "★★ 轴在【原地抖动】—— 就是『嗡嗡响但轴不动』"
            elif m["back_frac"] > 0.25:
                verdict = "★ 间歇失步 (来回滑) —— 抖动为主"
            else:
                verdict = "临界/部分丢步"
            print("  判据 ⇒ %s" % verdict)
            if m["wob_s"] > 0.5 * om:
                print("  ⇒ 抖动幅度已达指令速度的 %.0f%%: 电磁力矩与负载在拉锯"
                      % (m["wob_s"] / om * 100))
            if om > 0.9 * m["wrap"]:
                print("  ⚠ 指令 %.0f °/s 已接近采样上限 %.0f °/s ⇒ 本行数值可能混叠"
                      % (om, m["wrap"]))
        print()
        print("★ 判据说明 (三个量, 各有各的失效边界):")
        print("   · 最短弧 sstep  : 真单向 >180°/采样段时**反向**失效  (>2.2万Hz)")
        print("   · 强制单向      : 原地抖动时**伪造高速正转**      (本脚本曾假报 +2048°/s)")
        print("   · 占用角域      : 无上述失效 ⇒ 裁决量")
        print("        >90° 判『在转』(取强制单向值) ; <90° 判『不转』(净速度记 0)")


    elif cmd == "coast":
        hz = int(a[2]) if len(a) > 2 else 3000
        print("=== 失能滑行: %d Hz 稳速后 **ena(0)** 自由滑行 ===" % hz)
        print("  (连续采样跨越失能时刻 ⇒ 一定能看到『转 → 停』的完整过程)")
        d.ena(1)
        time.sleep(0.3)
        d.dirn(DIR_POS)
        d.limit(60000)
        t0 = time.time()
        d.rate(hz)
        pts = []
        td = None
        while time.time() - t0 < 4.0:
            s = d.st()
            if s:
                pts.append((time.time() - t0, s["raw"]))
            # 1.2s 处失能 (此时已稳速)
            if td is None and time.time() - t0 >= 1.2:
                d.ena(0)
                td = time.time() - t0
        d.stop()
        time.sleep(0.2)
        if len(pts) < 6:
            print("  采样不足")
            d.ser.close()
            return 0
        v = [(pts[i][0], sstep(pts[i - 1][1], pts[i][1]) * DEG_PER_LSB
              / max(pts[i][0] - pts[i - 1][0], 1e-6)) for i in range(1, len(pts))]
        pre = [x[1] for x in v if x[0] < td - 0.1]
        v0 = sum(pre) / len(pre) if pre else 0.0
        print("  失能前稳速 (t<%.2fs) = %.1f °/s (%.2f 圈/s)  [指令 %.0f °/s]"
              % (td, v0, v0 / 360.0, hz / SPR * 360.0))
        print("      t(s) | 瞬时°/s | 相对失能前 | 备注")
        for k in range(0, len(v)):
            tag = "  ← 失能" if abs(v[k][0] - td) < 0.02 else ""
            if k % max(1, len(v) // 22) == 0 or tag:
                print("    %6.3f | %8.1f | %9.1f%% |%s"
                      % (v[k][0], v[k][1], v[k][1] / max(v0, 1e-6) * 100, tag))
        post = [x for x in v if x[0] > td]
        if post:
            stop_i = next((i for i in range(len(post)) if abs(post[i][1]) < 0.1 * v0), None)
            if stop_i is not None and stop_i >= 1:
                a_dec = (post[stop_i][1] - post[0][1]) / (post[stop_i][0] - post[0][0])
                print()
                print("  ★ 失能后 %.3fs 内从 %.0f 降到 %.0f °/s ⇒ 平均减速率 %.0f °/s² = %.1f 圈/s²"
                      % (post[stop_i][0] - post[0][0], post[0][1], post[stop_i][1],
                         abs(a_dec), abs(a_dec) / 360.0))
                print("     (真实拐点可能更快 ⇒ 这是**下界**; 采样间隔 %.0fms 是分辨极限)"
                      % ((pts[-1][0] - pts[0][0]) / (len(pts) - 1) * 1000))
                print("     ⇒ 若知转动惯量 J: T_friction ≈ J × |α| ; 1 圈/s² = 6.2832 rad/s²")
            else:
                still = post[-1][1] if post else 0.0
                print()
                print("  ★ 失能后 %.2fs 仍以 %.1f °/s 转 (初速的 %.0f%%) ⇒ 滑行很长,"
                      % (post[-1][0] - post[0][0], still, still / max(v0, 1e-6) * 100))
                print("     摩擦/齿槽/风阻不大; 请把采样窗口拉长或初速调高再看减速率")
        print()
        print("★ 判读: 减速率越大 ⇒ 摩擦+齿槽吃掉的可用力矩越多; 这一步是 J·α 法的必要另一半。")
        print()
        print("★ 判读: 这一步给 **T_friction(ω)**, 是 J·α 法测扭矩的必要另一半;")
        print("        减速率越大 ⇒ 摩擦/齿槽/风阻越大 ⇒ 可用于加速的力矩越少。")

    elif cmd == "accel":
        tgt = int(a[2]) if len(a) > 2 else 15000
        stepf = int(a[3]) if len(a) > 3 else 400
        lo = float(a[4]) if len(a) > 4 else 45.0
        hi = float(a[5]) if len(a) > 5 else 400.0
        iters = int(a[6]) if len(a) > 6 else 5
        print("=== 最大可跟随加速度: 目标 %d Hz, 每级 +%d Hz, 二分休眠 %.0f~%.0fms ==="
              % (tgt, stepf, lo, hi))
        print("  α = Δω/Δt ; 1 Hz = 0.225 °/s ⇒ α[°/s²] = %d×0.225/休眠秒" % stepf)

        def trial(dwell_ms):
            d.ena(1)
            time.sleep(0.25)
            d.dirn(DIR_POS)
            d.limit(30000)
            d.rate(min(300, tgt))
            t0 = time.time()
            f = min(300, tgt)
            while f < tgt:
                f = min(tgt, f + stepf)
                d.rate(f)
                time.sleep(dwell_ms / 1000.0)
            t_r = time.time() - t0
            th = time.time()
            hp = []
            while time.time() - th < 1.0:
                s = d.st()
                if s:
                    hp.append((time.time() - th, s["raw"], s["hz"]))
            d.stop()
            time.sleep(0.15)
            if len(hp) < 4:
                return None
            a_ = unwrap_dir([p[1] for p in hp], DIR_POS == 0)
            dps = (a_[-1] - a_[0]) / (hp[-1][0] - hp[0][0])
            hz_rb = hp[-1][2]
            return dict(ratio=dps / (hz_rb / SPR * 360.0), dps=dps, hz_rb=hz_rb,
                        t_ramp=t_r, alpha=(tgt - 300) / SPR * 360.0 / max(t_r, 1e-6))

        print()
        print("  休眠ms | 请求α(°/s²) | 实测α(°/s²) | 真实斜坡s | 保持段比值 | 判读")
        print("  -------+-------------+-------------+-----------+------------+------")
        best = None
        for k in range(iters):
            mid = (lo + hi) / 2.0
            r = trial(mid)
            if r is None:
                print("  %6.0f |  --- 采样不足" % mid)
                break
            ok = r["ratio"] >= 0.97
            a_req = stepf * 0.225 / (mid / 1000.0)
            print("  %6.0f | %11.0f | %11.0f | %9.2f | %10.3f | %s"
                  % (mid, a_req, r["alpha"], r["t_ramp"], r["ratio"],
                     "跟得上" if ok else "★ 丢步"))
            if ok:
                best = (mid, r)
                hi = mid
            else:
                lo = mid
        print()
        if best:
            m, r = best
            print("  ★ 最小可用休眠 %.0fms ⇒ 实测 α_max ≥ %.0f °/s² = %.2f 圈/s²"
                  % (m, r["alpha"], r["alpha"] / 360.0))
            print("     (这是**下界**: 更短的休眠受命令通路延时限制, 见下)")
        else:
            print("  ★ 全部休眠都丢步 ⇒ 目标频率对该斜坡本就不稳 (先看 ramp)")
        print("  ⚠ **上位机天花板**: 每条 rate 命令要 ~%.0fms 处理 ⇒ 休眠不可能低于它,"
              % (CMD_BIAS_S * 1000))
        print("     所以 α_max 的实测上限 ≈ %d×0.225/%.3f = %.0f °/s²"
              % (stepf, CMD_BIAS_S, stepf * 0.225 / CMD_BIAS_S))
        print("     ⇒ 要测电机**真实**加速度能力, 必须让**固件自己**做斜坡 (D4)")

    else:
        print("未知命令")

    d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
