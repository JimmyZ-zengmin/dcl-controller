#!/usr/bin/env python3
"""
h723_path_cost_criteria.py — 扫描路径成本模型的**成对**判据（缺口① 的验收工具）

★ 为什么是"成对"而不是一条判据:
  预算模型给两条扫描体（ITCM `engine_scan_itcm` / FLASH `.scan_flash`）定价。
  第一版用一个**标量倍率** `OP_COST_PATH_NUM/DEN = 303/100`（斜率比 3.03）——
  **当天就被自己的实验证伪**：逐原语实测比值是 **2.98×~4.41×**
  ⇒ 标量**既不是上界也不是下界**，是**中枢值**，它**同时**有两种方向的错:

    · 对便宜原语（DIRECT）**低估 1.45×** ⇒ **放行会超载的程序**（静态门失守）
    · 对贵原语（PID）**恰好命中**     ⇒ ★ **所以没人会怀疑它**（这才是最危险的）

  ⇒ 一条判据只能看到一个方向 ⇒ 必须**在同一个固件上同时测两类点**。

判据（每条都能失败）
  H1 ACK 时 `budget` 读回 == 该模式预测值（成本模型与固件同源）
  H2 门的**位置**与预测一致（ACK↔NAK 的边界落在预测的那一对 N 上）
  H3 被拒那次**无副作用**（`n_routes` 不变）
  H4 ★ **洞的物理证据**（标量档）：ACK 的那个程序，**实跑**是否已超静态门承诺的 26000 cyc
      —— ★ 判据要对齐"**门自己的契约**"（`engine.h`：保证任何可部署程序每拍都在预算内），
        而**不是**拿更松的运行期兜底（80 µs = 32000 cyc）去比；
        用后者会把"契约已失守"判成 PASS（本工具第一版就是这么错的，已改）。
  H4'（表档对照）：同一点应 **NAK** ⇒ 程序**根本没被放行**

用法
  # 被测固件是"单标量 303/100"那版（BOOT_SEL=0，历史构建）
  python tools/h723_path_cost_criteria.py --mode scalar
  # 被测固件是"逐原语 FLASH 表"那版（当前，BOOT_SEL=0）
  python tools/h723_path_cost_criteria.py --mode table

退出码: 0 = 全 PASS / 1 = 有 FAIL。
记录: docs/exp-2026-09-18-overload/ §7；规矩: RULES-DETAIL.md §5.40。
"""
# ★ GBK 控制台上 print 一个 ⇒ 就崩（本项目踩过）—— 见 RULES-DETAIL §5.39
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, os, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from h723_client import Dcl  # noqa: E402

OP_DIRECT, OP_PID = 0x00, 0x05
SRC_CONST, DST_WIRE, ACTIVE = 2, 2, 1
GATE = 26000                  # engine.h EXEC_DEPLOY_BUDGET

# 实测的**每原语** FLASH 路径单价。
# ★ 必须取 `--stat max`（最坏情况）那一版: 预算门要的是最坏情况, `min` 是最坏情况的下界。
#   交叉印证: max 口径 PID = 432 cyc/条 vs **已部署程序** emax 斜率 434 cyc/条 ⇒ 吻合;
#   min 口径给 401（表档构建）/363（标量档构建）⇒ 系统性偏低（→ RULES-DETAIL §5.40）。
FLASH_UNIT = {OP_DIRECT: 247, OP_PID: 432}
# 标量模型: ceil(op_itcm × 303 / 100)
SCALAR_UNIT = {OP_DIRECT: 170, OP_PID: 440}   # ceil(56×3.03)=170, ceil(145×3.03)=440
# 真实实跑代价（`--stat max` 两点法, cyc/条）
REAL_UNIT = {OP_DIRECT: 246.94, OP_PID: 431.94}
TICK_CYC = 40000
EXEC_BUDGET_TB = 16000        # 动态判据: 80 µs（TB tick, TIM5 5 ns）⇒ ×2 = 32000 cyc

