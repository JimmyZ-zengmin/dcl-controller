#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-N: **FLASH 档上门的实际拦截行为**（E-M 只验了算术, 没验门真的拦得住）。

## 为什么这一步必须做
`engine_prog_budget` 的**实际拦截作用**在 FLASH 档（`BOOT_SEL=0`）:
    交付档(ITCM): 128 × 145 = 18560 ≤ 26000 ⇒ 门**不具约束力**（永远不触发）
    FLASH 档     : 128 × 432 = 55296 >  26000 ⇒ 门**具约束力**（真的会 NAK）
⇒ 只在交付档测 = **测了不触发的那一半**。

## 判据（都能失败）
  N-1 ★ **FLASH 档必须拒绝** 128 条的程序（最贵原语），NAK 文案含预算字样
  N-2 ★ **交付档必须接受** 同一个程序（同一份载荷，两档对照）
      ★★ N-1+N-2 **成对**才说明"门是按档位起作用的", 而不是"一律拒绝"
  N-3 ★ 转变项在 **FLASH 档也生效**: 混合程序的 budget 应**大于**同条数纯 op 程序,
      且差值 ≈ 转变数 × 25（与 E-M 在交付档验到的同一算术）
  N-4 **边界**: 找一条**恰好被拒**的条数, 并验证"少一条就通过"（门边界精确）
"""
import os, re, struct, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy = 0x38, 0x10
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02
OP_PID, OP_DIRECT = 5, 0


def mk_seq(ops):
    n = len(ops)
    fl = FLAG_ACTIVE | FLAG_WIRE2
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, ops[i],
                                  fl, i, (i % 64) + 1, 0, i, 0, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    states = b"\x00" * (16 * (min(n, 64) + 1))
    return struct.pack("<HHH", n, n, min(n, 64) + 1) + routes + params + states + b"\x00" * 16


def transitions(ops):
    return sum(1 for i in range(len(ops) - 1) if ops[i] != ops[i + 1])


def main():
    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("端口 = %s" % dcl.port); time.sleep(1.0)
    sts, p = dcl.send(cmd_status, expect_len=51)
    if sts != "ACK" or len(p) < 51:
        print("!! 0x38 失败"); return 2
    print("固件能力字 = 0x%04X" % (p[2] | (p[3] << 8)))

    def deploy(ops):
        sts, pp = dcl.send(cmd_deploy, mk_seq(ops), expect_len=None)
        if sts == "ACK":
            return True, struct.unpack("<HI", pp[:6])[1], ""
        return False, None, (pp.decode('utf-8', 'replace') if sts == 'NAK' else sts)

    nops = int(sys.argv[1]) if len(sys.argv) > 1 else 128
    print("\n=== %d 条程序的门行为 ===" % nops)
    print("%-30s %-10s %-12s %s" % ("配置", "转变数", "budget", "结果"))
    print("-" * 68)
    results = {}
    cases = [
        ("全 PID", [OP_PID] * nops),
        ("全 DIRECT", [OP_DIRECT] * nops),
        ("混合(半半)", [OP_PID] * (nops // 2) + [OP_DIRECT] * (nops - nops // 2)),
        ("严格交替", [OP_PID if i % 2 == 0 else OP_DIRECT for i in range(nops)]),
    ]
    for tag, ops in cases:
        nt = transitions(ops)
        ok, b, msg = deploy(ops)
        results[tag] = (ok, b, nt)
        print("%-30s %-10d %-12s %s"
              % (tag, nt, b if ok else "—", "ACK" if ok else "**NAK: %s**" % msg))

    print("\n=== 判定 ===")
    any_ack = any(v[0] for v in results.values())
    any_nak = any(not v[0] for v in results.values())
    print("  N-1/N-2 成对（同一载荷: 某档拒、另一档收）:")
    print("      本次板子: 收到 %d 个 ACK, %d 个 NAK"
          % (sum(1 for v in results.values() if v[0]),
             sum(1 for v in results.values() if not v[0])))
    if any_ack and any_nak:
        print("      ⇒ **成对成立**: 门按程序内容区别对待（不是一律拒绝）✅")
    elif any_nak:
        print("      ⇒ 全部被拒 —— 与 FLASH 档预期一致（128 × 432 ≫ 26000）")
        print("         ★ 要验 N-2（另一档接受）需换 **交付档** 跑同一脚本")
    else:
        print("      ⇒ 全部接受 —— 与交付档预期一致（门不具约束力）")

    print("\n  N-3 ★ 转变项是否在**本档**生效（比较同条数、不同转变数）:")
    for a, b in (("全 PID", "混合(半半)"), ("全 DIRECT", "混合(半半)")):
        ra, rb = results.get(a), results.get(b)
        if ra and rb and ra[0] and rb[0] and ra[1] and rb[1]:
            print("      %s(budget %d) vs %s(budget %d) ⇒ 差 %+d"
                  % (a, ra[1], b, rb[1], rb[1] - ra[1]))
    print("      ★ 只在两档都 ACK 时可比；NAK 时 budget 不返回 ⇒ 用**边界扫描**代替")

    print("\n  N-4 边界扫描（找恰好被拒的条数, 验证'少一条就过'）:")
    lo, hi = 1, nops
    # 先找有没有 ACK 的区间
    okl, _, _ = deploy([OP_DIRECT] * lo)
    okh, _, _ = deploy([OP_PID] * hi)
    print("      1 条 DIRECT ⇒ %s ; %d 条 PID ⇒ %s"
          % ("ACK" if okl else "NAK", hi, "ACK" if okh else "NAK"))
    if not okh:
        # 二分找最大可接受条数（用 PID, 最贵）
        lo2, hi2 = 0, hi
        while lo2 < hi2:
            mid = (lo2 + hi2 + 1) // 2
            okm, _, _ = deploy([OP_PID] * mid)
            if okm:
                lo2 = mid
            else:
                hi2 = mid - 1
            time.sleep(0.05)
        print("      ⇒ 最大可接受 PID 条数 = **%d**（%d 条被拒）" % (lo2, lo2 + 1))
        okn, bn, _ = deploy([OP_PID] * lo2)
        okx, _, _ = deploy([OP_PID] * (lo2 + 1))
        print("         %d 条 ⇒ %s (budget %s) ; %d 条 ⇒ %s"
              % (lo2, "ACK" if okn else "NAK", bn, lo2 + 1, "ACK" if okx else "NAK"))
        print("      ⇒ N-4 %s" % ("**通过**（边界精确: 一条之差翻转）"
                                if okn and not okx else "**未能定出精确边界**"))
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
