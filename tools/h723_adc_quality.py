#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_adc_quality.py — AI(ADC) 的**量化质量**指标：噪声 / 有效位数 ENOB / 重复性。

## 它解决什么问题
`h723_adc_live.py` 证明的是"**能读**"（跟着旋钮动、能到满量程）。本工具回答的是
"**读得准吗**" —— 这是两个不同的宣称，必须分开证。

## 为什么不需要万用表
噪声、ENOB、重复性都是**自洽指标**：只用 ADC 自己的读数就能算，
不需要外部参考电压。（反过来，**绝对精度**与 **INL** 必须要参考仪器 —— 本工具不声称测了它们，
见"本工具不证明什么"。）

## 工作方式（关键：自动分段）
连续高速采样，自动识别"读数停住的平台"：
  滑动窗口内极差 < THRESH ⇒ 判为稳定 ⇒ 归入当前段；否则开新段。
⇒ 你只需要「转到位置 → 停约 2s → 转到下一个」，不必报位置、不必匀速。

## 判据
  Q1 平台可分辨   : 稳定段数 ≥ 3（否则说明没有可辨的平台, 或者只是噪声在漂）
  Q2 段内噪声     : 每段 σ（LSB）+ 峰峰值; 汇总平均 σ
  Q3 有效位数 ENOB: ENOD ≈ log2(满幅 / (σ·√12))  —— 16bit 标称, 实测能到几位是硬指标
  Q4 重复性       : 若你"转过去再转回来"经过同一位置, 两段均值应接近 (差 < 4σ)
  Q5 单调性       : 段均值序列在"单方向转动"期间应单调

## 本工具**不**证明什么（诚实边界）
- **绝对精度**：需要可溯源的参考电压（万用表/基准源）。`raw×3.3/65535` 只是"按 VREF+=3.3V 换算"。
- **INL/DNL**：需要已知的输入步进（分压基准或精密源）。电位器的机械角度不是已知量。
  两者都要等有参考仪器再测 —— 不要用本工具的读数去声称它们。

## 用法
    python tools/h723_adc_quality.py [COMxx] [--ch 16] [--sec 60] [--thresh 64]
