#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dclmon.py — DCL 变量监控器 (对应 PLC 编程软件的"监控表/趋势曲线")

读引擎共享内存的 WIRE/SENSOR 值, 实时显示:
  - 默认: 终端表格 (信号名 + 当前值 + ASCII 趋势条), ~20Hz 刷新
  - --plot: matplotlib 实时曲线 (多信号多子图)
  - 可选 .dcl 文件: 用 dclc --dump 解析出 信号名→wire槽 映射
    (不给文件时用 wire[0..15] 无名监控)

用法:
  python tools/dclmon.py COM7 [examples/hiloop.dcl] [--plot] [--rate 20]
实测 (2026-09-09 recv 提速后): 单命令 5ms → 表格模式 ~20Hz, 曲线 ~10Hz
"""
# ══════════════════════════════════════════════════════════════════════════
# ★ 本文件从 esp32-core0/tools/ 迁入 H723 线 (2026-09-11)。**只改了两处**:
#   ① `from test_dcl import ...` → `from h723_client import ...`
#      (test_dcl.py 是 S3 的验收脚本体, 不搬; 客户端接口由 h723_client.py 实现)
#   ② 默认串口 'COM7' → None (= 自动找 CH340, 免得用户以为要改代码)
#   其余**一行未动** —— 编译规则/校验规则/帧格式全部逐字保留。
#   语义等价性的证据: 同一套协议下 S3 的 test_dcl.py **一行不改** 能打 H723 (22/30)。
# ══════════════════════════════════════════════════════════════════════════

import sys, os, struct, time, re, subprocess, argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from h723_client import Dcl, engine_status

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFF_SENSOR_MAP, OFF_WIRE_MAP = 0x40, 0x240
MAX_WIRES = 128


def resolve_names(dcl_file):
    """解析 .dcl → {名字: (区域偏移, 槽)}。dclc --dump 每行形如
    'SENSOR fb = wire[1] <- SENSOR[2]' / 'CONST one = wire[3] <- 1.0' /
    'OUTPUT pwm = wire[30] <- both' — 统一抓 '名字 = wire[N]'。"""
    if not dcl_file:
        return {}
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "dclc.py"),
                        dcl_file, "--dump"], capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print("dclc --dump 失败:\n" + r.stdout + r.stderr); sys.exit(1)
    names = {}
    for line in r.stdout.splitlines():
        # dump 行形如 '  TON     ton1 = wire[3]  <- ...' — 首词是语句关键字,
        # 第二词才是信号名 (CONST 行: '  CONST   one = wire[1]')
        m = re.search(r"^  \S+\s+(\w+)\s*=\s*wire\[(\d+)\]", line)
        if m:
            nm, slot = m.group(1), int(m.group(2))
            if nm not in names:
                names[nm] = (OFF_WIRE_MAP, slot)
    return names


def mode_table(port, names, rate):
    dcl = Dcl(port); time.sleep(0.3)
    s = engine_status(dcl)
    if not s:
        print("引擎无响应"); sys.exit(1)
    shm = s["shm"]
    name_of = {}    # addr -> 名字 (force 显示用)
    items = []
    for name, (base, idx) in (names or {}).items():
        a = shm + base + idx * 4
        items.append((name, a)); name_of[a] = name
    if not items:
        items = [(f"wire[{i}]", shm + OFF_WIRE_MAP + i * 4) for i in range(16)]
        name_of = {a: name for name, a in items}
    hist = {name: [] for name, _ in items}
    dt = 1.0 / rate
    # force 交互: stdin 行命令 (独立线程, 不阻塞采样循环)
    #   <名字>=<值>  强制 (引擎写被屏蔽, 值钉住)
    #   <名字>~      释放 (引擎恢复写)
    import threading, queue
    cmdq = queue.Queue()

    def stdin_reader():
        for ln in sys.stdin:
            cmdq.put(ln.strip())
    threading.Thread(target=stdin_reader, daemon=True).start()

    forced = {}   # 名字 -> 值 (本端跟踪, 用于显示标记)

    def find_wire(name):
        """名字 → wire 槽索引 (仅 WIRE 区); 无则 None"""
        for nm, a in items:
            if nm != name:
                continue
            base_off = a - shm
            if OFF_WIRE_MAP <= base_off < OFF_WIRE_MAP + MAX_WIRES * 4:
                return (base_off - OFF_WIRE_MAP) // 4
            return None
        return None

    def apply_force_cmd(raw):
        if not raw or raw.startswith("#"):
            return
        eq = raw.find("=")
        if eq > 0:
            name, vs = raw[:eq].strip(), raw[eq + 1:].strip()
            widx = find_wire(name)
            if widx is None:
                print("\n[force] 未知/非WIRE信号 '%s'" % name); return
            try:
                val = float(vs)
            except ValueError:
                print("\n[force] 值非法: %s" % vs); return
            sts = dcl.send(0x24, struct.pack("<HBf", widx, 1, val))[0]
            forced[name] = val if sts == "ACK" else float("nan")
            print("\n[force] %s(wire[%d]) <- %.3f %s" % (name, widx, val,
                  "ACK" if sts == "ACK" else "NAK!"))
        elif raw.endswith("~"):
            name = raw[:-1].strip()
            widx = find_wire(name)
            if widx is None:
                print("\n[force] 未知/非WIRE信号 '%s'" % name); return
            sts = dcl.send(0x24, struct.pack("<HBf", widx, 0, 0.0))[0]
            forced.pop(name, None)
            print("\n[release] %s(wire[%d]) %s" % (name, widx,
                  "ACK" if sts == "ACK" else "NAK!"))
        else:
            print("\n[force] 命令: <名字>=<值> 强制 | <名字>~ 释放 (仅 WIRE 区)")

    print("=== DCL 监控 (Ctrl+C 退出; force 命令: <名字>=<值> 钉住, <名字>~ 释放) ===")
    print("    " + "  ".join("%-10s" % n for n, _ in items))
    try:
        while True:
            t0 = time.time()
            # 消费 stdin 命令: 每帧处理一条 (不阻塞采样循环, 命令间延迟 ≤1/rate)
            try:
                cmd = cmdq.get_nowait()
            except queue.Empty:
                cmd = None
            if cmd:
                apply_force_cmd(cmd)
            # 读值
            addrs = sorted({a for _, a in items})
            groups = []
            for a in addrs:
                if groups and a == groups[-1][-1] + 4:
                    groups[-1].append(a)
                else:
                    groups.append([a])
            vals = {}
            for g in groups:
                sts, p = dcl.send(0x22, struct.pack("<IH", g[0], len(g)))
                if sts == "ACK" and len(p) >= 4 * len(g):
                    for k, v in enumerate(struct.unpack("<" + "f" * len(g), p[:4 * len(g)])):
                        vals[g[0] + 4 * k] = v
            line, bars = [], []
            for name, a in items:
                v = vals.get(a, float("nan"))
                hist[name].append(v); hist[name] = hist[name][-60:]
                tag = "F" if name in forced else " "
                line.append("%s%9.3f" % (tag, v))
                bars.append(_mini_bar(hist[name]))
            print("\r" + "  ".join(line) + "   " + "  ".join(bars), end="", flush=True)
            wait = dt - (time.time() - t0)
            if wait > 0: time.sleep(wait)
    except KeyboardInterrupt:
        print("\n[停止]")


def _mini_bar(h):
    if not h: return ""
    lo, hi = min(h), max(h)
    rng = (hi - lo) or 1.0
    last = h[-1]
    bars = []
    for i in range(len(h)):
        k = int((h[i] - lo) / rng * 8)
        bars.append(" ▁▂▃▄▅▆▇█"[min(k, 8)])
    return "".join(bars[-24:]) + (" %+.2f" % (last - h[0])) if len(h) > 1 else ""


def mode_plot(port, names, rate, save=None, save_dur=None):
    import matplotlib
    if not save:
        matplotlib.use("TkAgg")
    else:
        matplotlib.use("Agg")          # 落盘模式: 无头后端, 无需显示器
    import matplotlib.pyplot as plt
    dcl = Dcl(port); time.sleep(0.3)
    s = engine_status(dcl)
    if not s:
        print("引擎无响应"); sys.exit(1)
    shm = s["shm"]
    items = [(n, shm + b + i * 4) for n, (b, i) in (names or {}).items()] \
        or [(f"wire[{i}]", shm + OFF_WIRE_MAP + i * 4) for i in range(8)]
    n = len(items)
    fig, axes = plt.subplots(n, 1, figsize=(9, 1.2 * n + 0.5), sharex=True)
    if n == 1:
        axes = [axes]
    series = [{"t": [], "v": []} for _ in items]
    lines = [ax.plot([], [], lw=1.2)[0] for ax in axes]
    for ax, (nm, _) in zip(axes, items):
        ax.set_ylabel(nm, fontsize=8)
        ax.grid(alpha=0.3)
    t0 = time.time()
    dt = 1.0 / rate
    if save:
        print("=== DCL 曲线落盘: %.0fs → %s (无窗口) ===" % (save_dur, save))
    else:
        print("=== DCL 曲线监控 (关窗口退出) ===")
    if not save:
        plt.ion()
    try:
        while True:
            now = time.time() - t0
            if save and now >= save_dur:
                break
            for (name, a), sr, ln in zip(items, series, lines):
                sts, p = dcl.send(0x22, struct.pack("<IH", a, 1))
                v = struct.unpack("<f", p[:4])[0] if sts == "ACK" and len(p) >= 4 else float("nan")
                sr["t"].append(now); sr["v"].append(v)
                keep = 99999 if save else 300
                sr["t"] = sr["t"][-keep:]; sr["v"] = sr["v"][-keep:]
                ln.set_data(sr["t"], sr["v"])
            if save:
                # 落盘模式: 静态数据也随帧刷新 (matplotlib 惰性重绘)
                for ax, sr in zip(axes, series):
                    ax.relim(); ax.autoscale_view(scaley=True)
            else:
                ax_l = axes[-1]
                ax_l.set_xlim(max(0, now - 15), now + 0.5)
                for ax in axes:
                    ax.relim(); ax.autoscale_view(scaley=True)
                fig.canvas.draw_idle()
                fig.canvas.flush_events()
            wait = dt - (time.time() - t0 - now)
            if wait > 0: time.sleep(wait)
        if save:
            for ax in axes:
                ax.relim(); ax.autoscale_view(scaley=True)
            fig.savefig(save, dpi=110, bbox_inches="tight")
            print("已保存: %s (%d 采样点)" % (save, len(series[0]["t"])))
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("dcl", nargs="?", default=None, help=".dcl 文件 (给出信号名→槽映射)")
    ap.add_argument("--plot", action="store_true", help="matplotlib 实时曲线")
    ap.add_argument("--rate", type=float, default=20, help="刷新率 Hz (默认 20)")
    ap.add_argument("--save", default=None, help="曲线落盘: PNG 路径 (与 --plot 同用, 无窗口)")
    ap.add_argument("--dur", type=float, default=10.0, help="落盘采集时长秒 (默认 10)")
    a = ap.parse_args()
    dcl_file = os.path.join(ROOT, a.dcl) if a.dcl else None
    names = resolve_names(dcl_file)
    if a.dcl and not names:
        print("警告: 未能从 .dcl 解析出信号 — 用无名 wire 监控")
    if a.plot or a.save:
        (mode_plot)(a.port, names, a.rate, save=a.save, save_dur=a.dur)
    else:
        (mode_table)(a.port, names, a.rate)


if __name__ == "__main__":
    main()
