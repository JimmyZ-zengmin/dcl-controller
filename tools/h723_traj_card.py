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


def cmd_analyze(a):
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


def _judge(recs, A, dwell):
    """按卡片数据判形状。★ 速度口径：**滑窗累积**（窗宽 ≥ 编码器回填间隔）——
    逐记录差分在本装置上不可用（环记录率 ≠ 被测更新率，见记忆 §5.28）。"""
    on = [i for i, r in enumerate(recs) if r[3] > A / 2]
    if len(on) < 50:
        print("  ⛔ 窗口内 A 相只有 %d 条 ⇒ **判无效**（采样不足）" % len(on))
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
    edges = [i for i in range(1, len(w12)) if w12[i - 1] > A / 2 and w12[i] <= A / 2]
    fits, ks = [], []
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
    lin = [f for f in fits if f[1] >= 0.90]
    print("  T2 逐半周期拟合 (k, R², n): %s" % ", ".join("(%.0f,%.2f,%d)" % f for f in fits[:6]))
    ok &= rec_ok(len(fits) >= 2 and len(lin) >= max(2, len(fits) // 2),
                 "T2 实测速度在过半半周期内线性(R²≥0.90)", "%d/%d" % (len(lin), len(fits)))
    ok &= rec_ok(any(k > 0 for k in ks) and any(k < 0 for k in ks),
                 "T2b 斜率有正有负（真三角波）",
                 "k>0:%d k<0:%d" % (sum(1 for k in ks if k > 0), sum(1 for k in ks if k < 0)))
    vs = sorted(vh); pk = vs[-1] if vs else 0.0
    ok &= rec_ok(0.85 <= pk / A <= 1.20, "T3 实测峰值 ≈ A（±15%/20%）", "%.2f×A" % (pk / A))
    tot = 0
    for i in range(1, len(seg)):
        dv = (int(round(raw[i])) - int(round(raw[i - 1]))) & 0xFFF
        if dv > 2048:
            dv -= 4096
        tot += dv
    mean_hz = abs(tot) / span / K
    ok &= rec_ok(0.75 <= mean_hz / (A / 2) <= 1.25, "T5 累积平均 ≈ A/2（三角波均值）",
                 "%.0f Hz vs %.0f" % (mean_hz, A / 2))
    zs = [vh[i] for i in range(len(vh)) if tw[i] <= 0.01]
    zmax = max(zs) if zs else 0.0
    ok &= rec_ok(zmax < 0.15 * A, "R 命令=0 时不应有速度", "max %.0f Hz" % zmax)
    print("=== %s ===" % ("全部通过" if ok else "有 FAIL —— 见上"))
    return 0 if ok else 1


def cmd_run(a):
    """只走串口：部署 → 重绑反馈 → 设参数 → 起运动 secs 秒 → 收尾。**不碰 pyocd**。
    ⇒ 卡插在板子里时执行；这段运动会被卡日志记下来，之后 `analyze` 就能找到。"""
    from h723_client import Dcl, find_board, engine_status
    import h723_client as HC
    dwell = a.dwell
    slope = int(a.A / dwell)
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
    time.sleep(0.2)
    wset(10, 1.0); wset(11, a.A)
    print("  运动 %.0f s（A=%.0f Hz, 段长 %.0f ms, 周期 %.2f s）..."
          % (a.secs, a.A, dwell * 1000, 2 * dwell))
    time.sleep(a.secs)
    wset(11, 0.0); wset(10, 0.0)
    d.send(0x39, bytes([19, 17]) + struct.pack("<I", 0))
    d.send(0x39, bytes([19, 13]) + struct.pack("<I", 0))
    d.send(0x39, bytes([19, 3]) + struct.pack("<I", 0))
    print("  已收尾（请求 0 / 关斜坡 / 运动源回脚手架 / 失能）")
    d.close()
    print("  ⇒ 现在**断电/拔卡**，把卡插到电脑，跑 `analyze`")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "analyze"])
    ap.add_argument("--disk", type=int, default=None)
    ap.add_argument("--blocks", type=int, default=131072, help="analyze: 往回最多搜多少块")
    ap.add_argument("--A", type=float, default=1200.0)
    ap.add_argument("--secs", type=float, default=6.0)
    ap.add_argument("--dwell", type=float, default=None, help="秒；缺省从 .dcl 读")
    ap.add_argument("--port", default=None)
    a = ap.parse_args()
    if a.dwell is None:
        import re
        txt = open(os.path.join(ROOT, "examples", "h723_step_traj_tri.dcl"), encoding="utf-8").read()
        a.dwell = float(re.search(r"DWELL\s+([\d.]+)ms", txt).group(1)) / 1000.0
    print("=== h723_traj_card: %s (A=%.0f, 段长 %.0f ms) ===" % (a.cmd, a.A, a.dwell * 1000))
    return cmd_run(a) if a.cmd == "run" else cmd_analyze(a)


if __name__ == "__main__":
    sys.exit(main())
