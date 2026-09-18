#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_io_isr_check.py — "I/O 搬进拍内" (P1/P2) 的验收读数

用途: 在同一块板子上, 分别烧**交付档**与**对照档**, 各跑一次本脚本, 对比输出。
      两档的差异必须能被下面这几条判据抓到 —— 抓不到就说明判据是空的。

判据 (逐条都能失败):
  ① 存活    : g_tick_count / HEARTBEAT 两次采样必须增长  —— 否则"没跑起来", 后面全部无意义
  ② 拍周期  : g_per_cyc_min ≈ 40000 cyc (100µs@400MHz)  —— I/O 搬进来**没有**拖垮拍
  ③ 时基    : g_per_glitch_n == 0                       —— DWT 时基完好 (本项目铁律 0 的前置断言)
  ④ 未超载  : g_isr_overrun == 0                        —— ISR 时长 < EXEC_BUDGET_CYCLES
  ⑤ ★核心   : g_adc_sm_done  —— 交付档必须**单调增**(ADC 状态机在拍内跑);
               对照档必须 **== 0** (状态机根本没被调用)。
               这一条是"搬进拍内"的**直接证据**: 若两档都是 0, 说明搬了个寂寞。
  ⑥ 无违规  : g_adc_sm_timeout == 0 (转换没超时) 且 g_safe_mask_oob == 0 (GPIO_MASK 没被越界写)
  ⑦ 成本    : g_isr_cyc_max / g_isr_cyc_min 两档对比 —— 交付档应**略大**(它多做了 I/O),
               但这个增量应当远小于拍长 (见 docs/PLAN-io-into-engine.md §7 的预估 ≈225 cyc)

★ 为什么用**两次采样算增量**而不是读一次:
  大多数量是"自启动以来的累计值", 单次读数无法区分"一直在动"与"早就停了"。
  两次采样 + 求差, 才能把"活的"与"死的"分开 —— 这是本项目踩过的坑 (阈值/计数类判据)。

用法:
  python tools/h723_io_isr_check.py --tag DUT      # 交付档
  python tools/h723_io_isr_check.py --tag CTRL     # 对照档
  python tools/h723_io_isr_check.py --tag DUT --json out.json   # 顺带落盘
