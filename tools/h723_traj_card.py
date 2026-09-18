#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_traj_card.py —— 轨迹形状验收：**走 SD 卡这条路**（卡的环 31 GB，没有在片环 960 槽的限制）

## 为什么需要它（前几轮撞的墙）
- **在片环**（AXI `0x24004000`，960 槽 × 256B）：SWD 读得到，但"变化才记" ⇒ 运动时只覆盖 **~0.4 s**
  ⇒ 装不下 1.2 s 周期的三角波 ⇒ 形状判据只能判 SKIP（见 `h723_traj_bb_verify.py`）。
- **卡上日志环**（`LBA 1..61067263` = 31 GB，写指针 `h[7]`，跨上电持久）
  ⇒ **容量上没有任何问题**，只要运动窗口**被记进卡**。

⇒ 本工具两个子命令：
  · `run`     ：**只走串口**（不碰 pyocd）把运动跑起来一段时间 —— 卡在板子里时执行，让日志记下这一段。
  · `analyze` ：从卡的**写指针往回**找 `WIRE12>A/2` 的窗口，然后跑**同一套形状判据**。

## 复用了什么（不重复实现）
`Disk` / `looks_like_card` / `REC` / `BLK` / `REC_MAGIC` 全部 **import 自 `tools/sd_log_read.py`**
（本项目纪律：同一个量/结构不许两处实现。**血证**：我上一支探针工具自己抄了一遍头部偏移，
抄错了 `h[7]→w[3]`，于是探针全落在数据区开头而"看起来正常" ⇒ 这里**必须复用 + 自检**。）

用法（★ 需要 **系统 python**，它才有 `pyocd`；纯 `analyze` 用哪个解释器都行）:
  python tools/h723_traj_card.py run  --A 1200 --secs 6      # 卡插在板子上
  python tools/h723_traj_card.py analyze                     # 卡插在电脑上
  python tools/h723_traj_card.py analyze --blocks 262144     # 往回搜得更远