"""
import sys
import time
import os
import math
import serial

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from h723_proto import crc16

VREF = 3.3
FULL = 65535.0
CH_NAME = {16: "PA0", 17: "PA1", 18: "PA4", 19: "PA5"}


def mk(cmd, p=b""):
    b = bytes([cmd, len(p) & 0xFF, (len(p) >> 8) & 0xFF]) + p
    c = crc16(b)
    return bytes([0xC0]) + b + bytes([c & 0xFF, c >> 8])


def talk(ser, fr, to=1.0):
    ser.reset_input_buffer()
    ser.write(fr)
    ser.flush()
    buf = b""
    end = time.time() + to
    while time.time() < end:
        ch = ser.read(1)
        if not ch:
            continue
        buf += ch
        i = buf.find(b"\xC1")
        if i < 0:
            buf = b""
            continue
        if i > 0:
            buf = buf[i:]
        if len(buf) < 4:
            continue
        n = buf[2] | (buf[3] << 8)
        need = 4 + n + 2
        while len(buf) < need and time.time() < end:
            m = ser.read(need - len(buf))
            if m:
                buf += m
        if len(buf) >= need:
            return buf[1], buf[4:4 + n]
    return None, None


def read_ch(ser, ch):
    sts, pl = talk(ser, mk(0x37, bytes([ch, 1])))
    if sts != 0 or pl is None or len(pl) < 2:
        return None
    return int.from_bytes(pl[0:2], "little")


def read_avg(ser, ch, n):
    """★ 连采 n 次取平均 —— 数字滤波的等效物（零硬件成本）。
    若噪声是白噪声, σ 应随 n 降到 ~σ1/√n；下降明显慢于此 ⇒ 含低频漂移成分,
    平均收益有限, 那时才需要考虑模拟 RC。"""
    if n <= 1:
        return read_ch(ser, ch)
    s = 0
    got = 0
    for _ in range(n):
        v = read_ch(ser, ch)
        if v is not None:
            s += v
            got += 1
    if not got:
        return None
    return s / got


def stats(xs):
    n = len(xs)
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    return m, math.sqrt(var), min(xs), max(xs)


def main():
    args = sys.argv[1:]
    port = "COM18"
    if args and not args[0].startswith("-"):
        port = args[0]
    ch = int(args[args.index("--ch") + 1]) if "--ch" in args else 16
    sec = float(args[args.index("--sec") + 1]) if "--sec" in args else 60.0
    thresh = int(args[args.index("--thresh") + 1]) if "--thresh" in args else 64
    avg = int(args[args.index("--avg") + 1]) if "--avg" in args else 1

    WIN = 16          # 稳定判定的滑动窗口长度
    MIN_SEG = 24      # 一个段至少这么多样本才算数
    SIGMA_MAX = 128.0  # ★ 段内 σ 上限(LSB): 超过它 = "转动中"而非"停住", 剔除出噪声统计

    ser = serial.Serial(port, 115200, timeout=0.15)
    print("=== ADC 质量测试 @ %s  ch%d(%s)  采 %.0fs  稳定阈值=%d LSB  平均=%d 次 ==="
          % (port, ch, CH_NAME.get(ch, "?"), sec, thresh, avg))
    print("★ 请这样配合：转到某个位置 → 停住约 2 秒 → 转到下一个位置 …… 重复 5~7 次")
    print("  （工具会自动识别你停住的平台，不需要报位置）\n")

    t_end = time.time() + sec
    samples = []
    nfail = 0
    last_show = 0.0
    while time.time() < t_end:
        v = read_avg(ser, ch, avg)
        if v is None:
            nfail += 1
            continue
        samples.append(v)
        now = time.time()
        if now - last_show > 0.4:
            last_show = now
            print("  采样 %5d   当前 %6d  (%.4fV)   " % (len(samples), v, v * VREF / FULL),
                  end="\r")
        time.sleep(0.01)
    print()

    if len(samples) < MIN_SEG * 2:
        print("!! 有效样本太少 (%d) —— 链路不稳？" % len(samples))
        return 2
    if nfail:
        print("  (读取失败 %d 次, 已跳过)" % nfail)

    # ---- 自动分段: 滑动窗口极差 < 阈值 ⇒ 属于当前段 ----
    segs = []
    cur = [samples[0]]
    i = 1
    while i < len(samples):
        w = cur[-WIN:] if len(cur) >= WIN else cur
        if len(w) >= WIN and (max(w) - min(w)) >= thresh:
            # 当前段结束
            if len(cur) >= MIN_SEG:
                segs.append(cur)
            cur = []
        cur.append(samples[i])
        i += 1
    if len(cur) >= MIN_SEG:
        segs.append(cur)

    print("\n=== 自动识别到 %d 个稳定平台 ===" % len(segs))
    print("  段  样本   均值(raw)   均值(V)       σ(LSB)   σ(mV)   峰峰值(LSB)  判定")
    print("  --- ----- ---------- -----------  -------  ------  ----------  --------")
    rows = []
    for k, s in enumerate(segs):
        m, sd, lo, hi = stats(s)
        rows.append((m, sd, lo, hi, len(s)))
        print("  %3d %5d %10.1f %11.5f  %7.2f  %6.3f  %10d   %s"
              % (k + 1, len(s), m, m * VREF / FULL, sd, sd * VREF / FULL * 1000, hi - lo,
                 "停住" if sd <= SIGMA_MAX else "转动中"))

    # ★★ 必须把"转动过程"从噪声统计里剔掉 —— 否则 avg σ 会被它拉高一个量级。
    #    判据用 **段内 σ** 而不是窗口极差: 慢转时窗口极差很小, 会被误判成"停住"
    #    (本轮实测踩到: 一段 σ=6321 LSB 把 avg 从 ~12 拉到 366, ENOB 从 10.6 掉到 5.7)。
    stable = [r for r in rows if r[1] <= SIGMA_MAX]
    shaky = len(rows) - len(stable)
    if shaky:
        print("\n  (其中 %d 段判为【转动中】, 已从噪声统计中剔除 —— 见上面【判定】列)" % shaky)

    if len(stable) >= 1:
        sds = sorted(r[1] for r in stable)
        avg_sd = sum(sds) / len(sds)
        med_sd = sds[len(sds) // 2]
        mn_sd = sds[0]
        note = "" if len(stable) >= 3 else "  ⚠ 段数偏少, σ 代表性有限(采样慢时分段粒度粗)"
        print("\n=== 汇总（只用【停住】的 %d 段）%s ===" % (len(stable), note))
        print("  σ 最小 = %.2f LSB (%.3f mV)" % (mn_sd, mn_sd * VREF / FULL * 1000))
        print("  σ 中位 = %.2f LSB (%.3f mV)" % (med_sd, med_sd * VREF / FULL * 1000))
        print("  σ 平均 = %.2f LSB (%.3f mV)" % (avg_sd, avg_sd * VREF / FULL * 1000))
        # ENOB: 满幅正弦 RMS = FULL/(2√2); 噪声 RMS = σ
        if med_sd > 0:
            snr = 20 * math.log10(FULL / (2 * math.sqrt(2) * med_sd))
            enob = (snr - 1.76) / 6.02
            print("  SNR(按σ中位) ≈ %.1f dB   ⇒ **ENOB ≈ %.2f 位** (标称 16 位)" % (snr, enob))
            print("  ⇒ 1 LSB(理想) = %.1f µV；实测噪声 ≈ %.1f LSB" %
                  (VREF / FULL * 1e6, med_sd))
        # 端点(零偏/满幅) —— 这不是绝对精度, 只是"两端有没有到位"
        ms = [r[0] for r in stable]
        print("  · 端点: 最低段均值 %.1f LSB 最高段均值 %.1f LSB (理想 0 / 65535)"
              % (min(ms), max(ms)))
        # 重复性: 找均值接近的段对
        rep = []
        for a in range(len(stable)):
            for b in range(a + 1, len(stable)):
                if abs(stable[a][0] - stable[b][0]) < 64:
                    rep.append(abs(stable[a][0] - stable[b][0]))
        if rep:
            print("  · 重复性: 有 %d 对段落在同一位置(均值差<64LSB), 最大差 %.1f LSB"
                  % (len(rep), max(rep)))
            print("    ⇒ 若该差值 ≲ σ, 说明**噪声是白噪声、没有系统性漂移**（这是好结果）")

    print("\n=== 判据 ===")
    ok = True
    if len(segs) >= 3:
        print("  ✓ Q1 识别到 %d 个稳定平台 (≥3)" % len(segs))
    else:
        print("  ✗ Q1 只识别到 %d 个平台 —— 转动幅度太小/太快, 或噪声大到平台分不出" % len(segs))
        ok = False
    if rows:
        print("  · Q2 段内噪声见上表 (峰峰值 = 同一位置的最大抖动)")
    # 单调性: 相邻段均值差的方向
    if len(rows) >= 3:
        ds = [rows[i + 1][0] - rows[i][0] for i in range(len(rows) - 1)]
        inc = sum(1 for d in ds if d > 0)
        dec = sum(1 for d in ds if d < 0)
        print("  · Q5 相邻段方向: 升 %d 次 / 降 %d 次 (你转的方向反转几次就该有几段反向)" % (inc, dec))
    print("\n  ★ 本工具不测绝对精度与 INL/DNL（那需要可溯源参考仪器）—— 别用这些数字声称它们。")
    ser.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
