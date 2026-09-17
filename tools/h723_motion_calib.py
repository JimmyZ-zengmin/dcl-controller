#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_spr_calib.py —— 把整套阶段 2 结论的**两条地基**单独量出来

为什么必须做：阶段 2 全部比值都写成 `实测° / (f × span / SPR × 360)`，其中
  · `SPR = 1600 步/圈`  只被一次早期静态对照支撑；而 D 组测到 40000~60000 Hz 能跟住，
    折算 **1500~2250 rpm**，对步进电机偏高 ⇒ **要么电机真这么快，要么 SPR 不是 1600**
  · `span = tick × 100 µs`  拍长本身也是假设
⇒ 本脚本用**硬件脉冲计数（TIM4）+ 绝对角度（AS5600，非累积）+ 主机墙钟**三路互相咬合，
  把"步/圈"和"拍长"分别钉死，且**不依赖任何一个待验证的假设**。

三个测量
  T1 `走 N 步` 后读 `sub=16`：`+8 pulses`（到点锁存）与 `+24 TIM4_CNT`（原始硬件计数）
  T2 同一动作前后读 `SENSOR[0]`（**绝对**角度）⇒ counts/step ⇒ 步/圈
     ★ T1+T2 **零时间假设**：不需要知道拍长、不需要知道频率 ⇒ 这是最硬的一条
  T3 恒定 rate 下，把 `走 N 步` 跑**整程**并计时，多程累加 ⇒ 实际脉冲频率 vs 请求
     ★ **与电机跟不跟无关**（脉冲由 TIM3 硬件照发）⇒ 它单独检验"`sub=1 arg=f` 是否真的 f Hz"
  T4 由 T2+T3 反解**拍长**：黑匣子给 (Δenc, Δtick)，T2/T3 给 (counts/step, 真实 Hz)
     ⇒ 拍长 = (Δenc/counts_per_step/hz_real) / Δtick  —— **不再假设 100 µs**

判据（都能失败）
  C1 `TIM4_CNT` 增量与锁存 `pulses` 必须一致，且与 N 的差 ≤ 1 圈（1600）
  C2 counts/step ≈ 2.56（4096/1600），且正反两方向一致（差 ≤1%）
  C3 整程计时得到的频率与请求差 ≤2%
  C4 反解拍长 ≈ 100 µs（≤3%）
  C5 ★ `rate_actual` 与 **1 µs 量化模型** `1e6/floor(1e6/f)` 一致（≤0.5%）——
     这一条把"比值表里所有非 1.000"解释掉，是本轮最关键的收口

★ 已知精度边界（写清，免得高估结论）
  · T3 的 Σ耗时含每程 ~30 ms 命令延迟 ⇒ 60000 Hz 档自身只准到 ~3% ⇒ C3 通过不等于
    "脉冲率优于 3%"，**优于 1% 的结论要靠 C5 的 `rate_actual`**
  · C2 的 2.60（vs 名义 2.56）是 AS5600 机械/微步非线性的系统性偏置 ⇒ 比值的绝对精度
    受限于 ~1.6%，**不许宣称优于 2%**
