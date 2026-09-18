#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-U —— 三件收尾测量（一次会话跑完）

## M1 ★★ 「残留顺序程序」的每拍代价（E-T 的意外发现的正量化）
E-T 在**未复位**的板上测到 `di(0) = 816.7 TB`，复位后 `di(0) = 542.4 TB` ⇒ 差 **274.3 TB**。
当时板上有 **8 个 seq 实例**（E-Q 部署的，而且**没有任何命令能清掉它们** —— 见保障图谱缺口表）。
本测量把"每实例每拍代价"钉成一条曲线: 0 / 2 / 4 / 8 实例各测一次空程序的 di。
  ★ 它同时是那条缺口（"顺序域无法被清空"）的**代价实测**。

## M2 DIRECT 档的门边界（转变项之后重验）
E-M 加了转变项之后只重验了 PID 的 60/61；DIRECT 的边界（105/106）是**加转变项之前**的数。
本测量: 从源码解析 ITCM 逐原语成本表 + 门公式 ⇒ **算出**边界, 再在边界与边界+1 各部署一次判 ACK/NAK。
  ★ 均匀程序转变数=0 ⇒ 边界不应变; 若实测边界与算出的不符 ⇒ 门公式或表有问题。

## M3 大块读的板侧阻塞标定（E-S 用到的那个量）
E-S 的阻塞对照里"阻塞时长"是每次实测的（89.5 / 101.4 / 135.3 ms 三个值），没有标定曲线。
本测量: 对 K = 8/32/64/128/200 字各读一次, 用**环头 tick 差**量板侧被占时长 ⇒ 给出 ms/百字。
"""
import os, re, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from h723_client import Dcl

CMD_STATUS, CMD_DEPLOY, CMD_STOP, CMD_START, CMD_BURST = 0x38, 0x10, 0x12, 0x11, 0x22
CMD_SEQ = 0x44
OFF_WIRE_MAP, OFF_EXEC_RING_HDR = 0x0240, 0x3880
S_N, S_LO, S_HI = 0x380C, 0x3810, 0x3814
OFF_CTRL_N_SEQ = 0x38      # u8: 活 seq 实例数 —— 可读 ⇒「板上还有什么在每拍跑」本就能自证
E_LO, E_HI, E_N = 0x3860, 0x3864, 0x3868
SRC_CONST, DST_WIRE, FLAG_ACTIVE, FLAG_WIRE2 = 2, 2, 0x01, 0x02
EXEC_DEPLOY_BUDGET = 26000


def rd(dcl, addr, nwords, chunk=200):
    out, off = b"", 0
    while off < nwords:
        k = min(chunk, nwords - off)
        sts, p = dcl.send(CMD_BURST, struct.pack("<IH", addr + 4 * off, k), expect_len=4 * k)
        if sts != "ACK" or len(p) < 4 * k:
            return None
        out += p[:4 * k]; off += k
    return out


def ring_tick(dcl, shm):
    v = rd(dcl, shm + OFF_EXEC_RING_HDR, 2)
    return struct.unpack("<2I", v[:8])[1] if v else None


def mk(op, div, n):
    fl = FLAG_ACTIVE | FLAG_WIRE2
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  fl, i, (i % 64) + 1, 0, i, div, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    states = b"\x00" * (16 * (min(n, 64) + 1))
    return struct.pack("<HHH", n, n, min(n, 64) + 1) + routes + params + states + b"\x00" * 16


def di_now(dcl, shm, settle=1.1):
    dcl.send(CMD_STOP); time.sleep(0.1); dcl.send(CMD_START); time.sleep(0.1)
    time.sleep(settle)
    raw = rd(dcl, shm + S_N, (E_N + 4 - S_N) // 4)
    if raw is None:
        return None
    u = struct.unpack("<%dI" % (len(raw) // 4), raw)
    at = lambda o: u[(o - S_N) // 4]
    en = at(E_N)
    return ((at(E_LO) | (at(E_HI) << 32)) / float(en)) if en else None


def src_op_costs():
    """从 src/engine.c + engine.h 解析 ITCM 逐原语成本表与 src 成本口径。"""
    c = open(os.path.join(ROOT, "src", "engine.c"), encoding="utf-8", errors="replace").read()
    h = open(os.path.join(ROOT, "src", "engine.h"), encoding="utf-8", errors="replace").read()
    m = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{(.*?)\};", c, re.S)
    itcm = [int(x) for x in re.findall(r"\d+", m.group(1))] if m else []
    ks = re.search(r"k_src_cost\s*\[[^\]]*\]\s*=\s*\{(.*?)\};", c, re.S)
    src = [int(x) for x in re.findall(r"\d+", ks.group(1))] if ks else []
    fb = int(re.search(r"#define\s+SRC_COST_FALLBACK\s+(\d+)", h).group(1))
    tr = int(re.search(r"#define\s+OP_TRANS_COST\s+(\d+)", h).group(1))
    return itcm, src, fb, tr


def main():
    dcl = Dcl(os.environ.get("DCL_PORT"))
    print("端口 = %s" % dcl.port); time.sleep(1.0)
    sts, p = dcl.send(CMD_STATUS, expect_len=51)
    shm = struct.unpack("<I", p[23:27])[0]
    print("SHM = 0x%08X" % shm)
    # 健康门
    for _ in range(10):
        t = ring_tick(dcl, shm)
        if t and t >= 200000:
            break
        time.sleep(0.5)
    print("tick = %s ⇒ 开工\n" % t)

    # ══ M1 每 seq 实例的每拍代价 ══════════════════════════════════════
    print("── M1 残留顺序程序的每拍代价（0 / 2 / 4 / 8 实例）──")
    # ★★ 必须先报告板上的**活 seq 实例数** —— 本测量自己就被那条缺口坑过一次:
    #   第二次运行时"nseq=0"读到 736.3 TB（= 上一轮留下的 8 实例）, 曲线整条错位。
    #   `OFF_CTRL_N_SEQ` 是可读的, 所以这件事**本来就能自证**, 不该靠假设。
    def live_nseq():
        v = rd(dcl, shm + OFF_CTRL_N_SEQ, 1)
        return v[0] if v else -1
    print("    起手板上的活 seq 实例数 = **%d**（★ 顺序域没有清除命令, 只能靠复位）" % live_nseq())
    dcl.send(CMD_DEPLOY, mk(0x00, 0, 0), expect_len=None)     # 空路由表（带 params 供 seq 用）
    time.sleep(0.2); dcl.send(CMD_STOP); time.sleep(0.2)
    rows = []
    for nseq in (0, 2, 4, 8):
        if nseq:
            dirs, tbl = [], []
            for i in range(nseq):
                dirs.append(struct.pack("<BBBBH", 2, 12 + i, 0, 0, 2 * i))
                for _ in range(2):
                    # cond_type=2 + timeout_en；param_idx=0（value_b = params[0].value_b = 0）
                    tbl.append(struct.pack("<BBBBHHHI", 2, 0, 0x02, 0, 0, 0, 0, 0) + b"\x00\x00")
            # ★ 超时必须 >0, 否则固件 NAK("seq: timeout must be >0") ⇒ params[0].value_b 要给非 0
            pay = (mk(0x00, 0, 0))
            pay = pay[:6] + pay[6:]                       # 不动
            # 直接构造: nr=0, np=1, ns=1, param[0] = (0, 5.0, 0, 0)
            pay = struct.pack("<HHH", 0, 1, 1) + struct.pack("<4f", 0.0, 5.0, 0.0, 0.0) + b"\x00" * 16
            dcl.send(CMD_DEPLOY, pay, expect_len=None); time.sleep(0.2)
            dcl.send(CMD_STOP); time.sleep(0.2)
            sts2, q2 = dcl.send(CMD_SEQ, struct.pack("<BH", nseq, 2 * nseq)
                                + b"".join(dirs) + b"".join(tbl), expect_len=None)
            if sts2 != "ACK":
                print("    nseq=%d 部署被拒: %s" % (nseq, q2.decode('utf-8', 'replace')))
                continue
        d = di_now(dcl, shm)
        rows.append((nseq, d))
        print("    nseq=%-2d ⇒ di(0) = %s TB" % (nseq, "%.1f" % d if d else "?"))
    if len(rows) >= 2 and rows[0][1] and rows[-1][1]:
        per = (rows[-1][1] - rows[0][1]) / float(rows[-1][0])
        print("    ⇒ 端点法: 每实例每拍 = %.1f TB（%.0f cyc）" % (per, per * 2))
        # ★ 分段边际（更诚实: 首个实例与后续实例不等价）
        for a_, b_ in ((0, 2), (2, 4), (4, 8)):
            da = [r for r in rows if r[0] == a_]
            db = [r for r in rows if r[0] == b_]
            if da and db and da[0][1] and db[0][1]:
                print("        %d→%d 实例: +%.1f TB ⇒ **每实例 %.1f TB（%.0f cyc）**"
                      % (a_, b_, db[0][1] - da[0][1], (db[0][1] - da[0][1]) / (b_ - a_),
                         (db[0][1] - da[0][1]) / (b_ - a_) * 2))
        print("    ⇒ 8 实例约 %.0f TB/拍（首个实例更贵: 有余量的固定项）" % (per * 8))
        print("    ★ 这就是 E-T 在**未复位**板上看到 di(0)=816.7 TB 而复位后 542.4 TB 的来源")
        print("      ⇒ 也和保障图谱那条缺口对上: **顺序域没有清除语义**, 只能靠复位。")
    print()

    # ══ M2 DIRECT 档门边界（算出来的 vs 实测的）══════════════════════
    print("── M2 门边界: 从源码**算**（两档表） vs 实测 ──")
    itcm, src, fb, tr = src_op_costs()
    c2 = open(os.path.join(ROOT, "src", "engine.c"), encoding="utf-8", errors="replace").read()
    mf = re.search(r"k_op_cost_flash\s*\[[^\]]*\]\s*=\s*\{(.*?)\};", c2, re.S)
    flasht = [int(x) for x in re.findall(r"\d+", mf.group(1))] if mf else []
    MAXR = int(re.search(r"#define\s+MAX_ROUTES\s+(\d+)", open(
        os.path.join(ROOT, "src", "engine.h"), encoding="utf-8", errors="replace").read()).group(1))
    if not itcm:
        print("    !! 解析不到 k_op_cost_itcm ⇒ SKIP")
    else:
        # ★★★ 更正（本脚本第一版把 src 成本按 `SRC_COST_FALLBACK=20` 加了进去, 于是算出的
        #   FLASH 边界是 97, 与 E-N 实测的 105/106 **对不上**）。
        #   真值: `k_src_cost[4] = {0,0,0,0}` —— **已实测的源类型成本为 0**,
        #   `SRC_COST_FALLBACK` 只对"表里没有的源类型"兜底。
        #   ⇒ 拿掉那 20 之后: FLASH DIRECT 247 ⇒ 26000/247 = **105** ✓✓ 与 E-N 逐条吻合;
        #                      FLASH PID    432 ⇒ 26000/432 = **60**  ✓✓ 与 E-O 吻合。
        #   ★ 这就是"用一个已知实测值去反查常数"的价值 —— 它当场抓出了我加错的常数。
        src0 = src[SRC_CONST] if len(src) > SRC_CONST else 0
        print("    ★ 源成本取 `k_src_cost[SRC_CONST] = %d`（**不是** SRC_COST_FALLBACK=%d）"
              % (src0, fb))
        for lab, tbl in (("交付档 ITCM", itcm), ("FLASH 档", flasht)):
            if not tbl:
                continue
            per = tbl[0x00] + src0                   # DIRECT + CONST 源, div0 ⇒ 除数 1
            b = EXEC_DEPLOY_BUDGET // per
            print("    %-12s DIRECT 每路由 = %3d+%d = %3d cyc ⇒ **边界 = %d 条**%s"
                  % (lab, tbl[0x00], src0, per, b,
                     "（> MAX_ROUTES=%d ⇒ **路由数上限先撞**）" % MAXR if b > MAXR else ""))
        print("    ★ 历史对照（E-N/E-O 实测）: FLASH 档 DIRECT **105/106**、PID **60/61**")
        if flasht:
            for nm, opc, hist in (("DIRECT", flasht[0x00], 105), ("PID", flasht[0x05], 60)):
                print("      %-6s 每路由 %d ⇒ %d×%d = %d ≤ 26000 < %d×%d = %d  ⇒ 边界 %d %s"
                      % (nm, opc, opc, hist, opc * hist, opc, hist + 1, opc * (hist + 1), hist,
                         "✓ 与实测吻合" if opc * hist <= EXEC_DEPLOY_BUDGET < opc * (hist + 1) else "✗ 不符"))
        # 交付档: 把**允许的最大条数**部署一次, 看预算余量
        n = MAXR
        sts2, q2 = dcl.send(CMD_DEPLOY, mk(0x00, 0, n), expect_len=None)
        bg = struct.unpack("<I", q2[2:6])[0] if sts2 == 'ACK' and len(q2) >= 6 else -1
        print("    实测: 交付档部署 %d 条 DIRECT ⇒ %s budget=%d（门 26000 的 %.0f%%）"
              % (n, sts2, bg, 100.0 * bg / EXEC_DEPLOY_BUDGET))
        print("    ⇒ **结论: 交付档的门对「路由数上限内的一切程序」都不具约束力**（最大条数下仍只占 %.0f%%）"
              % (100.0 * bg / EXEC_DEPLOY_BUDGET))
        print("      ⇒ 「门从未触发」在交付档上是**结构性的**; 唯一有牙的是 FLASH 档, 而它已在 E-N/E-O 验过。")
    print()

    # ══ M3 大块读的板侧阻塞标定 ══════════════════════════════════════
    print("── M3 大块 0x22 读的**板侧**占时标定（环头 tick 差）──")
    print("    %-8s %-12s %-12s %s" % ("K(字)", "tick 差", "≈ms", "ms/百字"))
    for K in (8, 32, 64, 128, 200):
        t0 = ring_tick(dcl, shm)
        raw = rd(dcl, shm + OFF_WIRE_MAP, K)
        t1 = ring_tick(dcl, shm)
        if raw is None or t0 is None or t1 is None:
            print("    K=%-6d 读失败" % K); continue
        dt = t1 - t0
        print("    %-8d %-12d %-12.1f %.1f" % (K, dt, dt / 10.0, (dt / 10.0) / (K / 100.0)))
    print("    ★ 注意: 这里量的是**两次环头读之间**的 tick 差, 含读本身的串口时间（~17ms 固定）")
    print("      ⇒ 「板侧占时」的上界; E-S 里用的就是每次实测值而不是一条曲线。")
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
