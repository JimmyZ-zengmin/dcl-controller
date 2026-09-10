#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
la_tick_freq.py — 用 Saleae Logic2 (MCP over HTTP) 外部测量 H723 拍的**真实频率**

为什么需要它 (MIGRATE-H723.md §3.6 / 项目铁律"宣称=实现"):
  固件能"声称"自己在 450MHz; PLL 可能锁得上但实际频率跑偏 (晶振错 / 分压错 /
  电压不足导致降频)。只有把 PA8 引到 LA 上, 才是**独立于固件的物理证据**。

原理 (可直接反推真实时钟):
  固件 TIM2 的 ARR = CLK_TICK_TIMCNT-1, 而 CLK_TICK_TIMCNT 由**假定的**频率算出。
  所以 PA8 的实际周期与假定值成反比:
      cpu_actual = cpu_assumed * 100us / period_measured
  例: 测到 100us → 假定值正确; 测到 90us → 实际比假定高 11%。

失败指示 (固件侧, 也可被 LA 看到):
  时钟初始化失败 → PA8 闪 |错误码| 次, 停 1s, 循环 (不是 200us 方波)

用法:
  python la_tick_freq.py [--cpu 400] [--ch 4] [--rate 4000000] [--dur 1.0] [--no-reset]
"""
import os, sys, json, time, argparse, subprocess, glob, struct
import urllib.request

MCP     = "http://127.0.0.1:10530/"
HERE    = os.path.dirname(os.path.abspath(__file__))   # 本脚本位于项目根
OUTDIR  = os.path.join(HERE, ".la", "raw")             # 导出临时目录
os.makedirs(OUTDIR, exist_ok=True)


def rpc(method, params=None, timeout=180):
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


def find_capture_id(txt):
    if not txt:
        return None
    for key in ("captureId", "id"):
        try:
            v = json.loads(txt).get(key)
            if v is not None:
                return int(v)
        except Exception:
            pass
    try:
        return int(str(txt).strip().strip('"'))
    except Exception:
        return None


def _parse_clock_defines(path):
    """把 src/clock.h 里的 CLK_* 宏解成数值 (逐层代入, 不依赖求值顺序)。

    ★★ 为什么要这一步 (H8 假阳性事故):
      反推公式 `cpu_actual = cpu_assumed * 100us / period` 成立的**前提**是
      cpu_assumed **等于固件编译期的假定值** —— 因为 TIM2 的 ARR 也是按那个假定值算的。
      本脚本原先把默认值写死成 450MHz, 而固件是 400MHz: 用默认参数跑当前固件会
      测到 100us → 打印"反推 450.00MHz、偏差 +0.000%、时钟树正确且精确" ——
      **一个看起来完美的假阳性**(真实 400MHz)。所以默认值必须从源码解析,
      并且把"来源"打印出来。"""
    import re
    defs = {}
    try:
        for line in open(path, encoding="utf-8"):
            m = re.match(r"\s*#define\s+(CLK_[A-Z0-9_]+)\s+(.+?)\s*(?:/\*.*)?$", line)
            if not m:
                continue
            expr = re.sub(r"\b(\d+)[uUlL]+\b", r"\1", m.group(2).strip())
            defs[m.group(1)] = expr
    except OSError:
        return {}
    env = {}
    for _ in range(24):
        progressed = False
        for k, v in defs.items():
            if k in env:
                continue
            e = re.sub(r"\b(CLK_[A-Z0-9_]+)\b",
                       lambda mm: str(env[mm.group(1)]) if mm.group(1) in env else mm.group(0), v)
            if "CLK_" in e:
                continue
            try:
                env[k] = int(eval(e, {"__builtins__": {}}, {}))
                progressed = True
            except Exception:
                pass
        if not progressed:
            break
    return env


def default_cpu_mhz():
    """返回 (MHz, 来源说明) —— 从 src/clock.h 解析, 解析不出时返回 (None, 原因)"""
    path = os.path.join(HERE, "src", "clock.h")
    env = _parse_clock_defines(path)
    cpu = env.get("CLK_CPU_HZ")
    if cpu:
        return cpu / 1e6, "src/clock.h: CLK_CPU_HZ = %d Hz" % cpu
    return None, "无法从 %s 解析 CLK_CPU_HZ" % path


def main():
    ap = argparse.ArgumentParser()
    _dcpu, _dsrc = default_cpu_mhz()
    ap.add_argument("--cpu", type=float, default=_dcpu,
                    help="固件**编译期假定**的 CPU MHz (默认从 src/clock.h 解析)")
    ap.add_argument("--ch",   type=int,   default=4,     help="LA 通道 (PA8 接哪个)")
    ap.add_argument("--rate", type=int,   default=4_000_000, help="采样率 Hz")
    ap.add_argument("--dur",  type=float, default=1.0,   help="采集时长 s")
    ap.add_argument("--no-reset", action="store_true", help="不复位板子")
    a = ap.parse_args()

    print("=" * 68)
    if a.cpu is None:
        print("!! 无法确定固件假定的 CPU 频率 (%s)" % _dsrc)
        print("   必须显式给 --cpu, 且它**必须等于固件编译时的假定值**, 否则反推无意义。")
        return 2
    print("H723 拍频率 · LA 外部测量   (假定 CPU = %.0f MHz)" % a.cpu)
    print("   假定值来源: %s" % (_dsrc if a.cpu == _dcpu else
          "--cpu 命令行显式指定 (★ 若它与固件编译期假定值不一致, 反推结果无意义)"))
    print("=" * 68)

    # ---- 1) 复位板子让固件自由运行 (核心必须 run, 否则 PA8 不翻转) ----
    if not a.no_reset:
        print("[1] 复位板子 → 固件自由运行…")
        try:
            p = subprocess.run(
                ["pyocd", "cmd", "-t", "stm32h723xx",
                 "-O", "connect_mode=under-reset", "-c", "reset"],
                capture_output=True, text=True, timeout=90)
            print("    pyocd: %s" % (p.stdout or p.stderr or "").strip()[:80])
        except Exception as e:
            print("    pyocd 失败 (%s) — 继续, 板子可能已在跑" % e)
        time.sleep(1.5)
    else:
        print("[1] 跳过复位 (--no-reset)")

    # ---- 2) 设备 ----
    txt, st = call("get_devices", {})
    try:
        dev = json.loads(txt)["devices"][0]
    except Exception:
        print("!! 拿不到 LA 设备: %s %s" % (st, (txt or "")[:200]))
        return 2
    dev_id = dev["deviceId"]
    print("[2] LA 设备: %s (%s)" % (dev_id, dev.get("deviceType")))

    # ---- 3) 启动采集 ----
    n_samples = int(a.rate * a.dur)
    txt, st = call("start_capture", {
        "deviceId": dev_id,
        "logicDeviceConfiguration": {
            "logicChannels": {"digitalChannels": [a.ch]},
            "digitalSampleRate": a.rate,
        },
        "captureConfiguration": {"timedCaptureMode": {"durationSeconds": a.dur}},
    })
    cap = find_capture_id(txt)
    print("[3] start_capture → %s cap=%s  (CH%d @ %.2f MHz, %.2fs = %d 采样)"
          % (st, cap, a.ch, a.rate / 1e6, a.dur, n_samples))
    if cap is None:
        print("!! 没有 captureId: %s" % (txt or "")[:200])
        return 2

    # ---- 4) 等采集完成 ----
    call("wait_capture", {"captureId": cap}, timeout=180)
    time.sleep(0.5)
    print("[4] 采集完成")

    # ---- 5) 导出原始数据 (binary) ----
    for f in glob.glob(os.path.join(OUTDIR, "*")):
        try:
            os.remove(f)
        except Exception:
            pass
    txt, st = call("export_raw_data_binary", {
        "captureId": cap,
        "directory": OUTDIR,
        "logicChannels": {"digitalChannels": [a.ch]},
        "analogDownsampleRatio": 1,
    }, timeout=300)
    print("[5] export_raw_data_binary → %s %s" % (st, (txt or "")[:160]))

    files = sorted(glob.glob(os.path.join(OUTDIR, "*")),
                   key=os.path.getmtime, reverse=True)
    if not files:
        print("!! 没有导出文件")
        return 2
    for f in files:
        print("    文件: %s  (%.2f MB)" % (os.path.basename(f),
                                           os.path.getsize(f) / 1e6))
    path = files[0]

    # ---- 6) 解析: Saleae 二进制 (<SALEAE> 跳变列表) / 逐采样 / 位打包 ----
    raw = open(path, "rb").read()
    n = len(raw)
    if raw[:8] == b"<SALEAE>":
        # 头部: magic(8) ver(4) type(4) ?(8)=1 ?(4) t0(double,8) ntr(u64,8) = 44B
        ntr = struct.unpack_from("<Q", raw, 36)[0]
        if n != 44 + 8 * ntr:
            print("!! SALEAE 文件长度不符: %d vs 44+8*%d=%d" % (n, ntr, 44 + 8 * ntr))
            return 2
        ts = list(struct.unpack_from("<%dd" % ntr, raw, 44))
        print("[6] 解析: Saleae 跳变列表, %d 个跳变" % ntr)
        if ntr < 2:
            print("\n>>> 结果: **PA8 几乎不翻转** (%d 个跳变) → 固件没有在跑!" % ntr)
            for i, t in enumerate(ts):
                print("      跳变 %d @ %.6f s" % (i + 1, t))
            print("    (对照: 正常应 1 秒约 10000 个跳变 = 5kHz 方波)")
            return 1
        span_s = ts[-1] - ts[0]
        f_fine = (ntr - 1) / span_s
        deltas = [ts[i + 1] - ts[i] for i in range(ntr - 1)]
        t_cap = None
        dc = None
    else:
        if n == n_samples:
            mode, samples = "byte/sample", list(raw)
        elif n == (n_samples + 7) // 8:
            mode = "bit-packed"
            samples = []
            for b in raw:
                for k in range(8):
                    samples.append((b >> k) & 1)
        else:
            print("!! 未知格式: %d 字节 vs %d(byte)/%d(packed)"
                  % (n, n_samples, (n_samples + 7) // 8))
            return 2
        print("[6] 解析: %s, %d 字节 → %d 采样" % (mode, n, len(samples)))
        edges = [i for i in range(1, len(samples)) if samples[i] != samples[i - 1]]
        if not edges:
            print("\n>>> 结果: **无任何跳变** (恒定电平) → PA8 没在翻转")
            return 1
        dc = sum(samples) / len(samples)
        t_cap = len(samples) / a.rate
        rising = sum(1 for i in edges if samples[i] == 1)
        rise_idx = [i for i in edges if samples[i] == 1]
        deltas = [(edges[i + 1] - edges[i]) / a.rate for i in range(len(edges) - 1)]
        f_fine = ((len(rise_idx) - 1) / ((rise_idx[-1] - rise_idx[0]) / a.rate)
                  if len(rise_idx) >= 2 else rising / t_cap)
        span_s = (rise_idx[-1] - rise_idx[0]) / a.rate if len(rise_idx) >= 2 else t_cap

    # ★判据门槛: 跳变太少说明只是"启动残余", 不能当作"在跑"
    #   (曾出假阳性: 470MHz 只有 5 个跳变, 却算出"10kHz 正确")
    MIN_TR = 200
    if len(deltas) + 1 < MIN_TR:
        print("\n>>> 结果: **跳变太少 (%d 个 < %d) → 固件没有持续运行**"
              % (len(deltas) + 1, MIN_TR))
        if raw[:8] == b"<SALEAE>":
            for i, t in enumerate(ts[:12]):
                print("      跳变 %d @ %.6f s" % (i + 1, t))
        print("    判读: 不是稳定运行, 而是跑了几下就停/崩了 (启动残余)")
        return 1

    # ★抗异常统计: 线上偶发窄毛刺(几十~几百 ns)会污染 min/max 与均值,
    #   但**不改变"拍周期"本身** —— 所以用中位数/众数刻画拍, 异常项单独计数。
    import collections
    ds = sorted(deltas)
    d_med = ds[len(ds) // 2]
    good = [d for d in deltas if abs(d - d_med) <= 0.10 * d_med]   # 偏离中位数 >10% 视为异常
    outl = len(deltas) - len(good)
    g_mean = sum(good) / len(good) if good else float("nan")
    g_min, g_max = (min(good), max(good)) if good else (float("nan"), float("nan"))
    # 众数 (量化到 1 个采样点) —— 最稳的"拍周期"估计
    mode_cnt, mode_n = collections.Counter(
        round(v * a.rate) for v in good).most_common(1)[0]
    # ★单位: ISR 每拍翻转一次 PA8 → 相邻边沿间隔【就是】拍周期 (不乘 2!)
    edge_mode_us = mode_cnt / a.rate * 1e6
    period_mode_us = edge_mode_us
    period_us = period_mode_us                       # ★用众数(抗毛刺), 不用 min/max
    cpu_actual = a.cpu * 100.0 / period_us if period_us > 0 else float("nan")

    print("\n" + "=" * 68)
    print("测量结果")
    print("=" * 68)
    print("  跳变数          : %d" % (ntr if raw[:8] == b"<SALEAE>" else len(edges)))
    print("  观测跨度        : %.6f s" % span_s)
    if dc is not None:
        print("  占空比          : %.2f %%  (理想 50%%)" % (dc * 100))
    print("  ------------------------------------------------")
    print("  PA8 频率 (精测) : %.4f kHz" % (f_fine / 1e3))
    print("  PA8 周期        : %.4f us  (固件设计值 100.0000)" % period_us)
    print("  --> 反推 CPU    : %.2f MHz  (固件假定 %.2f MHz)" % (cpu_actual, a.cpu))
    print("      偏差        : %+.3f %%" % ((cpu_actual - a.cpu) / a.cpu * 100))
    print("  ------------------------------------------------")
    print("  边沿间隔 中位数 : %.4f us" % (d_med * 1e6))
    print("  边沿间隔 众数   : %.4f us   (占 %.1f%%)  ← 即拍周期"
          % (edge_mode_us, mode_n / len(good) * 100))
    if outl:
        print("  异常项          : %d / %d (%.3f%%)  ← 线上窄毛刺, 不计入拍周期"
              % (outl, len(deltas), outl / len(deltas) * 100))
    print("  抖动(去异常)    : 极差 %.0f ns (=%.1f 采样 @ %.0f MHz; LA 量化 %.0f ns)"
          % ((g_max - g_min) * 1e9, (g_max - g_min) * a.rate, a.rate / 1e6, 1e9 / a.rate))
    print("  ★注意: LA 自身时基精度约 0.1% 量级 —— 判定频率对不对看量级, 不看末位")

    print("\n" + "-" * 68)
    err = abs(cpu_actual - a.cpu) / a.cpu
    if a.cpu != _dcpu:
        # ★ H8: 显式指定的假定值与源码不符时, "偏差 0%" 不能当成"时钟树正确"
        print("判读: ⚠ 假定值是命令行指定的 (%.0f MHz), 与 src/clock.h 的 %.0f MHz 不一致"
              % (a.cpu, _dcpu or 0))
        print("      本条只能说明「拍周期/假定值 = %.4f」; 要判『时钟树是否正确』, "
              % (100.0 / period_us))
        print("      请用 src/clock.h 里的假定值 (即不带 --cpu) 复跑。")
    elif err < 0.01:
        print("判读: 实测与(源码解析出的)假定值一致 (%.3f%%) → 时钟树正确且精确" % (err * 100))
    else:
        print("判读: ★实测与假定差 %.1f%% → 真实 CPU ≈ %.0f MHz (偏差的成因需单独查)"
              % (err * 100, cpu_actual))
    print("-" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
