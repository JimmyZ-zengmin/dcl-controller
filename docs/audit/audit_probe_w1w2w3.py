#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
H723 W1/W2/W3 批次外部审计 —— 独立复现探针
================================================================
对应报告: docs/audit/H723-W1W2W3-AUDIT.md

把该轮审计中「由审计方独立执行」的验证固化下来, 供任何人复跑:

  离线 (无需硬件):
    P1  CRC 覆盖自检值复算          -> 报告 §1.5
    P2  bytes(int) 陷阱演示          -> 报告 §2  (h723_w1.py:128 恒 FAIL 的根因)
    P3  W1 判据与固件语义对照清单    -> 报告 §3  (仅打印, 不做硬件访问)

  在线 (需 --port, CH340 接 PA9/PA10):
    P4  条数双来源不一致探针         -> 报告 §5  (0x38/0x43 报陈旧值, SHM 是真相)

用法:
    python docs/audit/audit_probe_w1w2w3.py                 # 只跑离线
    python docs/audit/audit_probe_w1w2w3.py --port COM14    # 离线 + 在线

注意:
    - 在线部分会先 RESET 再 deploy 一个 8 条程序, 覆盖板上当前组态。
    - §5 的判据是「三来源读数不一致」, 不是「谁对谁错」——SHM 是真相。
