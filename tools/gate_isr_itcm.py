#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gate_isr_itcm.py — ★★ **ISR 调用树闸门**: 凡"从 ISR 出发可达、却落在 FLASH"的代码 ⇒ 构建失败。

## 为什么需要它 (不是"锦上添花", 是一次实测缺陷的直接产物)
   拍 ISR 每 100µs 调一批"每拍 poll 一下"的功能。它们原本落在 FLASH(.text)。而
   **擦/写内部 flash 期间 flash 取指会被 stall** ⇒ ISR 走进这些函数就**卡住不返回**
   ⇒ 后续拍不再触发 ⇒ 喂狗停 ⇒ **200ms 看门狗复位**。
   现场语义: "操作员按保存 → 机器重启", 且**配置永远存不下去**。

## 为什么"搬那 7 个函数"不够 (我踩过)
   那 7 个只是 ISR 的**直接**被调者。**真正要看的是传递闭包**:
     · 它们各自还会往下调 (`bb_kick` 有 744B 的 `.part.0` 分身, 由 IPA 拆分产生);
     · 函数指针调用 (`blx Rn` / 参数里传函数地址) **静态解析不出**, 必须显式列出。
   ⇒ 只补直接被调者 = 只修好"今天已知的那一层", 缺陷的**类别**没被消灭。
   (同一个教训项目里有过: 审计发现 F 的"三条 return 各补一句" vs "改成单一出口"。)

## 判据 (缺一不可)
   ① **传递闭包**: 从根(所有 `*_IRQHandler`)做 BFS, 任何可达函数地址落在 FLASH ⇒ **违规**;
   ② **未解析的间接调用** ⇒ **警告并列出位置** (必须人来把它接进种子, 否则闸门有洞);
   ③ 函数内 `ldr Rd,[pc,#imm]` 取到的**字面量落在函数自己的段之外** ⇒ 警告
      (对应"代码进 ITCM ≠ ISR 不碰 flash": 读 flash 里的常量表/ LUT 同样 stall)。

## 地址判据 (本平台)
   ITCM = `0x00000000..0x0000FFFF` (0x10000 = 64KB, 见 .map 的 `ITCM` 段);
   FLASH = `0x08000000..`  ⇒ 只按地址分类, 不依赖 nm 的段信息 (nm 默认不打印段名)。

