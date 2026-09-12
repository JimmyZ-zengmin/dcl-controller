#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_pe_probe.py — GPIOE 可用位实测 (P3 输出面的**前置条件**)

背景: docs/PLAN-io-into-engine.md §5 把 DO 输出面定案在 **GPIOE[15:0]**, 依据之一是
      "GPIOE 在本固件中完全未被使用"。但那条依据来自**代码**, 不能证明**引脚的物理
      可用性** —— 板子上 PE 是否都引出、有没有被板载电路(强上拉/下拉/LED/PHY)
      占住, 只有实测才知道。本脚本就是那一步。

判据 (四条, 缺一不可):
  ① GPIOE 时钟能开      —— AHB4ENR bit4 写 1 后读回为 1 (否则整个测试环境不成立)
  ② MODER 能配成输出    —— 读回 == 0x55555555
  ③ 写 ODR 后 IDR 跟随  —— 4 个 pattern (0x0000/0xFFFF/0x5555/0xAAAA) 逐位一致
  ④ 逐位判定            —— 某位若被外部强拉, 该位在对应 pattern 上会"写 1 读 0"

★ 为什么必须在**停机态**做 (本项目铁律 0 "观测不得改变被测对象"):
  本脚本会改 GPIOE 的时钟/MODER/ODR —— 属于"会改变目标状态的手段"。协议通道
  表达不了"逐脚写读"(固件没有这条命令), 所以允许用调试器; 但**用完必须显式恢复**:
  脚本结尾 `reset` + `go` 把板子交回固件正常运行态, 并**读回 MODER 核对已恢复**。
  ⇒ 不做任何"留在板上的运行期配置"。

★ 为什么用 halt 而不是运行态读:
  运行态读写寄存器会与固件竞争(且本项目已知"DAP 运行态单次读会返回假值")。
  halt 态独占访问, 结果才可信 (与 h723-core0 的 HARDWARE-FINDINGS 结论 2 一致)。

用法:
  python tools/h723_pe_probe.py
  python tools/h723_pe_probe.py --keep     # 不恢复(只在排障时用, 交付流程不要用)
