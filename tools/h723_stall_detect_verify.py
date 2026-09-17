#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_stall_detect_verify.py —— 实机验证 `examples/h723_step_stall_recover.dcl`
（**闭环失步自动检测 + 恢复正常转动**）

## 被测对象怎么造
阶段 2 已量化：**从静止"突加"到 >16500 Hz 必失速**（拐点 16500~16750，`tools/h723_stall_edge.py`）
⇒ 本工具就注入 **18000 Hz 突加**：轴不转、持续通电流 ⇒ 检测器应当发现并把它救回转动。

## 判据（每条都能失败；★ 每条都写了"它会怎么红"）
前置
  P1 引擎 RUN（`0x38 r[22]==1`）
  P2 运动源 = 程序面（`sub=13` 读回 == 1）—— 否则程序写的 `wire[12]` 没人读
  P3 编码器反馈活着（两次读不同值；否则本判据**无效**不是通过）
  P4 命令 0 时 `stall==0 且 nfail==0`（基线干净）
正题
  T1 注入 18000 突加 ⇒ `wire[57] stall` 必须在 3 s 内变 **1**   ← 红法: 检测器不响应
  T2 `wire[58] nfail` ≥ 1                                      ← 红法: 没记到失步次数
  T3 `wire[59] der` == 0.25                                    ← 红法: 没降额(会继续堵转)
  T4 `wire[60] hz` ≈ 0.25×18000 = 4500（±10%）                 ← 红法: 下发频率没跟着降
  T5 ★ **恢复正常转动**：末窗 1 s 内编码器累积转角 ≥ 30% 理论值  ← 红法: 停了不动=没救回来
反向
  R 把请求改成 4000 Hz（**低于拉起拐点**）⇒ `stall` 必须保持 **0**
     ← 红法: 若它也报失步, 说明判据是"只要不在动就报"的假判据

★ 收尾（finally，判据 FAIL 也要执行）：请求清 0 + 失能 + `sub=13 arg=0` 切回脚手架

用法: python tools/h723_stall_detect_verify.py [--port COM22] [--keep]
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from h723_client import Dcl, find_board, engine_status, read_wires   # noqa: E402

CMD_READ, CMD_WRITE = 0x20, 0x21
OFF_WIRE_MAP = None          # 运行时从 h723_client 取
HZ_DEMAND_SLOT = 11          # 主机请求频率 Hz      (程序里 wire[11])
ENA_DEMAND_SLOT = 10         # 主机请求使能          (程序里 wire[10])
W_DAB, W_STALL, W_NFAIL, W_DER, W_HZ = 56, 57, 58, 59, 60   # 程序的观测槽
SPR = 1600.0
CPR = 4096.0
HZ_STALL = 18000.0           # 突加注入（> 拉起拐点 16500~16750）
HZ_SAFE = 4000.0             # 反向判据用的安全频率（远低于拐点）
_t = 0


def rec(ok, name, detail=""):
    global _t
    _t += 1
    print("  [%s] %-44s %s" % ("PASS" if ok else "FAIL", name, detail))
    return ok