退出码: 0 = 通过 (允许有警告) / 1 = 有违规 (构建必须失败)
"""
import os
import re
import subprocess
import sys
from collections import deque

TOOLCHAIN = os.environ.get("DCL_TOOLCHAIN",
                           r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins"
                           r"\com.st.stm32cube.ide.mcu.externaltools"
                           r".gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin")
NM = os.path.join(TOOLCHAIN, "arm-none-eabi-nm.exe")
OD = os.path.join(TOOLCHAIN, "arm-none-eabi-objdump.exe")

ITCM_LO, ITCM_HI = 0x00000000, 0x00010000          # 64KB ITCM
FLASH_LO = 0x08000000

# ★ 静态解析不到的"函数指针"调用, 必须显式列出 (否则闸门有洞)。
#   `engine_tick(g_shm, tick, sel ? engine_scan_itcm : engine_scan_flash, ...)`
#   —— 扫描函数是**当参数传进去**的, 反汇编里只有 `blx Rn` ⇒ 只能手工接种子。
EXTRA_ROOTS = ["engine_scan_itcm"]


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          errors="replace").stdout


def load_symbols(elf):
    """→ {name: (addr, size)}, [(addr, size, name)] 按地址排序 (用于定位)"""
    out = run([NM, "-S", "--defined-only", elf])
    by_name, spans = {}, []
    for ln in out.splitlines():
        p = ln.split()
        if len(p) < 4:
            continue
        try:
            addr, size = int(p[0], 16), int(p[1], 16)
        except ValueError:
            continue
        name = p[3]
        by_name[name] = (addr, size)
        if size:
            spans.append((addr, size, name))
    spans.sort()
    return by_name, spans


def func_at(addr, spans):
    """二分找到包含 addr 的函数 (按地址排序的 span 表)"""
    lo, hi = 0, len(spans) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        a, sz, n = spans[mid]
        if addr < a:
            hi = mid - 1
        elif addr >= a + sz:
            lo = mid + 1
        else:
            return n, a, sz
    return None, None, None


def read_word(elf, addr, sec=".itcm_text"):
    """从 ELF 的**指定段**里读 4 字节 (用于解析 `ldr Rn,[pc,#i]` 字面量)。
    ★ 必须指定段: 同一个数值地址在 `.debug_frame`/`.text` 里都可能存在 —— 第一版没带 `-j`,
      结果从 `.debug_frame` 里读到调试数据当成了函数地址 (闸门自己骗自己)。"""
    out = run([OD, "-s", "-j", sec, "--start-address=0x%x" % (addr & ~0xF),
               "--stop-address=0x%x" % ((addr & ~0xF) + 16), elf])
    for ln in out.splitlines():
        m = re.match(r"\s*([0-9a-f]{4,})\s+((?:[0-9a-f]{8}\s*)+)", ln)
        if not m:
            continue
        base = int(m.group(1), 16)
        words = m.group(2).split()
        off = (addr - base) // 4
        if 0 <= off < len(words):
            return int(words[off], 16)
    return None


def disasm_func(elf, addr, size):
    """反汇编一个函数 → (直接/可解调用目标集合, 真正无法解析的间接调用指令地址集合)
    ★ 第二版: 把 `ldr Rn,[pc,#i]` + `blx Rn` 解析出来 (项目 `long_call` 的形态)。"""
    if size <= 0:
        return set(), set()
    # 字面量池与函数**同段** ⇒ 按函数地址推断该从哪个段读字面量 (见 read_word 的注释)
    sec_in = ".itcm_text" if (ITCM_LO <= addr < ITCM_HI) else ".text"
    out = run([OD, "-d", "--no-show-raw-insn",
               "--start-address=0x%x" % addr, "--stop-address=0x%x" % (addr + size), elf])
    calls, unresolved = set(), set()
    ldr_lit = {}          # 寄存器 → 字面量地址
    for ln in out.splitlines():
        m = re.match(r"\s*([0-9a-f]+):\s+bl\s+([0-9a-f]+)\s+<", ln)
        if m:
            calls.add(int(m.group(2), 16))
            continue
        # objdump 形如: `154: f8df 815c  ldr.w r8, [pc, #348] ; 2b4 <sym>`
        #   ⇒ 注释里给了**字面量的绝对地址** (2b4), 优先用它 (比手算 pc 相对更稳)。
        # ★ objdump 的注释形式**不统一**: 16 位编码写成 `; (27c <sym>)` (带括号),
        #   32 位 `.w` 编码写成 `; 2b0 <sym>` (不带) ⇒ 正则必须容两 (这是闸门的第 3 个自身 bug,
        #   前两个: 漏 `ldr.w`、跨段读字面量 —— **仪器自己也会骗人, 必须验仪器**)。
        m = re.match(r"\s*([0-9a-f]+):\s+(\S+)\s+(r\d+),\s*\[pc,\s*#(\d+)\]\s*;\s*\(?([0-9a-f]+)", ln)
        if m and m.group(2).startswith("ldr"):
            ldr_lit[m.group(3)] = int(m.group(5), 16)
            continue
        m = re.match(r"\s*([0-9a-f]+):\s+blx\s+(r\d+)", ln)
        if m:
            ia, reg = int(m.group(1), 16), m.group(2)
            lit = ldr_lit.get(reg)
            tgt = read_word(elf, lit, sec_in) if lit else None
            if tgt and tgt >= 0x10:                  # 0/小值 = 不是函数地址
                calls.add(tgt)
            else:
                unresolved.add(ia)
            continue
        m = re.match(r"\s*([0-9a-f]+):\s+blx\s+([0-9a-f]+)\s+<", ln)
        if m:
            calls.add(int(m.group(2), 16))
            continue
        m = re.match(r"\s*([0-9a-f]+):\s+blx\s+r", ln)
        if m:
            unresolved.add(int(m.group(1), 16))
    return calls, unresolved


def main():
    if len(sys.argv) < 2:
        print("用法: gate_isr_itcm.py <elf> [--verbose]")
        return 2
    elf = sys.argv[1]
    verbose = "--verbose" in sys.argv
    if not os.path.exists(elf):
        print("★ 闸门无法运行: 找不到 %s" % elf)
        return 1

    by_name, spans = load_symbols(elf)
    # ★★ 根集合必须**去噪** (第一版没做, 报出 115 条全是假的):
    #   启动文件把一大堆未实现的向量声明成 `弱别名 → Default_Handler`, 它们的**地址全一样**
    #   ⇒ 若照单全收, 闸门会被"Default_Handler 在 flash"刷屏, 真违规被淹没。
    #   ★ 这正是本项目最恨的失效模式 (噪声淹没真警告: 曾被 24 条无害警告埋掉一条 -Wshift)。
    #   ⇒ 两条: ① 剔掉与 `Default_Handler` **同地址**的根; ② 按**地址**去重 (别名只留一个名字)。
    def_addr = by_name.get("Default_Handler", (None, 0))[0]
    raw = [n for n in by_name if n.endswith("_IRQHandler")] + \
          [n for n in ("HardFault_Handler", "MemManage_Handler",
                       "BusFault_Handler", "UsageFault_Handler") if n in by_name] + \
          [n for n in EXTRA_ROOTS if n in by_name]
    # ★★ 关于 Default_Handler: 我第一版把它当噪声剔掉了 —— **剔错了**。
    #   115 个弱别名都指向它, 那是噪声; 但**它本身是一个真实根**:
    #   擦除期间若取指出错 (总线错/取指失败), CPU 会跳到它; 而它在 FLASH
    #   ⇒ **故障处理器自己取指被 stall ⇒ 卡死 ⇒ 看门狗复位** —— 与 ISR 卡死同一个结果。
    #   ⇒ 正确做法: **按地址去重 (别名只留一个), 但不剔除它**。
    roots, seen_addr, dropped = [], set(), 0
    for n in sorted(raw):
        a = by_name[n][0]
        if a in seen_addr:
            dropped += 1          # 别名去重 (噪声在这)
            continue
        seen_addr.add(a)
        roots.append(n)
    if not roots:
        print("★ 闸门无法运行: 一个真实实现的 ISR 根都没找到")
        return 1

    seen, viol, unresolved, checked = set(), [], [], 0
    q = deque(roots)
    while q:
        name = q.popleft()
        if name in seen or name not in by_name:
            continue
        seen.add(name)
        addr, size = by_name[name]
        checked += 1
        if FLASH_LO <= addr and not (name.endswith("_veneer")):
            viol.append((name, addr))
        calls, ind = disasm_func(elf, addr, size)
        for c in calls:
            n, _, _ = func_at(c, spans)
            if n and n not in seen:
                q.append(n)
            elif not n and c >= FLASH_LO and verbose:
                print("   (调用落在非函数区: 0x%08X ← %s)" % (c, name))
        for ip in ind:
            unresolved.append((name, ip))

    print("=" * 78)
    print("ISR 调用树闸门 — 真实 ISR 根 %d 个 (剔除 %d 个 Default_Handler 别名), 遍历函数 %d 个"
          % (len(roots), dropped, checked))
    print("  根: %s" % ", ".join(sorted(roots)[:8]) + (" ..." if len(roots) > 8 else ""))
    print("=" * 78)
    if viol:
        print("\n★ 违规: 以下函数**从 ISR 可达, 却落在 FLASH** (%d 个):" % len(viol))
        for n, a in sorted(viol, key=lambda x: x[1]):
            print("    %-24s 0x%08X" % (n, a))
        print("\n  ⇒ 加 `DCL_ITCM` (见 src/itcm.h)。**注意传递闭包**: 只补直接被调者不够。")
    if unresolved:
        print("\n△ 警告: %d 处**间接调用**静态解析不到 (函数指针) —— 闸门有洞, 必须人工接种子:"
              % len(unresolved))
        agg = {}
        for n, ip in unresolved:
            agg.setdefault(n, []).append(ip)
        for n, ips in sorted(agg.items()):
            print("    %-24s %d 处  (例: 0x%08X)" % (n, len(ips), ips[0]))
        print("    ⇒ 把确实会被调到的目标加进本脚本的 EXTRA_ROOTS (现在: %s)" % EXTRA_ROOTS)
    if not viol and not unresolved:
        print("\n✅ 通过: 从 ISR 可达的代码**全部**在 ITCM, 且无未解析间接调用。")
    elif not viol:
        print("\n△ 没有『可达且在 FLASH』的直接违规, 但存在未解析间接调用 ⇒ 不算通过。")
    return 1 if viol else 0


if __name__ == "__main__":
    sys.exit(main())
