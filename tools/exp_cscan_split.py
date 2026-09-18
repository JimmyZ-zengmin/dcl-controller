#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C_scan 分解: 用 **ACTIVE 位做差分**, 把扫描段拆成「循环开销」与「每路由体开销」。

## 为什么这个差分是干净的（先读代码再定方案 —— §7 的教训）
`engine.c` 的扫描体:
```c
for (k = 0; k < n; k++) {
    if (!(r->flags & ROUTE_FLAG_ACTIVE)) continue;   // ← 未激活: 只走
    ... 读源 / 取参数 / 取状态 / wire2 / 有限性检查 / prim_exec / 写回 ...  // ← 激活: 全走
}
```
⇒ 把 `flags` 的 ACTIVE 位清掉, 循环**照样转 n 次**, 但**不执行任何路由体**。
⇒ 差分 = 「每路由体开销」; 截距 = 「循环 + 调用 + 清环等固定开销」。

## 四组测量
  A1  n=128, **全未激活**, div0   ⇒ 循环开销（转 128 次但什么都不做）
  A2  n=128, **全激活**,   div0   ⇒ 循环开销 + 128 × 体开销
  A3  n=128, **全未激活**, div1   ⇒ 循环开销（桶路径, 实际每拍只访问桶里的）
  A4  n=128, **全激活**,   div1   ⇒ 对照
  另: n=16 / n=64 全未激活 ⇒ 验证"循环开销 ∝ n"还是"常数"

## 判据（都能失败）
  Z-1 「全未激活」的扫描段应 **≈ 常数 + k×n**（与 n 成正比的那部分 = 每次迭代成本）
  Z-2 差分出的「每路由体开销」应与 E-C 的 `m_op` **同量级**（差 ≤30%）
      ★ 这是**交叉验证**: 两条独立路径（差分 / 回归斜率）应给同一个数
  Z-3 「全未激活」应 **显著低于**「全激活」（否则 ACTIVE 判定没起作用 ⇒ 差分无效）
  Z-4 ★ 反证: n=0 不可测, 但「全未激活 n=16」的扫描段应 < 「全未激活 n=128」
      若两者**相等** ⇒ 循环开销与 n 无关（那 Z-1 的结论要改写成"纯常数"）
