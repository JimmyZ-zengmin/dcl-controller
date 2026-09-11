#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_t26.py — S3 回归 T26 的 H723 侧聚焦验收 (+ T15 的落盘半段)

★ 为什么单独有这个脚本 (而不是只跑 S3 的 test_dcl.py):
  T26 的判据链条长 (运行期不落盘 → dirty 挂起 → STOP 后**自动**落盘 → 新表真在工作),
  出问题时需要能定位到具体哪一环。本脚本把 S3 套件 `tools/test_dcl.py:540-573` 的
  逻辑**逐条照搬**, 但每步都打印中间量, 并额外读 H723 侧的观测面
  (g_persist_auto_runs / auto_gate / writes / skip_run —— 这些是 S3 没有的)。

★ 与 S3 的**必须不同之处** (不是简化, 是本平台的事实):
  · T15 的 "hard reset" 在 S3 上靠 RTS 拉 EN; H723 的 CH340 RTS **没有接到 NRST**
    (2026-09-11 实测: RTS 翻转后 samples 继续增长 20031→31602, 板子没复位) ->
    T15 的重启半段只能**手按板上 RST**。本脚本因此只验 T15 的落盘半段。
  · 落盘触发: S3 有后台 persist_task; H723 是裸机单循环, 由**纯查询型 0x43**触发
    (固件 h_persist_w2 的 g_persist_auto)。语义等价物: "只有上位机在问, 才在停机
    窗口落盘" —— 见 src/main.c 里 g_persist_auto 的长注释。

用法:
    python tools/h723_t26.py              # 全自动找 CH340
    python tools/h723_t26.py --port COM14