"""

import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import sys
import re
import subprocess
import argparse

# ── 寄存器 (与 src/regs.h 同源: GPIO_BASE(n) = 0x58020000 + 0x400*n) ──
GPIOE_BASE    = 0x58021000          # = GPIO_BASE(4)
R_GPIOE_MODER = GPIOE_BASE + 0x00
R_GPIOE_OTYPR = GPIOE_BASE + 0x04
R_GPIOE_OSPD  = GPIOE_BASE + 0x08
R_GPIOE_PUPDR = GPIOE_BASE + 0x0C
R_GPIOE_IDR   = GPIOE_BASE + 0x10
R_GPIOE_ODR   = GPIOE_BASE + 0x14
R_RCC_AHB4ENR = 0x580244E0          # RCC_BASE(0x58024400) + 0xE0

# ★ 写 AHB4ENR 时**不能**只写 0x10: 那是覆盖写, 会把 GPIOA/C/D… 的时钟一起关掉,
#   而固件正在用 GPIOA (UART1/TIM3/AI/HIL) 与 GPIOC (DI) ⇒ 会当场打断运行中的固件。
#   ⇒ 写成"所有 GPIO 端口全开" (bit0..bit10 = GPIOA..GPIOK)。开时钟本身无副作用,
#     测试结束的 reset 会把它恢复成固件自己的值。
AHB4_ALL_GPIO = 0x000007FF

PATS = [0x0000, 0xFFFF, 0x5555, 0xAAAA]


def run_pyocd(cmds, timeout=90):
    args = ["pyocd", "cmd", "-t", "stm32h723xx", "-O", "connect_mode=under-reset"]
    for c in cmds:
        args += ["-c", c]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


def parse_reads(out):
    """按出现顺序取出每次 read32 的值。
    兼容 pyocd 的两种输出格式: `58021010:  00005555` 与 `0x58021010:  0x00005555`。"""
    vals = []
    for line in out.splitlines():
        m = re.search(r":\s*(.*)$", line)
        if not m:
            continue
        hexes = re.findall(r"\b[0-9a-fA-F]{8}\b", m.group(1))
        if hexes:
            vals.append(int(hexes[0], 16))
    return vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="测试后不恢复(仅排障用)")
    ap.add_argument("--show-raw", action="store_true", help="打印 pyocd 原始输出")
    a = ap.parse_args()

    cmds = [
        "reset", "go", "sleep 300", "halt",     # 让固件把时钟配好, 再挂住 (halt 态读写才可信)
        # ★ 先读一次 MODER 当**基准** —— 不假设"复位值是几": STM32H7 的 GPIO MODER
        #   复位默认是 **0xFFFFFFFF**(全部 analog), 与 F4 的 0x00000000 不同。
        #   硬编码"恢复后应等于 0"会把一次**正确的恢复**判成失败 (第一版就是这么错的,
        #   实测读回 0xFFFFFFFF 而判据说"未回到默认" —— 是判据错, 不是硬件错)。
        "read32  0x%08X" % R_GPIOE_MODER,
        "write32 0x%08X 0x%08X" % (R_RCC_AHB4ENR, AHB4_ALL_GPIO),   # ① 开 GPIO 时钟
        "read32  0x%08X" % R_RCC_AHB4ENR,
        "write32 0x%08X 0x55555555" % R_GPIOE_MODER,                # ② 全 16 脚 = 输出
        "write32 0x%08X 0x00000000" % R_GPIOE_OTYPR,                #    推挽
        "write32 0x%08X 0xFFFFFFFF" % R_GPIOE_OSPD,                 #    最高速
        "write32 0x%08X 0x00000000" % R_GPIOE_PUPDR,                #    无上下拉
        "read32  0x%08X" % R_GPIOE_MODER,
    ]
    for p in PATS:                                                  # ③ 4 个 pattern
        cmds += ["write32 0x%08X 0x%08X" % (R_GPIOE_ODR, p),
                 "read32  0x%08X" % R_GPIOE_IDR]
    if not a.keep:
        cmds += ["reset", "go", "sleep 200", "halt",                # ★ 恢复: 交回固件
                 "read32 0x%08X" % R_GPIOE_MODER]                   #   并核对已恢复

    print("=== GPIOE 可用位实测 ===")
    print("  目标: 为 DO 输出面 (docs/PLAN-io-into-engine.md §5) 确认 PE0..PE15 的物理可用性")
    print("  方法: halt 态配置 → 4 个 ODR pattern → 逐位比对 IDR; 结束复位并核对恢复")
    print()

    out = run_pyocd(cmds)
    if a.show_raw:
        print("── pyocd 原始输出 ──")
        print(out)
        print()

    v = parse_reads(out)
    need = 3 + len(PATS) + (0 if a.keep else 1)
    if len(v) < need:
        print("!! 读回 %d 个值, 期望 %d 个 —— pyocd 输出格式可能变了。" % (len(v), need))
        print("   请带 --show-raw 重跑, 看清 read32 的实际返回格式。")
        print("   (这不是「固件有问题」, 是**测量链**有问题 —— 先修脚本再下结论。)")
        return 2

    moder_base = v[0]          # 配置前 (复位后) 的 MODER —— 恢复判据的基准
    ahb4       = v[1]
    moder      = v[2]
    idrs       = v[3:3 + len(PATS)]
    modr_r     = v[3 + len(PATS)] if not a.keep else None

    print("① GPIOE 时钟")
    print("   AHB4ENR 写后读回 = 0x%08X  (bit4 GPIOEEN = %d)"
          % (ahb4, (ahb4 >> 4) & 1))
    if not ((ahb4 >> 4) & 1):
        print("   [FAIL] GPIOE 时钟没开起来 ⇒ **测试环境不成立**, 后面所有读数都不可信。")
        print("          先查 RCC 地址/连接模式, 不要据此说「GPIOE 不可用」。")
        return 1

    print("② MODER (期望 0x55555555 = 全 16 脚输出)")
    print("   读回 = 0x%08X  %s" % (moder, "OK" if moder == 0x55555555 else "[异常]"))
    if moder != 0x55555555:
        print("   [WARN] MODER 读回与写入不符 —— 可能有脚被锁定(LCKR)或该端口部分未实现。")

    print("③ 4 个 pattern 的 IDR")
    for p, r in zip(PATS, idrs):
        mark = "一致" if r == p else "**不一致**"
        print("   ODR=0x%04X → IDR=0x%04X   %s" % (p, r, mark))
        if r != p:
            diff = (p ^ r) & 0xFFFF
            print("        差异位: %s   (写 1 读 0 = 被外部拉低; 写 0 读 1 = 被外部拉高)"
                  % " ".join("PE%d" % i for i in range(16) if (diff >> i) & 1))

    print()
    print("④ 逐位判定")
    ok_bits, bad_bits = [], []
    for i in range(16):
        want = [(p >> i) & 1 for p in PATS]
        got  = [(r >> i) & 1 for r in idrs]
        (ok_bits if want == got else bad_bits).append(i)
    print("   可用位 %2d/16: %s" % (len(ok_bits), " ".join("PE%d" % i for i in ok_bits) or "(无)"))
    print("   可疑位 %2d/16: %s" % (len(bad_bits), " ".join("PE%d" % i for i in bad_bits) or "(无)"))
    if bad_bits:
        print("   ★ 可疑位**不是缺陷结论**, 而是「这几位别拿来当输出面」的实证依据 ——")
        print("     可能是板载电路占用、未引出、或该脚在此封装上不存在。")

    if not a.keep:
        print()
        print("⑤ 恢复核对 (复位后 MODER 应回到**配置前**的值)")
        print("   配置前 = 0x%08X   复位后 = 0x%08X   %s"
              % (moder_base, modr_r, "已恢复" if modr_r == moder_base else "[未回到基准值]"))

    print()
    if len(ok_bits) >= 8:
        print("=== 结论: GPIOE 至少 %d 位可用 ⇒ 定案② 成立 (DO 面取可用位的连续前缀) ==="
              % len(ok_bits))
        return 0
    print("=== 结论: 可用位不足 8 ⇒ **定案② 需要重新考虑** ===")
    print("    备选: ① 换一个端口 ② 缩窄 DO 位宽 (改断言 mask < (1<<N)) ③ 查是否某脚被板载占用")
    return 1


if __name__ == "__main__":
    sys.exit(main())
