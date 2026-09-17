#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_ab_step_response.py —— **收益 A/B：PC 在环 vs 片内闭环**（临时脚手架）

## 测什么（"怎么测收益"定案后的第一条）
收益 = **消除"死时间"造成的固定误差项**（= 角速度 × 死时间）。
- PC 在环：死时间 **87 ms**（本日复现 3 次：87.4/87.8/80.1 ms）
- 片内：死时间 ≈ **1 拍** + 反馈量化 ⇒ 仍小 **~100 倍**
⇒ 判据取**阶跃响应的上升时间**：**两侧之差应 ≈ 死时间之差**（~80 ms 量级）。

## 公平性条件（不满足就又是一个不可信对照）
两侧**同增益 / 同误差带 / 同频率限幅 / 同目标 / 同起点**，**唯一变量 = 环路在哪**。
参数**直接照抄** `examples/h723_step_bounded_position.dcl`：
    tgt = -40.0（=320°）· kp = 4 · HZ_LO = 60 · hz_hi = 3000 · band = 0.5 · 最短弧折角 · 双极性

## ★ 起动时刻怎么对齐
- **PC 臂**：起动脉冲 = 循环里**第一次写 `sub=1`**
- **片内臂**：`sub=13`（运动源）**是开关** —— 先 `0`（脚手架，片内环不生效），把轴放到起点，
  再 `1` ⇒ **那一刻片内环接管** = 起动时刻 ✓（否则 `.dcl` 一部署就在闭环，"从远处起动"根本测不到）

## 采样公平性
- **PC 臂**用它**自己的循环**记录（时间戳+角度）—— 它的"控制周期"就是该循环，采样免费；
- **片内臂**用独立采样（**不影响片内控制**）。
★ 两者各自反映**自己的真实能力**，这才是公平的。

