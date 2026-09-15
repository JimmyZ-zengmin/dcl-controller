#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_as5600_check.py — 经 SWD 读回固件里的 AS5600 探针结果并解码

为什么这么绕：工程里原本没有任何 I2C 代码，串口(CH340)又时常不在。
⇒ 固件里跑一个位操作 I2C 探针（每 200ms 一轮，扫 4 组候选引脚对），结果全落在全局量里，
  PC 端**直接读内存**取回。地址从 `.map` 解析，不写死。

判读规则
--------
① `下拉IDR`（内部 40k 下拉下仍读到高）⇒ 该对引脚有**外部强上拉**（有线 + 那侧已供电）
   ★ 但它**不能证明是 AS5600** —— 必须配 ②
② `写ACK` = 1 ⇒ 地址 0x36 被应答 ⇒ **器件确实在那两根线上**
③ `RAW` ∈ 0..4095 ⇒ 协议级连通，且这就是当前角度（×360/4096 得度数）
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PAIRS = ["(PB10 SCL, PB11 SDA)  ← 原计划接线",
         "(PB11 SCL, PB10 SDA)  ← 接反",
         "(PB6  SCL, PB7  SDA)",
         "(PB8  SCL, PB9  SDA)"]


def sym_addr(name, arr=False):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    txt = open(os.path.join(root, "build", "dcl_h723.map"), encoding="utf-8", errors="replace").read()
    m = re.search(r"0x([0-9a-f]{16})\s+" + re.escape(name) + r"\b", txt)
    return int(m.group(1), 16) if m else None


def watch(secs=6.0):
    """连续看角度 —— 用手转轴, 看数值是否跟着变 (功能性验证)"""
    import time
    from pyocd.core.helpers import ConnectHelper
    a = sym_addr("g_as_raw")
    if a is None:
        print("✗ 找不到 g_as_raw"); return
    sess = ConnectHelper.session_with_chosen_probe(
        target_override="stm32h723xx",
        options={"connect_mode": "halt", "frequency": 2000000}, blocking=False)
    if sess is None:
        print("✗ 找不到探针"); return
    with sess:
        t = sess.target
        print("连续采样 %ds —— 转一下轴:" % int(secs))
        t0 = time.time()
        prev = None
        while time.time() - t0 < secs:
            t.halt(); v = t.read32(a); t.resume()
            if v <= 4095 and v != prev:
                print("   %6d  (%.1f°)" % (v, v * 360.0 / 4096.0))
                prev = v
            time.sleep(0.15)


def main():
    if "--watch" in sys.argv:
        watch(float(sys.argv[sys.argv.index("--watch") + 1]) if len(sys.argv) > sys.argv.index("--watch") + 1 else 6.0)
        return
    names = ["g_as_n", "g_as_idr_float", "g_as_idr_pu", "g_as_idr_pd", "g_as_mag",
             "g_as_drive_lo", "g_as_drive_hi"]
    base = {n: sym_addr(n) for n in names}
    for n in ("g_as_ack", "g_as_idrpd", "g_as_raw"):
        base[n] = sym_addr(n)
    if any(v is None for v in base.values()):
        print("✗ .map 里找不到探针全局量 ⇒ 固件是不是没编进去?")
        return

    from pyocd.core.helpers import ConnectHelper
    sess = ConnectHelper.session_with_chosen_probe(
        target_override="stm32h723xx",
        options={"connect_mode": "halt", "frequency": 2000000},
        blocking=False,
    )
    if sess is None:
        print("✗ 找不到探针 (DAPLink)")
        return
    with sess:
        t = sess.target
        t.halt()
        v = {n: t.read32(a) for n, a in base.items()}
        arr = {}
        for n in ("g_as_ack", "g_as_idrpd", "g_as_raw"):
            arr[n] = [t.read32(base[n] + 4 * k) for k in range(4)]
        t.resume()

    print("=" * 76)
    print("AS5600 探针读回 (SWD)")
    print("=" * 76)
    print("  已跑轮数 g_as_n = %d   %s" % (v["g_as_n"], "(在跑)" if v["g_as_n"] else "(没跑! 固件没起来?)"))
    for k, nm in (("g_as_idr_float", "浮空"), ("g_as_idr_pu", "内部上拉"), ("g_as_idr_pd", "内部下拉")):
        print("  PB10/PB11 %s时 IDR = 0x%04X  (SCL=%d SDA=%d)"
              % (nm, v[k], (v[k] >> 10) & 1, (v[k] >> 11) & 1))
    print()
    print("  ★ 输出通路自检: 拉低时读回 0x%04X (期望 0x0000)   释放后读回 0x%04X (期望 0x0C00)"
          % (v["g_as_drive_lo"], v["g_as_drive_hi"]))
    if v["g_as_drive_lo"] != 0:
        print("     ⇒ ✗✗ **拉不低!** 输出通路有问题 ⇒ START 条件根本不成立 ⇒ 后面的时序全是空谈")
    print()
    print("  %-32s %-14s %-7s %s" % ("候选引脚对", "下拉时IDR", "写ACK", "RAW_ANGLE"))
    hit = []
    for k in range(4):
        raw = arr["g_as_raw"][k]
        raw_s = ("%d (%.1f°)" % (raw, raw * 360.0 / 4096.0)) if raw <= 4095 else "—"
        print("  %-32s 0x%04X        %-7d %s"
              % (PAIRS[k], arr["g_as_idrpd"][k], arr["g_as_ack"][k], raw_s))
        if arr["g_as_ack"][k]:
            hit.append(k)
    print()
    if hit:
        print("  ⇒ ✅ 编码器在 **组%d**：%s" % (hit[0], PAIRS[hit[0]]))
        print("     磁状态 g_as_mag = 0x%04X" % v["g_as_mag"])
    else:
        pullups = [k for k in range(4) if arr["g_as_idrpd"][k]]
        if pullups:
            print("  ⇒ ✗ 组%s 有外部上拉(线接上了)，但 0x36 **无人应答**" % pullups)
            print("     先查: ① 供电电压对不对(该 5V) ② GND 是否真通 ③ PGO 是否悬空 ④ 磁铁/器件是否坏")
        else:
            print("  ⇒ ✗ 四组都没有上拉 ⇒ 编码器**根本没接到这几根线上**")


if __name__ == "__main__":
    main()
