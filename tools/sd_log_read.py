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
    [判据A] 相邻两条记录"48 通道值(按位比)"全同的对数必须 == 0
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
REC = 256
BLK = 512

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


def _bits48(vals):
    """把 48 个通道值打成位串 —— 与固件里 uint32 逐位比较**等价**。

    为什么不用字符串/浮点比: 固件比的是 RAW 位 (snap[4+i] != s_prev[i])。
    -0.0 与 +0.0 数值相等但位不同; NaN 更不用说。走位串才和固件同一把尺子。"""
    return struct.pack("<48f", *vals)


def load_csv_rows(path):
    """从 sd_log_read.py --csv 导出的文件里恢复 (tick, seq, vals48)。"""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.reader(f)
        next(rd, None)                              # 表头
        for r in rd:
            if len(r) < 53:
                continue
            rows.append((int(r[0]), int(r[1]),
                         tuple(float(x) for x in r[5:53])))
    rows.sort(key=lambda x: x[1])
    return rows


def verify_rows(rows, label):
    """跑"变化才记"判据。返回 True=全部通过。"""
    print("\n=== 判据 ===")
    if len(rows) < 3:
        print("  [!] 记录太少 (%d), 无法判" % len(rows))
        return False
    bad = []

    # [判据 0] tick 严格递增 (顺序/时间轴自洽)
    noninc = sum(1 for a, b in zip(rows, rows[1:]) if b[0] <= a[0])
    print("  [0] tick 严格递增          : 违反 %d 处 %s" % (noninc, "OK" if noninc == 0 else "FAIL"))
    if noninc:
        bad.append("tick 不严格递增")

    # [判据 1] seq 连续 (无丢包; 回卷边界会表现为 1 处跳变)
    gaps = sum(1 for a, b in zip(rows, rows[1:]) if b[1] != a[1] + 1)
    print("  [1] seq 连续 (丢包/回卷)   : 跳变 %d 处 %s" % (gaps, "OK" if gaps <= 1 else "FAIL"))
    if gaps > 1:
        bad.append("seq 跳变 %d 处 (丢包或回卷)" % gaps)

    # ★ [判据 2] 变化才记生效性 —— 这是唯一"能失败"的判据
    same = sum(1 for a, b in zip(rows, rows[1:]) if _bits48(a[2]) == _bits48(b[2]))
    ratio = 100.0 * same / (len(rows) - 1)
    print("  [2] 相邻记录 48 通道全同   : %d/%d = %.2f%% %s"
          % (same, len(rows) - 1, ratio, "OK" if same == 0 else "FAIL"))
    if same:
        bad.append("有 %d 对相邻记录内容完全相同 ⇒ 变化才记没生效" % same)

    cov = (rows[-1][0] - rows[0][0]) / max(1, len(rows) - 1)
    print("  [i] 平均每条覆盖 %.2f 拍 (压缩比 %.1fx)" % (cov, cov))
    print("  [i] tick %d..%d  记录 %d 条  源: %s" % (rows[0][0], rows[-1][0], len(rows), label))

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

    h = struct.unpack_from("<16I", d.read(0, 512), 0)
    if sum(h[0:15]) != h[15]:
        print("[!] 头部校验和不符 (可能正被写入) —— 仍继续读")
    print("\n=== 日志头 ===")
    print("  版本=%d  记录=%dB  每块%d条  块=%dB" % (h[1], h[2], h[3], h[4]))
    print("  数据区 LBA %d .. %d  (共 %d 块 = %.2f GB)" % (h[5], h[5] + h[6] - 1, h[6], h[6] * 512 / 1e9))
    print("  已落盘记录 = %d   下一块 = LBA %d" % (h[8], h[7]))
    print("  最后一条: tick=%d seq=%d" % (h[9], h[10]))
    print("  丢包 = %d 条   已回卷 = %s   批次数 = %d" % (h[11], "是" if h[12] else "否", h[13]))

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

    recs.sort(key=lambda w: w[2])                # 按 seq 排序 = 时间顺序
    if not recs:
        print("[!] 这些块里没有有效记录。")
        return 1
    print("  有效记录 %d 条   seq %d..%d   tick %d..%d" %
          (len(recs), recs[0][2], recs[-1][2], recs[0][1], recs[-1][1]))
    rows = [(w[1], w[2], struct.unpack("<48f", struct.pack("<48I", *w[4:52])))
            for w in recs]
    ok = verify_rows(rows, "卡上最近 %d 块" % nblk) if do_verify else True

    print("\n=== 最后 3 条 (带 tick 标注) ===")
    for w in recs[-3:]:
        ctl = w[3]
        print("  tick=%-9d seq=%-9d run=%d routes=%d seq_step=%d" %
              (w[1], w[2], (ctl >> 24) & 0xFF, (ctl >> 16) & 0xFF, ctl & 0xFFFF))
        print("     SENSOR  [0:4]=%s" % ["%.5g" % struct.unpack_from("<f", struct.pack("<I", x))[0] for x in w[4:8]])
        print("     WIRE    [0:4]=%s" % ["%.5g" % struct.unpack_from("<f", struct.pack("<I", x))[0] for x in w[20:24]])
        print("     ACTUATOR[0:4]=%s" % ["%.5g" % struct.unpack_from("<f", struct.pack("<I", x))[0] for x in w[36:40]])

    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            cw = csv.writer(f)
            cw.writerow(["tick", "seq", "run", "routes", "seq_step"]
                        + ["SENSOR%d" % i for i in range(16)]
                        + ["WIRE%d" % i for i in range(16)]
                        + ["ACT%d" % i for i in range(16)])
            f32 = lambda x: struct.unpack_from("<f", struct.pack("<I", x))[0]
            for w in recs:
                ctl = w[3]
                cw.writerow([w[1], w[2], (ctl >> 24) & 0xFF, (ctl >> 16) & 0xFF, ctl & 0xFFFF]
                            + [f32(x) for x in w[4:20]]
                            + [f32(x) for x in w[20:36]]
                            + [f32(x) for x in w[36:52]])
        print("\n[+] CSV 已导出: %s (%d 行, 每行带 tick)" % (csv_path, len(recs)))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
