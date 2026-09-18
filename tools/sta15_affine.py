#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-15: 结构量 vs 实测的**仿射**模型 + 留一验证（步 A' 的收口检验）。

## 为什么想到仿射（数据里的线索, 不是事后凑）
  入口块只有 **2 条**指令的 `DIRECT` 实测 **56 cyc**; 4~5 条的 `AND/OR/NOT` 实测 **71~78 cyc**。
  若成本 ∝ 条数, 这些点该逼近 0 —— 实际有一个 **~55 cyc 的地板**。
  ⇒ 每处理一条路由有一份**与 op 无关的固定开销**（循环推进 + 写回 + 记账）。
     这与 §4.4 的发现一致: 那块"共享尾块"(27 条) 是**每路由都要走**的。
  ⇒ 正确形式应是 `cost ≈ c0 + m × (op 独有部分)`。

## 判据（都能失败, 且都带 LOO）
  H1 仿射拟合的 LOO 中位|残差| 必须**显著小于**纯比例模型
  H2 截距 `c0` 必须是**正数**且量级合理（几十 cyc 级）—— 若拟合出负数 ⇒ 模型形状错
  H3 把 `c0` 与"共享尾块的实测份额"对照:
     尾块 27 条, 若 `c0 / 27` 落在"每条指令周期"的合理区间(1~10), 说明**两个独立量自洽**
  H4 残差不得与 op 类别系统相关（报出最大几个残差, 逐个人工判读）
"""
import os, re, subprocess, sys, statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
       "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32."
       "7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-")
ELF = os.path.join(ROOT, "build", "dcl_h723")
ENGINE_C = os.path.join(ROOT, "src", "engine.c")
OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
       "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]
SHARED_TAIL = 27          # 步 A 定量: 每路由共用尾块（块6）的指令数


def run(args):
    return subprocess.run([BIN + "objdump.exe"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def target(op):
    m = re.match(r"^([0-9a-f]+)\s", op.strip())
    return int(m.group(1), 16) if m else None


def ols2(xs, ys):
    """一元线性: y = c0 + m x。返回 (c0, m) 或 None。"""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    m = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / den
    return my - m * mx, m


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
    BR = re.compile(r"^(b|bl|blx|bx|cbz|cbnz|tbb|tbh|it)\b")
    leaders = {0}
    for i, (a, mn, opnd) in enumerate(body):
        if BR.match(mn):
            t = target(opnd)
            if t in addr2i:
                leaders.add(addr2i[t])
            if i + 1 < N:
                leaders.add(i + 1)
    leaders = sorted(leaders)
    bp = [(s, (leaders[j + 1] if j + 1 < len(leaders) else N), body[s][0])
          for j, s in enumerate(leaders)]
    blk = {}
    for bi, (s, e, _) in enumerate(bp):
        for i in range(s, e):
            blk[i] = bi
    sec = run(["-s", "--section=.itcm_text", ELF])
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
    txt = open(ENGINE_C, encoding="utf-8", errors="replace").read()
    mm = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", txt, re.S)
    meas = [int(x) for x in re.findall(r"\d+", mm.group(1))]

    names, sizes, M = [], [], []
    for op in range(19):
        e = entry[op]
        if e not in addr2i:
            continue
        bi = blk[addr2i[e]]
        s, en, _ = bp[bi]
        names.append(OPS[op]); sizes.append(en - s); M.append(meas[op])

    # 全量拟合
    c0, m = ols2(sizes, M)
    print("全量仿射拟合: cost ≈ %.1f + %.2f × (入口块条数)" % (c0, m))

    # H2 截距符号
    print("\nH2 截距 = %.1f cyc（%s）" % (c0, "正数 ✓" if c0 > 0 else "**负数 ⇒ 模型形状错**"))

    # H1 LOO: 仿射 vs 比例
    aff_err, prop_err = [], []
    for i in range(len(sizes)):
        xs = [sizes[j] for j in range(len(sizes)) if j != i]
        ys = [M[j] for j in range(len(M)) if j != i]
        r = ols2(xs, ys)
        if r:
            aff_err.append(M[i] - (r[0] + r[1] * sizes[i]))
        K = statistics.median(ys[j] / float(xs[j]) for j in range(len(xs)))
        prop_err.append(M[i] - K * sizes[i])
    aae = sorted(abs(e) for e in aff_err)
    ape = sorted(abs(e) for e in prop_err)
    print("\nH1 留一交叉验证（LOO）:")
    print("    仿射(c0+m·n): 中位|残差| %6.1f  最大 %6.1f  平均相对 %5.1f%%"
          % (statistics.median(aae), max(aae),
             100 * sum(abs(aff_err[i]) / M[i] for i in range(len(M))) / len(M)))
    print("    比例(K·n)   : 中位|残差| %6.1f  最大 %6.1f  平均相对 %5.1f%%"
          % (statistics.median(ape), max(ape),
             100 * sum(abs(prop_err[i]) / M[i] for i in range(len(M))) / len(M)))
    beta = statistics.median(aae) < 0.6 * statistics.median(ape)
    print("    ⇒ 仿射%s显著优于比例（中位比 %.2f）" % ("**" if beta else "**未**",
                                              statistics.median(aae) / max(statistics.median(ape), 1e-9)))

    # H3 与共享尾块自洽性
    print("\nH3 与步 A 的共享尾块定量对照:")
    print("    尾块指令数 = %d（块6, 每路由都要走）" % SHARED_TAIL)
    print("    截距 c0/TOTAL ≈ %.1f cyc/条" % (c0 / SHARED_TAIL) if c0 > 0 else "    （截距非正, 不适用）")
    print("    斜率 m = %.2f cyc/条（op 独有部分）" % m)
    print("    ★ 判读: 若 c0/尾块条数 与 m 落在**同一量级**, 说明两个独立量自洽;")
    print("      若差一个数量级, 说明「固定开销」并不等于那块尾块 —— 需点名。")

    # H4 残差清单
    print("\nH4 残差清单（正 = 结构高估）:")
    print("    %-9s %-6s %-7s %-9s %+8s" % ("op", "条数", "实测", "仿射预测", "残差"))
    for i, nm in enumerate(names):
        p = c0 + m * sizes[i]
        print("    %-9s %-6d %-7d %-9.1f %+8.1f%s"
              % (nm, sizes[i], M[i], p, M[i] - p,
                 "  ★" if abs(M[i] - p) > 0.25 * M[i] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
