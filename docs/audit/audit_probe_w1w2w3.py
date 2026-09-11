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
    """★ 2026-09-11 修正 —— 旧版无区分力。
    对方在 RESPONSE §4.1 指出: 旧判据写死"0x38/0x43 与 SHM 不一致", 于是 D 项修复后
    依然 PASS、且仍打印"报陈旧值", 而实测 0x38=8(正确)、0x43=0(未落盘, 也正确)。
    ⇒ 一条"修复前后都通过"的判据不具区分力, 不能作为证据。
    新版**主动制造区分**: 先落盘 3 条 → 再 deploy 8 条(不落盘)
      期望: 0x38 跟 SHM (=8), 0x43 仍报 flash 里的 3  → 落盘后才变 8。
    """
    print()
    print("=" * 64)
    print(f"P4  条数双来源探针 (含区分力)   [port={port}]")
    print("=" * 64)
    try:
        import time
        import serial
    except ImportError:
        print("  跳过: 未安装 pyserial")
        return None

    ser = serial.Serial(port, 115200, timeout=1.0)

    def xchg(frame, wait=0.8):
        ser.reset_input_buffer()
        ser.write(frame)
        ser.flush()
        time.sleep(wait)
        b = ser.read(512)
        if not b or b[0] != SOF_RESP:
            return None, b
        n = b[2] | (b[3] << 8)
        return b[1], b[4:4 + n]

    def dep(n):
        rts = b"".join(struct.pack("<BBBBBBHHHHBB", 2, i, 2, i, 0, 0x01, i, 0, 0, 0, 0, 0)
                       for i in range(n))
        ps = b"".join(struct.pack("<ffff", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
        ss = b"".join(struct.pack("<ffff", 0.0, 0.0, 0.0, 0.0) for _ in range(n))
        return xchg(build_frame(CMD_DEPLOY, struct.pack("<HHH", n, n, n) + rts + ps + ss), 1.2)

    def get38():
        st, pl = xchg(build_frame(CMD_ENGINE_STATUS))
        return struct.unpack("<H", pl[20:22])[0] if st == 0 and pl and len(pl) >= 22 else None

    def get43(mode=0):
        pl_in = bytes([mode]) if mode else b""
        # ★ mode=1 的 ACK 要等 flash 擦除完成 (H7 单 sector 128KB, 实测 0.84s, 可达数秒)
        st, pl = xchg(build_frame(CMD_PERSIST_STATUS, pl_in), 4.0 if mode else 0.8)
        return struct.unpack("<H", pl[1:3])[0] if st == 0 and pl and len(pl) >= 3 else None

    st, pl = xchg(build_frame(CMD_ENGINE_STATUS))
    if st != 0 or not pl or len(pl) < 27:
        print("  0x38 失败; 请确认已烧最新固件且串口正确")
        ser.close()
        return None
    shm = struct.unpack("<I", pl[23:27])[0]
    print(f"  SHM base = 0x{shm:08X}")

    # [1] 先落盘 3 条 -> flash 里持久化条数 = 3 (制造区分基准)
    xchg(build_frame(CMD_RESET), 0.5)
    st, _ = dep(3)
    print(f"  [1] deploy 3 条 -> ACK={st == 0}")
    get43(1)
    f3 = get43()
    print(f"      -> 0x43 报 flash 里 = {f3}   (期望 3)")

    # [2] 不 RESET, 直接 deploy 8 条 (未落盘): 0x38 应跟 SHM, 0x43 应仍是 3
    st, _ = dep(8)
    e8, f8 = get38(), get43()
    rv = xchg(build_frame(CMD_READ, struct.pack("<I", shm + SHM_OFF_N_ROUTES_WORD)))[1]
    nr_shm = (struct.unpack("<I", rv[:4])[0] >> 16) & 0xFFFF if rv and len(rv) >= 4 else None
    print(f"  [2] deploy 8 条(未落盘): SHM={nr_shm}  0x38={e8}  0x43={f8}")

    # [3] 落盘 -> 0x43 应变 8
    get43(1)
    f8b = get43()
    print(f"  [3] 落盘后: 0x43 = {f8b}")

    a = (nr_shm == 8 and e8 == 8)
    b = (f3 == 3 and f8 == 3)
    c = (f8b == 8)
    print()
    print(f"  P4a 0x38 跟随 SHM (deploy 后立即=8)   : {'PASS' if a else 'FAIL'}  (SHM={nr_shm} 0x38={e8})")
    print(f"  P4b 0x43 报 flash 内容 (未落盘仍=3)    : {'PASS' if b else 'FAIL'}  (0x43={f8})")
    print(f"  P4c 落盘后 0x43 == 8                  : {'PASS' if c else 'FAIL'}  (0x43={f8b})")
    ok = a and b and c
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