"""
import os, re, struct, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst = 0x38, 0x10, 0x12, 0x11, 0x22
S_LAST, S_MIN, S_MAX, S_N, S_LO, S_HI, S_NRUN = (0x3800, 0x3804, 0x3808,
                                                 0x380C, 0x3810, 0x3814, 0x3818)
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02


def mk(op, div, n, active=True):
    fl = FLAG_WIRE2 | (FLAG_ACTIVE if active else 0)
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

    def measure(op, div, n, active, tag):
        sts, pp = dcl.send(cmd_deploy, mk(op, div, n, active), expect_len=None)
        if sts != "ACK":
            print("  %-22s deploy 被拒" % tag); return None
        # ★ deploy 之后必须 STOP/START 清环; 注意 0x13 RESET 会清程序, 不能用
        dcl.send(cmd_stop); time.sleep(0.12)
        dcl.send(cmd_start); time.sleep(0.12)
        time.sleep(1.2)
        raw = rd(dcl, SHM + S_LAST, (S_NRUN + 4 - S_LAST) // 4)
        if raw is None:
            print("  %-22s 读失败" % tag); return None
        u = struct.unpack("<%dI" % (len(raw) // 4), raw)

        def at(o):
            return u[(o - S_LAST) // 4]

        sn, slo, shi = at(S_N), at(S_LO), at(S_HI)
        if not sn:
            print("  %-22s 无样本" % tag); return None
        mean = (slo | (shi << 32)) / float(sn)
        nrun = at(S_NRUN)
        print("  %-22s 扫描段=%9.1f TB (%.0f cyc)  nrun=%d  n=%d" % (tag, mean, mean * 2, nrun, n))
        return dict(mean=mean, nrun=nrun, n=n)

    print("\n=== 全未激活（只转循环，不执行路由体）===")
    ia = {}
    for n in (16, 64, 128):
        ia[n] = measure(0, 0, n, False, "INACTIVE n=%d div0" % n)
    ia128_d1 = measure(0, 1, 128, False, "INACTIVE n=128 div1")

    print("\n=== 全激活（对照）===")
    ac0 = measure(0, 0, 128, True, "ACTIVE n=128 div0 DIRECT")
    ac0p = measure(5, 0, 128, True, "ACTIVE n=128 div0 PID")
    ac1 = measure(0, 1, 128, True, "ACTIVE n=128 div1 DIRECT")

    print("\n=== 判据 ===")
    vals = [(n, ia[n]["mean"]) for n in (16, 64, 128) if ia.get(n)]
    if len(vals) >= 2:
        print("  Z-1/Z-4 全未激活 vs n:")
        for n, m in vals:
            print("      n=%-4d  %8.1f TB" % (n, m))
        (n0, m0), (n1, m1) = vals[0], vals[-1]
        k = (m1 - m0) / float(n1 - n0)
        c = m0 - k * n0
        print("      ⇒ 拟合: 常数 %.1f TB + %.3f TB/次迭代（%.2f cyc/次）" % (c, k, k * 2))
        print("      Z-4 两者%s ⇒ %s"
              % ("不等" if abs(m1 - m0) > 5 else "相等",
                 "循环开销 ∝ n（每次迭代有成本）" if abs(m1 - m0) > 5
                 else "**循环开销与 n 无关** ⇒ 是纯常数"))

    if ac0 and ia.get(128):
        d = ac0["mean"] - ia[128]["mean"]
        print("\n  Z-2 差分出的「每路由体开销」(DIRECT, div0):")
        print("      全激活 %.1f − 全未激活 %.1f = %.1f TB / 128 条 = **%.2f TB/条 (%.1f cyc)**"
              % (ac0["mean"], ia[128]["mean"], d, d / 128.0, d / 128.0 * 2))
        print("      E-C 独立测得 DIRECT m_op = 29.51 TB/条 (59.0 cyc)")
        r = abs(d / 128.0 - 29.51) / 29.51 * 100
        print("      ⇒ 相差 %.1f%% ⇒ %s" % (r, "**同量级（交叉验证通过）**" if r <= 30
                                      else "**不一致, 需复查**"))
        print("  Z-3 全未激活(%.1f) %s 全激活(%.1f) ⇒ %s"
              % (ia[128]["mean"], "<<" if ia[128]["mean"] < ac0["mean"] * 0.5 else "!<<",
                 ac0["mean"],
                 "ACTIVE 判定有效, 差分成立" if ia[128]["mean"] < ac0["mean"] * 0.5
                 else "**差分无效**"))
    if ia128_d1:
        print("\n  div1 全未激活 = %.1f TB（对照 div0 的 %.1f）"
              % (ia128_d1["mean"], ia[128]["mean"] if ia.get(128) else -1))
        print("      ★ 桶路径下, 每拍只访问桶里那几条 ⇒ 若它明显小于 div0 的循环开销,")
        print("        说明「循环开销 ∝ 实际处理的条数」而不是「表里的 n」")

    print("\n=== 汇总: C_scan 的构成 ===")
    if ia.get(128):
        print("  扫描段 = 固定开销 + 每路由体开销 × nrun")
        print("    固定开销（n=128 全未激活, 仍含 128 次迭代）= %.1f TB (%.0f cyc)"
              % (ia[128]["mean"], ia[128]["mean"] * 2))
        print("    ★ 其中「每迭代」= %.2f TB ⇒ 128 次 = %.1f TB" % (k, k * 128) if len(vals) >= 2 else "")
        print("    每路由体（DIRECT）= 29.5 TB/条 ; （PID）= 64.2 TB/条")
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
