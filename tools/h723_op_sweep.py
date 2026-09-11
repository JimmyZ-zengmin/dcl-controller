#!/usr/bin/env python3
"""
h723_op_sweep.py — 逐原语成本实测（为 deploy 预算模型建立**本平台自己的** cost[]）

★ 为什么不能照抄 S3 的 op_cost[]
  S3 那张表是 240MHz / ESP32 架构上的实测值（DIRECT = 234 cyc/条）；
  H723 是 400MHz + ITCM（阶段 2 实测 56 cyc/条）。照抄过来，deploy 的预算门
  就会用错的数去把关 —— 那正是本项目最不能接受的"宣称≠实现"。

方法
  1. **单一原语模式**：`g_table_profile = 100 + op`（engine.c 新增），
     128 条路由全用同一个原语 → 成本只反映这一个原语
  2. **两点法**：同一原语分别测 n=128 与 n=64
     斜率 = (min128 − min64) / 64 = **边际单条成本**
     （除掉函数调用 + DWT 探针的常数项，这正是预算模型 Σcost 需要的量）
  3. 扫描模式固定 `全表扫 + ITCM`，与阶段 2 的口径一致 → 数字可直接比

判据（每一项都可失败；任一项 ✗ ⇒ 该原语数据不可信）
  · 表校验和 == Python 独立预测（逐 op 比对，防"填错表还以为在测这个原语"）
  · eng_div0 == 0 且 eng_n > 0（"零成本"一定是测量坏了）
  · sel_used / n_used == 写入值（防跑错配置）
  · 斜率 > 0

用法
  python tools/h723_op_sweep.py                 # 全部 19 原语
  python tools/h723_op_sweep.py --ops 0,5,0x12  # 指定原语
  python tools/h723_op_sweep.py --dur 0.4 --json build/op_cost.json
"""

# ★ Windows 控制台默认 GBK: 脚本自己 print 出来的个别字符 (⇒ / ✓ 等) 会以
#   UnicodeEncodeError **直接崩掉整个脚本** —— 数据都量到了, 却崩在"打印结论"这一步,
#   症状看起来像"脚本坏了"而不是"编码问题"。⇒ 统一在入口把 stdout 的错误策略改成
#   "永不抛" (换成 ?), 让验收脚本不可能因为自己的输出而失败。
#   (2026-09-11 实测: audit_m234 / w1 真的这么崩过一次, 整份结果都没打出来。)
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NM = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools."
      "gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-nm.exe")
CPU_HZ = 400e6
TICK_CYC = 40000