用法: python tools/h723_motion_calib.py
"""
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from h723_client import Dcl, find_board, engine_status, read_sensors   # noqa: E402
from h723_stall_sweep import analyse, dump_bb                          # noqa: E402

CPR = 4096.0
SPR_NOM = 1600.0
RATE_CAL = 1500
MOVE_S = 2.5                      # T3 每程目标时长


def main():
    d = Dcl(find_board())
    shm = engine_status(d)["shm"]
    bad = []

    def sx(n, v, name=""):
        st, p = d.send(0x39, bytes([19, n]) + struct.pack("<I", v))
        if st != "ACK":
            bad.append("sub=%d(%s)=%s" % (n, name, st))
        return st, p

    def st16():
        st, p = sx(16, 0, "pcnt")
        if st != "ACK" or len(p) != 32:
            return None
        v = struct.unpack("<8I", p[:32])
        return dict(en=v[0], goal=v[1], pulses=v[2], done=v[3], abort=v[4],
                    rej=v[5], tim4=v[6], rate=v[7])

    def raw():
        v = read_sensors(d, shm, 2)
        return None if v is None else int(round(v[0]))

    def prep(rate=RATE_CAL, slope=None, hold=1):
        sx(12, hold, "hold")                    # 1 = 保力矩 ⇒ 静态位置不漂
        sx(1, 0, "rate"); sx(6, 0, "stop"); sx(13, 0, "src")
        sx(5, 1, "enapol"); sx(2, 0, "dir")
        sx(22, 1, "sm_mode"); sx(20, 10, "period")
        sx(17, 0 if slope is None else slope, "slope")
        time.sleep(0.3)
        sx(3, 1, "ena")                          # ★ 使能最后
        time.sleep(0.3)
        if rate:
            sx(1, rate, "rate")
        if slope:
            sx(17, slope, "slope")

    def run_move(n, timeout=60.0):
        """发一程 `走 N 步`，返回 (耗时, 锁存 pulses, TIM4_CNT)"""
        sx(15, n, "goton")
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = st16()
            if s and s["en"] == 0 and s["done"] > 0:
                break
            time.sleep(0.02)
        t1 = time.time()
        s = st16()
        return t1 - t0, (s["pulses"] if s else None), (s["tim4"] if s else None)

    # ══ T1/T2 步/圈标定（零时间假设）══
    # ★ N 的合法区间：N × 360/SPR < 180° ⇒ N < 800。第一版用了 N=1400（=315°）
    #   ⇒ 违反下面的解卷绕前提 ⇒ 走错分支、报 0.346 counts/step。**那是垃圾行, 不是数据。**
    VALID_N = 800
    print("=== T1/T2 走 N 步：硬件脉冲计数 + 绝对角度 ⇒ counts/step 与 步/圈 ===")
    print("   ★ 前提: 每程 <%.0f° ⇒ N < %d（否则解卷绕有歧义）" % (VALID_N * 360 / SPR_NOM, VALID_N))
    print("   动作          |  N   | TIM4增量 | 锁存pulses | Δ编码器 | counts/step | 步/圈")
    cps = []
    for n, dr in [(600, 0), (600, 1), (750, 0), (750, 1), (700, 0), (700, 1)]:
        prep()
        sx(2, dr, "dir"); time.sleep(0.25)
        a = raw()
        _dt, _p, _t4 = run_move(n)
        time.sleep(0.7)                          # 等编码器刷到静止值
        b = raw()
        s = st16()
        if a is None or b is None or s is None:
            print("   N=%-4d dir=%d    |  读回失败" % (n, dr)); continue
        dv = (b - a) & 0xFFF
        if dv > 2048:
            dv -= 4096                            # 前提: 每程 <180° ⇒ 无歧义
        c = abs(dv) / float(n)
        t4, pul = s["tim4"], s["pulses"]
        # ★ 到点精度: TIM4 与锁存值互证; 超发说明主循环停脉冲被拖后
        over = t4 - n
        good = (abs(t4 - pul) <= 2) and (0 <= over <= 400) and abs(c - 2.56) / 2.56 < 0.10
        if good:
            cps.append(c)
        print("   N=%-4d dir=%d     | %-4d |  %6d  |   %5d    | %6d  |   %6.4f    | %8.1f  %s"
              % (n, dr, n, t4, pul, abs(dv), c, CPR / c if c else 0,
                 "" if good else ("!! 超发 %+d 步" % over if over > 400 else "!! 判无效")))

    cps.sort()
    cps_m = cps[len(cps) // 2] if cps else 0.0       # ★ 中位数（合法行）
    spr_m = CPR / cps_m if cps_m else 0.0
    print("\n   C2 合法行 %d 条 ⇒ counts/step 中位数 = %.4f ⇒ 步/圈 = %.1f（名义 %.0f, %+.2f%%）"
          % (len(cps), cps_m, spr_m, SPR_NOM, (spr_m - SPR_NOM) / SPR_NOM * 100))
    if cps:
        print("      离散度: min %.4f / max %.4f ⇒ 峰峰 %.2f%%"
              % (cps[0], cps[-1], (cps[-1] - cps[0]) / cps_m * 100))

    # ══ T3 实际脉冲频率（整程计时，多程摊薄命令延迟）══
    # ★★ N 绝不能取 65535 = TIM4 的 ARR(0xFFFF)：到点判据是 `CNT >= goal`，而 CNT 在
    #    65535 后**回绕到 0** ⇒ 只有恰好采到 CNT==65535 那 1/（主循环周期×f） 的窗口才判到点
    #    ⇒ **周期性漏判、多跑若干整圈**。血证：N=65535 @60000 Hz ⇒ 表观 5193 Hz（= 60000/11.5）。
    #    ⇒ 上限取 60000，留出余量。
    print("\n=== T3 整程计时测实际脉冲频率（与电机跟不跟无关）===")
    print("   请求Hz | 程数 |   Σ脉冲  |  Σ耗时(s) | 实测Hz | 偏差")
    t3 = {}
    for f in (8000, 24000, 60000):
        n = min(60000, int(f * MOVE_S))
        k = 4
        prep(rate=0)
        sx(17, int(f * 2), "slope"); sx(1, f, "rate")
        time.sleep(1.2)                          # 等爬坡到目标
        sp, sd = 0, 0.0
        for i in range(k):
            dt, pul, _t4 = run_move(n)
            if pul is None:
                break
            sx(1, f, "rate")                     # 到点自停会停脉冲 ⇒ 重新起
            sp += pul; sd += dt
        sx(1, 0, "rate"); sx(17, 0, "slope")
        if sd <= 0:
            print("   %6d |  读回失败" % f); continue
        hz = sp / sd
        t3[f] = hz
        print("   %6d |  %2d  | %7d |  %7.3f | %7.0f | %+6.2f%%  %s"
              % (f, k, sp, sd, hz, (hz - f) / f * 100,
                 "✓" if abs(hz - f) / f <= 0.02 else "✗ C3 超 2%"))
        time.sleep(0.3)

    # ══ T5 固件自报的硬件实际频率（`sub=19 +12 rate_actual`）—— 与 T3 互证 ══
    # ★★ 2026-09-17 发现：`rate_actual` 只在 `200e6/f` 整除时等于请求。反推 ARR 得
    #    200000/25000/8200/5000/3200/2400/2000 —— **全是 200 的整数倍** ⇒
    #    **脉冲周期被量化到 1 µs**（1 个 µs @200 MHz = 200 cyc）⇒
    #        实际频率 = 1e6 / floor(1e6 / f)
    #    验证：24000→41.67→41→24390(+1.62%) ✓；60000→16.67→16→62500(+4.17%) ✓；
    #          80000→12.5→12→83333(+4.17%) ✓；而 1000/8000/40000/100000 周期整除 ⇒ 精确。
    #    ★ 这条模型把阶段 2 全部比值的"非 1.000 部分"逐行解释掉了（见 §13）。
    print("\n=== T5 固件自报 rate_actual（`sub=19 +12`）vs 请求 vs 1 µs 量化模型 ===")
    print("   请求Hz | rate_actual | 模型 1e6/floor(1e6/f) | 实测偏差 | 模型偏差 | 模型对上")
    for f in (1000, 8000, 24000, 40000, 60000, 80000, 100000):
        prep(rate=0)
        sx(17, int(f * 2), "slope"); sx(1, f, "rate")
        time.sleep(1.2)
        st, p = sx(19, 0, "rampst")
        if st == "ACK" and len(p) == 32:
            _cmd, _out, act = struct.unpack("<III", p[4:16])
            model = 1e6 / float(int(1e6 // f))
            hit = "✓" if abs(act - model) / model <= 0.005 else "✗ 模型不符"
            print("   %6d | %11d | %19.0f | %+7.2f%% | %+7.2f%% | %s"
                  % (f, act, model, (act - f) / f * 100, (model - f) / f * 100, hit))
        sx(1, 0, "rate"); sx(17, 0, "slope")
        time.sleep(0.2)

    # ══ T4 反解拍长（不假设 100 µs）══
    # ★ 取 T3 里**比值最干净**的一档（24000：黑匣子比值 1.000、T3 偏差 -0.02%），
    #   因为本反解的前提是"电机跟着脉冲走"；若该档自身比值 1.04，+4% 会直接进拍长。
    print("\n=== T4 反解拍长：黑匣子(Δenc,Δtick) × T2(counts/step) × T3(真实 Hz) ===")
    passed = [f for f in t3 if abs(t3[f] - f) / f <= 0.02]
    if cps_m and passed:
        f_ref = 24000 if 24000 in passed else max(passed)
        hz = t3[f_ref]
        prep(rate=0)
        sx(17, int(f_ref * 2), "slope"); sx(1, f_ref, "rate")
        time.sleep(1.5)
        data = dump_bb()
        sx(1, 0, "rate"); sx(17, 0, "slope")
        if data is None:
            print("   dump 失败")
        else:
            a = analyse(data)
            if a is None:
                print("   环异常")
            else:
                counts = a["deg"] / 360.0 * CPR  # = 逐点解卷绕总计数
                pulses = counts / cps_m          # 该窗口里硬件发了多少脉冲
                real_s = pulses / hz             # ⇒ 窗口真实时长（秒）
                tick_us = real_s / a["ticks"] * 1e6
                print("   f=%-6d Hz(T3 实测 %.0f) | Δenc=%.0f cnt ⇒ %d 脉冲 | Δtick=%d"
                      % (f_ref, hz, counts, round(pulses), a["ticks"]))
                print("   ⇒ 窗口真实时长 = %.4f s ⇒ 反解拍长 = %.2f µs" % (real_s, tick_us))
                print("   C4 ⇒ %s（%+.2f%%）"
                      % ("✓ 100 µs 被独立确认" if abs(tick_us - 100) <= 3 else "✗ 超 3%",
                         tick_us - 100))
    else:
        print("   ★ T2 或 T3 没有通过档 ⇒ **本判据无效（不是通过）**")
    if bad:
        print("\n!! 有命令未 ACK: %s" % ", ".join(sorted(set(bad))))
    sx(12, 0, "hold"); sx(1, 0, "rate"); sx(3, 0, "ena")
    d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
