#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sd_probe_distance.py —— **从卡上按"距离写指针多远"取样**（只读，几 KB 级 I/O）

## 为什么需要它
卡上的日志是 **31 GB 的大环**（`下一块 = LBA 28597889`、`已回卷 = 是`），
历史有几千万条 —— **不可能顺序翻**。要"找某一段事件（比如某次运动）在哪"，
正确做法是从写指针往回**按对数距离取样**：每个点只读 1 个扇区（2 条记录），
看一眼 `tick / WIRE12 / ACT9` 就知道那个年代是空闲还是在跑。

★ **只读**：只用 `CreateFileW(GENERIC_READ)` + `SetFilePointerEx` + `ReadFile`，没有任何写路径。
★ 偏移与长度必须是 **512 的整数倍**。
★ 几何（数据区范围 / 写指针）**从 LBA0 的头部读**，不硬编码。

用法:
  <系统python> tools/sd_probe_distance.py               # 默认 8 个对数距离
  <系统python> tools/sd_probe_distance.py --disk 2 --dist 65536,262144
"""
import argparse, ctypes, ctypes.wintypes as wt, struct, sys

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateFileW.restype = wt.HANDLE
k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
k.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k.SetFilePointerEx.argtypes = [wt.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wt.DWORD]
k.SetFilePointerEx.restype = wt.BOOL
BLK, MAGIC = 512, 0x4B424C44
HDR_MAGIC = 0x474F4C44          # "DLOG"


def open_disk(n):
    h = k.CreateFileW(r"\\.\PhysicalDrive%d" % n, 0x80000000, 3, None, 3, 0, None)
    if h in (None, -1, 0):
        raise SystemExit("打不开 PhysicalDrive%d (err=%d)" % (n, ctypes.get_last_error()))
    return h


def rd(h, lba, nblk):
    buf = ctypes.create_string_buffer(nblk * BLK); got = wt.DWORD(0)
    pos = ctypes.c_longlong(lba * BLK)
    if not k.SetFilePointerEx(h, pos, ctypes.byref(pos), 0):
        raise SystemExit("seek 失败 (err=%d)" % ctypes.get_last_error())
    if not k.ReadFile(h, buf, nblk * BLK, ctypes.byref(got), None):
        raise SystemExit("read 失败 (err=%d)" % ctypes.get_last_error())
    return buf.raw[:got.value]


def header(h):
    """★ 头部字段偏移**不能猜** —— 这里是按**已测工具** `tools/sd_log_read.py` 的用法抄的
    （它又是按固件 `src/sd.c` 的写头函数抄的）：
        h[0]=magic "DLOG"  h[1]=版本  h[2]=记录尺寸  h[3]=每块条数  h[4]=块尺寸
        h[5]=数据区起始 LBA  h[6]=数据区块数  **h[7]=下一块(写指针 LBA)**  h[8]=已落盘记录
    ★★ 血证（本工具第一版）：我**猜**了偏移（用 w[3] 当写指针）⇒ 读出"写指针 LBA=2"，
      于是所有探针都落在**数据区开头**、取样全无意义，而**看起来一切正常**。
      ⇒ 所以下面加**自检**：几何必须自洽，否则直接拒绝（而不是拿错偏移继续算）。"""
    hd = rd(h, 0, 1)
    w = struct.unpack("<128I", hd)
    if w[0] != HDR_MAGIC:
        raise SystemExit("LBA0 不是 DLOG 头 ⇒ 卡不对")
    # 自检：几何必须与固件写的常量一致（记录 256B / 每块 2 条 / 块 512B / 数据区从 LBA1 起）
    if (w[2], w[3], w[4], w[5]) != (256, 2, 512, 1):
        raise SystemExit("头部几何自检失败: 记录=%d 每块=%d 块=%d 起始LBA=%d ⇒ 偏移猜错了"
                         % (w[2], w[3], w[4], w[5]))
    if not (w[5] <= w[7] <= w[5] + w[6]):
        raise SystemExit("写指针 LBA=%d 不在数据区内 ⇒ 偏移猜错了" % w[7])
    return dict(ver=w[1], start=w[5], nblk=w[6], wp=w[7], recs=w[8])


def slots(b):
    out = []
    for i in range(len(b) // 256):
        o = i * 256
        mg, tick, seq, ctrl = struct.unpack("<4I", b[o:o + 16])
        if mg != MAGIC:
            continue
        d = struct.unpack("<60f", b[o + 16:o + 16 + 240])
        out.append((tick, seq, d[28], d[0], d[41]))     # tick,seq,WIRE12,SENSOR0,ACT9
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disk", type=int, default=2)
    ap.add_argument("--dist", default="65536,262144,1048576,4194304,16777216")
    a = ap.parse_args()
    h = open_disk(a.disk)
    hd = header(h)
    print("  头部: 版本=%d 数据区 LBA %d..%d(共 %d 块) **写指针 LBA=%d** 已落盘=%d 条"
          % (hd["ver"], hd["start"], hd["start"] + hd["nblk"] - 1, hd["nblk"],
             hd["wp"], hd["recs"]))
    print("  距离(块)    LBA      tick      WIRE12  SENSOR0  ACT9")
    for d in [int(x) for x in a.dist.split(",")]:
        lba = hd["wp"] - d
        while lba < hd["start"]:
            lba += hd["nblk"]                      # 环形回卷
        for r in slots(rd(h, lba, 1))[:1]:
            print("  %8d %8d  %8d  %6.0f  %7.0f   %d" % (d, lba, r[0], r[2], r[3], r[4]))


if __name__ == "__main__":
    sys.exit(main())
