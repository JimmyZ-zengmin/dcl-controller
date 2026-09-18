#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-2: 从 `engine_scan_itcm` 的 **tbh 跳转表** 切出每个原语的代码区间。

为什么这是"FPGA 那套"的第一步:
  FPGA 工具靠"网表 + 原语延迟"算关键路径。MCU 上的对应物是
  "指令序列 + 指令延迟"。本项目有利之处: 19 个原语**全部内联**进扫描体
  （`prim_exec` 没有独立符号 ⇒ 编译器把 switch 展开了), 而 op 分派是
  **`tbh` 跳转表** ⇒ **每个原语的代码区间可以从 ELF 里精确取出来**。
  ⇒ 这就把"程序执行时间"变成**可沿结构累加**的量, 而不是只能实测的黑盒。

做法:
  1. 反汇编 engine_scan_itcm, 找 `tbh [pc, rN, lsl #1]`（半字表, 相对 PC）
  2. 解析该处的 PC 与表内容（ELF 文件里按 vaddr→offset 换算）
  3. 表项 = 相对 PC 的半字偏移 ⇒ 得到每个 case 的入口地址
  4. 按入口排序 ⇒ 每个原语的 [起, 止) 区间 ⇒ 指令清单与分类直方图
"""
import os, re, struct, subprocess, sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
       "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32."
       "7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-")
OBJDUMP, READELF = BIN + "objdump.exe", BIN + "readelf.exe"
ELF = os.path.join(ROOT, "build", "dcl_h723")


def run(tool, args):
    return subprocess.run([tool] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def sections():
    """读段落表 → [(name, vaddr, size, offset)]"""
    out = run(READELF, ["-S", "-W", ELF])
    secs = []
    for line in out.splitlines():
        m = re.match(r"\s*\[\s*\d+\]\s+(\S+)\s+(\S+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)", line)
        if m:
            name, _typ, addr, off, size = m.groups()
            secs.append((name, int(addr, 16), int(size, 16), int(off, 16)))
    return secs


def read_vaddr(secs, blob, addr, n):
    for name, va, size, off in secs:
        if va <= addr and addr + n <= va + size and size:
            fo = off + (addr - va)
            return blob[fo:fo + n]
    return None


def main():
    blob = open(ELF, "rb").read()
    secs = sections()
    out = run(OBJDUMP, ["-d", "--no-show-raw-insn", ELF])
    ins = []
    for line in out.splitlines():
        m = re.match(r"\s*([0-9a-f]+):\s+([a-z][a-z0-9.]*)\s*(.*)$", line)
        if m:
            ins.append((int(m.group(1), 16), m.group(2), m.group(3).strip()))
    hdrs = sorted((int(m.group(1), 16), m.group(2))
                  for m in re.finditer(r"^([0-9a-f]{8}) <([^>]+)>:", out, re.M))
    lo = next(a for a, n in hdrs if n == "engine_scan_itcm")
    nxt = [a for a, _ in hdrs if a > lo]
    hi = nxt[0] if nxt else lo + 0x4000
    body = [(a, mn, op) for a, mn, op in ins if lo <= a < hi]
    print("engine_scan_itcm: 0x%08X .. 0x%08X（%d 条指令）" % (lo, hi, len(body)))

    tbhs = [(a, mn, op) for a, mn, op in body if mn in ("tbh", "tbb")]
    print("跳转表 %d 个:" % len(tbhs))
    for a, mn, op in tbhs:
        m = re.match(r"\[pc,\s*r(\d+),\s*lsl #1\]", op)
        if not m:
            print("   0x%08X %s %s（解析不了, 跳过）" % (a, mn, op)); continue
        base = (a + 4) & ~3
        # 表长未知 ⇒ 先按 32 项读（op 只有 19 个 + 越界档）
        raw = read_vaddr(secs, blob, base, 2 * 40)
        if raw is None:
            print("   0x%08X tbh @表 0x%08X —— 读不到表内容" % (a, base)); continue
        ents = struct.unpack("<40H", raw)
        tgts = [base + 2 * v for v in ents]
        print("   0x%08X tbh [pc,r%s] 表@0x%08X" % (a, m.group(1), base))
        print("      前 20 项目标: %s" % " ".join("%X" % t for t in tgts[:20]))
        # 只保留落在扫描体内的目标（其余是越界/默认档）
        inb = sorted(set(t for t in tgts[:20] if lo <= t < hi))
        print("      落在扫描体内的不重复目标 %d 个: %s"
              % (len(inb), " ".join("%X" % t for t in inb)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