"""
import argparse
import sys

# Windows 控制台默认 GBK: 个别字符 (⇒ / ✓ 等) 会以 UnicodeEncodeError 直接崩掉脚本,
# 而崩在"打印结论"这一步最冤 —— 数据都已经量到了。改成永不抛。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass
import struct
import subprocess
import sys
import time

try:
    import serial
except ImportError:
    print("!! 需要 pyserial")
    sys.exit(2)

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from h723_modbus import Link, find_port
from h723_seq import rt_route, OP_DIRECT
from h723_w1 import build_frame

# S3 test_dcl.py:24 同款 —— 本脚本刻意用 SRC_CONST (常量源) 而不是 SRC_WIRE,
# 这样 WIRE 的值完全由 param 表决定, 判据 "WIRE1 == 2.0" 才有意义
# (用 SRC_WIRE 起点都是 0, 分不出"新表生效"还是"旧表残留")。
SRC_CONST = 2  # h723_seq 只定义了 SRC_WIRE=1 (M234 审计用不到 CONST), 这里补齐

CMD_DEPLOY = 0x10
CMD_START = 0x11
CMD_STOP = 0x12
CMD_RESET = 0x13
CMD_READ_BURST = 0x22
CMD_ENGINE_STATUS = 0x38
CMD_PERSIST = 0x43

STS_ACK = 0x00
OFF_WIRE_MAP = 0x0240

# ── H723 侧观测面 (由 `nm` **运行时解析**) ──
# ★★ 为什么不硬编码地址 (2026-09-11 踩到): 加/删任何一个全局量都可能让 DTCM 布局**整体
#   位移**。本表第一版是硬编码的, 给 main.c 加一个 `g_per_glitch_n` 之后就整体 +4 字节,
#   于是"gate"读到了"auto" —— **判据报告"RUN 期间没被拦下"**, 而原因纯粹是读错了地址。
#   症状看起来像固件语义变了, 与被测对象毫无关系。
#   ⇒ 一律从 `nm` 解析 (h723_w2_probe.py 早已这么做)。名字错了会直接报"符号缺失",
#     而不是安静地读到邻居的 0。
OBS_NAMES = [
    "g_persist_writes",     # 累计成功落盘次数
    "g_persist_auto_runs",  # 自动落盘执行次数
    "g_persist_auto_gate",  # 因 RUN 被拦下的次数
    "g_persist_skip_run",   # 因 RUN 跳过 persist_save 的次数
    "g_persist_dirty",      # 1 = 有未落盘配置
    "g_tick_count",         # 100μs 拍计数
]

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
ELF = os.path.join(_ROOT, "build", "dcl_h723")
NM = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
      "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update"
      ".win32_1.5.0.202011040924/tools/bin/arm-none-eabi-nm.exe")


def nm_syms():
    r = subprocess.run([NM, ELF], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print("!! arm-none-eabi-nm 失败:", r.stderr)
        sys.exit(2)
    syms = {}
    for line in r.stdout.splitlines():
        p = line.split()
        if len(p) == 3:
            try:
                syms[p[2]] = int(p[0], 16)
            except ValueError:
                pass
    return syms


OBS = {}   # 由 main() 在 nm 成功后填入
PYOCD = ["pyocd", "cmd", "-t", "stm32h723xx", "-O", "connect_mode=under-reset"]
# ★ 只读观测面时用 **halt** 而不是 attach (2026-09-11 实测):
#   `connect_mode=attach` 在本探针 (Luxiaoban Flash Pro / CMSIS-DAP) 上初始化不了 AP ——
#   稳定报 "Error reading AP#0 IDR … No cores were discovered" (重试 3 次皆然, 降频也无效);
#   `connect_mode=halt` 会挂住核但**不复位**, 观测面 (DTCM 全局) 原样保留 -> 正是我们要的。
PYOCD_ATTACH = ["pyocd", "cmd", "-t", "stm32h723xx", "-O", "connect_mode=halt"]

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))


# ══════════ S3 套件的同款辅助函数 (逐条照搬) ══════════

def deploy(dcl, routes, params, start=True):
    """S3 test_dcl.py:122 同款"""
    payload = struct.pack("<HHH", len(routes), len(params), 0)
    payload += b"".join(routes) + b"".join(params)
    sts, _ = dcl.xact(CMD_DEPLOY, payload, timeout=2.0)
    if sts != STS_ACK:
        return False
    if start:
        sts, _ = dcl.xact(CMD_START, timeout=2.0)
        if sts != STS_ACK:
            return False
    return True


def persist_flags(dcl):
    """S3 test_dcl.py:137 同款 —— 返回 (dirty, persisting) 或 None"""
    sts, p = dcl.xact(CMD_PERSIST, b"", timeout=2.0)
    if sts == STS_ACK and len(p) >= 8:
        return (p[7] & 1) != 0, (p[7] & 2) != 0
    return None


def wait_flush(dcl, timeout_s=3.0):
    """S3 test_dcl.py:144 同款 —— 等 dirty 清 (20ms 轮询)

    ★ 关键: 这个轮询**本身**就是 H723 落盘的触发条件。
      S3 是后台任务自己落盘, PC 只是在观察; H723 没有后台任务, PC 的询问
      就是"现在可以占用引擎停机的窗口"的信号。"""
    t0 = time.time()
    n = 0
    while time.time() - t0 < timeout_s:
        f = persist_flags(dcl)
        n += 1
        if f and not f[0]:
            return True, n, time.time() - t0
        time.sleep(0.02)
    return False, n, time.time() - t0


def engine_status(dcl):
    """S3 test_dcl.py:155 同款"""
    sts, p = dcl.xact(CMD_ENGINE_STATUS, b"", timeout=2.0)
    if sts != STS_ACK or len(p) < 27:
        return None
    samples, pmin, pmax, emin, emax = struct.unpack("<IIIII", p[:20])
    n_routes, = struct.unpack("<H", p[20:22])
    run = p[22]
    shm, = struct.unpack("<I", p[23:27])
    return dict(samples=samples, pmin=pmin, pmax=pmax, emin=emin, emax=emax,
                n_routes=n_routes, run=run, shm=shm)


def read_wires(dcl, shm, count=4):
    """S3 test_dcl.py:167 同款"""
    sts, p = dcl.xact(CMD_READ_BURST, struct.pack("<IH", shm + OFF_WIRE_MAP, count),
                      timeout=2.0)
    if sts != STS_ACK or len(p) < count * 4:
        return None
    return struct.unpack("<%df" % count, p[:count * 4])


def param(a=0.0):
    return struct.pack("<ffff", a, 0.0, 0.0, 0.0)


# ══════════ pyocd 侧观测 ══════════

def obs_read():
    """一次会话读回全部观测面。

    ★★ 必须用 **不复位** 的连接 (halt, 见 PYOCD_ATTACH 注释) —— 第一版用了
       `-c reset`, 结果全读到 0: 这些量住在 DTCM, 而固件启动时 `cold_start_reset()`
       会把它们清零, 所以"读之前先复位"等于**自己把证据擦掉**。这是本项目
       "观测方法本身可能是错的"那一族坑的又一例 (观察者效应)。"""
    cmds = []
    for _, a in OBS.items():
        cmds += ["-c", "read32 0x%08X" % a]
    r = subprocess.run(PYOCD_ATTACH + cmds + ["-c", "go"],
                       capture_output=True, text=True, timeout=90)
    out = {}
    for line in r.stdout.splitlines():
        s = line.strip()
        if ":" not in s:
            continue
        a, v = s.split(":", 1)
        try:
            a = int(a, 16)
            v = int(v.split()[0], 16)
        except Exception:
            continue
        for k, ka in OBS.items():
            if ka == a:
                out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    a = ap.parse_args()
    port = find_port(a.port)
    print("端口: %s" % port)
    if not port:
        print("!! 找不到串口"); return 2

    # ★ 观测面地址由 nm 运行时解析 (见 OBS_NAMES 处的原因说明)。
    #   符号缺失**直接报错退出** —— 绝不退回"读某个默认地址"那种会安静出错的兜底。
    syms = nm_syms()
    missing = [n for n in OBS_NAMES if n not in syms]
    if missing:
        print("!! 观测面符号缺失 (固件改了名字却没同步本脚本?): %s" % missing)
        return 2
    OBS.update({n: syms[n] for n in OBS_NAMES})
    print("观测面 (nm 解析): " + ", ".join("%s=0x%08X" % (n, OBS[n]) for n in OBS_NAMES))
    if not os.path.exists(os.path.join(_HERE, "..", "build", "dcl_h723")):
        print("!! 先构建"); return 2

    ser = serial.Serial(port, 115200, timeout=0.5)
    time.sleep(0.3)
    dcl = Link(ser)

    try:
        # ══════════ T26 (S3 test_dcl.py:540-573 逐条对照) ══════════
        print("\n── T26: 运行期 0 冻结 + PERSISTENT 落盘 ──")
        dcl.xact(CMD_RESET, timeout=2.0)
        time.sleep(0.2)
        ok, n, dt = wait_flush(dcl)              # RESET 清 dirty → 应立刻返回
        print("     [0] RESET 后 dirty 清零: %s (轮询 %d 次 %.2fs)" % (ok, n, dt))
        record("T26-0 RESET 后无挂起待落", ok, "轮询%d次 %.2fs" % (n, dt))

        ok26d = deploy(dcl, [rt_route(SRC_CONST, 0, 0, OP_DIRECT, 0, div=0)],
                       [param(1.0)], start=True)          # 基线, RUN
        time.sleep(0.4)
        s26a = engine_status(dcl)
        print("     [1] 基线 deploy=%s  samples=%s pmax=%d run=%s"
              % (ok26d, s26a["samples"] if s26a else "?", s26a["pmax"] if s26a else 0,
                 s26a["run"] if s26a else "?"))

        # RUN 中二次 deploy: 热重载 + 登记 dirty —— 但引擎在跑, 不得落盘
        ok26d = ok26d and deploy(dcl, [rt_route(SRC_CONST, 0, 1, OP_DIRECT, 0, div=0)],
                                 [param(2.0)], start=True)
        time.sleep(0.3)
        f_run = persist_flags(dcl)
        dirty_pending = bool(f_run and f_run[0])
        print("     [2] RUN 中 deploy 后 dirty = %s (期望 1 = 挂起未落盘)" % (f_run,))

        # ★★ "零冻结"必须取 **RUN 窗口内的 pmax 峰值**, 不能只抓一次快照:
        #   实测一次快照可能正好撞上 stats_reset 之后的 0 (2026-09-11 见到 pmax=0),
        #   那会让 `pmax < 300000` 变成**永远能过的空判据** —— 与"判据必须能失败"直接冲突。
        #   -> 再加一条 "采样器是活的" (pmin/pmax 都非 0) 作为前置, 两条一起才算数。
        time.sleep(0.4)
        ppeak = 0
        pmin_live = 0
        for _ in range(3):
            s = engine_status(dcl)
            if s:
                ppeak = max(ppeak, s["pmax"])
                pmin_live = max(pmin_live, 0 if s["pmin"] == 0xFFFFFFFF else s["pmin"])
            time.sleep(0.2)
        f_run2 = persist_flags(dcl)
        still_pending = bool(f_run2 and f_run2[0])
        s26b = engine_status(dcl)
        sampler_live = (ppeak > 0 and pmin_live > 0)
        no_freeze = sampler_live and ppeak < 300000    # 零冻结: 无 ~28ms 巨拍

        record("T26-1 RUN 中 deploy 只标 dirty 不落盘", dirty_pending,
               "dirty=%s" % (f_run,))
        record("T26-2 1s 后仍挂起 (RUN 期 0 flash op)", still_pending,
               "dirty=%s" % (f_run2,))
        record("T26-3a 拍周期采样器是活的 (pmin/pmax 非 0)", sampler_live,
               "pmin=%d pmax=%d (若恒为 0, 下面那条判据就是空过)" % (pmin_live, ppeak))
        record("T26-3b 运行期无冻结 (pmax<300000)", no_freeze,
               "RUN 窗口 pmax 峰值=%d (≈拍长 40000 cyc @400MHz)" % ppeak)
        print("     ★ 说明 (口径要说准): T26-3b 是**防回归护栏** —— 它证明整个 RUN 段"
              "一拍没丢\n       (落盘机制没有在运行期扰动引擎)。修复前 T26 失败在 **T26-4 "
              "(无自动落盘)** 这一环,\n       不是这一条。")

        # STOP -> 自动落盘 (此刻引擎不跑, 无冻结可言)
        dcl.xact(CMD_STOP, timeout=2.0)
        flushed, n, dt = wait_flush(dcl)
        print("     [3] STOP 后等落盘: %s (轮询 %d 次 %.2fs)" % (flushed, n, dt))
        record("T26-4 STOP 后自动落盘 (dirty 清)", flushed, "%.2fs / %d 次轮询" % (dt, n))

        sts_s, _ = dcl.xact(CMD_START, timeout=2.0)
        time.sleep(0.2)
        w26 = read_wires(dcl, s26b["shm"], 2) if (s26b and flushed and sts_s == STS_ACK) else None
        ok26 = (ok26d and dirty_pending and still_pending and no_freeze and flushed
                and w26 is not None and len(w26) > 1 and abs(w26[1] - 2.0) < 0.01)
        record("T26 运行期0冻结+PERSISTENT落盘 (完整判据)", ok26,
               "WIRE1=%s (期望2.0)" % (w26[1] if (w26 and len(w26) > 1) else "N/A"))

        # ══════════ T15 的落盘半段 (S3:334-341 的 ok15d & p_ok) ══════════
        print("\n── T15 (落盘半段; 重启半段本平台须手按 RST) ──")
        dcl.xact(CMD_RESET, timeout=2.0)
        time.sleep(0.2)
        wait_flush(dcl)
        ok15d = deploy(dcl, [rt_route(SRC_CONST, 0, 0, OP_DIRECT, 0, div=0)],
                       [param(7.77)], start=False)               # 引擎当前已停
        flushed15, n15, dt15 = wait_flush(dcl)
        sts, p = dcl.xact(CMD_PERSIST, b"", timeout=2.0)
        p_ok = (flushed15 and sts == STS_ACK and len(p) >= 7
                and p[0] == 1 and (p[1] | (p[2] << 8)) == 1)
        record("T15-落盘 deploy(停机) → 自动落盘 → 0x43 ok=1 nr=1", p_ok,
               "ok=%s nr=%s (%.2fs/%d次)" % (p[0] if len(p) >= 1 else "?",
                                             (p[1] | (p[2] << 8)) if len(p) >= 3 else "?",
                                             dt15, n15))

        # ══════════ H723 侧观测面 (S3 没有的量) ══════════
        print("\n── H723 侧观测面 (pyocd, 证明触发路径真的是我们设计的那条) ──")
        o = obs_read()
        for k in OBS:
            print("     %-22s = 0x%08X (%d)" % (k, o.get(k, -1), o.get(k, -1)))
        runs = o.get("g_persist_auto_runs", 0) or 0
        gate = o.get("g_persist_auto_gate", 0) or 0
        record("观测A: 自动落盘路径真的被执行 (runs>=2)", runs >= 2,
               "runs=%d (本脚本触发 2 次: T26 STOP 后 + T15 停机 deploy 后)" % runs)
        record("观测B: RUN 中被问到的请求确实被 PERSISTENT 门拦下 (gate>=2)", gate >= 2,
               "gate=%d (T26 的 f_run / f_run2 两次查询) —— 这条挡住的正是 still_pending" % gate)
        record("观测C: 落盘留下了可复核的写入计数 (writes>=2)", (o.get("g_persist_writes", 0) or 0) >= 2,
               "writes=%s" % o.get("g_persist_writes"))

    finally:
        ser.close()

    print("\n=== 结果汇总 ===")
    npass = sum(1 for _, ok, _ in RESULTS if ok)
    for nm, ok, _ in RESULTS:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", nm))
    print("\n%d/%d 通过" % (npass, len(RESULTS)))
    return 0 if npass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
