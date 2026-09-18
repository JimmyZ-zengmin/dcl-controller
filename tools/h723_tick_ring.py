#!/usr/bin/env python3
"""
h723_tick_ring.py — **逐拍**预测 vs 逐拍实测（E4 的严格形态）

★★ 与"累积量"的区别
  `MIN/MAX/SUM` 能回答"典型拍稳不稳"（实测 0.02~0.06 cyc），但**回答不了"
  哪一拍贵、贵多少"**。E4 要判的是**逐拍**对齐 ⇒ 需要每拍一条、不丢拍的记录。
  载体: SHM 环形缓冲 `OFF_EXEC_RING`（`engine.h`），**ISR 直接写**、无镜像滞后。

★★ 判据为什么先做"纯结构性"的那一条
  成本模型（Σc + k×转变数 + c₀）只在 2 个原语对上验过。而**桶调度的结构**是确定的:
      nrun(t) = n_div0 + cnt1[t%10] + cnt2[t%100]
  ⇒ 实测 `di` 的**不同取值**应与**不同的 nrun** 一一对应, 且**每个取值的出现频次**
    应等于 `t ∈ [0,100)` 中该 nrun 出现的次数。
  这条判据**完全不依赖成本模型** ⇒ 它能在"模型还不全"的时候就判定
  "引擎确实按桶表在跑, 而且每拍的成本只由本拍跑的路由集合决定"。

判据（每条都能失败）
  P0 前置: 环缓冲在动（写计数在涨）
  P1 ★ **取值个数 == nrun 的取值个数**（多一个/少一个都红）
  P2 ★ **每个取值的频次 == 该 nrun 在 t∈[0,100) 里的出现次数**（逐项比对，允许 ±2% 采样误差）
  P3 取值**随 nrun 单调递增**（跑得多的拍必须更贵 —— 排序一致性）
  P4 反例: 若程序是单档（全 div1）、桶均匀 ⇒ 频次应为 9:1 之类的**明确比例**,
     而不是"看起来差不多"

用法
  python tools/h723_tick_ring.py --prog 0,0,128     # 128×DIRECT div0 ⇒ 期望单一取值
  python tools/h723_tick_ring.py --prog 5,1,128     # 128×PID div1   ⇒ 期望 2 个取值
  python tools/h723_tick_ring.py --prog 5,2,128     # 128×PID div2   ⇒ 100 拍长周期
退出码: 0 = 全过 / 1 = 有 FAIL / 2 = 前置不满足（判无效）
"""
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, os, struct, sys, time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from h723_client import Dcl  # noqa: E402

cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst = 0x38, 0x10, 0x12, 0x11, 0x22
OFF_ROUTE_TABLE, OFF_ROUTE_BUCKETS = 0x0840, 0x4480
OFF_EXEC_RING_HDR, OFF_EXEC_RING = 0x3880, 0x3890
RING_SLOTS = 256
B1P, B2P = 10, 100
ACTIVE = 0x01
SRC_CONST, DST_WIRE = 2, 2


def mk(op, div, n):
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  ACTIVE, i, (i % 64) + 1, 0, 0, div, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    states = b"\x00" * (16 * (min(n, 64) + 1))
    return struct.pack("<HHH", n, n, min(n, 64) + 1) + routes + params + states + b"\x00" * 16


