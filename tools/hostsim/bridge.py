#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hostsim/bridge.py —— 运动可视化上位机的"桥接层"

把**板子的真实运动**变成浏览器能画的数据。

## 为什么不用现成的运动控制上位机
查过：现有的（Sophon / SmartMotion / 各种 Qt·WinForm 步进上位机）全是
**"发指令 + 画曲线"**型，**没有一个做机构可视化**；而且协议是 Modbus-RTU 之类，
**接不到本项目的 DCL 协议上** ⇒ 桥接层无论如何都得自己写。

## 架构（零第三方依赖）
    [板子 COMxx]  --pyserial-->  bridge.py（内置 http.server）
                                      ├── GET /        → index.html
                                      └── GET /state   → JSON 状态
    [浏览器]  fetch('/state') 每 40 ms → three.js 更新 电机 / 丝杆 / 滑台

## 两种数据源
    --live COM21        实时读板子（`0x22` 读 SENSOR[0]/[1] + WIRE[12]）
    --csv  tri.csv      回放 SD 日志（吃 `tools/sd_log_read.py --csv` 的产物）

## 解算（这就是"电机 → 丝杆 → 滑台"的物理绑定）
    raw = SENSOR[0]            AS5600 12 位绝对角，0..4095
    Δ   解绕（跨 0 边界补 ±4096）→ 累计 counts
    revs = counts / 4096        每圈 4096 counts
    pos  = revs × pitch         pitch = 丝杆导程 mm/rev（默认 8.0，T8 常见）

## ★★★ 速度的两个来源（2026-09-22 定案）
    vel_mm_s = Δcounts / (Δtick × 拍长) × pitch    ← **设备口径**（免疫 PC 侧拍频）
                 Δtick = SHM+0x08 HEARTBEAT 的差（每拍 +1，实测 10 001 Hz vs 期望 10 000）
                 ★ 改前用的是 PC 墙钟 `dt` —— 那是噪声，显示的"速度抖动"大半是采集口径
    ap_mm_s  = WIRE[64] / 1574.4 × pitch          ← **板子每拍算的"已应用频率"**，最权威
                 ★ 它与 WIRE[12]（请求值）**配对看**："设了就算"必错（MEMORY.md §〇 第 6 条）
    ⇒ 画"实际速度"用 `ap_mm_s`；`vel_mm_s` 留作交叉验证。

★ 串口是**独占资源** —— bridge 跑着的时候别的工具用不了那个口。

用法：
    python tools/hostsim/bridge.py --live COM21 --pitch 8
    python tools/hostsim/bridge.py --csv tri.csv --pitch 8
    # 浏览器开 http://127.0.0.1:8765