"""
import argparse
import struct
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ── DCL 协议常量 ────────────────────────────────────────────
SOF, SOF_RESP = 0xC0, 0xC1
CMD_GET_VERSION     = 0x01
CMD_DEPLOY          = 0x10
CMD_RESET           = 0x13
CMD_READ            = 0x20
CMD_ENGINE_STATUS   = 0x38
CMD_PERSIST_STATUS  = 0x43

SHM_OFF_N_ROUTES_WORD = 0x0C   # u32@0x0C 的高半字 = N_ROUTES(u16)


def crc16(data, crc=0xFFFF):
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF), 与固件一致。"""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def build_frame(cmd, payload=b""):
    body = bytes([cmd, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    return bytes([SOF]) + body + struct.pack("<H", crc16(body))


# ── P1: CRC 覆盖自检值 ──────────────────────────────────────
def p1_crc_selftest():
    print("=" * 64)
    print("P1  CRC 覆盖自检值复算   (报告 §1.5)")
    print("=" * 64)
    pl = bytes([0x00, 0x02, 0x33, 0x00])           # 固件自检用响应 payload
    sts, n = 0x00, len(pl)
    correct = bytes([sts, n & 0xFF, (n >> 8) & 0xFF]) + pl          # 3+n = 7 字节 (正确)
    oldbug  = bytes([sts, n & 0xFF, (n >> 8) & 0xFF]) + pl[:-1]     # 2+n = 6 字节 (旧 bug)
    c_ok, c_bug = crc16(correct), crc16(oldbug)
    print(f"  覆盖 3+n=7 字节 (正确)  -> CRC=0x{c_ok:04X} -> 线上低先高后 = {c_ok & 0xFF:02X} {c_ok >> 8:02X}")
    print(f"  覆盖 2+n=6 字节 (旧bug) -> CRC=0x{c_bug:04X} -> 线上 = {c_bug & 0xFF:02X} {c_bug >> 8:02X}")
    print( "  固件注释声称: 正确 = C9 C9, 旧实现 = 44 E7")
    ok = ((c_ok & 0xFF) == 0xC9 and (c_ok >> 8) == 0xC9 and
          (c_bug & 0xFF) == 0x44 and (c_bug >> 8) == 0xE7)
    print(f"  独立复算: {'PASS 与声称一致' if ok else 'FAIL 不一致'}")
    return ok


# ── P2: bytes(int) 陷阱 ─────────────────────────────────────
def p2_bytes_int_trap():
    print()
    print("=" * 64)
    print("P2  bytes(int) 陷阱演示   (报告 §2, h723_w1.py:128 恒 FAIL 的根因)")
    print("=" * 64)
    buf = bytearray([0xC1, 0x00, 0x04, 0x00, 0x00, 0x02, 0xF7, 0x00, 0x59, 0x13])
    print(f"  响应字节: {buf.hex(' ')}")
    print(f"  buf[1] (sts) = {buf[1]}   类型 = {type(buf[1]).__name__} (int)")
    print(f"  bytes(buf[1]) = {bytes(buf[1])!r}   长度 {len(bytes(buf[1]))}, 不是 b'\\x00'")
    print(f"  旧判据 'bytes(buf[1]) != 0' = {bytes(buf[1]) != 0}   <- 恒为 True")
    print(f"  正确判据 'buf[1] == 0'      = {buf[1] == 0}")
    print(f"  若 sts=0xFF: bytes(0xFF) 生成 {len(bytes(0xFF))} 个零字节 -> 同样判 FAIL")
    print("  => 该行让 T0 永远 FAIL 并 return 2, 后续 40+ 项判据一行都没跑过")
    print("  => 与「W1 唯一 FAIL、其它工具全通过」的现象完全吻合")
    return True


# ── P3: W1 判据清单 ─────────────────────────────────────────
def p3_w1_criteria():
    print()
    print("=" * 64)
    print("P3  W1 工具 6 项判据与固件语义不符   (报告 §3)")
    print("=" * 64)
    items = [
        ("S5",  "写「128x140 远超 26000」——算错: 128x140=17920; persist 工具 T24 口径相反"),
        ("S2",  "判据读 g_isr_n(拍中断), 而 STOP 只停引擎不停拍"),
        ("S1b", "读 shm+0x0D(u8, 非 4 对齐) -> 被守卫拒, 固件行为正确"),
        ("R2",  "写 WIRE_MAP, 而引擎 RUN 时每拍覆写它"),
        ("R9",  "测 shm+0x7F00+64 字恰好落在 SHM 内, 本就不越界"),
    ]
    for k, v in items:
        print(f"  {k:5s} {v}")
    print("  => 7 个 FAIL 里 6 个是工具判据问题, 仅 MAGIC/条数为固件问题(报告 §4/§5)")
    return True


# ── P4: 条数双来源探针 (在线) ───────────────────────────────
def p4_route_count_probe(port):
    print()
    print("=" * 64)
    print(f"P4  条数双来源不一致探针   (报告 §5)   [port={port}]")
    print("=" * 64)
    try:
        import time
        import serial
    except ImportError:
        print("  跳过: 未安装 pyserial")
        return None

    ser = serial.Serial(port, 115200, timeout=0.8)

    def xchg(frame, wait=0.8):
        ser.reset_input_buffer()
        ser.write(frame)
        ser.flush()
        time.sleep(wait)
        b = ser.read(256)
        if not b or b[0] != SOF_RESP:
            return None, b
        n = b[2] | (b[3] << 8)
        return b[1], b[4:4 + n]

    def read32(off, shm):
        st, pl = xchg(build_frame(CMD_READ, struct.pack("<I", shm + off)))
        return struct.unpack("<I", pl[:4])[0] if st == 0 and pl and len(pl) >= 4 else None

    st, pl = xchg(build_frame(CMD_ENGINE_STATUS))
    if st != 0 or not pl or len(pl) < 27:
        print("  0x38 失败; 请确认已烧最新固件且串口正确")
        ser.close()
        return None
    shm = struct.unpack("<I", pl[23:27])[0]
    print(f"  SHM base = 0x{shm:08X}")

    xchg(build_frame(CMD_RESET), 0.5)
    NR = NP = NS = 8
    rts = b"".join(struct.pack("<BBBBBBHHHHBB", 2, i, 2, i, 0, 0x01, i, 0, 0, 0, 0, 0)
                   for i in range(NR))
    ps = b"".join(struct.pack("<ffff", 1.0, 0.0, 0.0, 0.0) for _ in range(NP))
    ss = b"".join(struct.pack("<ffff", 0.0, 0.0, 0.0, 0.0) for _ in range(NS))
    st, _ = xchg(build_frame(CMD_DEPLOY, struct.pack("<HHH", NR, NP, NS) + rts + ps + ss), 1.0)
    print(f"  deploy {NR} 条 DIRECT -> ACK={st == 0}")

    st38, pl38 = xchg(build_frame(CMD_ENGINE_STATUS))
    nr38 = struct.unpack("<H", pl38[20:22])[0] if pl38 and len(pl38) >= 22 else None
    st43, pl43 = xchg(build_frame(CMD_PERSIST_STATUS))
    nr43 = struct.unpack("<H", pl43[1:3])[0] if pl43 and len(pl43) >= 3 else None
    v = read32(SHM_OFF_N_ROUTES_WORD, shm)
    nr_shm = (v >> 16) & 0xFFFF if v is not None else None

    print()
    print(f"  SHM  N_ROUTES (真相)     = {nr_shm}")
    print(f"  0x38 r[20:22] (引擎报告) = {nr38}")
    print(f"  0x43 r[1:2]   (persist)  = {nr43}")
    print()
    if nr_shm == NR and (nr38 != NR or nr43 != NR):
        print("  PASS 坐实: SHM=8 但 0x38/0x43 报陈旧值 -> g_active_routes deploy 路径未更新")
        ok = True
    elif nr_shm == NR and nr38 == NR and nr43 == NR:
        print("  三来源一致 (该问题可能已修)")
        ok = True
    else:
        print("  结果异常, 请人工核对")
        ok = False
    ser.close()
    return ok


def main():
    ap = argparse.ArgumentParser(description="H723 W1/W2/W3 外部审计独立复现探针")
    ap.add_argument("--port", help="串口 (如 COM14); 省略则只跑离线探针")
    a = ap.parse_args()

    results = [("P1 CRC 自检值",   p1_crc_selftest()),
               ("P2 bytes(int) 陷阱", p2_bytes_int_trap()),
               ("P3 W1 判据清单",  p3_w1_criteria())]
    if a.port:
        results.append(("P4 条数双来源", p4_route_count_probe(a.port)))
    else:
        print()
        print("(P4 需 --port; 例: python docs/audit/audit_probe_w1w2w3.py --port COM14)")

    print()
    print("=" * 64)
    for name, r in results:
        tag = "PASS" if r else ("FAIL" if r is False else "SKIP")
        print(f"  {name:20s} {tag}")
    return 0 if all(r is not False for _, r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
