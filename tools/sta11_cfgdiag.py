#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-11: 诊断 CFG —— 为什么"能到循环顶"的集合是空的。

判据（每条都能失败）
  E1 从 LOOP_TOP 前向可达的块数应 ≈ 全部块数（循环体几乎所有块都在循环里）
  E2 从 LOOP_TOP 反向可达（即"能到 LOOP_TOP"）的块数应 ≥ 入口块与尾块的块数
  E3 必须存在一条 入口 → 尾块 → LOOP_TOP 的路径; 把该路径**逐块打印**出来
     （这是"路径口径"到底成不成立的决定性证据）
若 E1/E2/E3 任一失败 ⇒ 说明 CFG 构造有洞（多半是分支目标解析不全）, 而不是板子/代码的问题。
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


def run(args):
    return subprocess.run([BIN + "objdump.exe"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def target(op):
    m = re.match(r"^([0-9a-f]+)\s", op.strip())
    return int(m.group(1), 16) if m else None


def main():
    out = run(["-d", "--no-show-raw-insn", ELF])
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
    print("块数 %d" % len(blocks))

    succ = [[] for _ in blocks]
    unresolved = []
    for bi, (s, e, _) in enumerate(blocks):
        a, mn, opnd = body[e - 1]
        if BR.match(mn):
            t = target(opnd)
            if t in addr2i:
                succ[bi].append(blk_of[addr2i[t]])
            elif t is not None:
                unresolved.append((a, mn, opnd, t))
            if not UNCOND.match(mn) and e < N:
                succ[bi].append(blk_of[e])
        elif e < N:
            succ[bi].append(blk_of[e])

    if unresolved:
        print("!! 有 %d 条分支的目标**不在函数体内**（可能是跳到函数外/表）:" % len(unresolved))
        for a, mn, opnd, t in unresolved[:15]:
            print("    %08X %-9s %s  (目标 0x%X)" % (a, mn, opnd, t))

    # 哪些块没有后继？
    noseq = [(bi, blocks[bi][2], body[blocks[bi][1] - 1][1], body[blocks[bi][1] - 1][2])
             for bi in range(len(blocks)) if not succ[bi]]
    print("无后继的块 %d 个:" % len(noseq))
    for bi, a0, lmn, lop in noseq:
        print("    块%-3d @0x%08X 末条 %-9s %s" % (bi, a0, lmn, lop))

    LOOP_TOP = 0xD3C
    lt = blk_of[addr2i[LOOP_TOP]]
    print("\n循环顶块 = %d @0x%08X" % (lt, blocks[lt][2]))

    def fwd(start):
        seen, q = set(), deque([start])
        while q:
            b = q.popleft()
            if b in seen:
                continue
            seen.add(b)
            for s in succ[b]:
                if s not in seen:
                    q.append(s)
        return seen

    def rev(start):
        R = [[] for _ in blocks]
        for b, ss in enumerate(succ):
            for s in ss:
                R[s].append(b)
        seen, q = set(), deque([start])
        while q:
            b = q.popleft()
            if b in seen:
                continue
            seen.add(b)
            for r in R[b]:
                if r not in seen:
                    q.append(r)
        return seen

    F, B = fwd(lt), rev(lt)
    print("\nE1 从循环顶前向可达: %d/%d 块 ⇒ %s"
          % (len(F), len(blocks), "通过" if len(F) >= 0.8 * len(blocks) else "**失败**"))
    print("E2 能到循环顶（反向可达）: %d/%d 块 ⇒ %s"
          % (len(B), len(blocks), "通过" if len(B) >= 0.5 * len(blocks) else "**失败**"))

    # E3: 找一条 入口 → 循环顶 的路径, 打印出来
    print("\nE3 找 入口(0x10F4 ARITH) → 循环顶 的路径:")
    start = blk_of[addr2i[0x10F4]]
    prev = {start: None}
    q = deque([start])
    found = False
    while q:
        b = q.popleft()
        if b == lt:
            found = True
            break
        for s in succ[b]:
            if s not in prev:
                prev[s] = b
                q.append(s)
    if not found:
        print("    ✗ 找不到路径 ⇒ **路径口径在当前 CFG 上不成立**")
        print("    从入口可达的块: %s" % sorted(fwd(start)))
        print("    能到循环顶的块: %s" % sorted(B))
        print("    两者交集: %s" % sorted(fwd(start) & B))
    else:
        path, b = [], lt
        while b is not None:
            path.append(b)
            b = prev[b]
        path.reverse()
        print("    ✓ 路径 %d 块:" % len(path))
        for b in path:
            s, e, a0 = blocks[b]
            print("      块%-3d @0x%08X %2d 条  末条 %-9s %s"
                  % (b, a0, e - s, body[e - 1][1], body[e - 1][2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
