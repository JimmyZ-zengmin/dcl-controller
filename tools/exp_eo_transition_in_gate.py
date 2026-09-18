#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-O: ★ **直接证明转变项在 FLASH 档的门里起了作用**。

## 为什么需要一个专门的构造
E-N 已证：FLASH 档 128 条一律被拒（`128×432 = 55296 ≫ 26000`）——
但那是**每条路由自己的成本**就超了，**看不出转变项有没有参与**。

## 决定性判据（只有转变项生效才可能成立）
找一个条数 `n`，使得：
    · **纯 PID** 的 `n` 条 **ACK**（`n×432 ≤ 26000`）
    · 同一个 `n`、**同样条数**、但含**转变**的程序 **NAK**

★ 因为"同样条数"⇒ 每条自己的成本项**完全相同**，
  而两者**只是排列不同** ⇒ **任何差异只能来自转变项**。
  ⇒ 若后者被拒 ⇒ **转变项确实进了门**。

## 构造（把转变数做大, 让它比预算余量更显眼）
`n` 条里放 `m` 对 (PID, DIRECT)：DIRECT 比 PID 便宜 432−247 = **185**，
所以每把一条 PID 换成 DIRECT 会**腾出 185 cyc**；
但每次"PID→DIRECT 或 DIRECT→PID"的**转变**要**花 25 cyc**。

    k 个交替块（k 块 PID + k 块 DIRECT）⇒ 转变数 ≈ 2k−1，腾出 185k cyc，花 25(2k−1) cyc
    ⇒ 净腾出 ≈ 185k − 50k = 135k > 0 ⇒ **纯可加会认为它更便宜**，
       而真实预算是 `Σ + 25×转变数`
⇒ 存在一个窗口: **纯可加视角"有余量"、而实际预算已超**。
   落在这个窗口的 `n` 就是本步要找的判据点。
"""
import os, struct, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy = 0x38, 0x10
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02
OP_PID, OP_DIRECT = 5, 0
COST = {OP_PID: 432, OP_DIRECT: 247, }     # FLASH 表（n=1 实测反推: PID 432 / DIRECT 247）
TRANS = 25


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


def alternating(n, blocks):
    """n 条, k=blocks 段交替: 前半 PID 段与 DIRECT 段按 blocks 切分。"""
    seq = []
    for b in range(blocks):
        lo = b * n // blocks
        hi = (b + 1) * n // blocks
        seq.extend([OP_PID if b % 2 == 0 else OP_DIRECT] * (hi - lo))
    return seq[:n]


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

    print("\n=== 1) 找纯 PID 的 ACK/NAK 边界 ===")
    ok_n = None
    for n in range(56, 66):
        ok, b = deploy([OP_PID] * n)
        print("   纯 PID n=%-3d ⇒ %-6s %s" % (n, "ACK" if ok else "NAK", b if ok else ""))
        if ok:
            ok_n = n
    if ok_n is None:
        print("  找不到 ACK 的 n ⇒ 判无效"); dcl.close(); return 2
    print("   ⇒ 纯 PID 最大可接受 n = **%d**（budget %d）" % (ok_n, COST[OP_PID] * ok_n))

    print("\n=== 2) ★ 同一个 n=%d、同样条数、改成多段交替 ===" % ok_n)
    print("   %-8s %-8s %-12s %-12s %s"
          % ("blocks", "转变数", "纯可加预测", "含转变项预测", "固件"))
    print("   " + "-" * 62)
    found = None
    for blocks in (1, 2, 4, 8, 16, 32, ok_n):
        ops = alternating(ok_n, min(blocks, ok_n))
        nt = transitions(ops)
        np_ = sum(1 for o in ops if o == OP_PID)
        nd = len(ops) - np_
        pure = np_ * COST[OP_PID] + nd * COST[OP_DIRECT]
        full = pure + nt * TRANS
        ok, b = deploy(ops)
        mark = ""
        if ok and pure + nt * TRANS > 26000:
            mark = "  ★★ 只有转变项能解释它被拒"
        if (not ok) and pure <= 26000:
            found = (blocks, nt, pure, full)
            mark = "  ★★ 纯可加认为有余量, 实际被拒 ⇒ 转变项生效"
        print("   %-8d %-8d %-12d %-12d %s%s"
              % (blocks, nt, pure, full, ("ACK budget=%d" % b) if ok else "NAK", mark))

    print("\n=== 判定 ===")
    if found:
        blocks, nt, pure, full = found
        print("  O-1 ★★★ **通过**: n=%d, %d 段交替（转变数 %d）:" % (ok_n, blocks, nt))
        print("        纯可加视角 = %d ≤ 26000（有余量）" % pure)
        print("        含转变项   = %d >  26000（超了）" % full)
        print("        固件       = **NAK**")
        print("      ⇒ **门确实把转变项算进去了** —— 同样条数、只改排列就翻转了判定。")
        print("      ⇒ 若没有转变项, 这个程序会被**放行**（也就是会超载）。")
    else:
        print("  O-1 未找到「纯可加通过 / 含转变项被拒」的窗口")
        print("      ⇒ 可能是: 转变数不够大（相对预算余量）; 或转变项没进这一档的门")
        print("      ★ 这**不是**判无效 —— 见上表, 只要有一行 `pure ≤ 26000` 却 NAK 就算成立")

    print("\n  O-2 反证（成对）: 同一条数下, 纯 PID 必须 ACK")
    ok, b = deploy([OP_PID] * ok_n)
    print("       纯 PID n=%d ⇒ %s %s" % (ok_n, "ACK" if ok else "NAK", b if ok else ""))
    print("       ⇒ %s" % ("**成立**（说明不是一律拒绝）" if ok else "**不成立**"))
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
