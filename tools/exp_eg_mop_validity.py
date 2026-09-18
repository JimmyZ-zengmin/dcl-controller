#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""③ `m_op` 的**程序无关性**验证 —— 本模型的地基检验。

## 要测的命题
模型说 `m_op` 是**原语的属性**（与程序无关）⇒ 标定一次, 所有程序通用。
**这条从未被正面测过。** 若它不成立 ⇒ "可算"退回"按程序标定"。

## 怎么才能算"正面测"
固定 op, **只让"路由在桶里怎么分布"变**, 看每路由边际成本是否不变。
三个自变量:
    k      = 每个桶里几条路由（桶内密度）
    nrun   = 每拍总条数
    n_bkt  = 每拍有多少个桶非空

★ 关键: 用 **div1 vs div2** 做对照 —— 它们可以**同 k、同 nrun, 只差桶数**。

## 一个必须用上的固件事实（这决定了哪些点**物理上可达**）
`engine.c:990-992` 的部署器**覆盖** payload 里的 phase:
```c
ph = (uint8_t)(q1++ % BUCKET_DIV1_PHASES);   // div1: 0..9
ph = (uint8_t)(q2++ % BUCKET_DIV2_PHASES);   // div2: 0..99  ← 但…
r.period = (uint8_t)(dv | (uint8_t)(ph << PERIOD_PHASE_SHIFT));
```
而读侧 `engine.c:518` 是 `ph = (period >> SHIFT) & 0x3Fu` ⇒ **phase 只有 6 位 (0..63)**。
⇒ **`q2 % 100` 一旦 ≥64, 存进去的 phase 会被截成 `q2-64`**
⇒ **div2 在 n > 64 时, 桶 0..35 会被二次填充, 桶 36..99 恒空。**
★ 所以"div2 n=100 ⇒ 100 个桶各 1 条"这个**直觉是错的** —— 实际是
"桶 0..35 各 2~3 条, 桶 36..99 空"。
**本脚本先把桶表读出来核对这件事**（否则后面所有解释都建立在错的图景上）。

## 判据（都能失败）
  W-0 桶表核对: div2 n=128 时, `cnt2[64..99]` 是否恒 0; `cnt2[0..35]` 是否 >1
  W-1 ★ 主判据: **同 k、同 nrun, 只差桶数**的两组, 每路由成本差 ≤10%
      若差 >30% ⇒ **`m_op` 依赖桶分布 ⇒ 模型要重写**
  W-2 扫描段必须随 nrun 单调（否则取数坏了）
