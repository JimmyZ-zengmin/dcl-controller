#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_demo_e2e.py — **端到端**验收: 文本组态 → 编译 → 静态校验 → deploy → 引擎 → 读回

★ 为什么这一层必须有独立的验收 (2026-09-11 补):
  在此之前 H723 线的 tools/ 全是**底层探针** (pyocd 读内存 / 手搓协议帧)。
  协议层、引擎层各自都有套件, 但**"人写的文本程序能不能真的让这台控制器动起来"**
  这条端到端链路**一次都没跑过** —— 而 README 已经把"DSL 组态语言"列为强项。
  ⇒ 那是典型的"宣称 > 实现"。本工具把这条链路钉成可复跑的判据。

★ 它调用的是**真实的 `tools/dclc.py`** (子进程), 不是在本脚本里重实现一遍编译规则 ——
  否则测的是"我抄的那份", 不是交付给用户的那份。

★ 判据设计原则 (照本项目铁律):
  · 每条判据都要**能失败**。B 组用 CONST 驱动的确定性程序 → 输出是唯一确定值, 可逐条断言;
    "看起来在工作"不算证据。
  · 需要外部接线的判据**自动判别接线状态**: 没接线记 SKIP (与 PASS/FAIL 分开计数),
    不把"没做实验"混进"实验通过"。

用法:
    python tools/h723_demo_e2e.py               # 自动找 CH340
    python tools/h723_demo_e2e.py --port COM14