"""
import argparse
import csv
import json
import math
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))

COUNTS_PER_REV = 4096.0      # AS5600 12 位
STEPS_PER_REV = 1574.4       # 本项目实测（步/圈）
OFF_SENSOR, OFF_WIRE = 0x0040, 0x0240
OFF_CTRL = 0x0000            # SHM_CTRL: +0x08 = HEARTBEAT(每拍+1) ← ★ 唯一权威时间源
TICK_US = 100.0              # 交付档拍长（改拍长要同步这里; meta 里有自检会炸）
# ★★ 这三个偏移是**硬编码**的 —— 它们**不在 0x64 自报目录里**（已知欠账, 见 MEMORY.md §26.6）。
#   正确修法 = 给 0x64 加 SENSOR_MAP / WIRE_MAP 三行（固件改动, 要同步改 EXPECT_MD5）。
#   在补上之前靠 `sanity()` 兜底: 一旦读错区立刻报错, 而不是把别的数当位置画出来。
W_REQ_RATE = 12          # WIRE[12] = ③层程序写的**请求**频率 Hz（仅"程序面"模式下有意义）
# ★★ 不要用 `WIRE[64]`（STEP_MOT_SLOT_RATE_AP）当"已应用频率"读：
#   它由 `step_service_motion()` 写，而那函数在**脚手架模式下第一行就 return**（step.c:684）
#   ⇒ 那个槽在此模式下**从不刷新**，读到的是残留值（实测在 251~3762 之间乱跳，
#     看着像"频率失控"，其实是读了个没人维护的槽）。
#   权威来源 = `0x39 op=19 sub=14` 的 +16（同一个 `g_step_rate_hz`，且与运动源无关）。

STATE = {
    "ok": False, "src": "-", "msg": "未启动",
    "t": 0.0, "raw": 0.0, "deg": 0.0, "revs": 0.0, "pos_mm": 0.0,
    "vel_mm_s": 0.0, "cmd_hz": 0.0, "cmd_mm_s": 0.0,
    "ap_hz": 0.0, "ap_mm_s": 0.0,
    "mosrc": "-", "cmd_n": 0, "applied_n": 0, "rej_n": 0, "lim_rem": 0,
    "req_hz": 0.0, "src_mode": "-", "lim_ms": 0, "mismatch": False,
    "tick": 0, "d_tick": 0, "dt_s": 0.0, "rate": 0.0,
    "run": 0, "ov": 0, "pitch": 8.0, "n": 0, "src_rule": "-",
}
LOCK = threading.Lock()
CMDQ = []            # 浏览器下发的控制命令，由 live 线程（持有串口的那个）排空
# ★ 为什么走队列：串口是独占资源，控制命令**必须由同一个句柄**发出 ——
#   另开一个进程去开同一个口 = 本项目已踩过的"外层没关就去开第二个句柄"。


class Unwrap:
    """把 0..4095 的绝对角解开成连续累计 counts（跨 0 补 ±4096）。

    ★ 判据：单次 Δ 若 > 半圈，只能是被绕回来了（真运动不可能一步过半圈）。"""

    def __init__(self):
        self.prev = None
        self.total = 0.0

    def feed(self, raw):
        if self.prev is None:
            self.prev = raw
            return 0.0
        d = raw - self.prev
        if d > COUNTS_PER_REV / 2:
            d -= COUNTS_PER_REV
        elif d < -COUNTS_PER_REV / 2:
            d += COUNTS_PER_REV
        self.prev = raw
        self.total += d
        return d

    def reset(self):
        self.prev = None
        self.total = 0.0


# ══════════════════ 实时源 ══════════════════
def run_live(port, pitch, hz):
    """实时源。★ 三条口径纪律（2026-09-22 修）：

    ① **时间戳来自板子**：`SHM+0x08 HEARTBEAT` 每拍 +1（实测 10 001 Hz vs 期望 10 000）。
       速度用 `Δcounts / (Δtick × 拍长)` —— 分子分母**同为设备口径** ⇒ 免疫 PC 侧拍频。
       改前用的是 PC 墙钟 `dt`：USB 驱动缓冲 + Windows 调度会让数据**成簇到达**，
       于是显示出的"速度抖动"**绝大部分是采集口径的噪声，不是电机的**（MEMORY.md §5.27）。
    ② **速度有两个来源，都要给**：`ap_mm_s`（板子每拍算的**已应用**频率换算，最权威）
       与 `vel_mm_s`（位置域差分，用于交叉验证）。前端画"实际速度"应当用前者。
    ③ **每轮开头做一次量纲自检**：`MAGIC` + `raw` 是 [0,4096) 整数 + `deg` 与 raw 自洽。
       不过 ⇒ 直接抛错停住。理由是这三个偏移**硬编码**（不在 0x64 目录里），
       一旦读错区，把别的浮点当位置画出来**比报错坏得多**。"""
    from h723_client import Dcl
    d = Dcl(port)
    uw = Unwrap()
    with LOCK:
        STATE.update(src="live", msg="已连 %s" % d.port, pitch=pitch, ok=True)
    shm = None
    prev_cnt = 0.0
    prev_tick = None
    prev_wall = time.time()
    runv = ovv = 0
    tickv = 0
    n = 0
    err = 0
    sanity_done = False
    while True:
        try:
            # ── 先排空浏览器下发的控制命令（必须在同一个串口句柄上发）──
            while CMDQ:
                kind, a1, a2 = CMDQ.pop(0)
                if kind == "pin":
                    d.send(0x39, struct.pack("<BBI", 19, int(a1), int(a2)), expect_len=None)
                elif kind == "wire" and shm:
                    bits = struct.unpack("<I", struct.pack("<f", float(a2)))[0]
                    d.send(0x21, struct.pack("<II", shm + OFF_WIRE + int(a1) * 4, bits), expect_len=None)

            if shm is None or (n % 40 == 0):          # 0x38 慢组（run/ov 变得慢）
                s, p = d.send(0x38, expect_len=51)
                if s == "ACK" and len(p) >= 51:
                    shm = struct.unpack("<I", p[23:27])[0]
                    runv, ovv = p[22], struct.unpack("<I", p[27:31])[0]
                elif shm is None:
                    raise RuntimeError("0x38 no ACK")

            # ① SHM_CTRL(0x0000, 4 字) → [0]MAGIC [2]HEARTBEAT(每拍+1) ★ 唯一权威时间源
            sc, pc_ = d.send(0x22, struct.pack("<IH", shm + OFF_CTRL, 4), expect_len=None)
            if sc != "ACK" or len(pc_) < 16:
                raise RuntimeError("0x22 SHM_CTRL no ACK")
            wc = struct.unpack("<4I", pc_[:16])
            if wc[0] != 0x44434C31:
                raise RuntimeError("SHM MAGIC=0x%08X ≠ 0x44434C31 ⇒ 读错区" % wc[0])
            tickv = wc[2]

            # ② SENSOR[0..1] = raw / deg
            s, p = d.send(0x22, struct.pack("<IH", shm + OFF_SENSOR, 2), expect_len=None)
            if s != "ACK" or len(p) < 8:
                raise RuntimeError("0x22 SENSOR no ACK")
            raw, deg = struct.unpack("<ff", p[:8])
            if not sanity_done:                      # ★ 量纲自检：只做一次，读错区就响亮地停
                if not (0.0 <= raw < 4096.0) or raw != float(int(raw)):
                    raise RuntimeError("SENSOR0=%.3f 不是 [0,4096) 的整数 ⇒ "
                                       "SENSOR_MAP(0x%X) 不对" % (raw, OFF_SENSOR))
                if abs(deg - raw * 360.0 / COUNTS_PER_REV) > 0.01:
                    raise RuntimeError("deg=%.3f 与 raw=%.1f 不自洽 ⇒ 量纲不对" % (deg, raw))
                sanity_done = True

            # ③ WIRE[12] = ③层程序的**请求**频率
            #    ★ 只在"程序面"模式下有意义；脚手架模式下它是残留值（别拿它做判据）
            s2, p2 = d.send(0x22, struct.pack("<IH", shm + OFF_WIRE + W_REQ_RATE * 4, 1), expect_len=None)
            cmd = struct.unpack("<f", p2[:4])[0] if (s2 == "ACK" and len(p2) >= 4) else 0.0

            # ④ ★★ op=19 sub=14 = **运动能力面只读**（零副作用）—— "已应用频率"的**权威来源**。
            #    为什么不用 WIRE[64]：那个镜像由 `step_service_motion()` 写，而该函数第一行就是
            #      `if (g_motion_src != STEP_MOT_SRC_PROGRAM) return;`（step.c:684）
            #    ⇒ **脚手架模式下 WIRE[64] 从不刷新**，读到的是那个槽里的残留值。
            #    2026-09-22 实测：脚手架跑 1500 Hz 时 WIRE[64] 在 251~3762 之间乱跳，
            #    看起来像"频率失控"，其实是读了一个没人维护的槽。sub=14 的 +16 是
            #    同一个 `g_step_rate_hz`，但**与运动源无关**，还给 src/cmd_n/applied_n/rej_n。
            #    应答 32B: +0 src(0=脚手架 1=程序面) +4 cmd_n +8 applied_n +12 rej_n
            #              +16 已应用频率 Hz  +20 限时余量
            s4, p4 = d.send(0x39, struct.pack("<BBI", 19, 14, 0), expect_len=32)
            if s4 == "ACK" and len(p4) >= 24:
                mosrc, cmd_n, ap_n, rej_n, ap_u, lim_rem = struct.unpack("<6I", p4[:24])
                ap = float(ap_u)
            else:
                mosrc = cmd_n = ap_n = rej_n = 0; ap = 0.0; lim_rem = 0

            t = time.time()
            uw.feed(raw)
            revs = uw.total / COUNTS_PER_REV
            pos = revs * pitch
            d_cnt = uw.total - prev_cnt
            prev_cnt = uw.total

            # ★★ 速度 = 设备口径差分（改前是 PC 墙钟，那是噪声）
            d_tick = ((tickv - prev_tick) & 0xFFFFFFFF) if prev_tick is not None else 0
            prev_tick = tickv
            dt_s = d_tick * TICK_US * 1e-6                 # 板子口径的间隔（秒）
            vel = (d_cnt / COUNTS_PER_REV * pitch / dt_s) if dt_s > 1e-9 else 0.0
            pc_hz = 1.0 / max(1e-6, t - prev_wall)
            prev_wall = t

            n += 1
            # ★★ "请求 vs 已应用"配对 —— §〇 第 6 条"设了就算"必错。
            #   下发了 A Hz、WIRE[64] 却是 0 ⇒ 命令**没到执行面**（源选错 / 未使能 / 被限时停）。
            #   把它算成一个能失败的布尔量送前端标红，而不是让用户盯着两个数自己对。
            req = STATE.get("req_hz", 0.0)
            mis = bool(req > 1.0 and abs(ap - req) > max(1.0, req * 0.05))
            with LOCK:
                STATE.update(t=t, raw=raw, deg=deg, revs=revs, pos_mm=pos,
                             vel_mm_s=vel, cmd_hz=cmd, cmd_mm_s=cmd / STEPS_PER_REV * pitch,
                             ap_hz=ap, ap_mm_s=ap / STEPS_PER_REV * pitch,
                             mosrc=("program" if mosrc == 1 else "scaffold"),
                             cmd_n=cmd_n, applied_n=ap_n, rej_n=rej_n, lim_rem=lim_rem,
                             mismatch=mis,
                             tick=tickv, d_tick=d_tick, dt_s=dt_s,
                             run=runv, ov=ovv, n=n, ok=True, msg="实时",
                             src_rule="device_tick", rate=pc_hz)
            time.sleep(max(0.0, 1.0 / hz))
        except Exception as e:
            err += 1
            with LOCK:
                STATE.update(ok=False, msg="%s: %s" % (type(e).__name__, e))
            if err > 8:
                try:
                    d.close()
                except Exception:
                    pass
                time.sleep(0.5)
                try:
                    d = Dcl(port); shm = None; err = 0; sanity_done = False; prev_tick = None
                except Exception:
                    time.sleep(1.0)
            time.sleep(0.1)


# ══════════════════ CSV 回放 ══════════════════
def find_col(hdr, *names):
    for i, h in enumerate(hdr):
        hs = h.strip().lower().replace(" ", "")
        for nm in names:
            if nm in hs:
                return i
    return None


def run_csv(path, pitch, speed):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.reader(f)
        hdr = next(rd, [])
        rows = [r for r in rd if len(r) >= 6]
    i_tick = find_col(hdr, "tick") or 0
    i_raw = find_col(hdr, "sensor[0]", "raw")
    i_deg = find_col(hdr, "sensor[1]", "deg")
    i_cmd = find_col(hdr, "wire[12]")
    with LOCK:
        STATE.update(src="csv", msg="%s (%d 行)" % (os.path.basename(path), len(rows)),
                     pitch=pitch, ok=True)
    if i_raw is None:
        with LOCK:
            STATE.update(ok=False, msg="CSV 里找不到 SENSOR[0] 列；表头=%s" % hdr[:8])
        return
    uw = Unwrap()
    n = 0
    t0 = time.time()
    tick0 = float(rows[0][i_tick])
    while True:
        for r in rows:
            try:
                tick = float(r[i_tick])
                raw = float(r[i_raw])
                deg = float(r[i_deg]) if i_deg is not None else raw * 360.0 / COUNTS_PER_REV
                cmd = float(r[i_cmd]) if i_cmd is not None else 0.0
            except Exception:
                continue
            t_sim = (tick - tick0) / 10000.0            # 100 µs/拍
            uw.feed(raw)
            revs = uw.total / COUNTS_PER_REV
            n += 1
            prev = STATE.get("_pc", uw.total)
            d_cnt = uw.total - prev
            # ★ CSV 里的 tick 列**本来就是设备 tick** ⇒ 回放也能用设备口径算速度（与 live 一致）
            d_tick = int(tick - STATE.get("_ptick", tick))
            dt_s = d_tick * TICK_US * 1e-6
            with LOCK:
                STATE.update(t=t_sim, raw=raw, deg=deg, revs=revs, pos_mm=revs * pitch,
                             vel_mm_s=(d_cnt / COUNTS_PER_REV * pitch / dt_s) if dt_s > 1e-9 else 0.0,
                             cmd_hz=cmd, cmd_mm_s=cmd / STEPS_PER_REV * pitch,
                             ap_hz=0.0, ap_mm_s=0.0,
                             tick=int(tick), d_tick=d_tick, dt_s=dt_s,
                             rate=1.0 / dt_s if dt_s > 1e-9 else 0.0,
                             src_rule="device_tick(csv)",
                             n=n, ok=True, _pc=uw.total, _ptick=tick)
            # 按 speed 倍率对着墙钟放
            target = t0 + t_sim / max(0.01, speed)
            dtw = target - time.time()
            if dtw > 0:
                time.sleep(min(dtw, 0.2))
        t0 = time.time(); tick0 = float(rows[0][i_tick])


# ══════════════════ HTTP ══════════════════
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _ctl(self, path):
        q = {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}
        if path.startswith("/pin"):
            CMDQ.append(("pin", q.get("sub", 0), q.get("arg", 0)))
            return self._json({"queued": "pin", **q})
        if path.startswith("/wire"):
            CMDQ.append(("wire", q.get("i", 0), q.get("v", 0)))
            return self._json({"queued": "wire", **q})
        if path.startswith("/motion"):
            on = q.get("on", "1") not in ("0", "false")
            A = float(q.get("A", 3000)); slope = float(q.get("slope", 30000))
            # ★★ `src` 决定"运动源"（op=19 sub=13）。**必须显式选，不能默认程序面**：
            #   sub=13 arg=1（程序面）要求板上**已加载 ③层程序**去读 wire[10]/[11] 并写 wire[12]/[14]。
            #   裸板（空卡 n_routes=128）上那条路是**静默失效**的：ACK 了、wire 也写了、
            #   但没人消费 ⇒ 脉冲不动、`WIRE[64]` 恒 0。2026-09-22 实测撞到：
            #   `/motion?on=1` 之后 ap_hz=0.0、pos 一动不动，而应答是 "queued"（看着像成功）。
            #   ⇒ 这就是 §〇 第 6 条「"设了就算"必错」的现场。默认改走**脚手架直控**，
            #     它的生效条件不依赖任何外部状态。
            src = q.get("src", "scaffold")
            lim = int(q.get("lim", 30000))     # ★ 默认 30 s 限时（安全网：忘了点停也会自己停）
            if on:
                if src == "program":
                    # 顺序照 README §4：enapol → 程序面 → 斜坡 → 峰值 → 使能
                    CMDQ.append(("pin", 5, 1))
                    CMDQ.append(("pin", 13, 1))
                    CMDQ.append(("pin", 17, int(slope)))
                    CMDQ.append(("wire", 11, A))
                    CMDQ.append(("wire", 10, 1.0))
                else:
                    # 脚手架直控：声明极性 → 使能 → 限时 → 方向 → 选源 → 频率
                    CMDQ.append(("pin", 5, 1))
                    CMDQ.append(("pin", 3, 1))
                    CMDQ.append(("pin", 4, lim))
                    CMDQ.append(("pin", 2, int(q.get("dir", 0))))
                    CMDQ.append(("pin", 13, 0))
                    CMDQ.append(("pin", 1, int(A)))
            else:
                CMDQ.append(("pin", 1, 0))     # 频率 0 = 停脉冲
                CMDQ.append(("pin", 6, 0))     # 安全态（按 stop_hold 决定静止态）
            # 记下**我们自己下发的请求**，供 run_live 做"请求 vs 已应用"配对（§〇 第 6 条）
            with LOCK:
                STATE.update(req_hz=(A if on else 0.0), src_mode=src, lim_ms=(lim if on else 0))
            return self._json({"queued": "motion", "on": on, "A": A, "slope": slope,
                               "src": src, "lim_ms": (lim if on else 0)})
        self.send_error(404)

    def do_GET(self):
        p = urlparse(self.path).path
        if p.startswith("/pin") or p.startswith("/wire") or p.startswith("/motion"):
            return self._ctl(self.path)
        if self.path.startswith("/state"):
            with LOCK:
                body = json.dumps({k: v for k, v in STATE.items() if not k.startswith("_")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        p = os.path.join(HERE, "index.html")
        if not os.path.isfile(p):
            self.send_error(404, "index.html missing"); return
        with open(p, "rb") as f:
            b = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", metavar="COM")
    ap.add_argument("--csv", metavar="FILE")
    ap.add_argument("--pitch", type=float, default=8.0, help="丝杆导程 mm/rev（T8=8, T5=2）")
    ap.add_argument("--hz", type=float, default=25.0, help="实时轮询率")
    ap.add_argument("--speed", type=float, default=1.0, help="CSV 回放倍率")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if not a.live and not a.csv:
        a.csv = None
        print("★ 必须给 --live COMxx 或 --csv <文件>")
        return 2
    def _wrap(fn, fargs):
        """★ 采集线程必须**把死因写进 STATE**。

        否则线程死了、HTTP 还在服务，前端一直看到初始值 `未启动` —— 看起来像
        "还没连上"，实际是"已经崩了"，而且没有任何线索。2026-09-22 实测撞到：
        端口被另一个 bridge 实例占着 ⇒ 线程在 `Dcl(port)` 就抛了，
        而 /state 返回 `ok=false, msg=未启动` —— 我因此白查了一轮。
        ⇒ 与 §〇 第 8 条同族：**失败必须响亮**。"""
        try:
            fn(*fargs)
        except Exception as e:
            import traceback
            with LOCK:
                STATE.update(ok=False, src="dead",
                             msg="采集线程已退出: %s: %s" % (type(e).__name__, e))
            traceback.print_exc()

    th = threading.Thread(target=_wrap,
                          args=(run_csv, (a.csv, a.pitch, a.speed)) if a.csv
                          else (run_live, (a.live, a.pitch, a.hz)),
                          daemon=True)
    th.start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print("=" * 66)
    print("  运动可视化上位机  —  http://127.0.0.1:%d" % a.port)
    print("  数据源 : %s   丝杆导程 : %.2f mm/rev" % (a.csv or a.live, a.pitch))
    print("  Ctrl-C 退出")
    print("=" * 66)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
