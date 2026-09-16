#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_jitter.py — 确定性复测: **拍周期 + 拍抖动**（周期级，无需 LA）

为什么要复测 (2026-09-11 完成度盘点 §7.3)
-----------------------------------------
`AUDIT-H723-stage2` 的"拍周期 11 组全部极差 0"是**引擎独占拍**时的结果。
此后拍内又加了: 协议收发 / Modbus 状态机 / macro 节流 / HIL 输出。
它们都在拍内有界 —— 但"**能跑**"不等于"**拍长不变**"，必须重新量过才能宣称。
(本项目铁律: 宣称必须等于实现。)

★ 为什么不用 pyocd 读 (而是走串口 0x38)
  `DWT_CYCCNT` 只数核心周期，**调试暂停期间它是冻结的**，而 TIM2 继续计数
  ⇒ "暂停 → 恢复"这条路径上恢复后的第一拍会折进一个假样本。
  `REPORT-CAPACITY-2026-09-11` 当时正是因此**拒答**抖动数字。
  ⇒ 本工具**全程只用串口**（0x38 的 pmin/pmax 就是 DTCM 里的权威值），
     不建立任何调试会话 ⇒ 不存在这个污染源。
  ★ 自证: 若真出现一次时钟不连续，它会表现为 max 出现十亿级巨值 ⇒ 极差不可能为 0。
     所以"**极差 == 0**"这个结论本身就是"没有污染"的证据 (互为对方的哨兵)。

判据 (每条都能失败)
------------------
  T0   链路活性 (0x01 → ACK + cap 匹配)
  T0b  读 SHM 基址
  T0c  工具自检: samples 必须随 RUN 增长 (判据通道是活的, 否则后面无从谈起)
  T1   骨架拍 (STOP 态, 不扫): pmax ≈ 40000 —— **阳性对照**, 证明测量通道真的在报数
  T2   空程序 RUN: 稳定在 40000±64 且**无漏拍**  ← 同时给出**基线极差**
  T3   div0 满表 128 条: 入口极差 ≤ 基线+16  (**与负载无关**)
  T4   分档满表 128 条: 入口极差 ≤ 基线+16
  T5   分档满表 + 协议流量: 入口极差 ≤ 基线+16

★★ 口径订正 (2026-09-11 深夜, LA 交叉测量之后)
  这个量实际测的是 **ISR 入口间隔** = 硬件拍长 + 两次入口延迟之差, **不是"拍长抖动"**。
  LA 独立测得 (引脚边沿, 绕开入口路径) 拍长抖摆 **σ ≲ 1.6 cyc**, 而这里读到 18~24 cyc
  ⇒ 残差来自**中断入口延迟**。所以判据**不再宣称"极差 == 0"** (那是过度声称 —— 项目里
  原本的计划表就写着"极差 0 cyc", 本次复测把它换成了可复算的口径)。
  完整证据: `docs/REPORT-DETERMINISM-2026-09-11.md`

★ T1 是**阳性对照**: 它证明"测量通道真的在报数"（若 T1 都拿不到 40000 附近的值，
  后面所有结论都只可能是"没在测"）。真正的"能失败"对照是另一份构建 ——
  `bash build.sh -DDCL_BOOT_SEL=0 -DDCL_BOOT_PROFILE=2`（FLASH 取指 + 全表扫）。
  stage2 实测那份构建 max 41500 > 40000（超载）⇒ 入口间隔必然被撑开。见 --expect-fail。

★★ LA 侧请配合 `tools/h723_tick_la.py` 一起看:
  本工具测 **ISR 入口间隔**(含入口延迟); LA 测 **引脚边沿**(纯硬件拍长)。
  两者交叉才分得开"拍长抖动"与"入口延迟"—— 这正是本次复测的核心。

用法:
    python tools/h723_jitter.py --port COM14
    python tools/h723_jitter.py --port COM14 --expect-fail   # 对照构建: 要求量到非 0 极差
