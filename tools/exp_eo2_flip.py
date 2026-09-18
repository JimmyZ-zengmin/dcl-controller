#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-O 续: 找到"**纯可加会放行、含转变项被拒**"的实例。

## 为什么 E-O 第一版没找到
第一版用 PID(432) 与 DIRECT(247) 交替 —— 但每次交替会把一条 PID 换成便宜 185 的 DIRECT
⇒ **腾出的余量（185）远大于转变花费（25）** ⇒ 成本反而下降, 永远不会翻转判定。
（那条**仍然证明了转变项生效**: 固件返回的 budget 逐位等于 `纯可加 + 25×转变数`。）

## 换一个思路: 选**成本接近**的两个 op 做交替
成本差越小, 交替就"越不腾余量", 而每个转变都要花 25 ⇒ 净增。
    FLASH 表: NOT=299, OR=285 ⇒ 差 **14** < 25 ⇒ **每次交替净增 11**
⇒ 交替越多越贵, 终会越过 26000。

## 判据（决定性）
  P-1 ★★★ 构造一个程序, 使 `纯可加 ≤ 26000` 但固件 **NAK**
      ⇒ **只有转变项能解释它被拒** ⇒ 若没有转变项, 这个程序会被放行（=会超载）
  P-2 反证: 同条数的**纯 op** 程序必须 ACK（证明不是一律拒绝）
"""
import os, struct, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy = 0x38, 0x10
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02
TRANS = 25
LIMIT = 26000

import re
_s = open(os.path.join(ROOT, "src", "engine.c"), encoding="utf-8").read()
_m = re.search(r"k_op_cost_flash\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", _s, re.S)
_FLASH = [int(x) for x in re.findall(r"\d+", _m.group(1))]
OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
       "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]
# 挑成本最接近的一对（差 < TRANS），使"交替"净变贵
PAIRS = []
for i in range(19):
    for j in range(i + 1, 19):
        PAIRS.append((abs(_FLASH[i] - _FLASH[j]), i, j))
PAIRS.sort()
SMALL = [p for p in PAIRS if p[0] < TRANS][:6]


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


def interleave(n, a, b):
    return [a if i % 2 == 0 else b for i in range(n)]


def main():
    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("端口 = %s" % dcl.port); time.sleep(1.0)
    sts, p = dcl.send(cmd_status, expect_len=51)
    if sts != "ACK" or len(p) < 51:
        print("!! 0x38 失败"); return 2
    print("cap = 0x%04X" % (p[2] | (p[3] << 8)))

    def deploy(ops):
        sts, pp = dcl.send(cmd_deploy, mk_seq(ops), expect_len=None)
        if sts == "ACK":
            return True, struct.unpack("<HI", pp[:6])[1]
        return False, None

    print("\n=== 成本最接近的 op 对（差 < %d ⇒ 交替净变贵）===" % TRANS)
    for d, i, j in SMALL:
        print("   %-7s(%3d)  vs  %-7s(%3d)   差 %d  ⇒ 每次交替净增 %d"
              % (OPS[i], _FLASH[i], OPS[j], _FLASH[j], d, TRANS - d))

    if not SMALL:
        print("   没有差 < 25 的 op 对 ⇒ 本判据不可构造"); dcl.close(); return 2

    d, oa, ob = SMALL[0]
    print("\n=== 用 %s / %s 交替, 扫 n ===" % (OPS[oa], OPS[ob]))
    print("   %-6s %-8s %-12s %-12s %s" % ("n", "转变数", "纯可加", "含转变项", "固件"))
    print("   " + "-" * 58)
    found = None
    for n in range(40, 101, 2):
        ops = interleave(n, oa, ob)
        nt = transitions(ops)
        na = sum(1 for o in ops if o == oa)
        nb = n - na
        pure = na * _FLASH[oa] + nb * _FLASH[ob]
        full = pure + nt * TRANS
        ok, b = deploy(ops)
        mark = ""
        if (not ok) and pure <= LIMIT:
            found = (n, nt, pure, full)
            mark = "  ★★★ 纯可加 ≤ 26000 却被拒 ⇒ 只有转变项能解释"
        print("   %-6d %-8d %-12d %-12d %s%s"
              % (n, nt, pure, full, ("ACK %d" % b) if ok else "NAK", mark))
        if found:
            break

    print("\n=== 判定 ===")
    if found:
        n, nt, pure, full = found
        print("  P-1 ★★★ **通过** —— 决定性实例:")
        print("        n=%d, %s/%s 交替, 转变数 %d" % (n, OPS[oa], OPS[ob], nt))
        print("        纯可加视角 = %d ≤ %d（**有余量, 会被放行**）" % (pure, LIMIT))
        print("        含转变项   = %d >  %d（超了）" % (full, LIMIT))
        print("        固件       = **NAK**")
        print("      ⇒ **门确实把转变项算进去了。**")
        print("      ⇒ 没有这项, 这个程序会被放行 —— 而它按模型已经超载。")
    else:
        print("  P-1 未找到翻转实例 —— 如实记为未证, 不声称")

    print("\n  P-2 反证: 同条数的纯 op 程序")
    if found:
        n = found[0]
        for o in (oa, ob):
            ok, b = deploy([o] * n)
            print("       纯 %-7s n=%-4d ⇒ %s %s"
                  % (OPS[o], n, "ACK" if ok else "NAK", b if ok else ""))
        oka, _ = deploy([oa] * n)
        print("       ⇒ %s" % ("**成立**（不是一律拒绝, 判定随内容翻转）"
                              if oka else "纯 op 也被拒 ⇒ 该 n 太大, 换更小的 n"))
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
