#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_tick_erase.py — **判据能失败** 的实测: sector erase 期间 100μs 拍丢了多少

★ 为什么需要它 (2026-09-11, T26 配套):
  "擦除期间拍不丢"是本项目对**引擎确定性**的核心承诺之一。一个永远能过的判据等于没测,
  所以这份脚本用**同一套测量方法**打两份固件:
    · -DDCL_VTOR_ITCM=0  向量表留在 FLASH (= 改前行为) → 必须量到**缺口**
    · -DDCL_VTOR_ITCM=1  向量表搬进 ITCM + 设 VTOR  → 必须量到**零缺口**
  只有 0 那一半真的报出缺口, 1 那一半的"零缺口"才算证据。

  机制: sector erase 会 stall 从 FLASH 取指 -> 若向量表在 FLASH, 中断**根本进不来**,
        g_tick_count 停走; 搬进 ITCM 后取向量零等待 -> 拍照常。

  方法: 量 g_tick_count 的**增量**而不是绝对值 —— 环境噪声 (两机时钟差、pyocd 调度)
        影响的是每个采样点的绝对时刻, 而"100ms 槽里应该走 1000 拍"只依赖固件自己的
        100μs 时基, 与 PC 侧抖动无关。

用法:
    python tools/h723_tick_erase.py                 # 量当前板上的固件
    python tools/h723_tick_erase.py --bins 30 --bin-ms 100
