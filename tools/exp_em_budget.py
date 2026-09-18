#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-M: 验证 `engine_prog_budget` 的**转变项**已生效且算得对。

## 为什么判据是"精确算术"
`engine_prog_budget` 的返回是纯算术（无测量参与）:
    per     = Σ_ACTIVE ⌈(engine_op_cost(op) + src_cost + mult - 1) / mult⌉
    budget  = per + trans × OP_TRANS_COST
其中 div0 ⇒ mult=1 ⇒ ⌈(c+s)/1⌉ = c+s ;  src_type=CONST(2) ⇒ s = k_src_cost[2]
    engine_op_cost(PID)=145, engine_op_cost(DIRECT)=56（交付档 ITCM 表）
    OP_TRANS_COST = 25
⇒ 每个配置的 budget **可以手算**, 与固件回读的 ACK budget 逐位比对。
★ 这就是本步的判据: **逐位相等**, 不是"接近"。

## 交替序列的转变数（按 division 分组计数）
全 div0 ⇒ 单组。前 half 条 PID、后 half 条 DIRECT（`build_ops` 的 block=half 情形）:
    n=128 ⇒ 64 PID + 64 DIRECT ⇒ 转变数 **1**
    n=4   ⇒ 2 + 2              ⇒ 转变数 **1**
★ 注意: 若块大小 < half, 转变数会更多 —— 本脚本用**同样的构造器**算, 不手推。
"""
import os, re, struct, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy, cmd_burst = 0x38, 0x10, 0x22
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02
OP_PID, OP_DIRECT = 5, 0
TRANS = 25               # OP_TRANS_COST


def read_consts():
    """★ 从源码**读**常量, 不手写 —— 手写会在常量变更时静默失效。
    （本脚本第一版手写 `SRC_COST = 20`, 结果 5 个配置全部差 `n×20`,
      而"差一个与 n 成正比的量"正是 src_cost 项的特征 ⇒ 脚本自己的反推把它抓了出来。）"""
    src = open(os.path.join(ROOT, "src", "engine.c"), encoding="utf-8").read()
    hdr = open(os.path.join(ROOT, "src", "engine.h"), encoding="utf-8").read()
    m = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", src, re.S)
    vals = [int(x) for x in re.findall(r"\d+", m.group(1))]
    ms = re.search(r"k_src_cost\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", src, re.S)
    srcs = [int(x) for x in re.findall(r"\d+", ms.group(1))]
    mt = re.search(r"#define\s+OP_TRANS_COST\s+(\d+)", hdr)
    return (vals[OP_PID], vals[OP_DIRECT], srcs[SRC_CONST],
            int(mt.group(1)) if mt else TRANS)


COST_PID, COST_DIRECT, SRC_COST, TRANS = read_consts()
COST = {OP_PID: COST_PID, OP_DIRECT: COST_DIRECT}


def build_ops(n, half_blocks=True):
    """前一半 PID、后一半 DIRECT ⇒ 转变数 1（与 exp_el_mixed 的 block=half 同构）。"""
    half = n // 2
    return [OP_PID] * half + [OP_DIRECT] * (n - half)


def transitions(ops):
    return sum(1 for i in range(len(ops) - 1) if ops[i] != ops[i + 1])


def mk_seq(ops, n):
    fl = FLAG_ACTIVE | FLAG_WIRE2
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, ops[i],
                                  fl, i, (i % 64) + 1, 0, i, 0, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    states = b"\x00" * (16 * (min(n, 64) + 1))
    return struct.pack("<HHH", n, n, min(n, 64) + 1) + routes + params + states + b"\x00" * 16


def main():
    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("端口 = %s" % dcl.port); time.sleep(1.0)
    sts, p = dcl.send(cmd_status, expect_len=51)
    if sts != "ACK" or len(p) < 51:
        print("!! 0x38 失败"); return 2

    def budget(ops, n):
        sts, pp = dcl.send(cmd_deploy, mk_seq(ops, n), expect_len=None)
        if sts != "ACK":
            return None, pp
        return struct.unpack("<HI", pp[:6])[1], None

    print("=== E-M 预算算术验证（div0, src=CONST）===")
    print("%-26s %-8s %-8s %-9s %-9s %s"
          % ("配置", "条数", "转变数", "手算", "固件", "判定"))
    print("-" * 74)
    ok_all = True
    cases = [
        ("全 PID", [OP_PID] * 128, 128),
        ("全 DIRECT", [OP_DIRECT] * 128, 128),
        ("64 PID + 64 DIRECT", build_ops(128), 128),
        ("2 PID + 2 DIRECT", build_ops(4), 4),
        ("1 PID + 1 DIRECT", build_ops(2), 2),
    ]
    for tag, ops, n in cases:
        # 先只跑一个 op 时的 base，用来**反推** k_src_cost（避免手写错常量）
        per = 0
        for o in ops:
            per += COST[o] + SRC_COST          # div0 ⇒ mult=1 ⇒ ⌈c+s⌉ = c+s
        nt = transitions(ops)
        hand = per + nt * TRANS
        got, err = budget(ops, n)
        if got is None:
            print("%-26s %-8d %-8d %-9d %-9s **deploy 被拒: %s**"
                  % (tag, n, nt, hand, "-", err))
            ok_all = False
            continue
        eq = (got == hand)
        ok_all &= eq
        print("%-26s %-8d %-8d %-9d %-9d %s"
              % (tag, n, nt, hand, got, "✅逐位相等" if eq else "❌差 %+d" % (got - hand)))

    # ★ 反推 src_cost：用全 PID 一条的结果解 (145 + s) × 128 = budget
    sts, pp = dcl.send(cmd_deploy, mk_seq([OP_PID] * 128, 128), expect_len=None)
    if sts == "ACK":
        b = struct.unpack("<HI", pp[:6])[1]
        per_hand = b // 128
        print("\n  反推: 全 PID 128 条 ⇒ budget=%d ⇒ 每条摊 %d cyc" % (b, per_hand))
        print("        ⇒ ⌈(145 + k_src_cost)/1⌉ = %d ⇒ **k_src_cost = %d**"
              % (per_hand, per_hand - COST[OP_PID]))
        if per_hand - COST[OP_PID] != SRC_COST:
            print("        ⚠ 与脚本假设的 SRC_COST=%d 不同 ⇒ 请把上面那个值写回脚本"
                  % SRC_COST)

    print("\n=== 判定 ===")
    print("  M-1 ★ 5 个配置的 budget 与手算**逐位相等** ⇒ %s"
          % ("**通过**（转变项已生效且算得对）" if ok_all else "**未通过**"))
    print("  M-2 反证: 「64 PID + 64 DIRECT」的 budget 必须**大于**「全 DIRECT 128 条」")
    b_uniform, _ = budget([OP_DIRECT] * 128, 128)
    b_mixed, _ = budget(build_ops(128), 128)
    if b_uniform is not None and b_mixed is not None:
        print("        全 DIRECT=%d  混合=%d  ⇒ %s"
              % (b_uniform, b_mixed,
                 "**通过**（混合更贵, 与 E-L 实测方向一致）"
                 if b_mixed > b_uniform else "**未通过**"))
    print("\n  M-3 交付档的门是否仍不具约束力（128×145=18560 ≤ 26000）:")
    print("        加了转变项后最坏 = 128×(145+25) = %d ⇒ %s"
          % (128 * 170, "仍 ≤ 26000 ✓（断言保持）" if 128 * 170 <= 26000 else "**超了**"))
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