# 受试点: (原语, 路数, 期望判决)
#   "ack"    = 该 ACK, 且 budget 必须命中模型
#   "budget" = 该 NAK, 理由**正是** `exec budget exceeded`
#   "count"  = 该 NAK, 理由**必须不是**预算门 ⇒ 用来钉住"上界是 128 条, 不是预算"
#              (`main.c:1790` `counts exceed max`) —— 于是"标量档的门边界在 153×170"
#              这件事可被证明是**结构性不可达**(153 > MAX_ROUTES=128), 而不是"我们没试到"。
PTS = {
    # 标量: DIRECT 单价 170 ⇒ 128×170 = 21760 ≤ 26000 ⇒ **洞**（放行了 80% 拍的程序）
    "scalar": [(OP_DIRECT, 128, "ack"), (OP_DIRECT, 129, "count"),
               (OP_PID, 59, "ack"), (OP_PID, 60, "budget")],
    # 表  : DIRECT 247 ⇒ 105×247=25935 ACK / 106×247=26182 NAK; PID 432 ⇒ 60 ACK / 61 NAK
    "table":  [(OP_DIRECT, 128, "budget"), (OP_DIRECT, 105, "ack"), (OP_DIRECT, 106, "budget"),
               (OP_PID, 60, "ack"), (OP_PID, 61, "budget")],
}


def mk(op, n):
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  ACTIVE, i, 1, 0, 0, 0, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    return struct.pack("<HHH", n, n, 1) + routes + params + b"\x00" * 16


def st(dcl, tries=4):
    for _ in range(tries):
        s, p = dcl.send(0x38, expect_len=51)
        if s == "ACK" and len(p) >= 51:
            d = struct.unpack("<IIIII", p[:20])
            return dict(n_routes=struct.unpack("<H", p[20:22])[0],
                        emax=d[4], ov=struct.unpack("<I", p[27:31])[0])
        time.sleep(0.3)
    return None