"""
import argparse
import os
import subprocess
import sys

# Windows 控制台默认 GBK, 个别字符 (-> 等) 会以 UnicodeEncodeError 直接崩掉脚本 ——
# 而崩在"打印结论"这一步最冤: 数据都已经量到了。改成永不抛。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

PYOCD = ["pyocd", "cmd", "-t", "stm32h723xx", "-O", "connect_mode=under-reset"]

# ★★ 地址一律由 `nm` **运行时解析**, 不硬编码 (2026-09-11 踩到的真事故):
#   给 main.c 加一个全局量就让 DTCM 布局整体位移, 硬编码的地址会**安静地读到邻居**,
#   于是工具报出"基线不连续"这种看起来像固件坏了、实际只是读错地址的结论。
#   (这次幸好那道"基线不连续就 SKIP"的闸门拦下了 —— 判据宁可拒答也不能给错答。)
_HERE2 = os.path.dirname(os.path.abspath(__file__))
_ELF = os.path.join(os.path.dirname(_HERE2), "build", "dcl_h723")
_NM = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
       "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update"
       ".win32_1.5.0.202011040924/tools/bin/arm-none-eabi-nm.exe")
_NEED = ["g_vtor", "g_vtor_want", "g_tick_count", "g_persist_req",
         "g_persist_writes", "g_persist_saves", "g_persist_skip_run", "_shm_start"]
_OFF_CTRL_ENGINE_RUN = 0x0D
A = {}


def load_syms():
    r = subprocess.run([_NM, _ELF], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print("!! arm-none-eabi-nm 失败:", r.stderr)
        return 2
    syms = {}
    for line in r.stdout.splitlines():
        p = line.split()
        if len(p) == 3:
            try:
                syms[p[2]] = int(p[0], 16)
            except ValueError:
                pass
    miss = [n for n in _NEED if n not in syms]
    if miss:
        print("!! 符号缺失 (固件改名却没同步本脚本?): %s" % miss)
        return 2
    A.update(syms)
    A["shm_run"] = A["_shm_start"] + _OFF_CTRL_ENGINE_RUN
    return 0


# 100μs 拍 -> 每 1ms 恰好 10 拍 (本判据的标称值)
TICKS_PER_MS = 10


def run_session(cmds, timeout=180):
    r = subprocess.run(PYOCD + cmds, capture_output=True, text=True, timeout=timeout)
    vals = {}
    for line in r.stdout.splitlines():
        s = line.strip()
        if ":" not in s:
            continue
        a, v = s.split(":", 1)
        try:
            vals[int(a, 16)] = int(v.split()[0], 16)
        except Exception:
            pass
    return vals, r.stdout + r.stderr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bins", type=int, default=30, help="采样槽数")
    ap.add_argument("--bin-ms", type=int, default=100, help="每槽毫秒")
    ap.add_argument("--margin-ms", type=int, default=600, help="请求落盘前先跑一段基线")
    a = ap.parse_args()

    if load_syms() != 0:
        return 2
    print("观测面 (nm 解析): tick=0x%08X vtor=0x%08X shm_run=0x%08X"
          % (A["g_tick_count"], A["g_vtor"], A["shm_run"]))

    cmds = ["-c", "reset", "-c", "sleep 600"]
    # ① 录 VTOR (判据前提) + 先把引擎停掉 (persist 的 PERSISTENT 门需要 RUN=0)
    cmds += ["-c", "read32 0x%08X" % A["g_vtor"],
             "-c", "read32 0x%08X" % A["g_vtor_want"],
             "-c", "write8 0x%08X 0" % A["shm_run"],
             "-c", "sleep 150"]
    # ② 基线: 先量几槽 (确认拍在走且速率正确 —— 否则后面的"缺口"没意义)
    base_bins = max(2, a.margin_ms // a.bin_ms)
    for _ in range(base_bins):
        cmds += ["-c", "read32 0x%08X" % A["g_tick_count"], "-c", "sleep %d" % a.bin_ms]
    # ③ 请求落盘, 然后连续采样
    cmds += ["-c", "write32 0x%08X 1" % A["g_persist_req"]]
    for _ in range(a.bins):
        cmds += ["-c", "read32 0x%08X" % A["g_tick_count"], "-c", "sleep %d" % a.bin_ms]
    # ④ 落盘结果
    cmds += ["-c", "read32 0x%08X" % A["g_persist_writes"],
             "-c", "read32 0x%08X" % A["g_persist_saves"],
             "-c", "read32 0x%08X" % A["g_persist_skip_run"],
             "-c", "go"]

    vals, out = run_session(cmds)

    vtor = vals.get(A["g_vtor"], -1)
    tick_series = []
    # 按出现顺序重放 tick 读数: pyocd 输出里读同一地址多次会被 dict 覆盖, 所以重新解析 stdout
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("%08x:" % A["g_tick_count"]):
            tick_series.append(int(s.split(":", 1)[1].split()[0], 16))

    print("── 前提 ──")
    print("  VTOR          = 0x%08X   (want 0x%08X)" % (vtor, vals.get(A["g_vtor_want"], -1)))
    on_itcm = 0 < vtor < 0x10000
    print("  向量表位置    = %s" % ("ITCM (取向量不过 flash)" if on_itcm else "FLASH (取向量要过 flash)"))
    print("  persist: writes=%s saves=%s skip_run=%s"
          % (vals.get(A["g_persist_writes"], -1), vals.get(A["g_persist_saves"], -1), vals.get(A["g_persist_skip_run"], -1)))

    if len(tick_series) < base_bins + 3:
        print("!! tick 采样点太少 (%d 个), 无法判读" % len(tick_series))
        return 2

    nominal = TICKS_PER_MS * a.bin_ms
    # ★ 抖动门限: pyocd 的 `sleep 100` 实测落在 1000±15 拍 (±1.5%, 它是 PC 侧宿主调度,
    #   与被测固件无关)。真缺口是 Δ=0 或掉几百拍 —— 两个量级差 50 倍以上,
    #   所以用 10% 作门限既不误判抖动, 也不会放过任何真实缺口。
    tol = max(20, nominal // 10)
    print("\n── 每槽增量 (标称 %d 拍/%dms; 缺口 = 该槽少走的拍数, 抖动门限 ±%d) ──"
          % (nominal, a.bin_ms, tol))
    lost_total = 0
    worst = 0
    base_ok = True
    for i in range(1, len(tick_series)):
        d = tick_series[i] - tick_series[i - 1]
        if d < 0:
            d += 0x100000000
        dev = nominal - d
        miss = dev if dev > tol else 0          # |dev| <= tol 一律算抖动
        if i <= base_bins:
            tag = " (基线)"
            if miss > 0:
                base_ok = False
        elif miss > 0:
            tag = "  <- 缺口"
            lost_total += miss
            worst = max(worst, miss)
        else:
            tag = ""
        print("  槽%02d: Δ=%5d  缺=%5d%s" % (i, d, miss, tag))

    print("\n── 结论 ──")
    print("  落盘前基线是否连续: %s"
          % ("是 (每槽 %d±%d 拍)" % (nominal, tol) if base_ok
             else "否 —— 基线本身有缺口, 本测量不适用 (先查板子/探针)"))
    print("  落盘窗口累计丢失 = %d 拍 (最大单槽缺 %d 拍 ≈ %.0fms)"
          % (lost_total, worst, worst / float(TICKS_PER_MS)))
    if not base_ok:
        print("  [SKIP] 基线不连续 -> 本次测量无效, 不做任何结论")
        return 2
    if on_itcm:
        ok = (lost_total == 0)
        print("  [%s] VTOR 在 ITCM -> 期望零缺口, 实测丢 %d 拍" % ("PASS" if ok else "FAIL", lost_total))
    else:
        ok = (lost_total > 0)
        print("  [%s] VTOR 在 FLASH -> 期望量到缺口 (对照组: 量不到 = 本判据是空判据); 实测丢 %d 拍"
              % ("PASS(判据有效)" if ok else "FAIL(判据无效!)", lost_total))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
