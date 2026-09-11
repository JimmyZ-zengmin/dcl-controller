#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_tick_la.py — 用 Saleae Logic2 **外部**测 PA8 拍信号: 周期 / 抖动 / 占空比

为什么需要它 (h723_jitter.py 的结论需要它来收口)
----------------------------------------------
固件侧的"拍周期"量的是 `p = DWT_CYCCNT(本次 ISR 入口) - DWT_CYCCNT(上次入口)`
  ⇒ 硬件拍长 + **两次中断入口延迟之差**。
实测: 39988~40012 cyc(极差 18~24 cyc), 且**不随扫描负载变化**(emax 407→7287, 18×,
极差基本平) ⇒ "拍长与负载解耦"成立, 但绝对 claim "极差 0 cyc" 复现不出来。
已排除两个嫌疑 (实测): ① USART1 不会延时 TIM2 (TIM2 优先级 0x00 最高, USART1 0x80 最低);
② 栈在 DTCM (`_estack = DTCM 末端`), 异常入栈无总线竞争。
⇒ 剩余嫌疑在**测量通道自身** ⇒ 只有 LA 能证伪: LA 直接测**引脚边沿**, 绕开中断入口。

★ 关键手法 (来自 skill `saleae-la-verify`):
  `export_raw_data_binary` 导出的**不是逐采样字节**, 而是 `<SALEAE>` **跳变时间戳列表**
  (magic 8B, ntr=u64@36, ts=double[]@44, 长度恒 44+8*ntr)。
  这些时间戳**精度高于采样网格** ⇒ 适合测周期/抖动, 比"整窗计数"准得多。

★★ 本工具**全程不调用 pyocd**(除 --mode idle 作悬空对照), 原因见 skill:
  `la_tick_freq.py` 第一步是 `pyocd -c reset` ⇒ 会清掉一切运行期配置,
  抓到的永远是复位后的默认态。本工具用串口部署负载, 不干扰被测目标。

用法:
    python tools/h723_tick_la.py --mode measure --setup mixed --port COM14
    python tools/h723_tick_la.py --mode idle            # 悬空对照: 核心被 held ⇒ 应 ~0 跳变