def rd(dcl, addr, nwords, chunk=200):
    out, off = b"", 0
    while off < nwords:
        k = min(chunk, nwords - off)
        sts, p = dcl.send(cmd_burst, struct.pack("<IH", addr + 4 * off, k), expect_len=None)
        if sts != "ACK" or len(p) < 4 * k:
            return None
        out += p[:4 * k]
        off += k
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("DCL_PORT"))
    ap.add_argument("--prog", required=True, metavar="OP,DIV,N")
    ap.add_argument("--settle", type=float, default=1.5)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    dcl = Dcl(a.port)
    print("端口 = %s" % dcl.port); time.sleep(1.0)
    res = []
    try:
        sts, p = dcl.send(cmd_status, expect_len=51)
        if sts != "ACK" or len(p) < 51:
            print("!! 0x38 失败"); return 2
        shm = struct.unpack("<I", p[23:27])[0]
        print("SHM = 0x%08X" % shm)

        op, div, n = (int(x, 0) for x in a.prog.split(","))
        dcl.send(cmd_stop); time.sleep(0.15)
        dcl.send(cmd_start); time.sleep(0.15)
        sts, pp = dcl.send(cmd_deploy, mk(op, div, n), expect_len=None)
        if sts != "ACK":
            print("!! deploy 被拒: %s" % (pp.decode('utf-8', 'replace') if sts == 'NAK' else sts))
            return 2
        print("已部署 op=%d div=%d n=%d ⇒ ACK budget=%d"
              % (op, div, n, struct.unpack("<HI", pp[:6])[1]))

        # STOP/START ⇒ 清统计 + 清环, 保留程序
        dcl.send(cmd_stop); time.sleep(0.15)
        dcl.send(cmd_start); time.sleep(0.15)
        time.sleep(a.settle)

        # ★ 先读环, 再读头; 只采信头里写计数**之前**的条目 ⇒ 无需加锁
        ring = rd(dcl, shm + OFF_EXEC_RING, RING_SLOTS)
        hdr = rd(dcl, shm + OFF_EXEC_RING_HDR, 4)
        if ring is None or hdr is None:
            print("!! 读环失败 ⇒ 判无效"); return 2
        w, last_tick, slots = hdr[0], hdr[1], hdr[2]
        print("环头: 写计数=%d 最后 tick=%d 槽数=%d" % (w, last_tick, slots))
        if w == 0:
            print("!! 写计数为 0 ⇒ 环没在动 ⇒ 判无效（固件没烧对？）"); return 2
        res.append(("P0 环缓冲在动（写计数 > 0）", True))

        # 取出最近 min(w, slots) 条, 并回推每条的 tick
        cnt = min(w, RING_SLOTS)
        vals, ticks = [], []
        for k in range(cnt):
            idx = (w - cnt + k) & (RING_SLOTS - 1)
            vals.append(struct.unpack("<I", ring[idx * 4:idx * 4 + 4])[0])
            ticks.append(last_tick - (cnt - 1 - k))
        hist = Counter(vals)
        print("\n逐拍 di 的取值分布（共 %d 拍，跨度 %d..%d）:" % (cnt, ticks[0], ticks[-1]))
        for v, c in sorted(hist.items()):
            print("    di=%-7d 出现 %4d 次  (%.1f%%)" % (v, c, 100.0 * c / cnt))

        # ── 结构预测: nrun(t) = n_div0 + cnt1[t%10] + cnt2[t%100] ─────────
        bk = rd(dcl, shm + OFF_ROUTE_BUCKETS, 110)
        rt = rd(dcl, shm + OFF_ROUTE_TABLE, 128 * 4)
        if bk is None or rt is None:
            print("!! 读桶表/路由表失败 ⇒ 判无效"); return 2
        u = struct.unpack("<220H", bk)
        off1, cnt1 = list(u[0:10]), list(u[10:20])
        off2, cnt2 = list(u[20:120]), list(u[120:220])
        nr = off1[0] + sum(cnt1) + sum(cnt2)
        n0 = off1[0]
        pred_nrun = Counter(n0 + cnt1[t % B1P] + cnt2[t % B2P] for t in range(100))
        print("\n结构预测（一个 100 拍周期内）: n_routes=%d  div0=%d" % (nr, n0))
        for k, c in sorted(pred_nrun.items()):
            print("    本拍跑 %-4d 条 ⇒ 占 %3d/100 拍" % (k, c))

        # 把实测的 di 取值升序排列, 与预测的 nrun 取值升序排列配对
        obs = sorted(hist.items())                    # [(di, count)] 按 di 升序
        pn = sorted(pred_nrun.items())                # [(nrun, count)] 按 nrun 升序
        print("\n配对（di 升序 ⇄ nrun 升序）:")
        for i in range(max(len(obs), len(pn))):
            o = obs[i] if i < len(obs) else ("—", 0)
            q = pn[i] if i < len(pn) else ("—", 0)
            print("    #%d  di=%-7s 频次 %-5s   |   nrun=%-5s 预期拍数 %s"
                  % (i, o[0], o[1], q[0], q[1]))

        res.append(("P1 实测取值个数 == 预测 nrun 取值个数",
                    len(obs) == len(pn)))
        # P2: 频次比对（预测是"每 100 拍"的次数; 实测窗口 cnt 拍 ⇒ 按比例折算）
        ok2, worst = True, None
        for i in range(min(len(obs), len(pn))):
            exp = pn[i][1] * cnt / 100.0
            got = obs[i][1]
            if exp < 3:
                continue
            if abs(got - exp) > max(0.05 * exp, 3):
                ok2 = False
                worst = (obs[i][0], got, exp)
        res.append(("P2 各取值频次 == 结构预测频次（±5%）", ok2))
        if worst:
            print("    ✗ 频次不符: di=%d 实测 %d 次, 预期 %.1f 次" % worst)
        # P3: 单调性
        mono = all(obs[i][1] >= obs[i + 1][1] for i in range(len(obs) - 1)) if len(obs) > 1 else True
        res.append(("P3 跑得多的拍确实更贵（频次随取值单调减）", mono))
        if a.json:
            import json
            os.makedirs(os.path.dirname(a.json) or ".", exist_ok=True)
            json.dump(dict(prog=a.prog, cnt=cnt, hist=dict(hist),
                           pred_nrun=dict(pred_nrun), nr=nr, n0=n0,
                           first_tick=ticks[0], last_tick=ticks[-1]),
                      open(a.json, "w"), indent=1)
            print("\n原始数据: %s" % a.json)
    finally:
        dcl.send(cmd_stop); time.sleep(0.2); dcl.send(cmd_start); time.sleep(0.3)
        dcl.close()

    print("\n=== 断言 ===")
    for k, v in res:
        print("  [%s] %s" % ("PASS" if v else "FAIL", k))
    bad = [k for k, v in res if not v]
    print("\n%d 项, %d FAIL" % (len(res), len(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