"""
import os, re, struct, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst, cmd_reset = 0x38, 0x10, 0x12, 0x11, 0x22, 0x13
S_LAST, S_MIN, S_MAX, S_N, S_LO, S_HI, S_NRUN = (0x3800, 0x3804, 0x3808,
                                                 0x380C, 0x3810, 0x3814, 0x3818)
OFF_BUCKETS = 0x4480
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02


def mk(op, div, n):
    fl = FLAG_ACTIVE | FLAG_WIRE2
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  fl, i, (i % 64) + 1, 0, i, div, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    states = b"\x00" * (16 * (min(n, 64) + 1))
    return struct.pack("<HHH", n, n, min(n, 64) + 1) + routes + params + states + b"\x00" * 16


def rd(dcl, addr, nwords):
    out, off = b"", 0
    while off < nwords:
        k = min(200, nwords - off)
        sts, p = dcl.send(cmd_burst, struct.pack("<IH", addr + 4 * off, k), expect_len=None)
        if sts != "ACK" or len(p) < 4 * k:
            return None
        out += p[:4 * k]
        off += k
    return out


def main():
    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("端口 = %s" % dcl.port); time.sleep(1.0)
    sts, p = dcl.send(cmd_status, expect_len=51)
    if sts != "ACK" or len(p) < 51:
        print("!! 0x38 失败"); return 2
    SHM = struct.unpack("<I", p[23:27])[0]
    print("SHM = 0x%08X" % SHM)
    ok = 0
    for _ in range(8):
        v = rd(dcl, SHM + 0x3880, 2)
        if v and struct.unpack("<2I", v[:8])[1] >= 200000:
            ok += 1
            if ok >= 3:
                break
        else:
            ok = 0
        time.sleep(0.25)
    if ok < 3:
        print("!! 板子不健康 ⇒ 判无效"); dcl.close(); return 2
    print("健康门通过")

    def deploy_and_measure(op, div, n, tag):
        sts, pp = dcl.send(cmd_deploy, mk(op, div, n), expect_len=None)
        if sts != "ACK":
            return None
        dcl.send(cmd_stop); time.sleep(0.12)
        dcl.send(cmd_start); time.sleep(0.12)
        time.sleep(1.2)
        raw = rd(dcl, SHM + S_LAST, (S_NRUN + 4 - S_LAST) // 4)
        if raw is None:
            return None
        u = struct.unpack("<%dI" % (len(raw) // 4), raw)

        def at(o):
            return u[(o - S_LAST) // 4]

        sn, slo, shi = at(S_N), at(S_LO), at(S_HI)
        if not sn:
            return None
        return dict(mean=(slo | (shi << 32)) / float(sn), nrun=at(S_NRUN))

    # ── W-0: 桶表核对（这是后面所有解释的前提）──
    print("\n=== W-0 桶表核对: div2 的 phase 截断 ===")
    sts, pp = dcl.send(cmd_deploy, mk(5, 2, 128), expect_len=None)
    if sts == "ACK":
        dcl.send(cmd_stop); time.sleep(0.12)
        dcl.send(cmd_start); time.sleep(0.2)
        bk = rd(dcl, SHM + OFF_BUCKETS, 110)
        if bk:
            u = struct.unpack("<220H", bk)
            off1, cnt1 = list(u[0:10]), list(u[10:20])
            off2, cnt2 = list(u[20:120]), list(u[120:220])
            nz = [(i, c) for i, c in enumerate(cnt2) if c]
            print("  div2 n=128: 非空桶 %d 个" % len(nz))
            print("    桶 0..35 的 cnt2 = %s" % cnt2[:36])
            print("    桶 36..63 的 cnt2 = %s" % cnt2[36:64])
            print("    桶 64..99 的 cnt2 = %s" % cnt2[64:100])
            print("  ⇒ 桶 64..99 %s（断言: 恒 0）"
                  % ("**全 0 ✓**" if all(c == 0 for c in cnt2[64:]) else "**有非零 ✗**"))
            print("  ⇒ 桶 0..35 %s（预期 >1, 因 phase 截断二次填充）"
                  % ("**有 >1 的 ✓**" if any(c > 1 for c in cnt2[:36]) else "全 ≤1"))
            s = sum(cnt2)
            print("  cnt2 总和 = %d（应 = 128）" % s)
            print("  每拍条数 nrun = 非空桶数 = **%d**（预期 min(非空桶数, n)）" % len(nz))
        else:
            print("  桶表读失败")

    # ── W-1: 同 k、同 nrun、只差桶数的配对 ──
    #   div1 n=10k ⇒ 10 个桶 × k 条      (k<=6 时 phase 0..9 不截断)
    #   div2 n=32k ⇒ 36 个桶各 ~k 条 + 64 空桶   ← 桶更多, 但每个桶内密度同量级
    print("\n=== W-1 ★ 配对: 同 op、近似同 k、只差桶数 ===")
    print("%-28s %-9s %-8s %s" % ("配置", "扫描段", "nrun", "每路由(TB)"))
    print("-" * 62)
    rows = []
    PAIRS = [
        # (div1 n, div2 n, k)
        (10, 36, 1),
        (20, 72, 2),
        (30, 108, 3),
        (40, 128, 4),   # div2 n 上限 128
    ]
    for n1, n2, k in PAIRS:
        a = deploy_and_measure(5, 1, n1, "div1 n=%d" % n1)
        b = deploy_and_measure(5, 2, n2, "div2 n=%d" % n2)
        for tag, r in (("div1 n=%-4d" % n1, a), ("div2 n=%-4d" % n2, b)):
            if not r:
                print("%-28s 取数失败" % tag); continue
            nrun = r["nrun"]
            per = r["mean"] / nrun if nrun else float('nan')
            print("%-28s %-9.1f %-8d %.2f" % (tag, r["mean"], nrun, per))
            rows.append((tag, r["mean"], nrun, per, k))
        if a and b and a["nrun"] and b["nrun"]:
            pa = a["mean"] / a["nrun"]
            pb = b["mean"] / b["nrun"]
            d = abs(pa - pb) / max(pa, pb) * 100
            print("    ⇒ k=%d: div1 每路由 %.1f vs div2 每路由 %.1f ⇒ 差 **%.1f%%**%s"
                  % (k, pa, pb, d, "  ★>30% ⇒ 模型要重写" if d > 30 else ""))

    print("\n=== W-2 单调性 ===")
    seq = [r for r in rows if r[2] > 0]
    bad = 0
    for i in range(len(seq) - 1):
        if seq[i + 1][2] > seq[i][2] and seq[i + 1][1] < seq[i][1]:
            bad += 1
    print("  nrun 增大而扫描段反而减小的次数 = %d（应为 0；单调性可失败）" % bad)
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
