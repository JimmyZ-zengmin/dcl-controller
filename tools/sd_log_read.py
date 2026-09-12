#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sd_log_read.py — 读 H723 的"每拍落盘"日志 (专用裸 SD 卡), 输出带 tick 标注的表。

卡上布局 (见 src/sd.c 的日志段):
    LBA0            : 头部块 (512B)  magic "DLOG" + 几何 + 写指针 + 计数
    LBA1 .. 容量-1  : 数据区 (环形回卷), 每 512B 块 = 2 条 256B 记录
每 256B 记录 (见 src/blackbox.h):
    [0] magic "DLBK"   [1] tick   [2] seq   [3] ctrl
    [4..19] SENSOR[0..15]  [20..35] WIRE[0..15]  [36..51] ACTUATOR[0..15]

★★ 记法是"**变化才记**"(2026-09-12): 每拍都算, 但**只有内容变了才写一条**。
   ⇒ 每条记录仍是一个**完整全量状态**且自带 tick;
     两条记录之间的所有拍, 其值**必然与前者相同**。
   ⇒ **tick 会出现跳变, 这是设计如此, 不是丢数据**:
     tick=1000 的下一条可能是 tick=1007 —— 说明 1000~1006 拍值未变。
   ⇒ 这样数据量降到约 1/13, 落盘缓冲从 96ms 提升到秒级, 丢包归零。

为什么必须按**物理盘**直读: 这是专用裸介质, 没有文件系统。
本脚本用 ctypes 设备句柄, **不需要管理员权限**。

用法:
    python sd_log_read.py                  # 自动找卡, 读最后 512 块 (≈1024 条 = 0.1s)
    python sd_log_read.py --disk 2         # 指定物理盘
    python sd_log_read.py --blocks 4096    # 读最后 4096 块 (≈8192 条 = 0.8s)
    python sd_log_read.py --csv out.csv    # 导出 CSV (带 tick 列)
    python sd_log_read.py --csvin old.csv  # 不碰卡, 直接核一个已导出的 CSV (跑判据/做对照)
    python sd_log_read.py --verify         # 跑"变化才记"判据, 失败退出码 = 1

★★ 判据设计 (为什么不能只看 tick 连号率):
  "tick 连号率低" 在变化才记下**是设计如此**, 所以它**不可能失败** ——
  一个不可能失败的判据等于没有判据。真正能失败的是下面这条:
    [判据A] 相邻两条记录"60 个数据槽(按位比)"全同的对数必须 == 0
            若 > 0 ⇒ 固件里"没变就不写"没生效 (或这是逐拍全量的旧数据)
  对照 (证明判据A 真的能失败): 逐拍全量数据上这个数应 ≈ 92%
     python sd_log_read.py --csvin <旧数据.csv> --verify   ==> 期望 FAIL (数千对)
