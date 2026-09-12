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


def main():
    argv = sys.argv[1:]
    disk_no = int(argv[argv.index("--disk") + 1]) if "--disk" in argv else None
    nblk = int(argv[argv.index("--blocks") + 1]) if "--blocks" in argv else 512
    csv_path = argv[argv.index("--csv") + 1] if "--csv" in argv else None

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
    gaps = sum(1 for i in range(1, len(recs)) if recs[i][2] != recs[i - 1][2] + 1)
    print("  seq 不连续处 = %d  (回卷边界或丢包会体现为跳变; 变化才记不造成跳变)" % gaps)
    inc = sum(1 for i in range(1, len(recs)) if recs[i][1] == recs[i - 1][1] + 1)
    print("  tick 连号比例 = %d/%d = %.1f%%  (变化才记 ⇒ 低是正常的; 跳变处值保持不变)"
          % (inc, len(recs) - 1, 100.0 * inc / max(1, len(recs) - 1)))
    if len(recs) > 1:
        print("  平均每条记录覆盖 %.2f 拍 (省掉的都是重复)"
              % ((recs[-1][1] - recs[0][1]) / (len(recs) - 1)))

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
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
