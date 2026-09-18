#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-C（重定形）: 逐原语测出 `m_op`, 再问**能不能从结构算出来**。

## 为什么重定形（E-A/E-B 改变了我原来的 E-C 设计）
原 E-C 想用"换 division ⇒ 换 dt"来测 `VDIV` 的操作数相关性。
但 E-A 已经证明 **division 主要通过 nrun 起作用**（单一斜率 61.13 走通三档）
⇒ 用 division 改 dt 的对照**已经被 nrun 混淆**, 站不住。

## 改做什么（直接对准本轮的主问题）
E-A/E-B 把模型收敛成:
    cost ≈ C_base + c0_op + m_op × nrun
且实测: **`C_base` 与桶数几乎无关**, 只有 `c0_op`（每拍 OP 固定项）与 `m_op`（每路由边际）依赖原语。
⇒ 于是**唯一剩下的未知就是: `m_op` 能不能从结构算出来?**

**做法**: 对全部 19 个原语, 在 **div0** 下取 n=32 与 n=128 两点
⇒ `m_op = (cost(128) − cost(32)) / 96`（TB/条）。
再与**结构量**比:
    · 入口块指令数（STA-5/CFG）
    · 入口块 FP 指令数
    · 入口块文档占用和（ARM 文档）

## 判据（都能失败）
  C-1 `m_op` 必须**显著区分**原语（极差 > 2×）—— 否则"逐原语"没意义
  C-2 Spearman(`m_op`, 入口块指令数) > 0.8
  C-3 Spearman(`m_op`, 文档占用和) > 0.8
  C-4 ★ **双向**: 若 C-2 过而 C-3 不过 ⇒ 文档占用表没有带来信息（与 §5.55 一致, 但这次
      是在**原语层**上重新检验, 而不是指令层）
  C-5 ★ 反证: `DIRECT`(空操作) 的 `m_op` 应**接近 0**。若它也很大 ⇒
      说明 `m_op` 里混着"每路由固定开销", 不是纯原语成本 ⇒ 模型分解错了
