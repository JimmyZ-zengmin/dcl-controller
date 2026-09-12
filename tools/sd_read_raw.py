#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sd_read_raw.py — 在 PC 上按**物理盘**直读 SD 卡, 取回 H723 飞行记录仪落盘的数据。

为什么必须走物理盘: H723 (9.10 H723newest) 的 sd_dump_blackbox() 把 AXI 环形缓冲
(128KB = 256 块 × 512B) 写到卡的 **LBA 3000000 起**(约 1.46GB 偏移), 故意不建文件系统。
资源管理器看不到它 —— 而且这一写**会破坏卡上原有的文件系统**, Windows 因此会提示
"需要格式化"。**读数之前绝不要格式化**(格式化会覆盖现场)。

实现: 用 ctypes 的 CreateFileW/SetFilePointerEx/ReadFile 直读设备,
**不需要管理员权限**(Python 内置 open() 对设备句柄会报 Errno 22, 不能用)。

用法:
    python sd_read_raw.py                 # 自动找卡 (读 MBR 判断可移动), 并做全套取证
    python sd_read_raw.py --disk 2        # 指定物理盘号
    python sd_read_raw.py --no-dump       # 不落盘 raw 文件
"""
import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys

LBA0 = 3000000          # 与 src/sd.c 里 sd_dump_blackbox 的 base_lba 一致
BLK = 512
NBLK = 256
BYTES = BLK * NBLK      # 131072

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateFileW.restype = wt.HANDLE
k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
k.SetFilePointerEx.argtypes = [wt.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wt.DWORD]
k.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                              ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]

GENERIC_READ = 0x80000000
FILE_SHARE_ALL = 1 | 2
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080
IOCTL_DISK_GET_LENGTH_INFO = 0x7405C


class DevNum(ctypes.Structure):
    _fields_ = [("DeviceType", wt.DWORD), ("DeviceNumber", wt.DWORD), ("PartitionNumber", wt.DWORD)]


class RawDisk:
    def __init__(self, path):
        self.path = path
        self.h = k.CreateFileW(path, GENERIC_READ, FILE_SHARE_ALL, None, OPEN_EXISTING, 0, None)
        if not self.h or self.h == INVALID_HANDLE:
            err = ctypes.get_last_error()
            raise OSError("CreateFileW(%s) failed, WinError=%d" % (path, err))
        self.size = ctypes.c_longlong(0)
        br = wt.DWORD(0)
        if not k.DeviceIoControl(self.h, IOCTL_DISK_GET_LENGTH_INFO, None, 0,
                                 ctypes.byref(self.size), 8, ctypes.byref(br), None):
            self.size = ctypes.c_longlong(0)

    def read_at(self, byte_off, nbytes):
        pos = ctypes.c_longlong(byte_off)
        if not k.SetFilePointerEx(self.h, byte_off, ctypes.byref(pos), 0):
            raise OSError("seek to %d failed WinError=%d" % (byte_off, ctypes.get_last_error()))
        buf = ctypes.create_string_buffer(nbytes)
        got = wt.DWORD(0)
        if not k.ReadFile(self.h, buf, nbytes, ctypes.byref(got), None):
            raise OSError("ReadFile @%d failed WinError=%d" % (byte_off, ctypes.get_last_error()))
        return buf.raw[:got.value]

    def close(self):
        if self.h:
            k.CloseHandle(self.h)
            self.h = None


def mbr_entries(blob):
    """解析 MBR / 引导扇区里的 4 条分区表项 (offset 446)。"""
    out = []
    if len(blob) < 512:
        return out
    if blob[510:512] != b"\x55\xAA":
        return out
    for i in range(4):
        e = blob[446 + i * 16: 446 + (i + 1) * 16]
        ptype = e[4]
        lba = struct.unpack_from("<I", e, 8)[0]
        cnt = struct.unpack_from("<I", e, 12)[0]
        if ptype or lba or cnt:
            out.append((i, ptype, lba, cnt))
    return out


def describe_bootsec(bs):
    """识别引导扇区类型 (MBR / FAT32 / exFAT / NTFS / 空)。"""
    if len(bs) < 512:
        return "短读"
    if bs[3:11] == b"EXFAT   ":
        return "exFAT"
    if bs[3:11] == b"NTFS    ":
        return "NTFS"
    oem = bs[3:11]
    if bs[510:512] == b"\x55\xAA" and bs[0] == 0xEB:
        fstype = bs[54:62].decode("latin1").strip().strip("\x00")
        if bs[82:90].strip(b"\x00 ") == b"FAT32":
            fstype = "FAT32"
        elif bs[54:62].strip(b"\x00 ") == b"FAT16":
            fstype = "FAT16"
        return "FAT(BIOS-BPB) oem=%r type=%r" % (oem.decode("latin1"), fstype)
    if all(b == 0 for b in bs[:64]):
        return "**全 0 / 空白**"
    return "未识别 (前 16B=%s)" % bs[:16].hex()


def main():
    argv = sys.argv[1:]
    want = None
    if "--disk" in argv:
        want = int(argv[argv.index("--disk") + 1])
    do_dump = "--no-dump" not in argv

    print("=== ① 找卡 ===")
    targets = [want] if want is not None else list(range(0, 6))
    card = None
    for n in targets:
        path = r"\\.\PhysicalDrive%d" % n
        try:
            d = RawDisk(path)
        except OSError as e:
            print("  %-22s %s" % (path, e))
            continue
        bs = d.read_at(0, 512)
        ents = mbr_entries(bs)
        gb = d.size.value / 1e9
        print("  %-22s %7.2f GB  LBA0[0:4]=%s  分区表项=%s" % (path, gb, bs[:4].hex(), ents))
        if want is not None:
            # 显式指定: 直接采用 (分区表可能已被毁, 不能靠它判断)
            card = (n, d)
            break
        # 卡的特征: 有分区表 且 总容量 8..64GB
        if ents and 8e9 <= d.size.value <= 64e9:
            if card is None:
                card = (n, d)
            else:
                d.close()
        else:
            d.close()
    if card is None:
        print("\n[!] 没能唯一确定是哪块盘。请用 --disk N 指定 (看上面列表里 8~64GB 那块)。")
        return 2
    n, d = card
    print("  => 判定目标: PhysicalDrive%d (%.2f GB)" % (n, d.size.value / 1e9))

    print("\n=== ② 卡上原有文件系统 (物理盘 LBA 0) ===")
    bs0 = d.read_at(0, 512)
    for (i, ptype, lba, cnt) in mbr_entries(bs0):
        print("  分区%d: type=0x%02X 起始LBA=%-10d 扇区数=%-10d (%.2f GB)" %
              (i + 1, ptype, lba, cnt, cnt * 512 / 1e9))
    start = None
    for (i, ptype, lba, cnt) in mbr_entries(bs0):
        if ptype:
            start = lba
            break
    if start is not None:
        pbs = d.read_at(start * 512, 512)
        print("  分区引导扇区 @LBA%d: %s" % (start, describe_bootsec(pbs)))
        if pbs[82:90].strip(b"\x00 ") == b"FAT32":
            spc = pbs[13]                      # sectors per cluster
            rsvd = struct.unpack_from("<H", pbs, 14)[0]
            nfat = pbs[16]
            fatsz = struct.unpack_from("<I", pbs, 36)[0]
            tot = struct.unpack_from("<I", pbs, 32)[0]
            data_start = start + rsvd + nfat * fatsz
            print("    FAT32: 保留扇区=%d FAT数=%d 每FAT扇区=%d 簇扇区=%d 总扇区=%d"
                  % (rsvd, nfat, fatsz, spc, tot))
            print("    ⇒ 文件系统结构占 LBA %d..%d, 数据区起于 LBA %d"
                  % (start, data_start - 1, data_start))
            print("    ⇒ 我们的落点 LBA %d %s" % (LBA0,
                  "落在数据区(只坏数据, 不动元数据)" if LBA0 >= data_start
                  else "**落在元数据区(会破坏文件系统!)**"))

    print("\n=== ③ 读 LBA %d 起 %d 字节 (黑匣子落点) ===" % (LBA0, BYTES))
    blob = d.read_at(LBA0 * BLK, BYTES)
    print("  实读 %d 字节" % len(blob))
    if blob == b"\x00" * len(blob):
        print("  [!] 全 0 —— 数据不在这个位置 (或被格式化/别名覆盖了)。")
    else:
        nz = sum(1 for i in range(len(blob) // 256) if blob[i*256:(i+1)*256] != b"\x00" * 256)
        print("  非全零记录: %d / %d" % (nz, len(blob) // 256))
        print("\n  前 3 条非零记录:")
        shown = 0
        for i in range(len(blob) // 256):
            rec = blob[i*256:(i+1)*256]
            if rec == b"\x00" * 256 or len(rec) < 256:
                continue
            tick = struct.unpack_from("<I", rec, 0)[0]
            sen = struct.unpack_from("<16f", rec, 4)
            wire = struct.unpack_from("<16f", rec, 4 + 64)
            act = struct.unpack_from("<16f", rec, 4 + 128)
            ctl = struct.unpack_from("<I", rec, 4 + 192)[0]
            print("    [%3d] tick=%-9d run=%d routes=%d seq=%d" %
                  (i, tick, (ctl >> 24) & 0xFF, (ctl >> 16) & 0xFF, ctl & 0xFFFF))
            print("          SENSOR  [0:4] = %s" % ["%.5g" % v for v in sen[:4]])
            print("          WIRE    [0:4] = %s" % ["%.5g" % v for v in wire[:4]])
            print("          ACTUATOR[0:4] = %s" % ["%.5g" % v for v in act[:4]])
            shown += 1
            if shown >= 3:
                break

    if do_dump:
        out = "blackbox_lba%d_%d.bin" % (LBA0, len(blob))
        with open(out, "wb") as f:
            f.write(blob)
        print("\n[+] 原始数据已存: %s" % out)

    d.close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