"""

import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse
import glob
import json
import os
import struct
import subprocess
import sys
import time
import urllib.request

try:
    import serial
except ImportError:
    serial = None

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from h723_modbus import Link, find_port, open_serial  # noqa: E402

MCP = "http://127.0.0.1:10530/"
OUTDIR = os.path.join(os.path.dirname(_HERE), ".la", "raw")
CPU_HZ = 400_000_000          # clock.h 的编译期假定值 (用于把秒换算成 cyc)

CMD_DEPLOY, CMD_START, CMD_STOP, CMD_RESET = 0x10, 0x11, 0x12, 0x13
SRC_CONST, DST_WIRE, OP_DIRECT = 2, 2, 0x00
ROUTE_FLAG_ACTIVE = 0x01
DIV_FAST, DIV_MID, DIV_SLOW = 0, 1, 2


def rpc(method, params=None, timeout=120):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(MCP, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def call(tool, args, timeout=180):
    r = rpc("tools/call", {"name": tool, "arguments": args}, timeout)
    if "error" in r:
        return None, "ERR:%s" % r["error"]
    res = r.get("result", {})
    txt = "".join(c.get("text", "") for c in res.get("content", []))
    return txt, ("ERR" if res.get("isError") else "OK")


# ---------------- 被测负载 (与 h723_jitter.py 同款, 保证两边可比) ----------------
def route(src_i, wire, div=DIV_FAST, phase=0):
    per = (div & 0x03) | ((phase & 0x3F) << 2)
    return struct.pack('<BBBBBBHHHHB', SRC_CONST, src_i & 0xFF, DST_WIRE, wire & 0xFF,
                       OP_DIRECT, ROUTE_FLAG_ACTIVE, src_i & 0xFFFF, 0, 0, 0, per) + b'\x00'


def payload(routes, nparams):
    return (struct.pack('<HHH', len(routes), nparams, 0) + b''.join(routes)
            + b''.join(struct.pack('<ffff', 1.0 + i * 0.001, 0, 0, 0) for i in range(nparams)))


def prog_div0():
    return payload([route(i, i % 128) for i in range(128)], 128)


def prog_mixed():
    rs, q1, q2 = [], 0, 0
    for i in range(128):
        if i % 3 == 0:
            rs.append(route(i, i % 128, DIV_FAST))
        elif i % 3 == 1:
            rs.append(route(i, i % 128, DIV_MID, q1 % 10)); q1 += 1
        else:
            rs.append(route(i, i % 128, DIV_SLOW, q2 % 64)); q2 += 1
    return payload(rs, 128)


def setup_workload(L, name):
    """串口部署负载 (不碰调试器)"""
    L.xact(CMD_RESET); time.sleep(0.15)
    if name == "skeleton":
        L.xact(CMD_STOP); return "骨架 (RESET 后不 START)"
    if name == "empty":
        L.xact(CMD_START); return "空程序 RUN"
    if name == "div0":
        s, p = L.xact(CMD_DEPLOY, prog_div0(), timeout=2.0)
        if s != 0:
            return None
        L.xact(CMD_START); time.sleep(0.2); return "div0 满表 128 条 RUN"
    if name == "mixed":
        s, p = L.xact(CMD_DEPLOY, prog_mixed(), timeout=2.0)
        if s != 0:
            return None
        L.xact(CMD_START); time.sleep(0.2); return "分档满表 128 条 RUN"
    return None


# ---------------- 采集与解析 ----------------
def capture(ch, rate, dur):
    txt, st = call("get_devices", {})
    try:
        dev = json.loads(txt)["devices"][0]
    except Exception:
        print("!! 拿不到 LA 设备 (devices=%s)" % (txt or "").strip()[:120])
        print("   检查: Logic 2 是否在跑 + 分析仪 USB 是否插上 (VID_21A9)")
        return None
    dev_id = dev["deviceId"]
    print("[LA] 设备 %s (%s)" % (dev_id, dev.get("deviceType")))
    txt, st = call("start_capture", {
        "deviceId": dev_id,
        "logicDeviceConfiguration": {
            "logicChannels": {"digitalChannels": [ch]},
            "digitalSampleRate": rate,
        },
        "captureConfiguration": {"timedCaptureMode": {"durationSeconds": dur}},
    })
    try:
        cap = int(json.loads(txt).get("captureId"))
    except Exception:
        print("!! 没有 captureId: %s" % (txt or "")[:160]); return None
    print("[LA] cap=%d  CH%d @ %.1f MS/s  %.3f s" % (cap, ch, rate / 1e6, dur))
    call("wait_capture", {"captureId": cap}, timeout=240)
    time.sleep(0.4)

    for f in glob.glob(os.path.join(OUTDIR, "*")):
        try:
            os.remove(f)
        except Exception:
            pass
    call("export_raw_data_binary", {
        "captureId": cap,
        "directory": OUTDIR,
        "logicChannels": {"digitalChannels": [ch]},
        "analogDownsampleRatio": 1,
    }, timeout=300)
    files = sorted(glob.glob(os.path.join(OUTDIR, "*")), key=os.path.getmtime, reverse=True)
    if not files:
        print("!! 没有导出文件"); return None
    return files[0]


def parse(path):
    raw = open(path, "rb").read()
    if raw[:8] != b"<SALEAE>":
        print("!! 不是 <SALEAE> 跳变列表 (前 8 字节 = %r)" % raw[:8]); return None
    ntr = struct.unpack_from("<Q", raw, 36)[0]
    if len(raw) != 44 + 8 * ntr:
        print("!! 长度不符: %d vs 44+8*%d=%d" % (len(raw), ntr, 44 + 8 * ntr)); return None
    ts = list(struct.unpack_from("<%dd" % ntr, raw, 44)) if ntr else []
    return ts


def analyse(ts, ch, rate):
    n = len(ts)
    print("\n── 跳变分析 (CH%d) ──" % ch)
    print("  跳变数 N = %d   采样率 %.1f MS/s (采样网格 %.1f ns)"
          % (n, rate / 1e6, 1e9 / rate))
    if n < 200:
        print("  !! N < 200 —— 不判读 (saleae-la-verify skill: 跳变太少只是启动残余,")
        print("     本项目曾据此假阳性过)。真信号应为 0.5s≈5000 个跳变。")
        return None
    span = ts[-1] - ts[0]
    # 同向边沿 (隔一个) = 完整周期; 与起始电平无关 ⇒ 鲁棒
    periods = [ts[i + 2] - ts[i] for i in range(n - 2)]
    highs = [ts[i + 1] - ts[i] for i in range(0, n - 1, 2)]
    f_fine = (n - 1) / (2.0 * span)          # 每个完整周期含 2 个边沿
    pm, pmax, pmin = sorted(periods)[len(periods) // 2], max(periods), min(periods)
    # ★ 离群必须先摘出来再谈极差: PA8 线上有已知的窄毛刺 (pinout 文档记过
    #   0.01~0.12%), 它会把"极差"撑到几万 ns —— 那是**毛刺**不是抖动。
    #   阈值取 5%: 真实抖摆已验证 < 0.01%, 而毛刺最少也有 ~2% 偏差 ⇒ 分得开。
    #   (第一版用了 25% 阈值 ⇒ 漏掉了 ~23% 偏差的毛刺, "稳健极差"仍被污染。)
    outl = [p for p in periods if abs(p - pm) > 0.05 * pm]
    inl = [p for p in periods if abs(p - pm) <= 0.05 * pm]
    rmin, rmax = (min(inl), max(inl)) if inl else (pmin, pmax)
    spread_ns = (rmax - rmin) * 1e9
    mean_s = sum(periods) / len(periods)
    duty = (sum(highs) / len(highs)) / (sum(periods) / len(periods)) if highs else None

    print("  周期: 均值 %.6f µs  稳健 min %.6f  max %.6f" % (mean_s * 1e6, rmin * 1e6, rmax * 1e6))
    print("  ★ 稳健极差 = %.1f ns = %.1f cyc @%dMHz   (1 网格 = %.1f ns)"
          % (spread_ns, spread_ns * 1e-9 * CPU_HZ, CPU_HZ // 10**6, 1e9 / rate))
    print("  离群(毛刺) = %d / %d 个 (%.3f%%)  最极端 %s ns"
          % (len(outl), len(periods), 100.0 * len(outl) / len(periods),
             "%.1f" % (min(outl) * 1e9) if outl else "-"))
    print("  频率(精测 (N-1)/(2·span)) = %.4f Hz  (期望 5000.0000)" % f_fine)
    if duty:
        print("  占空比 = %.2f%%  (期望 50%%)" % (duty * 100))
    # 离散值直方图 —— 区分"真实抖动"与"采样量化"
    from collections import Counter
    cnt = Counter(round(p * 1e9, 1) for p in periods)
    top = cnt.most_common(8)
    print("  周期取值分布 (前 8, 单位 ns): %s" % ", ".join("%.1f×%d" % (k, v) for k, v in top))
    print("  互异周期值个数 = %d / %d" % (len(cnt), len(periods)))

    # ★★ 亚网格精度: 用**累积相位**打破采样网格的限制。
    #   单个周期只能分辨到 1 个网格 (= 1/rate), 但第 M 个边沿相对第 0 个的
    #   累积时间, 精度 = 1/M 个网格 ⇒ 均值周期的精度随 M 线性提高。
    #   做法: 对上升沿序列做最小二乘拟合 s_i ≈ a + b·i, b 即均值周期。
    rise = ts[1::2] if len(ts) > 3 else ts[1::2]
    M = len(rise)
    extra = ""

    def lsq(rr):
        m = len(rr)
        mi = (m - 1) / 2.0
        ms = sum(rr) / m
        sxx = sum((i - mi) ** 2 for i in range(m))
        sxy = sum((i - mi) * (rr[i] - ms) for i in range(m))
        if sxx <= 0:
            return None
        b = sxy / sxx
        a = ms - b * mi
        return a, b

    if M >= 50:
        fit = lsq(rise)
        b0 = fit[1] if fit else None
        # ★★ 鲁棒化: 第一遍拟合会被**毛刺**带偏 (实测一次: 2 个 111µs/188µs 的异常
        #   间隔把 σ 从 0.29 网格抬到 5.56 网格 ⇒ 假结论"有真实抖动")。
        #   ⇒ 按第一遍的残差剔除 >5 网格的点, 用**剩余点**重拟合。
        #   剔除量本身单独报出来 (那是毛刺计数, 不是抖动证据)。
        keep = rise
        if b0:
            a0, b0v = fit
            resid0 = [rise[i] - (a0 + b0v * i) for i in range(M)]
            g0 = 1.0 / rate
            keep = [rise[i] for i in range(M) if abs(resid0[i]) <= 5 * g0]
        nrej = M - len(keep)
        fit2 = lsq(keep) if len(keep) >= 50 else None
        if fit2:
            a, b = fit2
            rr = keep
            resid = [rr[i] - (a + b * i) for i in range(len(rr))]
            rmin, rmax = min(resid), max(resid)
            rstd = (sum(r * r for r in resid) / len(rr)) ** 0.5
            grid = 1.0 / rate
            ppm = (b / (200e-6) - 1.0) * 1e6
            print("  ── 亚网格分析 (鲁棒拟合: %d 个上升沿, 剔除 %d 个毛刺点) ──"
                  % (len(rr), nrej))
            print("     均值周期(拟合) = %.9f µs   (相对 200 µs 偏 %+.2f ppm)" % (b * 1e6, ppm))
            print("     拟合残差: 极差 %.1f ns = %.2f 网格   σ = %.1f ns = %.2f 网格"
                  % ((rmax - rmin) * 1e9, (rmax - rmin) / grid, rstd * 1e9, rstd / grid))
            print("     ★ 判据: 量化误差的理论 σ = 1/√12 = 0.289 网格。")
            print("       σ ≈ 0.29 网格 ⇒ 残差就是**边沿落在网格上的量化**, 硬件周期恒定;")
            print("       σ ≫ 0.5 网格 ⇒ 存在与网格无关的真实抖摆。")
            extra = " %+.2f ppm, σ=%.2f 网格" % (ppm, rstd / grid)

    print("  互异周期值个数 = %d / %d%s" % (len(cnt), len(periods), ""))
    return dict(n=n, mean=mean_s, pmin=pmin, pmax=pmax, spread_ns=spread_ns,
                freq=f_fine, duty=duty, distinct=len(cnt), extra=extra)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["measure", "idle"], default="measure")
    ap.add_argument("--setup", choices=["skeleton", "empty", "div0", "mixed"], default="mixed")
    ap.add_argument("--ch", type=int, default=4)
    ap.add_argument("--rate", type=int, default=16_000_000, help="16 MS/s (单通道)")
    ap.add_argument("--dur", type=float, default=0.5)
    ap.add_argument("--port", default=None)
    a = ap.parse_args()

    os.makedirs(OUTDIR, exist_ok=True)
    print("=" * 72)
    print("H723 PA8 拍信号 · LA 外部测量   (rate=%.1f MS/s, dur=%.3f s)"
          % (a.rate / 1e6, a.dur))
    print("=" * 72)

    L = None
    if a.mode == "measure":
        if serial is None:
            print("!! 需要 pyserial"); return 2
        port = find_port(a.port)
        if not port:
            print("!! 找不到串口"); return 2
        print("端口: %s" % port)
        L = Link(open_serial(port), False)
        time.sleep(0.3)
        desc = setup_workload(L, a.setup)
        if desc is None:
            print("!! 负载部署被拒"); return 2
        print("被测负载: %s" % desc)
        time.sleep(0.3)
    else:
        print("模式: 悬空对照 —— 用 pyocd 把核心 held 在复位态 (PA8 高阻)")
        print("      真信号应变为 ~0 跳变; 若仍有大量跳变 ⇒ 探针悬空在拾噪")
        subprocess.run(["pyocd", "cmd", "-t", "stm32h723xx",
                        "-O", "connect_mode=under-reset", "-c", "reset", "-c", "halt"],
                       capture_output=True, text=True, timeout=90)
        time.sleep(0.5)

    path = capture(a.ch, a.rate, a.dur)
    if not path:
        if L:
            L.ser.close()
        return 2
    print("[LA] 导出 %s (%.2f MB)" % (os.path.basename(path),
                                      os.path.getsize(path) / 1e6))
    ts = parse(path)
    if ts is None:
        return 2
    res = analyse(ts, a.ch, a.rate)

    if a.mode == "idle":
        ok = (res is None) or (res["n"] < 50)
        print("\n>>> 悬空对照判定: %s" % ("PASS (真信号停止 ⇒ 几乎无跳变, 探针确实在被测点上)"
                                          if ok else "FAIL (信号停止后仍有跳变 ⇒ 探针悬空拾噪)"))
        subprocess.run(["pyocd", "cmd", "-t", "stm32h723xx",
                        "-O", "connect_mode=under-reset", "-c", "go"],
                       capture_output=True, text=True, timeout=60)
        return 0 if ok else 1

    if L:
        L.xact(CMD_STOP); L.xact(CMD_RESET)
        L.ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