def dep(dcl, op, n, tries=3):
    for _ in range(tries):
        # ★ `expect_len=None` 必须: NAK 理由长度随门而变
        #   (`exec budget exceeded` = 20 B, 不是 ACK 的 6 B)。写死 6 ⇒ 应答被丢弃 3 次 ⇒ 假 TIMEOUT。
        s, p = dcl.send(0x10, mk(op, n), expect_len=None)
        if s == "ACK" and len(p) >= 6:
            return ("ACK", struct.unpack("<HI", p[:6])[1], None)
        if s == "NAK":
            return ("NAK", None, p.decode("utf-8", "replace"))
        time.sleep(0.5)
    return ("TIMEOUT", None, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=("scalar", "table"),
                    help="被测固件用的是哪种模型: scalar(303/100 单标量) / table(逐原语 FLASH 表)")
    a = ap.parse_args()
    unit = SCALAR_UNIT if a.mode == "scalar" else FLASH_UNIT

    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("port %s   模式=%s" % (dcl.port, a.mode)); time.sleep(1.2)
    print("每原语单价(本模式): " + ", ".join("%s=%d" % (["DIRECT", "PID"][op == OP_PID], v)
                                             for op, v in sorted(unit.items())))
    res = []
    pts = PTS[a.mode]
    seen = {}
    try:
        for op, n, kind in pts:
            exp = n * unit[op]
            want = "ACK" if kind == "ack" else "NAK"
            real = n * REAL_UNIT[op]
            dcl.send(0x12); time.sleep(0.2)      # STOP
            dcl.send(0x13); time.sleep(0.3)      # 清统计
            dcl.send(0x11); time.sleep(0.3)      # START
            s0 = st(dcl)
            before = s0["n_routes"] if s0 else -1
            r = dep(dcl, op, n)
            time.sleep(0.5)
            s1 = st(dcl)
            after = s1["n_routes"] if s1 else -1
            emax = s1["emax"] if s1 else -1
            ov = s1["ov"] if s1 else -1
            nm = "DIRECT" if op == OP_DIRECT else "PID"
            seen[(op, n)] = dict(verdict=r[0], emax=emax, ov=ov, after=after)
            head = ("  %-6s N=%-4d 模型预算 %-6d (门 %d) ⇒ 该 %s | 实跑 %d cyc = 拍 %.1f%%"
                    % (nm, n, exp, GATE, want, real, 100.0 * real / TICK_CYC))
            if r[0] == "ACK":
                ok = (want == "ACK") and (r[1] == exp)
                print("%s\n         实得 ACK budget=%-6d %s   emax=%d TB(%d cyc) ov=%d  n_routes %d→%d"
                      % (head, r[1], "OK" if ok else "**不符**", emax, emax * 2, ov, before, after))
                res.append(("%s N=%d ACK 且 budget 命中模型" % (nm, n), ok))
            elif r[0] == "NAK":
                if kind == "budget":
                    ok = (r[2] == "exec budget exceeded")
                    res.append(("%s N=%d NAK 理由**正是**预算门" % (nm, n), ok))
                elif kind == "count":
                    ok = (r[2] != "exec budget exceeded")
                    res.append(("%s N=%d 被**条数门**拦下(不是预算门) ⇒ 上限=128 条" % (nm, n), ok))
                else:
                    ok = False
                print("%s\n         实得 NAK %r %s   n_routes %d→%d"
                      % (head, r[2], "OK" if ok else "**不符**", before, after))
                res.append(("%s N=%d 拒绝无副作用" % (nm, n), before == after))
            else:
                print("%s\n         实得 TIMEOUT **失败**" % head)
                res.append(("%s N=%d 有应答" % (nm, n), False))

        # H4: 洞的物理证据 —— 必须在 (DIRECT,128) 那一点之后立刻判, 不能等循环跑完
        #     （后面那点已经换过程序了）
        s = seen.get((OP_DIRECT, 128))
        assert s is not None, "缺 (DIRECT,128) 采样"
        if a.mode == "scalar":
            real_cyc = s["emax"] * 2
            # ★ 判据对齐"门自己的契约": engine.h 说 EXEC_DEPLOY_BUDGET 的作用是
            #   "保证**任何可部署程序**每拍执行都在确定性预算内" —— 那就拿实跑去比它,
            #   而**不是**拿更松的运行期兜底 (80 µs) 去比。用后者会把"契约已失守"判成 PASS。
            hole = (s["verdict"] == "ACK" and real_cyc > GATE)
            print("\n  ★ H4 洞的物理证据 (标量档把 128×DIRECT 放行了, 读数就在那一刻):")
            print("      静态门承诺 ≤ %d cyc/拍 (拍 %.1f%%) ；模型自报 %d cyc (拍 %.1f%%)"
                  % (GATE, 100.0 * GATE / TICK_CYC, 128 * SCALAR_UNIT[OP_DIRECT],
                     100.0 * 128 * SCALAR_UNIT[OP_DIRECT] / TICK_CYC))
            print("      实跑 emax=%d TB = %d cyc = 拍 %.1f%% (每条约 %.1f cyc)"
                  % (s["emax"], real_cyc, 100.0 * real_cyc / TICK_CYC, real_cyc / 128.0))
            print("      ⇒ %s" % ("**是**, 实跑已超门承诺的 %d cyc —— 门对自己的契约失守 %.2f 倍"
                                  % (GATE, real_cyc / float(GATE))
                                  if hole else "未超门承诺值"))
            print("      ★ 同时记录: 运行期兜底 ov (拍 80%% = %d cyc) %s ⇒ 这条失守 %s"
                  % (EXEC_BUDGET_TB * 2, ("**未**触发(ov=%d)" % s["ov"]) if s["ov"] == 0
                     else "触发了(ov=%d)" % s["ov"],
                     "**不会被 ov 兜住** ⇒ 静态门是承重的" if s["ov"] == 0 else "被 ov 兜住了"))
            res.append(("H4 标量档 ACK 的程序实跑超静态门承诺的 %d cyc" % GATE, hole))
        else:
            print("\n  ★ H4' 对照 (逐原语表档): 同一点判决 = %s ⇒ 期望 NAK, 程序**根本没被放行**"
                  % s["verdict"])
            res.append(("H4' 表档下 128×DIRECT 未被放行", s["verdict"] == "NAK"))
    finally:
        dcl.send(0x12); time.sleep(0.2); dcl.send(0x13); time.sleep(0.3)
        dcl.send(0x11); time.sleep(0.3)
        dcl.close()

    print("\n=== 汇总 (%s) ===" % a.mode)
    for k, v in res:
        print("  %-44s %s" % (k, "PASS" if v else "FAIL"))
    return 0 if all(v for _, v in res) else 1


if __name__ == "__main__":
    sys.exit(main())