"""
import argparse
import os
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sd_log_read import Disk, looks_like_card, REC, BLK, REC_MAGIC     # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TICK_HZ = 10000.0
SPR, CPR = 1600.0, 4096.0
K = CPR / SPR
# ★ 头部字段偏移：与 `tools/sd_log_read.py` 的用法**逐项一致**（它又是按固件 src/sd.c 抄的）
H_MAGIC, H_VER, H_REC, H_PERBLK, H_BLKSZ, H_START, H_NBLK, H_WP, H_RECS = 0, 1, 2, 3, 4, 5, 6, 7, 8
# 数据槽下标（卡上的映射表已核实与固件默认表逐项一致）
D_RAW, D_W12 = 0, 28


def _hdr(d):
    """读 LBA0 头部 + **几何自检**。自检存在的唯一理由：让我这类"抄错偏移"当场失败。"""
    h = struct.unpack_from("<128I", d.read(0, BLK), 0)
    if h[H_MAGIC] != 0x474F4C44:
        raise SystemExit("LBA0 不是 DLOG 头")
    if (h[H_REC], h[H_PERBLK], h[H_BLKSZ], h[H_START]) != (REC, 2, BLK, 1):
        raise SystemExit("几何自检失败: 记录=%d 每块=%d 块=%d 起始=%d ⇒ 偏移抄错了"
                         % (h[H_REC], h[H_PERBLK], h[H_BLKSZ], h[H_START]))
    if not (h[H_START] <= h[H_WP] <= h[H_START] + h[H_NBLK]):
        raise SystemExit("写指针 %d 不在数据区 ⇒ 偏移抄错了" % h[H_WP])
    return h


def _find_card(disk=None):
    for n in ([disk] if disk is not None else range(0, 8)):
        try:
            d = Disk(n)
        except OSError:
            continue
        try:
            if looks_like_card(d):
                print("  命中 PhysicalDrive%d" % n)
                return d
        except Exception:
            pass
        d.close()
    raise SystemExit("没找到带 DLOG 头的物理盘（卡插好了吗？换个 --disk 试试）")


def _decode(blob):
    """一个 blob → [(tick, seq, raw, w12)]（按块内物理顺序，天然时间序）"""
    out = []
    for i in range(len(blob) // REC):
        o = i * REC
        mg, tick, seq = struct.unpack_from("<3I", blob, o)
        if mg != REC_MAGIC:
            continue
        raw = struct.unpack_from("<f", blob, o + 16 + D_RAW * 4)[0]
        w12 = struct.unpack_from("<f", blob, o + 16 + D_W12 * 4)[0]
        out.append((tick, seq, raw, w12))
    return out


def _backward_blocks(d, hdr, want, max_blocks):
    """从写指针**往回**分块读，直到找到含 `WIRE12>threshold` 的块。返回 (blob, lba0, 扫了几块)"""
    start, nblk, wp = hdr[H_START], hdr[H_NBLK], hdr[H_WP]
    scanned = 0
    step = 4096                                   # 每步 2MB（2 条/块 ⇒ 8192 条）
    while scanned < max_blocks:
        lo = wp - step
        if lo < start:
            lo += nblk
        # 从 lo 正向读 step 块（跨回卷点也保持时间序）
        parts = []
        lba, left = lo, step
        while left > 0:
            n = min(left, start + nblk - lba)
            parts.append(d.read(lba, n * BLK))
            lba += n
            left -= n
            if lba >= start + nblk:
                lba = start
        recs = _decode(b"".join(parts))
        scanned += step
        if want(recs):
            return recs, lo, scanned
        wp = lo
    return [], None, scanned


def _read_range(d, hdr, max_blocks):
    """从写指针往回**读满 max_blocks 块**（不做早停），返回全部记录（按块内物理顺序 = 时间序）。
    ★ 为什么需要"不早停"：判别实验会在卡上留下**多次**运行，要一次都判掉 ⇒
      只找到第一个窗口就停会把后面两次白跑（还要再拔插一次卡）。"""
    start, nblk, wp = hdr[H_START], hdr[H_NBLK], hdr[H_WP]
    out, scanned = [], 0
    step = 4096
    chunks = []
    while scanned < max_blocks:
        lo = wp - step
        if lo < start:
            lo += nblk
        parts, lba, left = [], lo, step
        while left > 0:
            n = min(left, start + nblk - lba)
            parts.append(d.read(lba, n * BLK))
            lba += n
            left -= n
            if lba >= start + nblk:
                lba = start
        chunks.append((lo, _decode(b"".join(parts))))
        scanned += step
        wp = lo
    # 按时间（从旧到新）拼起来：chunk 是从新往旧收集的 ⇒ 反转
    for lo, recs in reversed(chunks):
        out += recs
    return out


def cmd_analyze_all(a):
    """把扫描范围内**所有**含 A 相的窗口都判一遍（每次运行一个窗口）⇒ 一次换卡拿到多组结论。"""
    d = _find_card(a.disk)
    try:
        hdr = _hdr(d)
        print("  头部: 写指针 LBA=%d 已落盘=%d 条" % (hdr[H_WP], hdr[H_RECS]))
        print("  往回读满 %d 块（%.0f MB）..." % (a.blocks, a.blocks * BLK / 1e6))
        recs = _read_range(d, hdr, a.blocks)
        print("  共 %d 条记录" % len(recs))
        nwin = 0
        for gi, g in enumerate(_split_boots(recs)):
            on = [i for i, r in enumerate(g) if r[3] > a.A / 2]
            if len(on) < 50:
                continue
            # 把 A 相按"间隔 > 2 s"切成不同次运行（tick 单位 100 µs ⇒ 20000）
            runs, s, prev = [], on[0], on[0]
            for i in on[1:]:
                if g[i][0] - g[prev][0] > 20000:
                    runs.append((s, prev)); s = i
                prev = i
            runs.append((s, prev))
            for x, y in runs:
                if y - x < 50:
                    continue
                nwin += 1
                print("\n══════ 窗口 #%d：%d 条 A 相（段 %d, tick %d..%d）══════"
                      % (nwin, y - x + 1, gi, g[x][0], g[y][0]))
                _judge(g[max(0, x - 200):min(len(g), y + 200)], a.A, a.dwell)
        if not nwin:
            print("  没找到任何含 A 相的窗口 ⇒ 检查 `run` 是否写进卡（看它的 s_log_blk Δ）")
            return 2
        return 0
    finally:
        d.close()


def _unwrap(recs):
    """把 raw（0..4095 单圈）**解卷绕**成单调序列 —— 位置域判据的前提。
    ★ 逐记录 Δ 很小（0.5 ms 间隔、≤1200 Hz ⇒ ≤2 counts）⇒ 最短弧解卷绕安全。"""
    u, prev = [], None
    for r in recs:
        x = int(round(r[2]))
        if prev is None:
            u.append(x)
        else:
            d = (x - prev) & 0xFFF
            if d > 2048:
                d -= 4096
            u.append(u[-1] + d)
        prev = x
    return u


def _fit_quad(xs, ys):
    """最小二乘二次拟合 y = a·x² + b·x + c ⇒ (a, b, c, 残差RMS)"""
    n = len(xs)
    if n < 6:
        return None
    mx = sum(xs) / n
    x = [v - mx for v in xs]                       # 居中降相关
    S = [sum(t ** k for t in x) for k in range(5)]
    T = [sum(ys[i] * (x[i] ** k) for i in range(n)) for k in range(3)]
    # 3x3 正规方程
    import itertools
    A = [[S[i + j] for j in range(3)] for i in range(3)]
    B = T[:]
    for col in range(3):                           # 高斯消元
        p = max(range(col, 3), key=lambda r: abs(A[r][col]))
        if abs(A[p][col]) < 1e-12:
            return None
        A[col], A[p] = A[p], A[col]
        B[col], B[p] = B[p], B[col]
        for r in range(col + 1, 3):
            f = A[r][col] / A[col][col]
            for c2 in range(col, 3):
                A[r][c2] -= f * A[col][c2]
            B[r] -= f * B[col]
    co = [0.0] * 3
    for r in (2, 1, 0):
        s = B[r] - sum(A[r][c2] * co[c2] for c2 in range(r + 1, 3))
        co[r] = s / A[r][r]
    c0, b0, a0 = co[0], co[1], co[2]
    res = [ys[i] - (a0 * x[i] ** 2 + b0 * x[i] + c0) for i in range(n)]
    rms = (sum(v * v for v in res) / n) ** 0.5
    return a0, b0, c0, rms


def _judge_position(seg, A, dwell, cands):
    def rec_ok(cond, name, detail=""):
        print("  [%s] %-50s %s" % ("PASS" if cond else "FAIL", name, detail))
        return cond
    """★★★ T2'' **位置域**形状判据（2026-09-18 新增）—— 因为速度域在本装置上分辨不出：
      测速噪声底实测 **84~92%**（20 ms 滑窗撞突发式回填）⇒ 判"斜坡线性度"必然无效。
      ⇒ 改成用**累积位移**（积分量、噪声低一个数量级）：
        ① 每个半周期算"**实测位移 vs 模型预测位移**"（模型由 A / 斜率 / 段长给定）
           ⇒ 比值 ≈1 即"形状与模型相符"；并且**能反认出这个窗口是哪档斜率**（判据自动识别参数✓）
        ② 在**模型预测的斜坡窗**内做二次拟合 ⇒ `a` 应 ≈ `slope·K/2`（位置在斜坡下是二次的）。
      ★ 这两条都不做"速度求导"，所以不受 §5.27/§5.28 那条噪声族的伤害。"""
    u = _unwrap(seg)
    T0 = seg[0][0]
    ts = [((r[0] - T0) & 0xFFFFFFFF) / TICK_HZ for r in seg]
    w = [r[3] for r in seg]
    edges = [i for i in range(1, len(w)) if (w[i - 1] > A / 2) != (w[i] > A / 2)]
    if len(edges) < 4:
        print("  ⛔ T2'' 半周期不足 ⇒ 判无效")
        return True
    halves = []
    for x, y in zip(edges, edges[1:]):
        if y - x < 8:
            continue
        halves.append((x, y, w[x] > A / 2))
    if len(halves) < 4:
        print("  ⛔ T2'' 可用的半周期不足 ⇒ 判无效")
        return True
    # ── ① 用候选斜率反认 + 位移比 ──
    best, _cand_rows = None, []
    for S in cands:
        rs, rs_r, rs_f = [], [], []
        for x, y, isA in halves:
            T = ts[y] - ts[x]
            v0, v1 = (0.15 * A, A) if isA else (A, 0.15 * A)
            c0, c1 = v0 * K, v1 * K                      # counts/s
            tr = min(T, abs(c1 - c0) / (S * K)) if S else T
            area = (c0 + c1) / 2 * tr + c1 * (T - tr)    # counts
            d = abs(u[y] - u[x])
            if area > 1:
                r = d / area
                rs.append(r)
                (rs_r if isA else rs_f).append(r)
        rs.sort()
        med = rs[len(rs) // 2] if rs else 0.0
        _cand_rows.append((S, rs, med))
        if best is None or abs(med - 1) < abs(best[2] - 1):
            best = (S, rs, med)
        # ★★ 升/降要**分开报**：混在一起的"中位数"是无意义的统计量
        #   （血证：升段 +70%、降段 −52% ⇒ 中位给 1.309，既不像升也不像降 ⇒ 什么都说明不了）
        if len(cands) == 1:
            for tag, arr in (("升段", rs_r), ("降段", rs_f)):
                arr.sort()
                mm = arr[len(arr) // 2] if arr else 0.0
                print("  T2''-1 %s：位移比中位 **%.3f**（%d 半）%s"
                      % (tag, mm, len(arr),
                         "← **比模型快**" if mm > 1.15 else ("← **比模型慢**" if mm < 0.85 else "✓")))
    S, rs, med = best
    # ★★ 自检：这个"反认"到底分不分得开？——下限 0.15A 时，斜坡长短对**模型面积**的影响很小
    #   （12000 与 30000 的模型面积只差几个百分点）⇒ 若各候选的比值都接近 1，则**认不出斜率**。
    spread = max(abs(r[2] - 1) for r in _cand_rows)
    print("  T2''-1 **位移比**（实测/模型）各候选：%s"
          % ", ".join("%d→%.3f" % (r[0], r[2]) for r in _cand_rows))
    print("  T2''-1 取最佳：**斜率≈%d Hz/s**，比值中位 **%.3f**" % (S, med))
    # ★ 弱判别闸门**只在"多候选"时适用**：若 `--slope` 已显式给出（单候选），
    #   斜率是**已知参数**而不是"反认结果" ⇒ 不存在"认不出来"的问题（第一版把它误判成 SKIP）。
    if len(_cand_rows) > 1 and spread < 0.02:
        print("     ⛔ 各候选的比值都接近 1（极差 <2%%）⇒ **这条反认不出斜率**（弱判别）")
        print("        ⇒ T2''-2 需要「斜率」当输入，而它认不出来 ⇒ **T2''-2 判无效（SKIP）**")
        print("        ⇒ 正解：把 `--slope` 显式传进来（已知这次跑的是哪档），或让程序把斜率也写进卡。")
        ok2 = True
    else:
        ok = rec_ok(0.85 <= med <= 1.15, "T2''-1 实测位移 ≈ 模型位移（模型=斜坡 A↔0.15A）",
                    "%.3f（>1 ⇒ **实际 slew 快于设定**；<1 ⇒ 慢于设定）" % med)
        # ★ 不 early-return：T2''-1 FAIL 时更要跑 T2''-2 —— 它给出**定量的 slew 比**（正如下面）。
    # ── ② 斜坡窗内的二次拟合 ──
    aa, resid = [], []
    for x, y, isA in halves:
        T = ts[y] - ts[x]
        v0, v1 = (0.15 * A, A) if isA else (A, 0.15 * A)
        c0, c1 = v0 * K, v1 * K
        tr = min(T, abs(c1 - c0) / (S * K)) if S else T
        j = x
        while j < y and ts[j] - ts[x] < 0.8 * tr:
            j += 1
        if j - x < 6:
            continue
        r = _fit_quad([ts[k] - ts[x] for k in range(x, j + 1)], [u[k] for k in range(x, j + 1)])
        if r:
            aa.append((abs(r[0]) / (S * K / 2) if S else 0.0, r[3], u[j] - u[x]))
    if aa:
        near = [1 for r in aa if 0.7 <= r[0] <= 1.3]
        rr = sorted(r[0] for r in aa)
        print("  T2''-2 斜坡窗内二次项 |a|/(slope·K/2)：中位 **%.2f**（%d 半，P25~P75 %.2f~%.2f）"
              % (rr[len(rr) // 2], len(rr), rr[len(rr) // 4], rr[3 * len(rr) // 4]))
        ok &= rec_ok(len(near) >= max(2, len(aa) // 2),
                     "T2''-2 斜坡段位置是二次的（|a| 比在 0.7~1.3）", "%d/%d 半" % (len(near), len(aa)))
    else:
        print("  ⛔ T2''-2 斜坡窗内样本不足 ⇒ 判无效")
    return ok


def cmd_analyze(a):
    """只判**最新一个** A 相窗口（首跑/单次运行用）。"""
    d = _find_card(a.disk)
    try:
        hdr = _hdr(d)
        print("  头部: 数据区 LBA %d..%d (%.2f GB)  写指针 LBA=%d  已落盘=%d 条"
              % (hdr[H_START], hdr[H_START] + hdr[H_NBLK] - 1, hdr[H_NBLK] * BLK / 1e9,
                 hdr[H_WP], hdr[H_RECS]))
        A = a.A
        print("  从写指针往回搜 WIRE12 > %.0f 的窗口（最多 %d 块）..." % (A / 2, a.blocks))
        recs, lo, scanned = _backward_blocks(
            d, hdr, lambda rs: any(r[3] > A / 2 for r in rs), a.blocks)
        if not recs:
            print("\n  ⛔ **卡上没找到运动窗口**（往回扫了 %d 块 = %d 条记录，全无 WIRE12>%.0f）"
                  % (scanned, scanned * 2, A / 2))
            print("     两种可能：① 窗口在更早的位置（加大 --blocks）；")
            print("               ② 那几次运行**根本没进卡日志**（跑的时候卡不在板子里 / 日志未开）。")
            print("     ⇒ 最干净的做法：**卡插回板子 → 跑 `run` → 再插回电脑 → 再 `analyze`**。")
            return 2
        print("  找到窗口：起点 LBA %d，往回 %d 块" % (lo, scanned))
        return _judge(recs, A, a.dwell)
    finally:
        d.close()


def _split_boots(recs):
    """★★★ 按**上电段**切分 —— 卡上的日志横跨多次上电，而 `tick`/`seq` **每次上电归零**
    （读卡技能的第一条纪律：「每次上电归零的计数器不能用来排跨上电的数据」）。
    ★ 血证（本工具第一版漏了这步）：找到的窗口跨了上电边界 ⇒ 用 `(tick-t0)&0xFFFFFFFF` 算时间
      ⇒ 跨度读出 **429478 s**（5 天）、峰值 **23×A**、累积 0 —— **全是荒谬值**，
      而"命令侧 A 相占 49%"却是对的（那一项不需要时间连续性）⇒ **最容易蒙混过去的那种错**。
    判据：`seq` 回减 或 `tick` 倒退 ⇒ 新上电（两个独立指标，避免单指标误判）。"""
    segs, s = [], 0
    for i in range(1, len(recs)):
        if recs[i][1] <= recs[i - 1][1] or recs[i][0] < recs[i - 1][0]:
            segs.append(recs[s:i]); s = i
    segs.append(recs[s:])
    return [g for g in segs if len(g) >= 32] or [recs]


def _judge(recs, A, dwell):
    segs = _split_boots(recs)
    if len(segs) > 1:
        print("  ⚠ 窗口跨 %d 个上电段（tick/seq 每次上电归零）⇒ **选含 A 相最多的那一段**判；"
              % len(segs))
        for g in segs:
            print("     段 %d 条（tick %d..%d, seq %d..%d, A相 %d）"
                  % (len(g), g[0][0], g[-1][0], g[0][1], g[-1][1],
                     sum(1 for r in g if r[3] > A / 2)))
    # ★ 不能盲取"最新段"：运动常发生在**上一次上电**里，而最新段是运动之后的空闲
    #   （本工具第二版就是这么判成"0 条 A 相"的）。⇒ 取**含 A 相最多**的那一段。
    recs = max(segs, key=lambda g: sum(1 for r in g if r[3] > A / 2))
    on = [i for i, r in enumerate(recs) if r[3] > A / 2]
    if len(on) < 50:
        print("  ⛔ 各段里 A 相最多的那段也只有 %d 条 ⇒ **判无效**（采样不足）" % len(on))
        return 2
    i0, i1 = on[0], on[-1]
    seg = recs[max(0, i0 - 200):min(len(recs), i1 + 200)]
    t0 = seg[0][0]
    ts = [((r[0] - t0) & 0xFFFFFFFF) / TICK_HZ for r in seg]
    span = ts[-1]
    w12 = [r[3] for r in seg]
    raw = [r[2] for r in seg]
    nz = sum(1 for x in w12 if x > A / 2)
    period = 2 * dwell
    print("  窗口 %d 条 / %.3f s（%.2f 个周期，周期=%.3f s）  命令侧 A 相占 %.0f%%"
          % (len(seg), span, span / period, period, 100.0 * nz / len(seg)))

    W = 0.020
    vh, tw = [], []
    j = 0
    for i in range(len(seg)):
        while ts[i] - ts[j] > W:
            j += 1
        if i == j:
            continue
        dt = ts[i] - ts[j]
        dv = (int(round(raw[i])) - int(round(raw[j]))) & 0xFFF
        if dv > 2048:
            dv -= 4096
        vh.append(abs(dv) / dt / K)
        tw.append(w12[i])

    ok = True

    def rec_ok(cond, name, detail=""):
        print("  [%s] %-46s %s" % ("PASS" if cond else "FAIL", name, detail))
        return cond

    ok &= rec_ok(0.35 < nz / len(seg) < 0.65, "T1 命令侧是 0/A 交替方波(≈50%占空)",
                 "%.0f%%" % (100.0 * nz / len(seg)))
    # 半周期划分：命令的下降沿
    # ★★★ 分段必须按**所有跳变**（上升沿 + 下降沿）切"半周期"。
    #   血证（本工具第三版）：按"下降沿到下降沿"切 ⇒ 切出来的是**一整个周期**（先降后升）
    #   ⇒ 线性拟合 R² 只有 0.42~0.74、且斜率**全是正的**（看起来像"不是三角波"）。
    #   那是**分段错**，不是硬件错。
    edges = [i for i in range(1, len(w12))
             if (w12[i - 1] > A / 2) != (w12[i] > A / 2)]
    fits, ks, halves = [], [], []
    for x, y in zip(edges, edges[1:]):
        pts = [(ts[k] - ts[x], vh[k]) for k in range(x + 1, min(y, len(vh)))]
        if len(pts) < 8:
            continue
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        n = len(xs); mx = sum(xs) / n; my = sum(ys) / n
        sxy = sum((xs[k] - mx) * (ys[k] - my) for k in range(n))
        sxx = sum((x - mx) ** 2 for x in xs); syy = sum((y - my) ** 2 for y in ys)
        k = sxy / sxx if sxx else 0.0
        r2 = (sxy * sxy / (sxx * syy)) if (sxx and syy) else 0.0
        ks.append(k); fits.append((k, r2, n))
        halves.append((w12[x] > A / 2, ys))          # 这一半是 A 相还是 0 相，及其速度样本
    # ★ 诊断：把升/降两半的**时长与速度均值**分别打出来 ⇒ 直接看"平均只有 74%"丢在哪
    for isA in (True, False):
        g = [h for h in halves if h[0] == isA]
        if g:
            print("  诊断 %s相：%d 半，速度均值 %.0f Hz（样本 %d）"
                  % ("A " if isA else "0 ", len(g),
                     sum(sum(h[1]) / len(h[1]) for h in g) / len(g),
                     sum(len(h[1]) for h in g)))
    lin = [f for f in fits if f[1] >= 0.90]
    print("  T2 逐半周期拟合 (k, R², n): %s" % ", ".join("(%.0f,%.2f,%d)" % f for f in fits[:6]))
    # ★★★ T2 判据改**三段模型**：被测曲线是"斜坡 → 平顶 →（反向）斜坡"的**梯形**（斜坡限幅 + 段长固定），
    #   对它用**单直线**拟合当然低 —— 血证：R² 只有 0.30~0.76，而 T2b（斜率正负交替）一直 PASS。
    #   ⇒ 正解：**只对"真正在变的那一段（斜坡段）"拟合**：取 |dv/dt| 超过"该半段最大斜率 20%"的样本再拟合。
    #     —— 这样"斜坡是否线性"才问得清楚，而平顶不参与 ⇒ 判据与"梯形的存在"解耦。
    ramp_fits = []
    for x, y in zip(edges, edges[1:]):
        pts = [(ts[k] - ts[x], vh[k]) for k in range(x + 1, min(y, len(vh)))]
        if len(pts) < 8:
            continue
        slopes = [(pts[i][1] - pts[i - 1][1]) / (pts[i][0] - pts[i - 1][0])
                  for i in range(1, len(pts)) if pts[i][0] > pts[i - 1][0]]
        if not slopes:
            continue
        smax = max(abs(s) for s in slopes) or 1.0
        sel = [pts[i] for i in range(1, len(pts))
               if abs(slopes[i - 1]) >= 0.20 * smax]
        if len(sel) < 6:
            continue
        xs = [p[0] for p in sel]; ys = [p[1] for p in sel]
        n = len(xs); mx = sum(xs) / n; my = sum(ys) / n
        sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        sxx = sum((x - mx) ** 2 for x in xs); syy = sum((y - my) ** 2 for y in ys)
        k = sxy / sxx if sxx else 0.0
        r2 = (sxy * sxy / (sxx * syy)) if (sxx and syy) else 0.0
        ramp_fits.append((k, r2, len(sel)))
    rampin = [f for f in ramp_fits if f[1] >= 0.90]
    print("  T2' **只对斜坡段**拟合 (k, R², n): %s"
          % ", ".join("(%.0f,%.2f,%d)" % f for f in ramp_fits[:6]))
    # ★★★ **先量噪声底再判**（否则会把"我的估计器不够准"判成"形状不合格"）：
    #   用**平顶段**（本该恒定）的样本算 `std/mean` ⇒ 那就是速度估计的噪声水平。
    #   若噪声底大到与"100 ms 斜坡的线性度要求"同量级 ⇒ 这条判据**判无效（SKIP）**，不判 FAIL。
    flat = []
    for x, y in zip(edges, edges[1:]):
        pts = [(ts[k] - ts[x], vh[k]) for k in range(x + 1, min(y, len(vh)))]
        if len(pts) < 8:
            continue
        slopes = [(pts[i][1] - pts[i - 1][1]) / (pts[i][0] - pts[i - 1][0])
                  for i in range(1, len(pts)) if pts[i][0] > pts[i - 1][0]]
        if not slopes:
            continue
        smax = max(abs(s) for s in slopes) or 1.0
        flat += [pts[i][1] for i in range(1, len(pts)) if abs(slopes[i - 1]) < 0.05 * smax]
    if len(flat) > 20:
        m = sum(flat) / len(flat)
        sd = (sum((v - m) ** 2 for v in flat) / len(flat)) ** 0.5
        nb = sd / m if m else 9.9
        print("  噪声底（平顶段 std/mean）= **%.1f%%**（%d 样本，均值 %.0f Hz）"
              % (100 * nb, len(flat), m))
        if nb > 0.15:
            print("     ⛔ 噪声底 >15% ⇒ **T2' 判无效（SKIP）**：在这个噪声水平下，")
            print("        100 ms 斜坡的线性度**分辨不出来**（要求 R²≥0.90 对它不可能成立）")
            print("        ⇒ 要判斜坡线性，应该改在**位置**上拟合（位置是积分量、噪声低得多），")
            print("          而不是在**差分出的速度**上（本项目 §5.27/§5.28 的规矩）。")
            print("        ⇒ 已登记的下一步：T2'' = 对半周期内的 raw(t) 做二次拟合（a≠0 即斜坡）。")
        else:
            ok &= rec_ok(len(ramp_fits) >= 2 and len(rampin) >= max(2, len(ramp_fits) // 2),
                         "T2' 斜坡段本身是线性的（R²≥0.90，平顶不参与）",
                         "%d/%d 段" % (len(rampin), len(ramp_fits)))
    else:
        ok &= rec_ok(len(ramp_fits) >= 2 and len(rampin) >= max(2, len(ramp_fits) // 2),
                     "T2' 斜坡段本身是线性的（R²≥0.90，平顶不参与）",
                     "%d/%d 段" % (len(rampin), len(ramp_fits)))
    ok &= rec_ok(any(k > 0 for k in ks) and any(k < 0 for k in ks),
                 "T2b 斜率有正有负（真三角波）",
                 "k>0:%d k<0:%d" % (sum(1 for k in ks if k > 0), sum(1 for k in ks if k < 0)))
    # ★★★ T3 用**稳健峰值**：`max()` 会被单点毛刺主导。
    #   血证：同一天里 3.26×A 的尖峰在 1/3 的窗口出现（且 ≈1e6/256 ⇒ 像某一拍 ARR 被写成 256），
    #   而 `max(vh)` 只要撞上一次就把 T3 判死 ⇒ **判据本身不稳健**，会让"真有缺陷"与"单点毛刺"分不开。
    #   ⇒ 取**前 1% 的中位数**当峰值；同时**打印 max**（max ≫ 稳健峰 ⇒ 显式标注"存在毛刺"）。
    vs_sorted = sorted(vh)
    if vs_sorted:
        top = vs_sorted[-max(1, len(vs_sorted) // 100):]
        pk = top[len(top) // 2]
        pk_max = vs_sorted[-1]
    else:
        pk = pk_max = 0.0
    glitch = pk_max > 1.25 * pk
    ok &= rec_ok(0.85 <= pk / A <= 1.20, "T3 实测峰值-稳健（前1%%中位数）≈ A",
                 "%.2f×A（max %.2f×A%s）"
                 % (pk / A, pk_max / A, " ← **有单点毛刺**" if glitch else ""))
    if glitch:
        print("     ⚠ max/稳健 = %.2f ⇒ 存在单点毛刺（不是持续现象）⇒ 下面 T5 用累积量、不受影响"
              % (pk_max / pk))
    tot = 0
    for i in range(1, len(seg)):
        dv = (int(round(raw[i])) - int(round(raw[i - 1]))) & 0xFFF
        if dv > 2048:
            dv -= 4096
        tot += dv
    mean_hz = abs(tot) / span / K
    ok &= rec_ok(0.75 <= mean_hz / (A / 2) <= 1.25, "T5 累积平均 ≈ A/2（三角波均值）",
                 "%.0f Hz vs %.0f" % (mean_hz, A / 2))
    # ★ R 反向：**带斜坡的曲线不能要求"命令=0 ⇒ 立刻 0"** —— 命令落到 0 之后轴还在**减速**，
    #   减完要 `A/slope` 秒（本档 1200/12000 = 0.1 s = 整个半周期）⇒ 旧判据把"正常的减速"
    #   判成了"命令=0 还在转"。正解：只看**每个 0 相的末段**（已经减完）速度是否 ≈0。
    # ★ R 反向判据**要按"带底三角波"更新**：下限是 0.15A（=180 Hz）⇒ 0 相"末段"本就该≈**底线**，
    #   而不是≈0。第一版仍按"降到 0"判 ⇒ 实测 242 Hz（≈底线+噪声）被误判成 FAIL。
    zt = []
    for x, y in zip(edges, edges[1:]):
        if w12[x] > A / 2:
            continue
        tail = range(max(x + 2, y - max(2, (y - x) // 5)), min(y, len(vh)))
        zt += [vh[i] for i in tail]
    zmax = max(zt) if zt else 0.0
    print("  R 每个 0 相**末段**的最大速度 = %.0f Hz（底线理论 0.15A=%.0f Hz，%d 点）"
          % (zmax, 0.15 * A, len(zt)))
    ok &= rec_ok(0.05 * A <= zmax <= 0.30 * A,
                 "R 0 相末段速度 ≈ 下限 0.15A（不是 0 —— 下限是设计值）",
                 "%.0f Hz" % zmax)
    print()
    ok &= _judge_position(seg, A, dwell, getattr(_judge, "_cands", [4000, 12000, 30000]))
    print("=== %s ===" % ("全部通过" if ok else "有 FAIL —— 见上"))
    return 0 if ok else 1


def cmd_run(a):
    """只走串口跑运动；**顺带用 pyocd 做两件卡路径必需的事**：
      ① 触发「重新开日志」（`SD_CFG[8]=1` + 魔数 `SD_CFG[15]=0xF00DBEEF`）——
         日志是**开机时**开的，卡插晚了/换卡就必须外部触发；**这正是"卡上找不到我的运动"的最可能原因**。
      ② **当场证明日志在记**：读 `s_log_blk`（本次上电写了多少块）在运动前后的差 ——
         差 >0 ⇒ 这段运动真的进了卡；差 =0 ⇒ 别去分析卡，先解决"没在记"。
    ★ 只读那一段 .bss **不靠猜 map 的换行**，而是**用已知值锚定**（`s_log_cap_data` = 数据区块数、
      `s_log_blk`+1 = 头部写指针）。"""
    from h723_client import Dcl, find_board, engine_status
    import h723_client as HC

    SD_CFG = 0x24000400
    CFG_MAGIC_OFF, CFG_REOPEN_OFF = SD_CFG + 15 * 4, SD_CFG + 8 * 4

    def _syms():
        """★ 变量地址**用 `nm` 解析**，不从 map 推、更不猜。
        血证（本工具第一版）：我按 map 的换行约定推，差 4 字节；又用「值 == 数据区块数」去锚定，
        而那个假设本身就是错的（`s_log_cap_data` ≠ 头部块数）⇒ 锚定失败。
        ★ 工具链路径**从 `cmake/arm-none-eabi.cmake` 的 TOOLCHAIN_BIN 读**（单一真值源）。"""
        import re as _re
        tc = _re.search(r'set\(TOOLCHAIN_BIN\s+"([^"]+)"',
                        open(os.path.join(ROOT, "cmake", "arm-none-eabi.cmake"), encoding="utf-8").read())
        if not tc:
            return {}
        nm = os.path.join(tc.group(1).replace("/", os.sep), "arm-none-eabi-nm.exe")
        if not os.path.exists(nm):
            return {}
        r = subprocess.run([nm, "-n", os.path.join(ROOT, "build", "dcl_h723")],
                           capture_output=True, text=True)
        out = {}
        for ln in r.stdout.splitlines():
            p = ln.split()
            if len(p) == 3:
                try:
                    out[p[2]] = int(p[0], 16)
                except ValueError:
                    pass
        return out

    SYM = _syms()

    def _u32(tgt, addr):
        return struct.unpack("<I", bytes(tgt.read_memory_block8(addr, 4)))[0]

    def log_blk(tgt):
        a = SYM.get("s_log_blk")
        return _u32(tgt, a) if a else None

    from pyocd.core.helpers import ConnectHelper
    sess = ConnectHelper.session_with_chosen_probe(
        target_override="stm32h723xx",
        options={"connect_mode": "halt", "resume_on_disconnect": True})
    sess.open()
    try:
        tgt = sess.target
        tgt.resume()
        time.sleep(0.3)
        print("  符号: s_log_blk=%s s_log_cap_data=%s（nm 解析）"
              % (hex(SYM["s_log_blk"]) if "s_log_blk" in SYM else "?",
                 hex(SYM["s_log_cap_data"]) if "s_log_cap_data" in SYM else "?"))
        blk0 = log_blk(tgt)
        print("  ① 触发重新开日志: SD_CFG[15]=0xF00DBEEF, SD_CFG[8]=1")
        tgt.write_memory(CFG_MAGIC_OFF, 0xF00DBEEF, 32)     # ★ pyocd 签名: (addr, data, transfer_size)
        tgt.write_memory(CFG_REOPEN_OFF, 1, 32)
        time.sleep(3.5)                       # 重开可能几秒（固件自己也声明了窗口）
        blkR = log_blk(tgt)
        print("     重开前后 s_log_blk: %s → %s" % (blk0, blkR))

        dwell = a.dwell
        # ★ `--slope` 覆盖：本实验要判"40 ms 死区 / 3.25×A 过冲"**跟不跟着斜率变**
        #   ⇒ 固定段长、只动斜率（不用重编 .dcl），看峰值与形状怎么变。
        slope = int(a.slope) if a.slope else int(a.A / dwell)
        peak_expected = min(a.A, slope * dwell)
        print("  斜率 = %d Hz/s（段长 %.0f ms ⇒ 相位末理论峰值 %.0f Hz%s）"
              % (slope, dwell * 1000, peak_expected,
                 "" if peak_expected >= a.A else " ← **达不到 A**"))
        for sc, extra in (("dclc.py", [os.path.join(ROOT, "examples", "h723_step_traj_tri.dcl")]),
                          ("h723_as5600_bind.py", [])):
            r = subprocess.run([sys.executable, os.path.join(HERE, sc)] + extra,
                               capture_output=True, text=True, cwd=ROOT)
            print("  %s rc=%d" % (sc, r.returncode))
            if r.returncode != 0:
                raise SystemExit((r.stdout or "") + (r.stderr or ""))
        d = Dcl(a.port or find_board())
        shm = engine_status(d)["shm"]

        def wset(n, v):
            return d.send(0x21, struct.pack("<II", shm + HC.OFF_WIRE_MAP + n * 4,
                                            struct.unpack("<I", struct.pack("<f", v))[0]))[0] == "ACK"
        d.send(0x39, bytes([19, 22]) + struct.pack("<I", 1))
        d.send(0x39, bytes([19, 20]) + struct.pack("<I", 10))
        d.send(0x39, bytes([19, 13]) + struct.pack("<I", 1))
        d.send(0x39, bytes([19, 17]) + struct.pack("<I", slope))
        time.sleep(0.25)
        # ★★★ **设了就算必错**（本项目铁律）：写完必须**读回**。
        #   血证（就在今天）：工具只打印"斜率 = 12000"（那是**我发的**、不是**它读回的**），
        #   于是"实际 slew 比设定快 70%"这个结论里混着"斜率根本没生效"的可能 ⇒ 判据不可归因。
        #   `sub=19` 是只读斜坡态：+0 ramp_hz_s / +4 rate_cmd / +8 rate_out / +12 rate_actual。
        st19, p19 = d.send(0x39, bytes([19, 19]) + struct.pack("<I", 0))
        if st19 == "ACK" and len(p19) >= 16:
            rb = struct.unpack("<4I", p19[:16])
            print("  读回 sub=19: ramp_hz_s=%d rate_cmd=%d rate_out=%d rate_actual=%d"
                  % rb)
            if rb[0] != slope:
                print("  ⛔ **回读 != 设定**（设 %d、读回 %d）⇒ 斜坡根本没按声明生效；"
                      % (slope, rb[0]))
                print("     这一轮的「实际 slew」结论**不可归因** ⇒ 先解决写入路径，别急着判硬件。")

        else:
            print("  ⚠ sub=19 读回失败（sts=%s len=%d）⇒ 本轮的斜率生效性**未证实**" % (st19, len(p19)))
        blk1 = log_blk(tgt)
        wset(10, 1.0); wset(11, a.A)
        print("  ② 运动 %.0f s（A=%.0f Hz, 段长 %.0f ms, 周期 %.2f s）..."
              % (a.secs, a.A, dwell * 1000, 2 * dwell))
        time.sleep(a.secs)
        wset(11, 0.0); wset(10, 0.0)
        blk2 = log_blk(tgt)
        d.send(0x39, bytes([19, 17]) + struct.pack("<I", 0))
        d.send(0x39, bytes([19, 13]) + struct.pack("<I", 0))
        d.send(0x39, bytes([19, 3]) + struct.pack("<I", 0))
        print("  已收尾（请求 0 / 关斜坡 / 运动源回脚手架 / 失能）")
        d.close()

        # ③ 判定"运动到底有没有被记进卡"
        if blk1 is not None and blk2 is not None:
            delta = blk2 - blk1
            print("\n  ③ **日志在不在记**：运动前 s_log_blk=%d → 运动后 %d（Δ=%d 块 = %d 条记录）"
                  % (blk1, blk2, delta, delta * 2))
            if delta > 0:
                print("     ✅ 这段运动**确实写进了卡**（Δ>0）⇒ 拔卡插电脑跑 `analyze` 一定能找到")
            else:
                print("     ⛔ Δ=0 ⇒ **这次运动没进卡**：先解决「日志没在记」（卡接触 / SD_CFG / SD 初始化），")
                print("        不然分析卡是白费 —— 这也**排除了「窗口在别处」的猜测**。")
        else:
            print("\n  ⚠ 没能锚定 s_log_blk ⇒ 无法判定「是否进卡」；仍可拔卡后跑 analyze 试。")
        print("  ⇒ 现在**断电/拔卡**，把卡插到电脑，跑 `analyze`")
        return 0
    finally:
        sess.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "analyze", "analyze-all"])
    ap.add_argument("--disk", type=int, default=None)
    ap.add_argument("--blocks", type=int, default=131072, help="analyze: 往回最多搜多少块")
    ap.add_argument("--A", type=float, default=1200.0)
    ap.add_argument("--secs", type=float, default=6.0)
    ap.add_argument("--slope", type=int, default=0, help="覆盖斜坡斜率 Hz/s（0=用 A/段长）")
    ap.add_argument("--cands", default="4000,12000,30000", help="T2'' 候选斜率（反认用）")
    ap.add_argument("--dwell", type=float, default=None, help="秒；缺省从 .dcl 读")
    ap.add_argument("--port", default=None)
    a = ap.parse_args()
    if a.dwell is None:
        import re
        txt = open(os.path.join(ROOT, "examples", "h723_step_traj_tri.dcl"), encoding="utf-8").read()
        a.dwell = float(re.search(r"DWELL\s+([\d.]+)ms", txt).group(1)) / 1000.0
    # ★ 给了 --slope 就用它当**唯一候选**（斜率是已知参数, 不做"反认"）⇒ 映射无歧义
    _judge._cands = [a.slope] if a.slope else [int(x) for x in a.cands.split(",")]
    print("=== h723_traj_card: %s (A=%.0f, 段长 %.0f ms) ===" % (a.cmd, a.A, a.dwell * 1000))
    if a.cmd == "run":
        return cmd_run(a)
    if a.cmd == "analyze-all":
        return cmd_analyze_all(a)
    return cmd_analyze(a)


if __name__ == "__main__":
    sys.exit(main())