"""
import argparse
import os
import struct
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import serial

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
from h723_client import Dcl, engine_status, OFF_SENSOR_MAP, OFF_WIRE_MAP

RESULTS = []
SKIPS = []


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))


def skip(name, why):
    SKIPS.append((name, why))
    print("  [SKIP] %s — %s" % (name, why))


class Session:
    """持有 Dcl, 但允许在跑子进程 (dclc.py) 期间**释放串口**。

    ★★★ 为什么必须这样 (2026-09-11 本工具第一版就踩了, 且是**项目已记录过**的坑):
      Windows 的 COM 口是**独占**的。本工具自己开着 COM14, 又 fork 出 `dclc.py`
      去下发 —— 子进程打不开口, 报 `PermissionError(13, 拒绝访问)`。
      这属于"**我自己的测试成了故障源**"那一族 (M4 审计同款), 症状看起来像"板子/工具坏了"。
      ⇒ 规则: **同一个串口在同一时刻只能有一个持有者。** 跑子进程前释放, 回来后重开。
    """

    def __init__(self, port):
        self.port = port
        self.d = Dcl(port)

    def release(self):
        if self.d is not None:
            self.d.close()
            self.d = None

    def reopen(self):
        if self.d is None:
            self.d = Dcl(self.port)
        return self.d


def compile_deploy(sess, dcl_file):
    """调**真实编译器**编译并下发 (它内部做 RESET → deploy → [0x44] → START)。

    ★ 释放串口 → 子进程 → 重开: 见 Session 的说明 (Windows COM 独占)。
    """
    sess.release()
    try:
        cmd = [sys.executable, os.path.join(ROOT, "tools", "dclc.py"), dcl_file]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=120)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    finally:
        sess.reopen()


def read_words(d, shm, start_word, n):
    """0x22 burst 读 SHM 里连续 n 个 float"""
    sts, p = d.send(0x22, struct.pack("<IH", shm + start_word * 4, n))
    if sts != "ACK" or len(p) < n * 4:
        return None
    return list(struct.unpack("<%df" % n, p[:n * 4]))


def write_word32(d, addr, bits):
    """0x21 写一个 32bit 字 (float 用 struct 转 bits)"""
    return d.send(0x21, struct.pack("<II", addr, bits & 0xFFFFFFFF))[0]


def write_f32(d, addr, val):
    return write_word32(d, addr, struct.unpack("<I", struct.pack("<f", val))[0])


# ══════════════════════════ A: 顺序域 ══════════════════════════
def suite_seq(sess, files):
    print("\n── A: 顺序域 (SFC 级) ──")
    ok, out = compile_deploy(sess, files["seq"])
    d = sess.d          # ★ 子进程期间释放过串口, 这里取重开后的句柄
    last = [l for l in out.splitlines() if l.strip()][-1] if out else ""
    record("A0 编译 + deploy + 0x44 SEQ_DEPLOY + START 全链路 ACK", ok, last[:90])
    if not ok:
        return
    shm = engine_status(d)["shm"]

    trans, prev = [], None
    t0 = time.time()
    while time.time() - t0 < 6.5:
        v = read_words(d, shm, OFF_WIRE_MAP // 4 + 30, 2)
        if not v:
            continue
        st, act = v
        if prev is None or st != prev:
            trans.append((time.time() - t0, st, act))
            prev = st
    if len(trans) < 5:
        record("A1 步号在推进 (≥5 次跳变)", False, "只采到 %d 次跳变" % len(trans))
        return

    steps = sorted({s for _, s, _ in trans})
    record("A1 四个步号都到齐", steps == [1.0, 2.0, 3.0, 4.0], "实测步号集合=%s" % steps)

    bad = [(s, a) for _, s, a in trans if (a == 1.0) != (s == 4.0)]
    record("A2 译码正确: act=1 当且仅当 步号=4 (GE 3.5 路由)", not bad,
           "违例 %d 处 %s" % (len(bad), bad[:3]))

    # ★ 丢掉**第一段**再判读: 采样起点落在某个停留步中间 ⇒ 首段是被截断的部分段,
    #   拿它去比"必须 = 1.000s"会得到一个假失败 (第一版就这么失败过, 实测 0.657s)。
    #   这是**测量方法**的缺陷, 不是固件缺陷 —— 判据必须能区分这两者。
    durs = [(trans[i + 1][0] - trans[i][0], trans[i][1]) for i in range(1, len(trans) - 1)]
    dwell = [x for x, s in durs if s in (2.0, 4.0)]
    inst = [x for x, s in durs if s in (1.0, 3.0)]
    # ★ 判据口径 (2026-09-11 加固): 原实现要求**每一段**都落在 ±60ms 内。
    #   采样是 PC 侧 0x22 往返, Windows 上单次可卡顿几十 ms ⇒ 会产生**偶发假失败**
    #   (实测: 一套 12 套件的回归里偶发 1 次)。改成**中位数 + 多数合格**:
    #   中位数对单个卡顿样本免疫, 而"档桶配错"会让**全部**段一起偏 ⇒ 照样抓得住。
    med = sorted(dwell)[len(dwell) // 2] if dwell else 0.0
    good = sum(1 for x in dwell if 0.94 <= x <= 1.06)
    ok_dwell = len(dwell) >= 2 and (0.97 <= med <= 1.03) and good >= max(2, len(dwell) - 1)
    record("A3 DWELL 步停留 1s (中位数 %.3f, %d/%d 段合格)" % (med, good, len(dwell)),
           ok_dwell, "各段 %s" % ["%.3f" % x for x in dwell[:5]])
    # UNTIL 立真的瞬时步只占一个 10ms 档桶 —— 但**不能按 8~13ms 卡**:
    #   采样是 PC 侧 0x22 往返 (Windows 上单次可抖动到几十 ms), 段时长会被测量误差抬高
    #   ⇒ 用 8ms 当上界会得到一个**会随机失败**的判据 (第一版实测就这样, 3 次里偶发 1 次 FAIL)。
    #   严格计时由 A3/A5 承担; A4 只做一个**抗抖动**的存在性判断:
    #   "瞬时步显著短于停留步" ⇒ 证明两类步在同一实例内被正确区分 (而不是都跑成 1s 或都瞬过)。
    ok_inst = bool(inst) and bool(dwell) and all(x <= 0.20 for x in inst) and min(inst) < 0.25 * min(dwell)
    record("A4 瞬时步显著短于停留步 (两类步被区分开)", ok_inst,
           "瞬时步 %s vs 停留步 %s" % (["%.3f" % x for x in inst[:3]],
                                        ["%.3f" % x for x in dwell[:3]]))

    t1s = [t for t, s, _ in trans if s == 1.0]
    per = [(t1s[i + 1] - t1s[i]) for i in range(len(t1s) - 1)] if len(t1s) > 1 else []
    # ★ 同上: 用中位数而不是"全部落在窗口内" —— 单次采样卡顿不应判固件不合格。
    pmed = sorted(per)[len(per) // 2] if per else 0.0
    ok_per = bool(per) and (1.98 <= pmed <= 2.07)
    record("A5 整周期 ≈ 2.02s (中位数 %.3f)" % pmed, ok_per,
           "各周期 %s (期望 2.02, 判中位数落在 ±0.05)" % ["%.3f" % x for x in per])

    d.send(0x12)                     # STOP
    time.sleep(0.35)
    a = read_words(d, shm, OFF_WIRE_MAP // 4 + 30, 1)
    time.sleep(0.35)
    b = read_words(d, shm, OFF_WIRE_MAP // 4 + 30, 1)
    record("A6 STOP 后步号冻结 (停机安全态)", a and b and a[0] == b[0],
           "STOP 后两次读: %s / %s" % (a, b))


# ══════════════════════════ B: 逻辑 / 算术域 ══════════════════════════
EXPECT_B = {40: 16.0, 41: 8.0, 42: 48.0, 43: 3.0, 44: 12.0, 45: 4.0,
            46: 4.0, 47: 1.0, 48: 0.0, 49: 1.0, 50: 10.0,
            51: 1.0, 52: 0.0, 53: 1.0, 54: 0.0}


def suite_logic(sess, files):
    print("\n── B: 逻辑域 + 标准块库 (CONST 驱动 ⇒ 输出唯一确定) ──")
    ok, out = compile_deploy(sess, files["logic"])
    d = sess.d          # ★ 子进程期间释放过串口, 这里取重开后的句柄
    last = [l for l in out.splitlines() if l.strip()][-1] if out else ""
    record("B0 编译 + deploy + START 全链路 ACK", ok, last[:90])
    if not ok:
        return
    shm = engine_status(d)["shm"]
    time.sleep(0.4)

    def snap():
        return read_words(d, shm, OFF_WIRE_MAP // 4 + 40, 15)

    v1 = snap()
    time.sleep(0.5)
    v2 = snap()
    if not v1 or not v2:
        record("B1 读回 15 个输出钉子", False, "burst 读失败")
        return

    bad = []
    for i, (wi, exp) in enumerate(sorted(EXPECT_B.items())):
        got = v1[i]
        if abs(got - exp) > 1e-3:
            bad.append("wire[%d]=%.3f≠%.3f" % (wi, got, exp))
    record("B1 15 条期望值逐条命中", not bad,
           "全部命中" if not bad else "不符: " + "; ".join(bad))

    drift = [abs(a - b) for a, b in zip(v1, v2)]
    record("B2 0.5s 后二次读无漂移 (确定性)", max(drift) < 1e-3,
           "最大漂移 %.5f" % max(drift))


# ══════════════════════════ C: 连续域 PID ══════════════════════════
def suite_pid(sess, files):
    print("\n── C: 连续域 PID (试验信号注入 sensor[0], 无需被控对象) ──")
    ok, out = compile_deploy(sess, files["pid"])
    d = sess.d          # ★ 子进程期间释放过串口, 这里取重开后的句柄
    last = [l for l in out.splitlines() if l.strip()][-1] if out else ""
    record("C0 编译 + deploy + START 全链路 ACK", ok, last[:90])
    if not ok:
        return
    shm = engine_status(d)["shm"]
    a_fb = shm + OFF_SENSOR_MAP          # sensor[0]

    def set_fb(v):
        write_f32(d, a_fb, v)

    def u():
        w = read_words(d, shm, OFF_WIRE_MAP // 4 + 20, 1)
        return w[0] if w else None

    def fmt(x):
        return "N/A" if x is None else "%.2f" % x

    KI, SP = 0.5, 50.0
    seen = []

    # ---- 上升段: err=+50 ⇒ 速率应为 KI*50 = 25 /s ----
    set_fb(0.0)
    time.sleep(0.4)
    u1, t1 = u(), time.time()
    time.sleep(1.0)
    u2, t2 = u(), time.time()
    seen += [("+err@t1", u1), ("+err@t2", u2)]
    rate_up = (u2 - u1) / (t2 - t1) if (u1 is not None and u2 is not None) else None
    ok_rate = rate_up is not None and abs(rate_up - KI * (SP - 0.0)) <= 0.4 * KI * SP
    record("C1 上升速率 ≈ KI·err = %.1f /s (实测 %s)"
           % (KI * SP, ("%.2f /s" % rate_up) if rate_up is not None else "N/A"),
           ok_rate, "u %s → %s (Δ=%.2f)" % (fmt(u1), fmt(u2), (u2 - u1) if u1 is not None else 0))

    # ---- 反向段: err=-40 ⇒ 速率应为 KI*(-40) = -20 /s ----
    set_fb(90.0)
    time.sleep(0.4)
    u3 = u()
    time.sleep(0.6)
    u4 = u()
    seen += [("-err@t3", u3), ("-err@t4", u4)]
    rate_dn = (u4 - u3) / 0.6 if (u3 is not None and u4 is not None) else None
    ok_dn = rate_dn is not None and rate_dn < -0.5 * KI * abs(SP - 90.0)
    record("C2 误差翻负后输出下降 (速率 %s /s, 期望约 -%.1f)"
           % (("%.2f" % rate_dn) if rate_dn is not None else "N/A", KI * 40),
           ok_dn, "u %s → %s" % (fmt(u3), fmt(u4)))
    record("C3 方向性: 正误差段输出 > 负误差段输出",
           (u2 is not None and u4 is not None and u2 > u4),
           "%s vs %s" % (fmt(u2), fmt(u4)))

    # ---- 长时间正误差 ⇒ 必须顶到 100 且不越界 ----
    set_fb(0.0)
    time.sleep(6.0)                      # 25/s × 6s = 150 理论 ⇒ 必然饱和
    u_sat = u()
    seen += [("饱和@fb=0", u_sat)]
    record("C4 长时间正误差 → 输出顶到上限 100 且不越界",
           u_sat is not None and 99.5 <= u_sat <= 100.0,
           "u=%s (理论 150, 限幅应夹在 100)" % fmt(u_sat))

    # ---- 抗积分饱和: 饱和后把误差翻负, 输出必须**立刻**离开 100 ----
    #   若积分曾无限累积 (无 anti-windup), 积分值会是 ~150 ⇒ 反向 0.5s 只退 ~12.5
    #   ⇒ u 仍被夹在 100; 有条件积分冻结时, 积分值就是 100 ⇒ 反向 0.5s 后 ≈ 87.5。
    #   ⇒ 这一条**能区分**"有 anti-windup"与"没有", 不是形式上的检查。
    set_fb(100.0)
    time.sleep(0.5)
    u_aw = u()
    seen += [("反向后0.5s", u_aw)]
    record("C5 抗积分饱和: 误差翻负 0.5s 内输出必须离开 100",
           u_aw is not None and u_aw < 99.0,
           "u=%s (期望 ≈87.5; 若仍为 100 说明积分在饱和期无限累积)" % fmt(u_aw))

    allv = [x for _, x in seen if x is not None]
    record("C6 全程限幅有效 (0 ≤ duty ≤ 100)", allv and all(0.0 <= x <= 100.0 for x in allv),
           "样本: " + ", ".join("%s=%s" % (k, fmt(v)) for k, v in seen))


# ══════════════════════════ D: 数字量输入域 ══════════════════════════
def suite_di(sess, files):
    print("\n── D: 数字量输入域 (DI) ──")
    ok, out = compile_deploy(sess, files["di"])
    d = sess.d          # ★ 子进程期间释放过串口, 这里取重开后的句柄
    last = [l for l in out.splitlines() if l.strip()][-1] if out else ""
    record("D0 编译 + deploy + START 全链路 ACK", ok, last[:90])
    if not ok:
        return
    shm = engine_status(d)["shm"]
    time.sleep(0.4)

    di1 = read_words(d, shm, OFF_SENSOR_MAP // 4 + 3, 4)          # sensor[3..6]
    time.sleep(0.35)
    di2 = read_words(d, shm, OFF_SENSOR_MAP // 4 + 3, 4)

    # ★★ 判据口径 (第一版写错了, 记录在此防复发): 第一版要求"四条 DI 空闲都必须 = 1.0"。
    #   那是**假定了接线状态**: 本项目工位就是把 PC1 拉了 GND (作低有效输入用) ⇒ sensor[4]=0。
    #   于是判据把"预期的接线"报成了"固件缺口"。⇒ 正确口径是
    #     ① 值域合法 (∈{0,1}) 且两次读一致 ⇒ 证明 DI 真在扫描 + 30ms 去抖后稳定;
    #     ② 下游译码按**实测的** DI 值推算期望, 而不是按"我以为的接线" ⇒ 与接线无关。
    valid = (di1 is not None and di2 is not None
             and all(x in (0.0, 1.0) for x in di1 + di2) and di1 == di2)
    record("D1 DI 在扫描: sensor[3..6] 值域{0,1} 且两次读一致", valid,
           "读回 %s (PC0..PC3; 非 1.0 说明该脚被外部拉低, 属接线事实不是故障)"
           % ([int(x) for x in di1] if di1 else None))
    if not valid:
        return

    b1 = 1.0 - di1[0]                     # btn1 = NOT di1 (低有效解读)
    b2 = 1.0 - di1[1]                     # btn2 = NOT di2
    exp_lamp = 1.0 if (b1 > 0.5 and b2 > 0.5) else 0.0
    w = read_words(d, shm, OFF_WIRE_MAP // 4 + 30, 3)             # wire[30..32]
    got_lamp = w[0] if w else None
    record("D2 译码与实测 DI 一致: lamp = (NOT di1) AND (NOT di2)", 
           got_lamp is not None and abs(got_lamp - exp_lamp) < 1e-3,
           "di1=%d di2=%d ⇒ 期望 lamp=%.0f, 实测 %.0f" % (di1[0], di1[1], exp_lamp,
                                                          got_lamp if got_lamp is not None else -1))

    # 计数器/边沿需要**物理上制造上升沿** (PC0 接地↔悬空来回), 无法自动完成
    skip("D3 外部边沿计数 (R_TRIG→CTU→alarm)",
         "需人工把 PC0 在 GND/悬空之间切换 ≥5 次; 期望 wire[31] 递增、wire[32] 在第 5 次后变 1。"
         " (CTU 本身的语义已由 B 组用 CONST 驱动验证过)")


# ══════════════════════════ main ══════════════════════════
FILES = {
    "seq":   "examples/h723_seq_demo.dcl",
    "logic": "examples/h723_logic_demo.dcl",
    "pid":   "examples/h723_pid_demo.dcl",
    "di":    "examples/h723_di_demo.dcl",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    a = ap.parse_args()

    for k, f in FILES.items():
        if not os.path.exists(os.path.join(ROOT, f)):
            print("!! 缺示例文件: %s" % f); return 2

    print("=" * 74)
    print("H723 端到端验收: 文本组态(.dcl) → 编译 → 静态校验 → deploy → 引擎 → 读回")
    print("=" * 74)
    sess = Session(a.port)
    st = engine_status(sess.d)
    if not st:
        print("!! 引擎无响应 (%s)" % sess.d.port); return 2
    print("端口=%s  shm=0x%08X  run=%d" % (sess.d.port, st["shm"], st["run"]))

    try:
        suite_seq(sess, FILES)
        suite_logic(sess, FILES)
        suite_pid(sess, FILES)
        suite_di(sess, FILES)
    finally:
        sess.d.send(0x12)
        sess.d.close()

    npass = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n=== 结果汇总 ===")
    for nm, ok, _ in RESULTS:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", nm))
    for nm, why in SKIPS:
        print("  [SKIP] %s — %s" % (nm, why))
    print("\n%d PASS / %d FAIL / %d SKIP  (共 %d 项判据)"
          % (npass, len(RESULTS) - npass, len(SKIPS), len(RESULTS) + len(SKIPS)))
    return 0 if npass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
