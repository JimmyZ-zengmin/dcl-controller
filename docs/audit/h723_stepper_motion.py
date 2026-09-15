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

    else:
        print("未知命令")

    d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
