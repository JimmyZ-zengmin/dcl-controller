#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-9（步 A 的调试）: 打印 op 入口的基本块与后继, 找出可达性为什么被低估。

现场: CMP/ARITH 的 `可达条数` = 8/7, 而它们的入口块分别有 8/7 条 —— 也就是说
**后继一个都没走到**。但两者末尾都是 `b.n 0xe5c`（无条件跳公共尾块）⇒ 不可能没有后继。
⇒ 判据: **入口块的后继列表不得为空**（除非块以 `bx lr` 结尾）。这条能失败, 直接暴露 bug。
"""
import os, re, subprocess, sys
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
       "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32."
       "7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-")
ELF = os.path.join(ROOT, "build", "dcl_h723")
BR = re.compile(r"^(b|bl|blx|bx|cbz|cbnz|tbb|tbh|it)\b")
UNCOND = re.compile(r"^(b|bx|tbb|tbh)\b")


def run(tool, args):
    return subprocess.run([tool] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def target(op):
    m = re.match(r"^([0-9a-f]+)\s", op.strip())
    return int(m.group(1), 16) if m else None


def main():
    out = run(BIN + "objdump.exe", ["-d", "--no-show-raw-insn", ELF])
    ins = []
    for line in out.splitlines():
        m = re.match(r"\s*([0-9a-f]+):\s+([a-z][a-z0-9.]*)\s*(.*)$", line)
        if m:
            ins.append((int(m.group(1), 16), m.group(2), m.group(3).strip()))
    hdrs = sorted((int(m.group(1), 16), m.group(2))
                  for m in re.finditer(r"^([0-9a-f]{8}) <([^>]+)>:", out, re.M))
    lo = next(a for a, n in hdrs if n == "engine_scan_itcm")
    hi = next((a for a, _ in hdrs if a > lo), lo + 0x4000)
    body = [x for x in ins if lo <= x[0] < hi]
    N = len(body)
    addr2i = {a: i for i, (a, _, _) in enumerate(body)}

    leaders = {0}
    for i, (a, mn, opnd) in enumerate(body):
        if BR.match(mn):
            t = target(opnd)
            if t in addr2i:
                leaders.add(addr2i[t])
            if i + 1 < N:
                leaders.add(i + 1)
    leaders = sorted(leaders)
    blocks = [(s, (leaders[j + 1] if j + 1 < len(leaders) else N), body[s][0])
              for j, s in enumerate(leaders)]
    blk_of = {}
    for bi, (s, e, _) in enumerate(blocks):
        for i in range(s, e):
            blk_of[i] = bi

    # 逐块判后继, 并把"为什么"打出来
    print("入口块后继检查（判据: 非 `bx lr` 结尾的块, 后继不得为空）")
    bad = []
    for op, ent in (("CMP", 0x120C), ("ARITH", 0x10F4), ("DIRECT", 0x0E56),
                    ("PID", 0x1140), ("LPF", 0x1114), ("CNT", 0x1032)):
        if ent not in addr2i:
            print("  %-8s 入口 0x%X 不在体内" % (op, ent)); continue
        bi = blk_of[addr2i[ent]]
        s, e, a0 = blocks[bi]
        last = e - 1
        la, lmn, lop = body[last]
        t = target(lop)
        chain, seen, q = [], set(), deque([bi])
        while q:
            b = q.popleft()
            if b in seen:
                continue
            seen.add(b)
            chain.append(b)
            bs, be, _ = blocks[b]
            ba, bmn, bop = body[be - 1]
            if BR.match(bmn):
                tt = target(bop)
                if tt in addr2i:
                    q.append(blk_of[addr2i[tt]])
                if not UNCOND.match(bmn) and be < N:
                    q.append(blk_of[be])
            elif be < N:
                q.append(blk_of[be])
        print("  %-8s 块@0x%08X %d 条, 末条 %-8s %-22s 后继=%s  可达块=%d"
              % (op, a0, e - s, lmn, lop, "有" if len(seen) > 1 else "**无**", len(seen)))
        if lmn != "bx" and len(seen) == 1:
            bad.append(op)
        # 末条若是分支, 单独核对目标是否被算成后继
        if BR.match(lmn):
            print("        末条分支目标 0x%X ⇒ 在体内? %s ⇒ 块 %s"
                  % (t if t else 0, t in addr2i,
                     blk_of.get(addr2i.get(t, -1), "**不在表里**")))
    print("\n判据结果: %s" % ("**通过**（每个入口块都有后继）" if not bad
                          else "**失败** —— 这些入口块没有后继: %s" % bad))

    print("\n★ 另: 打印 0x0E5C 那个共享尾块的内容（27 条, 被 17 个 op 可达）")
    bi = blk_of[addr2i[0x0E5C]]
    s, e, a0 = blocks[bi]
    for i in range(s, min(e, s + 30)):
        a, mn, opnd = body[i]
        print("    %08X  %-9s %s" % (a, mn, opnd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
