#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_stall_edge.py —— **阶段 2 收口**：失速拐点细扫 + "物理 vs 固件"决定性排除 + 斜坡的真实上限

主扫（`_stall_v2.py`）已证：1000~16000 Hz 两臂比值 0.995~1.008；≥18000 Hz **突加** 归零、**斜坡** 正常。
但"突加失败"还有两种解释，必须分开：
  H1 **物理**：从静止直接跳到 f，超过**拉起频率**（pull-in）⇒ 转子跟不上 ⇒ 失速
  H2 **固件**：`sub=1` 在高频 + `slope=0` 时被拒发/静默失效
⇒ **决定性对照 B**：**先在 8000 Hz 转起来，再突跳到 18000** ——
   若仍能跟着跑 ⇒ 说明高频本身能发 ⇒ 排除 H2；且"从静止失败、从转动成功"正是 H1 的定义。

判据：
  A 拐点细扫（突加，16000→18000 步进 250）
  B ★ 运行中突跳（8000→18000 / 8000→24000）
  C 失速是否**自愈**（突加 20000 后等 2.0 s 再看 —— 主扫只等 0.4 s）
  D 斜坡的**上限**（31000→100000，找 pull-out）
用法: python tools/h723_stall_edge.py
"""
import os
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from h723_client import Dcl, find_board            # noqa: E402
from h723_stall_sweep import analyse, dump_bb, SPR  # noqa: E402  ★ 共用同一套解析（不许抄第二份）

ALIAS_MAX_DEG = 90.0


def main():
    d = Dcl(find_board())
    bad = []

    def sx(n, v, name=""):
        st, p = d.send(0x39, bytes([19, n]) + struct.pack("<I", v))
        if st != "ACK":
            bad.append("sub=%d(%s)=%s" % (n, name, st))
        return st, p

    def prep():
        sx(1, 0, "rate"); sx(6, 0, "stop"); sx(17, 0, "slope"); sx(13, 0, "src")
        sx(5, 1, "enapol"); sx(2, 0, "dir"); sx(22, 1, "sm_mode"); sx(20, 10, "period")
        time.sleep(0.3); sx(3, 1, "ena"); time.sleep(0.3)

    def wait_full(f, budget=6.0):
        t0 = time.time()
        while time.time() - t0 < budget:
            st, p = sx(19, 0, "rampst")
            if st == "ACK" and len(p) == 32 and struct.unpack("<8I", p[:32])[3] >= f:
                return True
            time.sleep(0.05)
        return False

    def grab(f, dwell=0.4):
        time.sleep(dwell)
        data = dump_bb()
        sx(1, 0, "rate"); sx(17, 0, "slope"); sx(22, 0, "sm_mode"); sx(20, 100, "period")
        time.sleep(0.3)
        if data is None:
            return None
        a = analyse(data)
        if a is None:
            return None
        a["ratio"] = a["deg"] / (f * a["span_s"] / SPR * 360.0) if f else 0.0
        return a

    def row(tag, f, a):
        if a is None:
            print("  %-26s | %6s | dump/环异常" % (tag, f)); return
        print("  %-26s | %6s | %6.3f | %6.1f | %7.0f | %5d | %6.2f | %s"
              % (tag, f, a["ratio"], a["span_s"] * 1e3, a["n_distinct"] / a["span_s"],
                 a["n_distinct"], a["max_step_deg"],
                 "✓" if a["max_step_deg"] < ALIAS_MAX_DEG else "✗混叠"))

    hdr = "  %-26s | %6s |   比值 | 跨度ms | 记录率/s | 样本 | 单步° | 不混叠" % ("工况", "Hz")
    print("=== A 拐点细扫（突加，从静止）===")
    print(hdr)
    for f in (15000, 15500, 16000, 16250, 16500, 16750, 17000, 17250, 17500, 18000):
        prep(); sx(1, f, "rate"); wait_full(f)
        row("A 突加(静止)", f, grab(f))

    print("\n=== B ★决定性：先在 8000 转起来，再突跳到高频 ===")
    print(hdr)
    for f in (18000, 24000, 31000):
        prep()
        sx(17, 16000, "slope"); sx(1, 8000, "rate"); wait_full(8000); time.sleep(0.3)
        sx(1, f, "rate")                      # ★ 运行中突跳（不重设 slope，让它继续爬）
        row("B 8000→突跳", f, grab(f))
        prep()
        sx(17, 16000, "slope"); sx(1, 8000, "rate"); wait_full(8000); time.sleep(0.3)
        sx(17, 32000, "slope"); sx(1, f, "rate")   # 对照：同一起点但给斜坡
        wait_full(f); row("B' 8000→斜坡", f, grab(f))

    print("\n=== C 失速是否自愈（突加 20000，等 2.0 s 再看）===")
    print(hdr)
    for dwell in (0.4, 2.0):
        prep(); sx(1, 20000, "rate"); wait_full(20000)
        row("C 突加 20000 等%.1fs" % dwell, 20000, grab(20000, dwell))

    print("\n=== D 斜坡的上限（pull-out）===")
    print(hdr)
    for f in (31000, 40000, 50000, 60000, 80000, 100000):
        prep()
        sx(17, int(f * 2), "slope"); sx(1, f, "rate")
        ok = wait_full(f, 8.0)
        a = grab(f)
        row("D 斜坡%s" % ("" if ok else "(未达速)"), f, a)

    if bad:
        print("\n!! 有命令未 ACK: %s" % ", ".join(sorted(set(bad))))
    d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