用法: python build/_ab_step_response.py [每臂次数=3]
"""
import math
import os
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from h723_client import Dcl, find_board       # noqa: E402

TGT = -40.0            # 与 .dcl 一致（=320°）
KP = 4.0
HZ_LO, HZ_HI = 60.0, 3000.0
BAND = 0.5
START_FBK = TGT - 40.0    # = -80°（=280°）⇒ 行程 40°，远离 ±180 奇异点
SPR = 1600.0
DEG2HZ = SPR / 360.0      # °/s → Hz


def fold(deg):
    """单圈角度折到 [-180,180) —— 与 .dcl 的 ADD/GE/SEL 等价。"""
    return deg if deg < 180.0 else deg - 360.0


def main():
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    d = Dcl(find_board())
    G = struct.unpack("<I", d.send(0x38)[1][23:27])[0]

    def rd_f(off):
        s, p = d.send(0x20, struct.pack("<I", G + off))
        return struct.unpack("<f", p[:4])[0] if (s == "ACK" and len(p) >= 4) else None

    def ang():
        """读编码器角度。★ 口径与套件 `h723_stepper_motion.st()` **一致**：读 `op=19 sub=0` 的
        96B 透视里 `+32` 的 raw（那是 `g_as_raw_v`）。
        ★ 已核实：**协议面上没有"强制实时读"的命令** ⇒ 臂 A 的反馈就是"163 Hz 更新的全局量"，
          而它**仍比 PC 环频 13.5 Hz 快** ⇒ **臂 A 的真实瓶颈是"命令通路 87 ms"** ✓
        （臂 B 的反馈是 `sensor[]`（1.1 kHz，拍内），瓶颈是"反馈 1 ms"）"""
        s, p = d.send(0x39, bytes([19, 0]) + struct.pack("<I", 0))
        if s != "ACK" or len(p) < 36:
            return None
        raw = struct.unpack("<24I", p[:96])[8] if len(p) >= 96 else None
        return None if raw is None else fold(raw * 360.0 / 4096.0)

    def sx(n, v):
        return d.send(0x39, bytes([19, n]) + struct.pack("<I", v))[0]

    def prep():
        """★ 使能**放最后**（`sub=6` 会按 hold=0 把使能关掉 —— 今天踩过两次）。"""
        sx(1, 0); sx(6, 0); sx(17, 0); sx(13, 0); sx(5, 1); sx(2, 0); sx(3, 1)
        time.sleep(0.3)

    def goto_start():
        """开环把轴放到 START_FBK 附近（±2°）。**不含闭环** ⇒ 两侧同款，不引入差异。"""
        for _ in range(400):
            a = ang()
            if a is None:
                time.sleep(0.05); continue
            e = START_FBK - a
            if abs(e) <= 2.0:
                break
            sx(1, 300) if abs(e) > 15 else sx(1, 60)
            sx(2, 0 if e >= 0 else 1)
            time.sleep(0.05)
        sx(1, 0); time.sleep(0.25)
        return ang()

    # ── 臂 A：PC 在环（控制与记录都在同一个循环里）──
    def arm_pc():
        a0 = goto_start()
        t0 = time.time(); pts = []; fired = False
        while time.time() - t0 < 8.0:
            a = ang()
            if a is None:
                continue
            tt = time.time() - t0
            pts.append((tt, a))
            e = TGT - a
            if abs(e) <= BAND:
                sx(1, 0)
                break
            hz = min(max(KP * abs(e), HZ_LO), HZ_HI)
            sx(2, 0 if e >= 0 else 1)
            if not fired:
                sx(3, 1); fired = True
            sx(1, int(hz))
        sx(1, 0)
        return a0, pts

    # ── 臂 B：片内闭环（`sub=13` 当起动开关）──
    def arm_chip():
        sx(13, 0)                        # 片内环不生效
        sx(22, 1); sx(20, 10)            # ★ 阶段 0：拍内反馈（~1 kHz，取代阻塞路 163 Hz）
        time.sleep(0.3)
        a0 = goto_start()
        t0 = time.time(); pts = []
        sx(3, 1)
        sx(13, 1)                        # ★ 起动时刻 = 片内环接管这一刻
        while time.time() - t0 < 8.0:
            a = ang()
            if a is None:
                continue
            pts.append((time.time() - t0, a))
            if abs(TGT - a) <= BAND:
                break
        sx(13, 0); sx(1, 0); sx(22, 0); sx(20, 100)   # ★ 收尾复原（阻塞路默认）
        return a0, pts

    def rise_time(pts, t_start=0.0):
        for tt, a in pts:
            if abs(TGT - a) <= BAND:
                return tt
        return None

    print("=== 收益 A/B：阶跃响应上升时间（tgt=%.0f° 起点≈%.0f° 行程 %.0f°）==="
          % (TGT, START_FBK, abs(TGT - START_FBK)))
    print("    同参数: kp=%.0f  band=%.1f°  限幅[%.0f,%.0f]  唯一变量 = 环路在哪" % (KP, BAND, HZ_LO, HZ_HI))
    res = {}
    for name, fn in (("A PC 在环", arm_pc), ("B 片内", arm_chip)):
        ts, overs = [], []
        for k in range(reps):
            prep()
            a0, pts = fn()
            rt = rise_time(pts)
            ts.append(rt if rt is not None else float("inf"))
            if pts:
                overs.append(max(abs(TGT - a) for _, a in pts) - abs(TGT - a0))
            print("   %-9s 第%d次: 起点 %7.2f°  上升时间 %s"
                  % (name, k + 1, a0 if a0 is not None else 0.0,
                     ("%.3f s" % rt) if rt is not None else ">8s(未到位)"))
            sx(1, 0); time.sleep(0.3)
        res[name] = (sorted(ts)[len(ts) // 2], max(overs) if overs else 0.0)
    print()
    ta, oa = res["A PC 在环"]
    tb, ob = res["B 片内"]
    print("   中位上升时间:  PC 在环 %.3f s   片内 %.3f s   ⇒ 差 %+.3f s" % (ta, tb, tb - ta))
    print("   最大超调:      PC 在环 %.2f°  片内 %.2f°" % (oa, ob))
    print()
    if tb < ta and abs(tb - ta) > 0.03:
        print("   ⇒ ★ 片内更快 %.0f ms（%.1f 倍）" % ((ta - tb) * 1000, ta / tb))
        # ★★★ 机制（**不是**"纯滞后 87 ms"——那是轨迹跟随的口径）：
        #   · PC 臂每周期要 **读1 + 写2 = 3 条命令** ⇒ 周期 ≈ 3 × 36 ms ≈ **110 ms**；
        #     kp=4 ⇒ 误差 4° 就给 16 Hz ⇒ 而 110 ms 内走 **~1.8 步 = 0.4°**… 但接近目标时
        #     `HZ_LO=60` 地板 ⇒ 一周期走 **60×0.11 = 6.6 步 = 1.5°** ⇒ **一步就跨过 ±0.5° 带**
        #     ⇒ **极限环振荡** ⇒ 收敛慢。
        #   · 片内每 **100 µs** 决策 ⇒ 一周期走 0.006° ⇒ **无过冲** ⇒ 平滑收敛。
        #   ⇒ 差异的主因是 **"控制周期"（极限环）**，不是"纯滞后"。
        print("      ★ 机制：PC 周期 ≈110 ms（读1+写2 三条命令）而 `HZ_LO=60` 地板下一周期就走了 1.5°")
        print("        ⇒ 跨过 ±0.5° 带 ⇒ **极限环振荡**；片内 100 µs/拍 ⇒ 一周期 0.006° ⇒ 无过冲")
    else:
        print("   ⇒ 两侧差异不显著（%.0f ms）。★ 不许当结论：先看『起点是否一致 / 是否都真到位』。" % ((tb - ta) * 1000))
    d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
