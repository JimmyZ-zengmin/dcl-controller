#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_stall_sweep.py —— 逐档"实际/请求比"扫描（**临时脚手架，不进交付物**）

## 为什么必须重做（2026-09-17 的两条教训）
① 套件 `ramp/speed` 在 31000 Hz 下 2 s 走 **38 圈**，而命令通路只有 **26 命令/s**
   ⇒ 每次采样间隔走**半圈** ⇒ 解卷绕**混叠**（它自己打印了 `⚠ 最短弧已混叠`）。
② 我自己第一版用"**首尾 raw 差 + 按预期值解卷绕**" ⇒ **循环论证**（假设它走得对，再证明它走得对）。

## 正确做法（两条硬要求）
1. **逐点采样 + 逐点最短弧累计**（相邻增量必须 < 180°）⇒ **不依赖任何"预期值"**；
2. **"不混叠"必须是可验证条件**：本脚本**实测**平均采样间隔 Δt，
   并校验 **`f × Δt < 800`**（因为 `Δangle = f/1600·Δt·360 < 180°` ⇔ `f·Δt < 800`）。
   ⇒ 不满足的档**拒绝出数**（标 `混叠`），而不是"照出数"。

## 扫描内容（回答"斜坡有没有收益"）
每档频率 f，各测一次：
  · **flat**：`sub=1 arg=f`（单次阶跃）
  · **ramp**：`sub=17 arg=2f`（斜率 2f Hz/s）+ `sub=1 arg=f`
⇒ 若某档 **flat < 1 而 ramp ≈ 1** ⇒ ★ **斜坡收益被证实**（且同时给出**失速拐点**）。

用法: python build/_stall_sweep.py
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from h723_client import Dcl, find_board       # noqa: E402

SPR = 1600.0
FREQS = [1000, 2000, 4000, 8000, 12000, 16000, 20000]
SEC = 1.0
ALIAS_LIMIT = 800.0        # f·Δt 必须 < 800（见文件头推导）


def main():
    d = Dcl(find_board())

    def st0():
        s, p = d.send(0x39, bytes([19, 0]) + struct.pack("<I", 0))
        return struct.unpack("<24I", p[:96]) if (s == "ACK" and len(p) >= 96) else None

    def sx(n, v):
        return d.send(0x39, bytes([19, n]) + struct.pack("<I", v))[0]

    def prep():
        # ★★★ 顺序很重要：**使能必须放最后**。
        #   `sub=6`（停+静止态）会按 `stop_hold` 决定静止态，而交付档 `hold=0` ⇒ **断电**。
        #   上一版把 `sx(6,0)` 放在 `sx(3,1)` 之后 ⇒ **把刚开的使能又关掉了**
        #   ⇒ 现象是"脉冲在发、编码器 raw 一动不动、比值恒 0"（同族：'部署动作删掉绑定表'）。
        sx(1, 0); sx(6, 0)                 # 先停干净 + 进静止态
        sx(17, 0)                          # 斜坡关
        sx(13, 0); sx(5, 1)                # 运动源=脚手架；声明极性
        sx(2, 0)                           # 方向
        sx(3, 1)                           # ★ 使能**放最后**
        time.sleep(0.35)

    def sample(sec):
        """逐点采样，返回 [(t, raw)]。"""
        pts, t0 = [], time.time()
        while time.time() - t0 < sec:
            u = st0()
            if u is not None:
                pts.append((time.time() - t0, u[8]))
        return pts

    def unwrap(pts):
        """**逐点**最短弧累计 ⇒ 不依赖预期值。返回 (累计度, 采样间隔秒, 最大单步增量度)"""
        if len(pts) < 4:
            return None
        tot, prev, mx = 0.0, pts[0][1], 0.0
        for _, raw in pts[1:]:
            dv = (raw - prev) & 0xFFF
            if dv > 2048:
                dv -= 4096
            if abs(dv) > mx:
                mx = abs(dv)
            tot += dv
            prev = raw
        dt = (pts[-1][0] - pts[0][0]) / max(len(pts) - 1, 1)
        return abs(tot) * 360.0 / 4096.0, dt, mx * 360.0 / 4096.0

    def ramp_actual():
        s, p = d.send(0x39, bytes([19, 19]))
        return struct.unpack("<8I", p[:32])[3] if (s == "ACK" and len(p) == 32) else None

    def run(f, mode):
        prep()
        if mode == "ramp":
            sx(17, int(f * 2))
        sx(1, f)
        # ★★ 必须等**实际频率到位**再采样 —— 否则斜坡段的前半程在爬坡（平均半频），
        #    总步数被"吃掉" ⇒ 会得出"斜坡更差"的假结论（R5 就踩过这个坑）。
        t0 = time.time()
        while time.time() - t0 < 4.0:
            a = ramp_actual()
            if a is not None and a >= f:
                break
            time.sleep(0.05)
        time.sleep(0.15)
        pts = sample(SEC)
        sx(1, 0); sx(17, 0)
        time.sleep(0.3)
        r = unwrap(pts)
        if r is None:
            return None
        deg, dt, mx = r
        steps = deg / (360.0 / SPR)
        exp = f * (pts[-1][0] - pts[0][0]) / SPR
        return dict(ratio=(steps / exp if exp else 0.0), dt=dt, mx=mx,
                    alias=(f * dt >= ALIAS_LIMIT), n=len(pts))

    print("=== 逐档 实际/请求比（逐点最短弧累计；不混叠校验 f·Δt < %.0f）===" % ALIAS_LIMIT)
    print("   频率Hz |  方式 | 比值  | 采样间隔ms | 最大单步° | 不混叠 | 点数")
    verdict = None
    for f in FREQS:
        row = {}
        for mode in ("flat", "ramp"):
            r = run(f, mode)
            row[mode] = r
            if r is None:
                print("  %6d | %-4s |   --  |     --     |    --     |   ?    |  --" % (f, mode))
                continue
            print("  %6d | %-4s | %.3f |   %6.1f   |   %6.1f  |  %s  | %3d"
                  % (f, mode, r["ratio"], r["dt"] * 1000, r["mx"],
                     "✗混叠" if r["alias"] else "✓", r["n"]))
        a, b = row.get("flat"), row.get("ramp")
        if a and b and not a["alias"] and not b["alias"]:
            if a["ratio"] < 0.9 and b["ratio"] >= 0.97 and verdict is None:
                verdict = f
    print()
    if verdict:
        print("★★★ 找到失速拐点：**%d Hz** —— 突加掉步而斜坡跟得上 ⇒ **斜坡收益被证实**" % verdict)
    else:
        print("⇒ 在 ≤20 kHz 且不混叠的范围内，**没有测到\"突加掉步而斜坡不掉\"的档**")
        print("   ⇒ 要么失速拐点在更高频（那超出 26 命令/s 的采样能力，需黑匣子），")
        print("     要么斜坡在这台电机上**没有收益**。★ 两种都不许当结论猜，只能标'未测到'。")
    d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