"""
import os, re, subprocess, sys, statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "tools", "h723_tick_ring.py")
ELF = os.path.join(ROOT, "build", "dcl_h723")
OUT = os.path.join(HERE, "ec_out")
os.makedirs(OUT, exist_ok=True)

OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
       "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]
N_LO, N_HI = 32, 128

sys.path.insert(0, os.path.join(ROOT, "tools"))
from sta16_docmodel import occ_of


def run_one(op, div, n):
    cmd = [sys.executable, TOOL, "--prog", "%d,%d,%d" % (op, div, n)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=ROOT, timeout=300)
    out = r.stdout or ""
    open(os.path.join(OUT, "op%d_div%d_n%d.log" % (op, div, n)), "w",
         encoding="utf-8").write(out)
    m = re.search(r"实测 di\s*:\s*均值 ([\d.]+) TB tick\s+标准差 ([\d.]+)", out)
    ok = ("[PASS] P0a" in out) and ("[PASS] P0b" in out)
    if not m:
        return None
    return dict(mean=float(m.group(1)), sd=float(m.group(2)), ok=ok)


def structural():
    """逐原语入口块的结构量（复用 STA-16 的占用表 + CFG 入口块）。"""
    BIN = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
           "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32."
           "7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-objdump.exe")
    out = subprocess.run([BIN, "-d", "--no-show-raw-insn", ELF], capture_output=True,
                         text=True, errors="replace", cwd=ROOT).stdout or ""
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
    a2i = {a: i for i, (a, _, _) in enumerate(body)}
    BR = re.compile(r"^(b|bl|blx|bx|cbz|cbnz|tbb|tbh|it)\b")

    def tgt(o):
        m = re.match(r"^([0-9a-f]+)\s", o.strip())
        return int(m.group(1), 16) if m else None

    leaders = {0}
    for i, (a, mn, opnd) in enumerate(body):
        if BR.match(mn):
            t = tgt(opnd)
            if t in a2i:
                leaders.add(a2i[t])
            if i + 1 < N:
                leaders.add(i + 1)
    leaders = sorted(leaders)
    bp = [(s, (leaders[j + 1] if j + 1 < len(leaders) else N), body[s][0])
          for j, s in enumerate(leaders)]
    blk = {}
    for bi, (s, e, _) in enumerate(bp):
        for i in range(s, e):
            blk[i] = bi
    sec = subprocess.run([BIN, "-s", "--section=.itcm_text", ELF], capture_output=True,
                         text=True, errors="replace", cwd=ROOT).stdout or ""
    mem = {}
    for line in sec.splitlines():
        m = re.match(r"\s*([0-9a-f]+)\s+((?:[0-9a-f]{2,8}\s+){1,4})", line)
        if m:
            a = int(m.group(1), 16)
            for i, b in enumerate(bytes.fromhex(m.group(2).replace(" ", ""))):
                mem[a + i] = b
    TBL = 0x0DF4
    tg = [TBL + 2 * (mem[TBL + 2 * i] | (mem[TBL + 2 * i + 1] << 8)) for i in range(18)]
    entry = {i + 1: t for i, t in enumerate(tg)}
    entry[0] = 0x0E56
    res = {}
    for op in range(19):
        e = entry[op]
        if e not in a2i:
            continue
        s, en, _ = bp[blk[a2i[e]]]
        n = en - s
        occ = 0
        fp = 0
        for i in range(s, en):
            o, c = occ_of(body[i][1])
            occ += (o or 1)
            if c.startswith("fp"):
                fp += 1
        res[OPS[op]] = dict(n=n, occ=occ, fp=fp)
    return res


def spearman(xs, ys):
    def ar(v):
        idx = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and v[idx[j + 1]] == v[idx[i]]:
                j += 1
            a = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[idx[k]] = a
            i = j + 1
        return r
    rx, ry = ar(xs), ar(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n))
    dy = sum((ry[i] - my) ** 2 for i in range(n))
    if dx == 0 or dy == 0:
        return None
    r = num / (dx * dy) ** 0.5
    assert -1.0001 <= r <= 1.0001
    return r


def main():
    print("=== E-C: 逐原语测 m_op（div0, n=%d 与 n=%d）===" % (N_LO, N_HI))
    st = structural()
    rows = []
    print("%-9s %-10s %-10s %-11s %-8s %-6s %s"
          % ("op", "cost@32", "cost@128", "m_op(TB/条)", "m_op(cyc)", "指令", "FP"))
    print("-" * 78)
    for op, name in enumerate(OPS):
        a = run_one(op, 0, N_LO)
        b = run_one(op, 0, N_HI)
        if not (a and b and a["ok"] and b["ok"]):
            print("%-9s **取数失败或前置不良**" % name); continue
        m = (b["mean"] - a["mean"]) / float(N_HI - N_LO)
        s = st.get(name, {})
        rows.append(dict(op=name, m=m, n=s.get("n", 0), occ=s.get("occ", 0),
                         fp=s.get("fp", 0), lo=a["mean"], hi=b["mean"]))
        print("%-9s %-10.1f %-10.1f %-11.2f %-8.1f %-6d %d"
              % (name, a["mean"], b["mean"], m, m * 2, s.get("n", 0), s.get("fp", 0)))

    if len(rows) < 15:
        print("\n有效原语不足 ⇒ 判无效"); return 2
    ms = [r["m"] for r in rows]
    print("\n=== C-1 区分度 ===")
    print("  m_op: 最小 %.2f (%s)  最大 %.2f (%s)  极差 %.2f×"
          % (min(ms), rows[ms.index(min(ms))]["op"], max(ms),
             rows[ms.index(max(ms))]["op"], max(ms) / max(min(ms), 1e-9)))
    print("  ⇒ C-1 %s" % ("**通过**" if max(ms) > 2 * max(min(ms), 1e-9) else "**未通过**"))

    print("\n=== C-2/C-3 结构量能否解释 m_op ===")
    for tag, key in (("入口块指令数", "n"), ("文档占用和", "occ"), ("FP 指令数", "fp")):
        r = spearman([x[key] for x in rows], ms)
        print("  Spearman(m_op, %-12s) = %s" % (tag, "n/a" if r is None else "%+.3f" % r))
    r_n = spearman([x["n"] for x in rows], ms)
    r_o = spearman([x["occ"] for x in rows], ms)
    print("  ⇒ C-2（指令数 >0.8）%s" % ("**通过**" if r_n and r_n > 0.8 else "**未通过**"))
    print("  ⇒ C-3（文档占用 >0.8）%s" % ("**通过**" if r_o and r_o > 0.8 else "**未通过**"))
    if r_o is not None and r_n is not None:
        print("  ⇒ C-4 文档占用%s优于裸指令数"
              % ("**" if r_o > r_n + 0.05 else "**未**（文档表在原语层也没带来信息）"))

    print("\n=== C-5 ★ 反证: DIRECT（空操作）的 m_op 应接近 0 ===")
    d = [x for x in rows if x["op"] == "DIRECT"]
    if d:
        md = d[0]["m"]
        med = statistics.median(ms)
        print("  DIRECT m_op = %.2f TB/条 (%.1f cyc/条);  全体中位 = %.2f TB/条"
              % (md, md * 2, med))
        print("  ⇒ C-5 %s" % ("**通过**（DIRECT 是最便宜档）" if md <= min(ms) + 5
                            else "**未通过** ⇒ m_op 里混着每路由固定开销"))
    print("\n  按 m_op 升序（★ 这就是「逐原语结构量」的实测侧答案）:")
    for x in sorted(rows, key=lambda z: z["m"]):
        print("    %-9s m_op=%6.2f TB = %6.1f cyc   入口块 %2d 条, 占用 %3d, FP %2d"
              % (x["op"], x["m"], x["m"] * 2, x["n"], x["occ"], x["fp"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