def _load_stage2():
    spec = importlib.util.spec_from_file_location("s2", os.path.join(HERE, "h723_stage2_read.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


OP_NAMES = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
            "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]
OP_MAX = 0x12


def symbols(elf):
    s = {}
    for l in subprocess.run([NM, elf], capture_output=True, text=True, timeout=60).stdout.splitlines():
        p = l.split()
        if len(p) == 3:
            s[p[2]] = int(p[0], 16)
    return s


# 每个 (op, n) 采样点读回的字段: (符号名, 词数)
SAMPLE_SYMS = [("g_eng_cyc_min", 1), ("g_eng_cyc_max", 1), ("g_eng_cyc_sum", 2),
               ("g_eng_n", 1), ("g_eng_div0", 1), ("g_table_ck", 1),
               ("g_active_routes", 1), ("g_guard_ok", 1),
               ("g_eng_sel_used", 1), ("g_eng_n_used", 1), ("g_isr_cyc_min", 1)]


def run(sym, ops, dur):
    """单会话跑完全部测量（pyocd 每次连接都会复位目标 → 必须一条链跑完）"""
    cmd = ["reset", "sleep 400",
           "write32 0x%08X 0" % sym["g_engine_gate"]]
    cur = None
    for op, n in ops:
        prof = 100 + op
        if prof != cur:
            cmd += ["write32 0x%08X %d" % (sym["g_table_profile"], prof),
                    "write32 0x%08X 1" % sym["g_reinit"], "sleep 300"]
            cur = prof
        cmd += ["write32 0x%08X 1" % sym["g_engine_sel"],      # 1 = ITCM
                "write32 0x%08X 0" % sym["g_scan_mode"],      # 0 = 全表扫 (阶段 2 口径)
                "write32 0x%08X %d" % (sym["g_n_routes"], n),
                "write32 0x%08X 1" % sym["g_stat_reset"],
                "write32 0x%08X 1" % sym["g_engine_gate"],
                "sleep %d" % int(dur * 1000)]
        for nm, w in SAMPLE_SYMS:
            for k in range(w):
                cmd.append("read32 0x%08X" % (sym[nm] + 4 * k))
        cmd.append("read32 0x%08X" % sym["g_stage"])          # 守卫
        cmd.append("write32 0x%08X 0" % sym["g_engine_gate"])  # 下一点前先关门
    args = ["pyocd", "cmd", "-t", "stm32h723xx", "-O", "connect_mode=under-reset"]
    for c in cmd:
        args += ["-c", c]
    args += ["-c", "go"]   # M4: 收尾 go, 别把核留在 halt
    r = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    vals = []
    for line in r.stdout.splitlines():
        m = re.match(r"^\s*([0-9a-f]{8}):(.*)", line)
        if m:
            vals += [int(x, 16) for x in re.findall(r"\b[0-9a-f]{8}\b", m.group(2).split("|")[0])]
    return vals, cmd, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ops", default=None, help="逗号分隔的原语码 (如 0,5,0x12); 默认全部")
    ap.add_argument("--dur", type=float, default=0.4)
    ap.add_argument("--elf", default="build/dcl_h723")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    if a.ops:
        wanted = [int(x, 0) for x in a.ops.split(",")]
    else:
        wanted = list(range(OP_MAX + 1))

    sym = symbols(a.elf)
    if "g_table_ck" not in sym:
        print("!! 符号表里没有 g_table_ck (检查 ELF 路径)")
        return 2

    ops = []
    for op in wanted:
        ops += [(op, 128), (op, 64)]

    s2 = _load_stage2()
    n_vals = len(ops) * (sum(w for _, w in SAMPLE_SYMS) + 1)
    print("单会话扫描 %d 个采样点 (%d 原语 × 2 点), 预计 %d 秒…"
          % (len(ops), len(wanted), int(len(ops) * (a.dur + 0.5))))
    vals, cmd, r = run(sym, ops, a.dur)
    if len(vals) < n_vals:
        print("!! 读回不足: %d / %d" % (len(vals), n_vals))
        print((r.stdout + r.stderr)[-800:])
        return 3

    it = iter(vals)
    rows = []
    for (op, n) in ops:
        st = {}
        for nm, w in SAMPLE_SYMS:
            v = 0
            for k in range(w):
                v |= next(it) << (32 * k)
            st[nm] = v
        next(it)  # guard
        rows.append(dict(op=op, n=n, st=st))

    by = {(r_["op"], r_["n"]): r_ for r_ in rows}
    print()
    print("=" * 84)
    print("%-4s %-9s %9s %9s %9s %7s %7s   %s" %
          ("op", "名字", "128条min", "64条min", "cyc/条", "占拍%", "表ck", "判据"))
    print("=" * 84)
    out = []
    for op in wanted:
        r128, r64 = by[(op, 128)], by[(op, 64)]
        m128, m64 = r128["st"]["g_eng_cyc_min"], r64["st"]["g_eng_cyc_min"]
        slope = (m128 - m64) / 64.0 if m128 >= m64 else float("nan")
        exp_ck = s2.predict_table(100 + op)[0]
        ck_ok = r128["st"]["g_table_ck"] == exp_ck
        ok = (ck_ok and r128["st"]["g_eng_div0"] == 0 and r128["st"]["g_eng_n"] > 0
              and r128["st"]["g_eng_sel_used"] == 1 and r128["st"]["g_eng_n_used"] == 128
              and r64["st"]["g_eng_sel_used"] == 1 and r64["st"]["g_eng_n_used"] == 64
              and r128["st"]["g_guard_ok"] == 1
              and slope == slope and slope > 0)
        reason = ""
        if not ck_ok:
            reason = "表ck✗0x%08X≠0x%08X" % (r128["st"]["g_table_ck"], exp_ck)
        elif not (slope == slope and slope > 0):
            reason = "斜率异常"
        elif not ok:
            reason = "守卫✗"
        print("%-4d %-9s %9d %9d %9.2f %6.2f%%  %s  %s" %
              (op, OP_NAMES[op] if op < len(OP_NAMES) else "?", m128, m64, slope,
               100.0 * m128 / TICK_CYC, "✓" if ck_ok else "✗",
               "✓" if ok else "✗ " + reason))
        out.append(dict(op=op, name=(OP_NAMES[op] if op < len(OP_NAMES) else "?"),
                        min128=m128, min64=m64, cyc_per_route=slope,
                        pct_of_tick=100.0 * m128 / TICK_CYC,
                        table_ck=r128["st"]["g_table_ck"], ck_predicted=exp_ck, ok=bool(ok)))

    good = [o for o in out if o["ok"]]
    print()
    print("%d / %d 原语数据可信" % (len(good), len(out)))
    if good:
        c = [o for o in good if o["op"] == 0x00]
        print("对照 S3 的 op_cost[DIRECT] = 234 cyc/条:", end=" ")
        if c:
            print("H723 实测 %.2f cyc/条 (%.1f× 快)" % (c[0]["cyc_per_route"],
                                                        234.0 / c[0]["cyc_per_route"]))
        print("最贵原语: %s" % max(good, key=lambda x: x["cyc_per_route"])["name"])
        print("最便宜原语: %s" % min(good, key=lambda x: x["cyc_per_route"])["name"])
        print()
        print("// ★ 可直接粘进 engine.h 的表 (值为 0 的槽表示该原语未测/不可信)")
        print("static const uint16_t k_op_cost_itcm[0x13] = {")
        print("    " + " ".join("%4d," % (round(o["cyc_per_route"]) if o["ok"] else 0)
                                for o in sorted(out, key=lambda x: x["op"])))
        print("};")

    if a.json:
        os.makedirs(os.path.dirname(a.json) or ".", exist_ok=True)
        json.dump(dict(rows=out, dur=a.dur, cpu_hz=CPU_HZ, tick_cyc=TICK_CYC),
                  open(a.json, "w"), indent=1)
        print("\n原始数据: %s" % a.json)
    return 0 if len(good) == len(out) else 1


if __name__ == "__main__":
    sys.exit(main())