"""

import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import sys
import os
import re
import json
import subprocess
import argparse

# 要读的符号 (都与"搬进拍内"的判据直接相关)
SYMS = [
    "g_tick_count",       # 拍计数 (存活)
    "g_stage",            # 启动阶段 (9 = 主循环)
    "g_per_cyc_min",      # 拍周期最小
    "g_per_cyc_max",      # 拍周期最大
    "g_per_cyc_last",     # 最近一次拍周期
    "g_isr_cyc_min",      # ISR 时长最小 (成本)
    "g_isr_cyc_max",      # ISR 时长最大 (成本)
    "g_isr_n",            # RUN 拍数
    "g_per_glitch_n",     # 时基不连续计数 (必须 0)
    "g_isr_overrun",      # 超预算计数 (必须 0)
    "g_adc_sm_done",      # ★ ADC 状态机完成数 (交付档必须增, 对照档必须 0)
    "g_adc_sm_timeout",   # ★ ADC 状态机超时数 (必须 0)
    "g_safe_mask_oob",    # GPIO_MASK 越界写 (必须 0)
    "g_safe_mask_nonzero",# GPIO_MASK 合法使用次数
    "g_out_surfaces",     # 登记的物理输出面数
    "g_safe_surfaces_ran",# 上次停机实际执行的面数
    "g_shm",              # SHM 基址 (用于读 HEARTBEAT)
]
OFF_CTRL_HEARTBEAT = 0x08     # SHM 偏移 (src/engine.h)


def parse_map(mapfile, names):
    """从 .map 里取符号地址。格式: `                0x0000000020000238                g_stage`"""
    want = set(names)
    found = {}
    with open(mapfile, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.match(r"^\s+0x([0-9a-fA-F]{8,16})\s+([A-Za-z_]\w*)\s*$", line)
            if m and m.group(2) in want and m.group(2) not in found:
                found[m.group(2)] = int(m.group(1), 16)
    return found


def parse_reads(out):
    vals = []
    for line in out.splitlines():
        m = re.search(r":\s*(.*)$", line)
        if not m:
            continue
        hexes = re.findall(r"\b[0-9a-fA-F]{8}\b", m.group(1))
        if hexes:
            vals.append(int(hexes[0], 16))
    return vals


def run_pyocd(cmds, timeout=120):
    args = ["pyocd", "cmd", "-t", "stm32h723xx", "-O", "connect_mode=under-reset"]
    for c in cmds:
        args += ["-c", c]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="RUN", help="本次读数的标签 (DUT / CTRL / ...)")
    ap.add_argument("--map", default="build/dcl_h723.map")
    ap.add_argument("--elf", default="build/dcl_h723.elf")
    ap.add_argument("--ms", type=int, default=900, help="两次采样之间/之前的运行时长")
    ap.add_argument("--json", default=None, help="把结果落盘成 JSON")
    ap.add_argument("--show-raw", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.map):
        print("!! 找不到 %s —— 先构建一次 (bash build.sh)。" % a.map)
        return 2
    sym = parse_map(a.map, SYMS)
    missing = [s for s in SYMS if s not in sym]
    if missing:
        print("!! map 里缺符号: %s" % " ".join(missing))
        print("   ★ 这是**测量链**问题 (比如某量被 --gc-sections 回收了没锚定),")
        print("     不是固件坏了 —— 检查 main.c 的 obs_anchor() 是否登记了它。")
        return 2

    reads = [("read32 0x%08X" % sym[s]) for s in SYMS]
    hb    = "read32 0x%08X" % (sym["g_shm"] + OFF_CTRL_HEARTBEAT)

    cmds = (["reset", "go", "sleep %d" % a.ms, "halt"]
            + reads + [hb]
            + ["go", "sleep %d" % a.ms, "halt"]
            + reads + [hb]
            + ["go"])                      # ★ 收尾放核 (铁律 0: 用完必须恢复)

    print("=== I/O 搬进拍内 · 验收读数 [%s] ===" % a.tag)
    print("  构建: %s" % a.elf)
    out = run_pyocd(cmds)
    if a.show_raw:
        print(out)

    v = parse_reads(out)
    n = len(SYMS) + 1                  # 每批 = 符号数 + HEARTBEAT
    if len(v) < 2 * n:
        print("!! 读回 %d 个值, 期望 %d —— pyocd 输出格式可能变了 (带 --show-raw 看)" % (len(v), 2 * n))
        return 2

    A = dict(zip(SYMS + ["HEARTBEAT"], v[:n]))
    B = dict(zip(SYMS + ["HEARTBEAT"], v[n:2 * n]))

    print()
    print("%-22s %12s %12s %12s" % ("量", "第一次", "第二次", "增量"))
    print("-" * 60)
    for k in SYMS + ["HEARTBEAT"]:
        d = (B[k] - A[k]) & 0xFFFFFFFF
        dstr = "+%d" % d if d < 0x80000000 else "(%d)" % (d - (1 << 32))
        print("%-22s %12s %12s %12s" % (k, "0x%08X" % A[k], "0x%08X" % B[k], dstr))

    # ── 判据 ──
    print()
    print("── 判据 ──")
    ok = True

    def chk(cond, name, detail=""):
        nonlocal ok
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  " + detail) if detail else ""))
        if not cond:
            ok = False
        return cond

    tick_d = (B["g_tick_count"] - A["g_tick_count"]) & 0xFFFFFFFF
    hb_d   = (B["HEARTBEAT"] - A["HEARTBEAT"]) & 0xFFFFFFFF
    chk(tick_d > 0 and hb_d > 0, "① 存活: 拍计数与心跳都在增长",
        "(tick +%d, hb +%d)" % (tick_d, hb_d))
    # ★ 判据② 不能用 pmin: pyocd 的 halt/go 会让"暂停后的第一个样本"变成一个小值
    #   —— DWT_CYCCNT 在调试暂停期间是**冻结**的 (见 main.c 里 g_per_prev 的说明),
    #   恢复后第一拍算出的是"暂停的残余时间"⇒ pmin 必然被污染, 与固件无关。
    #   用 **pmax** 才反映真实拍长 (它只受"真实变长的拍"影响; 跨纪元负增量另有
    #   g_per_glitch_n 单独计, 判据③ 已经把它挡住)。
    chk(39000 <= B["g_per_cyc_max"] <= 42000, "② 拍周期正常 (pmax ≈ 40000 cyc)",
        "pmax = %d  [pmin=%d 受 halt 污染, 仅参考]" % (B["g_per_cyc_max"], B["g_per_cyc_min"]))
    chk(B["g_per_glitch_n"] == 0, "③ 时基完好 (g_per_glitch_n == 0)",
        "= %d  ← 非 0 则任何计时结论都不可信" % B["g_per_glitch_n"])
    # ★★ 2026-09-18 定位（超载实验, 见 docs/exp-2026-09-18-overload/）:
    #   本项是**必要条件, 不是充分判据** —— 实测最重**合法**程序只吃到运行期门
    #   (`EXEC_BUDGET_TB` = 80 µs) 的 **66.8%** ⇒ 对任何合法程序 `ov` 都恒 0
    #   ⇒ 这半条**不可能失败** = 空判据（本项目最忌的那一族）。
    #   ⇒ 真正的健康判据换成 tools/h723_budget_fidelity.py 的
    #      **F1 模型保真**（实测扫描/预测 ≤ 1.30）与 **F2 有余量**（emax ≤ 门 × 0.8）。
    #   本项保留 —— 它仍是"真超载"的必要条件, 只是**不能单独当健康证据**。
    chk(B["g_isr_overrun"] == 0,
        "④ 未超预算 (ov==0; ★必要条件而非充分 —— 见 h723_budget_fidelity.py)",
        "= %d, isr_max = %d cyc" % (B["g_isr_overrun"], B["g_isr_cyc_max"]))
    chk(B["g_adc_sm_timeout"] == 0, "⑥a ADC 无超时",
        "= %d" % B["g_adc_sm_timeout"])
    chk(B["g_safe_mask_oob"] == 0, "⑥b GPIO_MASK 无越界写",
        "= %d" % B["g_safe_mask_oob"])
    print("  ---- ⑤ 核心判据 (由外部按 tag 判定) ----")
    print("       g_adc_sm_done: 第一次 %d → 第二次 %d" % (A["g_adc_sm_done"], B["g_adc_sm_done"]))
    if a.tag.upper().startswith("DUT"):
        chk(B["g_adc_sm_done"] - A["g_adc_sm_done"] > 0,
            "⑤ 交付档: ADC 状态机在拍内运行 (done 单调增)")
    elif a.tag.upper().startswith("CTRL"):
        chk(B["g_adc_sm_done"] == 0 and A["g_adc_sm_done"] == 0,
            "⑤ 对照档: 状态机未被调用 (done 恒 0)")

    res = {
        "tag": a.tag,
        "A": {k: A[k] for k in A},
        "B": {k: B[k] for k in B},
        "delta": {k: (B[k] - A[k]) & 0xFFFFFFFF for k in A},
        "checks_ok": ok,
    }
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2, ensure_ascii=False)
        print()
        print("结果已落盘: %s" % a.json)

    print()
    print("=== [%s] %s ===" % (a.tag, "全部判据 PASS" if ok else "**有 FAIL —— 见上**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