"""
import csv
import ctypes
import ctypes.wintypes as wt
import struct
import sys

REC_MAGIC = 0x4B424C44   # "DLBK"
HDR_MAGIC = 0x474F4C44   # "DLOG"
MAP_MAGIC = 0x50414D42   # "BMAP"
REC = 256
BLK = 512
MAP_N = 60
MAP_OFF = 16
MAP_SUM_OFF = MAP_OFF + MAP_N + 1        # 77
SEG_NAME = {0: "SENSOR", 1: "WIRE", 2: "ACT", 3: ""}
DEF_LABELS = (["SENSOR%d" % i for i in range(16)]
              + ["WIRE%d" % i for i in range(16)]
              + ["ACT%d" % i for i in range(16)]
              + ["SPARE%d" % i for i in range(12)])   # 无映射表时的旧布局标签


def map_sum(mapv):
    """与固件 bb_map_sum() 同算法 (FNV-1a 32)。"""
    h = 2166136261
    for v in mapv:
        h = ((h ^ (v & 0xFFFFFFFF)) * 16777619) & 0xFFFFFFFF
    return h


def make_labels(h):
    """从日志头取出通道映射 → 60 个列名。返回 (labels, map_or_None)。

    ★★ 必须与固件 bb_map_bind() 做**同一套越界判定**: 固件把越界的 (seg, idx)
       当成空槽(恒 0)。若 PC 端照抄标签, 就会出现"PC 说这列是 WIRE200, 实际恒 0"
       —— 宣称与实现不一致。越界一律标 BAD* 并报警。"""
    mapv = list(h[MAP_OFF:MAP_OFF + MAP_N])
    if h[MAP_OFF + MAP_N] != MAP_MAGIC:
        return list(DEF_LABELS), None
    if map_sum(mapv) != h[MAP_SUM_OFF]:
        print("  [!] 通道映射表校验和不符 ⇒ 退回默认 16/16/16 标签")
        return list(DEF_LABELS), None
    lim = {0: 64, 1: 128, 2: 64}                # 与固件 bb_map_bind 的上限一致
    out, nbad = [], 0
    for i, e in enumerate(mapv):
        seg, idx = e >> 16, e & 0xFFFF
        if seg in lim and idx < lim[seg]:
            out.append("%s%d" % (SEG_NAME[seg], idx))
        elif seg == 3:
            out.append("SPARE%d" % (i - 48 if i >= 48 else i))
        else:
            out.append("BAD%d_seg%d_idx%d" % (i, seg, idx))
            nbad += 1
    if nbad:
        print("  [!] 映射表里有 %d 个越界槽 (固件按空槽处理, 恒 0) —— 见 BAD* 列" % nbad)
    return out, mapv


def describe_map(labels, mapv):
    used = [l for l in labels if not l.startswith("SPARE")]
    print("  通道映射 = %s (共 %d 槽, 其中 %d 路有效)"
          % ("随头落卡的映射表" if mapv else "默认 16/16/16 (旧头, 无映射表)",
             len(labels), len(used)))
    from collections import Counter
    c = Counter(l.rstrip("0123456789") for l in used)
    print("    组成: %s" % ", ".join("%s x%d" % (k, v) for k, v in sorted(c.items())))
    print("    槽序: %s" % " ".join(labels))

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateFileW.restype = wt.HANDLE
k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
k.SetFilePointerEx.argtypes = [wt.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wt.DWORD]
k.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]


class Disk:
    def __init__(self, n):
        self.path = r"\\.\PhysicalDrive%d" % n
        self.h = k.CreateFileW(self.path, 0x80000000, 3, None, 3, 0, None)
        if not self.h or self.h == ctypes.c_void_p(-1).value:
            raise OSError("open %s failed WinError=%d" % (self.path, ctypes.get_last_error()))

    def read(self, lba, nbytes):
        off = lba * BLK
        p = ctypes.c_longlong(off)
        if not k.SetFilePointerEx(self.h, off, ctypes.byref(p), 0):
            raise OSError("seek %d failed" % off)
        buf = ctypes.create_string_buffer(nbytes)
        got = wt.DWORD(0)
        if not k.ReadFile(self.h, buf, nbytes, ctypes.byref(got), None):
            raise OSError("read @%d failed WinError=%d" % (off, ctypes.get_last_error()))
        return buf.raw[:got.value]

    def close(self):
        k.CloseHandle(self.h)


def looks_like_card(d):
    """LBA0 是我们写的头部 (magic) 就算命中。"""
    try:
        return struct.unpack_from("<I", d.read(0, 512), 0)[0] == HDR_MAGIC
    except OSError:
        return False


def _bits(vals):
    """把通道值打成位串 —— 与固件里 uint32 逐位比较**等价**。

    为什么不用字符串/浮点比: 固件比的是 RAW 位 (snap[4+i] != s_prev[i])。
    -0.0 与 +0.0 数值相等但位不同; NaN 更不用说。走位串才和固件同一把尺子。"""
    return struct.pack("<%df" % len(vals), *vals)


def load_csv_rows(path):
    """从 sd_log_read.py --csv 导出的文件里恢复 (tick, seq, vals)。

    ★ 不排序: CSV 已按**物理顺序**落行, 而物理顺序就是时间顺序。
      历史版本曾按 seq 排 —— 那是错的, 见 split_boots() 的说明。
    ★ 列数自适应: 旧 CSV 是 48 列 (无映射表), 新 CSV 是 60 列 (带映射表)。"""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.reader(f)
        next(rd, None)                              # 表头
        for r in rd:
            if len(r) < 53:
                continue
            rows.append((int(r[0]), int(r[1]),
                         tuple(float(x) for x in r[5:])))
    return rows


def split_boots(rows):
    """按"seq 回减"把物理顺序的记录切成若干段, 一段 = 一次上电。

    ★★ 为什么必须切: `seq` 是**每次上电从 0 重新计数**的 (bb_seq 是 RAM 静态量)。
       卡上的历史横跨多次上电 ⇒ "相邻两条 seq 差 1" 这个不变量**只在上电段内成立**。
       2026-09-12 曾因此报假 FAIL: 读窗口越过了上电边界, 多带进 512 条上一轮的记录,
       按 seq 排序后插进本轮序列 ⇒ 报"512 处跳变 + 512 处 tick 倒退"。
       **判据没错, 是我喂给它的数据混了两个上电。**"""
    segs, s = [], 0
    for i in range(1, len(rows)):
        if rows[i][1] <= rows[i - 1][1]:             # seq 不回增 ⇒ 新上电
            segs.append(rows[s:i])
            s = i
    segs.append(rows[s:])
    return segs


def verify_rows(rows, label, max_boots=8):
    """先切上电段, 再对**最后一段(=本次上电)**跑判据。返回 True=全通过。"""
    segs = split_boots(rows)
    if len(segs) > 1:
        print("  [i] 窗口跨 %d 个上电段 (段长 %s) ⇒ 只判最后一段"
              % (min(len(segs), max_boots), [len(x) for x in segs[-max_boots:]]))
    rows = segs[-1]

    print("\n=== 判据 (最后一段 = 本次上电) ===")
    if len(rows) < 3:
        print("  [!] 记录太少 (%d), 无法判" % len(rows))
        return False
    bad = []

    # [判据 0] tick 严格递增 (顺序/时间轴自洽)
    noninc = sum(1 for a, b in zip(rows, rows[1:]) if b[0] <= a[0])
    print("  [0] tick 严格递增          : 违反 %d 处 %s" % (noninc, "OK" if noninc == 0 else "FAIL"))
    if noninc:
        bad.append("tick 不严格递增")

    # [判据 1] seq 逐条 +1 (丢包会表现为前跳, 不是回减)
    gaps = sum(1 for a, b in zip(rows, rows[1:]) if b[1] != a[1] + 1)
    print("  [1] seq 逐条 +1 (丢包)     : 跳变 %d 处 %s" % (gaps, "OK" if gaps == 0 else "FAIL"))
    if gaps:
        bad.append("seq 跳变 %d 处 (丢包)" % gaps)

    # ★ [判据 2] 变化才记生效性 —— 唯一"能失败"的判据
    same = sum(1 for a, b in zip(rows, rows[1:]) if _bits(a[2]) == _bits(b[2]))
    ratio = 100.0 * same / (len(rows) - 1)
    print("  [2] 相邻记录 60 槽全同     : %d/%d = %.2f%% %s"
          % (same, len(rows) - 1, ratio, "OK" if same == 0 else "FAIL"))
    if same:
        bad.append("有 %d 对相邻记录内容完全相同 ⇒ 变化才记没生效" % same)

    cov = (rows[-1][0] - rows[0][0]) / max(1, len(rows) - 1)
    print("  [i] 平均每条覆盖 %.2f 拍 (压缩比 %.1fx)" % (cov, cov))
    print("  [i] tick %d..%d  记录 %d 条 (seq %d..%d)  源: %s"
          % (rows[0][0], rows[-1][0], len(rows), rows[0][1], rows[-1][1], label))

    if bad:
        print("  ==> 判定: FAIL  (%s)" % "; ".join(bad))
        return False
    print("  ==> 判定: PASS")
    return True


def main():
    argv = sys.argv[1:]
    disk_no = int(argv[argv.index("--disk") + 1]) if "--disk" in argv else None
    nblk = int(argv[argv.index("--blocks") + 1]) if "--blocks" in argv else 512
    csv_path = argv[argv.index("--csv") + 1] if "--csv" in argv else None
    csv_in = argv[argv.index("--csvin") + 1] if "--csvin" in argv else None
    do_verify = "--verify" in argv

    if csv_in:
        rows = load_csv_rows(csv_in)
        print("=== 离线核对 CSV: %s  (%d 行) ===" % (csv_in, len(rows)))
        return 0 if verify_rows(rows, csv_in) else 1

    print("=== 扫描物理盘找日志头 (magic DLOG @LBA0) ===")
    d = None
    for n in ([disk_no] if disk_no is not None else range(0, 6)):
        if n is None:
            continue
        try:
            cand = Disk(n)
        except OSError as e:
            print("  PhysicalDrive%d: %s" % (n, e))
            continue
        if looks_like_card(cand):
            d = cand
            print("  => 命中 PhysicalDrive%d" % n)
            break
        cand.close()
    if d is None:
        print("[X] 没找到日志头。卡没插? 或还没跑过带日志的固件? (可 --disk N 指定)")
        return 2

    h = struct.unpack_from("<128I", d.read(0, 512), 0)
    if sum(h[0:15]) != h[15]:
        print("[!] 头部校验和不符 (可能正被写入) —— 仍继续读")
    labels, mapv = make_labels(h)
    print("\n=== 日志头 ===")
    print("  版本=%d  记录=%dB  每块%d条  块=%dB" % (h[1], h[2], h[3], h[4]))
    print("  数据区 LBA %d .. %d  (共 %d 块 = %.2f GB)" % (h[5], h[5] + h[6] - 1, h[6], h[6] * 512 / 1e9))
    print("  已落盘记录 = %d   下一块 = LBA %d" % (h[8], h[7]))
    print("  最后一条: tick=%d seq=%d" % (h[9], h[10]))
    print("  丢包(跨上电累计) = %d 条   已回卷 = %s   批次数 = %d"
          % (h[11], "是" if h[12] else "否", h[13]))
    describe_map(labels, mapv)

    data_lba0, data_n = h[5], h[6]
    wp = h[7] - data_lba0                       # 数据区内的写指针 (0 起的块)
    start = wp - nblk
    while start < 0:
        start += data_n
    print("\n=== 读最后 %d 块 (从数据区块 %d 起, 环形推进) ===" % (nblk, start))

    recs = []
    CH = 256                                     # 每次读 256 块 (128KB)
    done = 0
    while done < nblk:
        cnt = min(CH, nblk - done)
        idx = (start + done) % data_n
        lba = data_lba0 + idx
        if idx + cnt > data_n:                   # 跨末端 ⇒ 分两段
            cnt = data_n - idx
        blob = d.read(lba, cnt * BLK)
        for b in range(cnt):
            for r in range(2):
                off = b * BLK + r * REC
                w = struct.unpack_from("<64I", blob, off)
                if w[0] != REC_MAGIC:
                    continue
                recs.append(w)
        done += cnt
    d.close()

    # ★★ 绝不排序! 读取顺序**已经**是时间顺序:
    #   start = 写指针 - nblk, 然后**正向**按模推进 ⇒ 跨回卷点也是时间顺序。
    #   历史版本在这里 recs.sort(key=seq) —— **是个陷阱**: seq 每次上电归零,
    #   卡上历史跨多次上电, 按 seq 排会把两个上电的记录揉在一起, 于是
    #   ① split_boots 只能在"重复 seq"处切段, 切出全是长度 2 的假段;
    #   ② 判据 [0][1][2] 拿到的是混段数据, 报假 FAIL。
    #   2026-09-12 实测: 保留排序 ⇒ 307239 条的一段里 seq 跨度 2,576,442 (自相矛盾);
    #   去掉排序 ⇒ 物理序 = 时间序, 段内 seq 逐条 +1 成立。
    if not recs:
        print("[!] 这些块里没有有效记录。")
        return 1
    print("  有效记录 %d 条   seq %d..%d   tick %d..%d"
          % (len(recs), recs[0][2], recs[-1][2], recs[0][1], recs[-1][1]))
    rows = [(w[1], w[2], struct.unpack("<60f", struct.pack("<60I", *w[4:64])))
            for w in recs]
    ok = verify_rows(rows, "卡上最近 %d 块" % nblk) if do_verify else True
    # 只导出**本次上电**那一段: 混段 CSV 会把两个上电的 seq 揉在一起, 无法判读
    segs = split_boots(rows)
    keep = len(recs) - len(segs[-1])
    if keep:
        print("  [i] CSV 只导出本次上电段 (%d 条, 丢弃前 %d 条跨上电记录)"
              % (len(segs[-1]), keep))
        recs = recs[keep:]

    print("\n=== 最后 3 条 (带 tick 标注) ===")
    f32 = lambda x: struct.unpack_from("<f", struct.pack("<I", x))[0]
    show = [i for i, l in enumerate(labels) if not l.startswith("SPARE")][:6]
    for w in recs[-3:]:
        ctl = w[3]
        print("  tick=%-9d seq=%-9d run=%d routes=%d seq_step=%d" %
              (w[1], w[2], (ctl >> 24) & 0xFF, (ctl >> 16) & 0xFF, ctl & 0xFFFF))
        print("     " + "  ".join("%s=%.5g" % (labels[i], f32(w[4 + i])) for i in show))

    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            cw = csv.writer(f)
            cw.writerow(["tick", "seq", "run", "routes", "seq_step"] + list(labels))
            for w in recs:
                ctl = w[3]
                cw.writerow([w[1], w[2], (ctl >> 24) & 0xFF, (ctl >> 16) & 0xFF, ctl & 0xFFFF]
                            + [f32(x) for x in w[4:64]])
        print("\n[+] CSV 已导出: %s (%d 行, 每行带 tick, 列名来自卡上映射表)" % (csv_path, len(recs)))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