def main():
    global OFF_WIRE_MAP
    import h723_client as HC
    OFF_WIRE_MAP = HC.OFF_WIRE_MAP

    port = None
    if "--port" in sys.argv:
        port = sys.argv[sys.argv.index("--port") + 1]
    keep = "--keep" in sys.argv
    d = Dcl(port or find_board())
    es = engine_status(d)
    shm = es["shm"]
    print("=== 失步自动检测 · 实机验证 (shm=0x%08X) ===" % shm)

    def w_snap():
        """★ 一次读 61 个 WIRE 槽 = **单帧快照** ⇒ 各量同一时刻(而不是各读各的、时刻错开)。"""
        return read_wires(d, shm, W_HZ + 1)

    def wset(n, v):
        return d.send(CMD_WRITE, struct.pack("<II", shm + OFF_WIRE_MAP + n * 4,
                                             struct.unpack("<I", struct.pack("<f", v))[0]))[0] == "ACK"

    def src_now():
        """★ `sub=13` 是**只写**的(没有应答载荷) ⇒ 读运动源必须走**只读的 `sub=14 +0`**。
        ★ 第一版拿 `sub=13 arg=0` 去"读回" ⇒ 那实际是"把运动源设成脚手架" —— 一条**自毁**判据。"""
        st, p = d.send(0x39, bytes([19, 14]) + struct.pack("<I", 0))
        if st != "ACK" or len(p) < 32:
            return None
        return struct.unpack("<8I", p[:32])

    def raw():
        v = HC.read_sensors(d, shm, 2)
        return None if v is None else int(round(v[0]))

    def sweep(secs):
        """在 secs 秒内按最短弧累积编码器转角(counts)"""
        t0 = time.time()
        acc, prev, mx = 0.0, raw(), 0.0
        n = 0
        while time.time() - t0 < secs:
            time.sleep(0.02)
            v = raw()
            if v is None:
                continue
            dv = (v - prev) & 0xFFF
            if dv > 2048:
                dv -= 4096
            acc += dv
            mx = max(mx, abs(dv))
            prev = v
            n += 1
        return acc, n, mx

    ok = True
    try:
        # ── 前置 ──
        ok &= rec(es["run"] == 1, "P1 引擎 RUN", "run=%d" % es["run"])
        # ★★ 必须**先显式切到程序面**：`dclc.py` 部署时会发 `0x13 RESET`，而 RESET 会把
        #    运动源复位回脚手架(0) ⇒ 不切的话程序写的 `wire[12]` 根本没人读，
        #    现象是"程序在跑、轴一动不动"。
        d.send(0x39, bytes([19, 13]) + struct.pack("<I", 1))
        time.sleep(0.3)
        r14 = src_now()
        src = r14[0] if r14 else None
        ok &= rec(src == 1, "P2 运动源 = 程序面(读 sub=14 +0)", "src=%s" % src)
        a0, n0, _ = sweep(0.4)
        ok &= rec(n0 >= 10, "P3 编码器反馈活着", "0.4s 采到 %d 次" % n0)
        w = w_snap()
        print("  基线: stall=%.0f nfail=%.0f der=%.2f hz=%.0f"
              % (w[W_STALL], w[W_NFAIL], w[W_DER], w[W_HZ]))
        ok &= rec(w[W_STALL] == 0 and w[W_NFAIL] == 0, "P4 基线干净（stall/nfail 为 0）")

        # ── 反向判据 R：安全频率下**不该**报失步 ──
        wset(ENA_DEMAND_SLOT, 1.0)
        wset(HZ_DEMAND_SLOT, HZ_SAFE)
        time.sleep(2.0)                       # 超过遮蔽窗 900ms + 去抖 200ms
        w = w_snap()
        r_acc, _, _ = sweep(0.5)
        print("  R@%dHz: stall=%.0f hz=%.0f dab=%.4f 半秒累积=%.0f counts"
              % (HZ_SAFE, w[W_STALL], w[W_HZ], w[W_DAB], r_acc))
        ok &= rec(w[W_STALL] == 0, "R 安全频率下**不报**失步（不该红的不红）",
                  "stall=%.0f" % w[W_STALL])
        ok &= rec(abs(r_acc) > 100, "R 安全频率下确实在转", "%.0f counts" % r_acc)
        ok &= rec(abs(w[W_HZ] - HZ_SAFE) < 200, "R 下发频率 ≈ 请求", "%.0f" % w[W_HZ])

        # ── 正题：注入突加失速 ──
        # ★★★ 注入条件必须是"**从静止起转**"：阶段 2 的决定性对照证明，
        #   已在转动时突跳到 30 kHz 也**不会**失速（那是 pull-out，不是 pull-in）。
        #   第一版直接从 4000 → 18000 ⇒ 电机跑得好好的（实测比值 1.03）⇒ 判据当然不触发。
        #   ⇒ 先回 0 停住（这一步同时经 `F_TRIG(running)` 把 nfail 清零），再从静止突加。
        print("\n  先停住（为「从静止起转」造条件）...")
        wset(HZ_DEMAND_SLOT, 0.0)
        time.sleep(1.5)
        w = w_snap()
        print("  停住后: stall=%.0f hz=%.0f dab=%.0f  (nfail 被 F_TRIG 清零 ⇒ %.0f)"
              % (w[W_STALL], w[W_HZ], w[W_DAB], w[W_NFAIL]))
        print("  注入突加 %.0f Hz（> 拉起拐点 16500~16750）..." % HZ_STALL)
        wset(HZ_DEMAND_SLOT, HZ_STALL)
        t0 = time.time()
        fired, samples = 0.0, []
        while time.time() - t0 < 3.0:
            w = w_snap()
            samples.append((time.time() - t0, w[W_STALL], w[W_NFAIL], w[W_DER],
                            w[W_HZ], w[W_DAB]))
            if w[W_STALL] == 1.0 and fired == 0.0:
                fired = time.time() - t0
                break
            time.sleep(0.05)
        print("  首 5 个采样 (t, stall, nfail, der, hz, dab):")
        for smp in samples[:5]:
            print("    %.2fs stall=%.0f nfail=%.0f der=%.2f hz=%.0f dab=%.4f" % smp)
        ok &= rec(fired > 0.0, "T1 检测到失速（stall 变 1）",
                  "在 %.2f s 后触发" % fired if fired else "3 s 内没触发")

        # T1 触发后给恢复动作留时间（断流 150ms + 重发 + 爬起）
        time.sleep(2.5)
        w = w_snap()
        exp = HZ_STALL * 0.25
        print("  恢复后: nfail=%.0f der=%.2f hz=%.0f (期望 %.0f)"
              % (w[W_NFAIL], w[W_DER], w[W_HZ], exp))
        ok &= rec(w[W_NFAIL] >= 1, "T2 失步次数已记", "nfail=%.0f" % w[W_NFAIL])
        ok &= rec(abs(w[W_DER] - 0.25) < 0.01, "T3 已降额", "der=%.2f" % w[W_DER])
        ok &= rec(abs(w[W_HZ] - exp) / exp < 0.10, "T4 下发频率 = 降额后", "hz=%.0f" % w[W_HZ])

        # ── T5 ★ 恢复正常转动 ──
        a5, n5, mx5 = sweep(1.0)
        hz = w[W_HZ]
        want = hz / SPR * CPR          # 1 s 应有 counts
        print("  末窗 1 s: 实测累积 %.0f counts / 理论 %.0f (降额后 %.0f Hz)" % (a5, want, hz))
        ok &= rec(abs(a5) >= 0.30 * want, "T5 ★ 轴已恢复转动",
                  "%.0f counts (阈值 %.0f)" % (a5, 0.30 * want))
    finally:
        if not keep:
            try:
                wset(HZ_DEMAND_SLOT, 0.0)
                wset(ENA_DEMAND_SLOT, 0.0)
                d.send(0x39, bytes([19, 13]) + struct.pack("<I", 0))      # 切回脚手架直控
                d.send(0x39, bytes([19, 3]) + struct.pack("<I", 0))       # 失能
                print("\n  已收尾: 请求清 0 / 失能 / sub=13 切回脚手架")
            except Exception as e:
                print("\n  ⚠ 收尾异常: %s" % e)
        d.close()
    print("=== %s ===" % ("全部通过" if ok else "有 FAIL —— 见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