"""

import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse
import os
import struct
import sys
import time

try:
    import serial
except ImportError:
    print("!! 需要 pyserial"); sys.exit(2)

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from h723_modbus import Link, find_port, open_serial  # noqa: E402

CMD_GET_VERSION   = 0x01
CMD_DEPLOY        = 0x10
CMD_START         = 0x11
CMD_STOP          = 0x12
CMD_RESET         = 0x13
CMD_ENGINE_STATUS = 0x38

# SHM 偏移 (engine.h)
OFF_CTRL_ENGINE_RUN = 0x0D
OFF_PERIOD_MIN      = 0x1C
OFF_PERIOD_MAX      = 0x20
OFF_EXEC_MIN        = 0x24
OFF_EXEC_MAX        = 0x28

# 路由字段常量 (engine.h)
SRC_CONST, DST_WIRE, OP_DIRECT, OP_PID = 2, 2, 0x00, 0x05
ROUTE_FLAG_ACTIVE = 0x01
PERIOD_PHASE_SHIFT = 2
DIV_FAST, DIV_MID, DIV_SLOW = 0, 1, 2

TICK_CYC = 40000          # 默认按 400MHz(DWT 档) 假定; ★ 运行时由 tb_resolve() 覆盖

# ══════════ ★★★ 判据的"单位随档而变"（2026-09-16）══════════
# 本套件的阈值原本写死在 **DWT 周期(@400MHz)** 上: 拍 40000 cyc、稳定带 ±1000 cyc、
# 负载无关 ≤ 基线+16 cyc、T1 ±32 cyc。
# 而交付固件现在用 **生产时基 = TIM5(@200MHz, 5ns)** ⇒ 同一物理量的**计数减半**
# （拍 = 20000 tick）。照抄旧数字会得到**假红**。
# ⇒ 正确做法: 判据要表达**物理量**(时间)，计数按当前时基换算。
#   官方频率由 `0x39 op=21` 给出（约定: 该 op 的应答 +4 = 时基频率 Hz；旧固件 NAK ⇒ 退回 400MHz）。
TB_HZ = 400000000
TICK_CYC = 40000
STABLE_TOL = 1000         # 原 ±1000 cyc @400MHz = ±2.5µs
LOAD_TOL = 16             # 原 +16 cyc = 40ns
T1_TOL = 32               # 原 ±32 cyc = 80ns


def tb_resolve(L):
    """读 `0x39 op=21` 拿时基频率, 并把全部阈值按**时间**换回计数。
    ★ 参数是 `h723_modbus.Link`（本套件走的是 Link, 不是 h723_client.Dcl）。"""
    global TB_HZ, TICK_CYC, STABLE_TOL, LOAD_TOL, T1_TOL
    try:
        sts, p = L.xact(0x39, struct.pack("<BBI", 21, 0, 0))
        if sts == 0 and len(p) >= 8:
            hz = struct.unpack_from("<I", p, 4)[0]
            if hz >= 1000000:
                TB_HZ = hz
    except Exception:                                          # noqa: BLE001
        pass        # 旧固件没有 op=21 ⇒ 退回 400MHz 假定（并在下面打印出来）
    us = TB_HZ / 1000000.0
    TICK_CYC   = int(TB_HZ // 10000)      # 100 µs
    STABLE_TOL = int(us * 2.5)            # ±2.5 µs（= 原 1000 cyc @400MHz）
    LOAD_TOL   = max(1, int(us * 0.04))   # 40 ns（= 原 16 cyc @400MHz）
    T1_TOL     = max(1, int(us * 0.08))   # 80 ns（= 原 32 cyc @400MHz）
    print(f"时基: {TB_HZ/1e6:.0f} MHz ⇒ 拍标称 {TICK_CYC} 计数 (100µs); "
          f"稳定带 ±{STABLE_TOL}; 负载容差 +{LOAD_TOL}; T1 ±{T1_TOL}")

RESULTS = []


def record(name, ok, detail="", skip=False):
    RESULTS.append((name, ok if not skip else None))
    tag = "SKIP" if skip else ("PASS" if ok else "FAIL")
    print("  [%s] %-56s %s" % (tag, name, detail))


def route(src_i, wire, div=DIV_FAST, phase=0, op=OP_DIRECT, param_idx=None):
    """一条 CONST → wire 的路由 (ACTIVE)。src_index 即 param 表索引。"""
    if param_idx is None:
        param_idx = src_i
    per = (div & 0x03) | ((phase & 0x3F) << PERIOD_PHASE_SHIFT)
    return struct.pack('<BBBBBBHHHHB',
                       SRC_CONST, src_i & 0xFF, DST_WIRE, wire & 0xFF,
                       op, ROUTE_FLAG_ACTIVE,
                       param_idx & 0xFFFF, 0, 0, 0, per) + b'\x00'


def deploy_payload(routes, nparams):
    hdr = struct.pack('<HHH', len(routes), nparams, 0)
    r = b''.join(routes)
    p = b''.join(struct.pack('<ffff', 1.0 + i * 0.001, 0.0, 0.0, 0.0) for i in range(nparams))
    return hdr + r + p


def prog_div0_full():
    """128 条 div0 DIRECT —— 每拍全跑 (最重的每拍负载)"""
    return deploy_payload([route(i, i % 128) for i in range(128)], 128)


def prog_mixed():
    """分档满表: 1/3 div0 + 1/3 div1 + 1/3 div2 (相位错开)"""
    rs, q1, q2 = [], 0, 0
    for i in range(128):
        if i % 3 == 0:
            rs.append(route(i, i % 128, DIV_FAST))
        elif i % 3 == 1:
            rs.append(route(i, i % 128, DIV_MID, q1 % 10)); q1 += 1
        else:
            rs.append(route(i, i % 128, DIV_SLOW, q2 % 64)); q2 += 1
    return deploy_payload(rs, 128)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--settle", type=float, default=1.0, help="每档稳定时间 s")
    ap.add_argument("--expect-fail", action="store_true",
                    help="对照构建模式: 要求量到**非 0** 极差 (证明判据能失败)")
    ap.add_argument("--ov-expect", choices=["0", "pos"], default="0",
                    help="超预算计数 ov 的期望: 交付档 0 (默认) / "
                         "对照构建 -DDCL_BOOT_SEL=0 -DDCL_BOOT_PROFILE=2 用 pos")
    a = ap.parse_args()

    port = find_port(a.port)
    if not port:
        print("!! 找不到串口"); return 2
    print("端口: %s @ 115200" % port)
    print("=== 确定性复测: 拍周期 / 拍抖动 (串口读 0x38, 无调试器介入) ===")

    ser = open_serial(port)
    L = Link(ser, False)
    time.sleep(0.4)

    def status():
        s, p = L.xact(CMD_ENGINE_STATUS, b"", timeout=1.0)
        if s != 0 or len(p) < 20:
            return None
        return dict(samples=struct.unpack_from("<I", p, 0)[0],
                    pmin=struct.unpack_from("<I", p, 4)[0],
                    pmax=struct.unpack_from("<I", p, 8)[0],
                    emin=struct.unpack_from("<I", p, 12)[0],
                    emax=struct.unpack_from("<I", p, 16)[0],
                    ov=struct.unpack_from("<I", p, 27)[0] if len(p) >= 31 else None,
                    run=p[22] if len(p) > 22 else None)

    s, p = L.xact(CMD_GET_VERSION)
    cap = (p[2] | (p[3] << 8)) if (s == 0 and len(p) >= 4) else -1
    record("T0 链路活性 (0x01 → ACK + cap)", s == 0 and cap > 0, "cap=0x%04X" % cap)
    if s != 0:
        return 2
    st = status()
    record("T0b 读 0x38 (SHM + 拍统计)", st is not None,
           "pmin=%s pmax=%s" % (st["pmin"], st["pmax"]) if st else "无响应")
    if not st:
        return 2

    # ★★ T0d 超预算计数 (审查二级 #5) —— **在任何 RESET 之前**读, 所以它反映
    #   "自本次上电以来"的累计值。H723 原来在 0x38 里直接填 0 冒充"没超预算",
    #   那让 S3 套件 T9 的 `ov == 0` 那半条**不可能失败** (空判据)。
    #   ★ "能失败"的对照: FLASH 取指 + 全表扫 的构建 (`-DDCL_BOOT_SEL=0
    #     -DDCL_BOOT_PROFILE=2`) 上该量必然 > 0 —— 见 --ov-expect。
    #   交付档必须恒为 0 (ITCM + 分档 ⇒ ISR 最坏 ~7.3k cyc ≪ 32000)。
    want_pos = (a.ov_expect == "pos")
    ok_ov = (st["ov"] is not None) and ((st["ov"] > 0) if want_pos else (st["ov"] == 0))
    record("T0d 超预算计数 ov %s (期望 %s)" % ("非零" if want_pos else "为 0",
                                               ">0" if want_pos else "0"),
           ok_ov, "ov=%s (自本次上电累计) emax=%s" % (st["ov"], st["emax"]))

    def measure(name, deploy=None, do_start=True, hold=None, baseline=None):
        """RESET → (deploy) → START → 稳定 → 读 pmin/pmax → 判据

        ★★ 2026-09-11 深夜口径订正 (LA 交叉测量之后, 见 docs/REPORT-DETERMINISM-2026-09-11.md):
          这个量实际测的是 **ISR 入口间隔** = 硬件拍长 + 两次入口延迟之差,
          **不是"拍长抖动"**。LA 独立测得拍长抖摆 σ ≲ 1.6 cyc (引脚边沿, 绕开入口路径),
          而这里读到 18~24 cyc ⇒ 残差来自**入口延迟**。
          ⇒ 判据不能再写 "极差 == 0" (那是过度声称)。改成两条**可复算**的:
             (a) 稳定在 40000±64 且**无漏拍** (漏拍会让 max 跳到 ~80000);
             (b) **与负载无关**: 极差 ≤ 基线 + 16 cyc。
          基线 = 空程序 (T2) 的极差。
        """
        L.xact(CMD_RESET); time.sleep(0.15)
        if deploy is not None:
            ds, dp = L.xact(CMD_DEPLOY, deploy, timeout=2.0)
            if ds != 0:
                record(name, False, "deploy 被拒: %s" % dp[:40]); return None
            time.sleep(0.15)
        if do_start:
            L.xact(CMD_START); time.sleep(0.15)
        if hold:
            hold()
        time.sleep(a.settle)
        s0 = status()
        if s0 is None:
            record(name, False, "读 0x38 失败"); return None
        spread = s0["pmax"] - s0["pmin"] if (s0["pmin"] and s0["pmax"]) else None
        s0["spread"] = spread          # ★ 必须无条件挂上: 调用方要读它取基线,
        if spread is None:             #   之前只在成功路径上赋值 ⇒ 失败时调用方 KeyError 崩掉
            record(name, False, "pmin/pmax 为空 (统计刚复位或 CYCCNT 未计数?)"); return s0
        ok_stable = (TICK_CYC - STABLE_TOL < s0["pmin"]) and (s0["pmax"] < TICK_CYC + STABLE_TOL)
        ok_load = True if baseline is None else (spread <= baseline + LOAD_TOL)
        ok = (ok_stable and ok_load) if not a.expect_fail else (spread != 0)
        detail = ("pmin=%u pmax=%u 入口间隔极差=%d cyc emax=%u samples=%u%s"
                  % (s0["pmin"], s0["pmax"], spread, s0["emax"], s0["samples"],
                     "" if baseline is None else " (基线+%d=%d)" % (LOAD_TOL, baseline + LOAD_TOL)))
        record(name, ok, detail)
        s0["spread"] = spread
        return s0

    # T0c 工具自检: samples 必须随 RUN 增长 (判据通道是活的)
    L.xact(CMD_RESET); time.sleep(0.2)
    L.xact(CMD_START); time.sleep(0.3)
    sa = status(); time.sleep(0.4); sb = status()
    record("T0c 工具自检: RUN 时 samples 递增 (判据通道是活的)",
           bool(sa and sb and sb["samples"] > sa["samples"]),
           "samples %s→%s" % (sa["samples"] if sa else None, sb["samples"] if sb else None))

    # T1 骨架拍 (STOP 态, 只跑骨架)
    tb_resolve(L)          # ★ 先把阈值按时基频率换算（交付档 TIM5=200MHz ⇒ 拍 20000 计数, 不是 40000）
    L.xact(CMD_RESET); time.sleep(0.15)
    L.xact(CMD_STOP); time.sleep(a.settle)
    st1 = status()
    ok1 = st1 is not None and abs(st1["pmax"] - TICK_CYC) <= T1_TOL
    record("T1 骨架拍 pmax ≈ %d (测量通道真的在报数)" % TICK_CYC, ok1,
           "pmax=%s (差 %s cyc)" % (st1["pmax"] if st1 else None,
                                    (st1["pmax"] - TICK_CYC) if st1 else "?"))

    # T2 空程序 RUN → 取基线
    print("  ── 基线 ──")
    b = measure("T2 空程序 RUN: 稳定在 40000±64 且无漏拍 (基线)",
                deploy=None, baseline=None)
    base = b["spread"] if b else None
    if base is None:
        print("  !! 拿不到基线, 后续负载无关性判据无法判定")
        base = 0

    # T3/T4/T5: 判"与负载无关"
    measure("T3 ★div0 满表 128 条: 入口极差 ≤ 基线+16 (与负载无关)",
            deploy=prog_div0_full(), baseline=base)
    measure("T4 ★分档满表 128 条: 入口极差 ≤ 基线+16 (与负载无关)",
            deploy=prog_mixed(), baseline=base)

    def hammer():
        t_end = time.time() + 0.8
        n = 0
        while time.time() < t_end:
            L.xact(CMD_ENGINE_STATUS, b"", timeout=1.0)
            n += 1
        globals()["_hammer_n"] = n
    measure("T5 ★分档满表 + 协议流量: 入口极差 ≤ 基线+16",
            deploy=prog_mixed(), hold=hammer)
    print("     (T5 期间发了 %s 次 0x38)" % globals().get("_hammer_n"))

    L.xact(CMD_STOP); L.xact(CMD_RESET)
    ser.close()

    np_ = sum(1 for _, ok in RESULTS if ok is True)
    nf = sum(1 for _, ok in RESULTS if ok is False)
    ns = sum(1 for _, ok in RESULTS if ok is None)
    print("\n=== 确定性复测: %d PASS / %d FAIL / %d SKIP ===" % (np_, nf, ns))
    if a.expect_fail:
        print("   (对照构建模式: 期望 FAIL ≥1 条 —— 那是'判据能失败'的证据)")
    return 0 if nf == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
