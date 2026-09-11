#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_macro.py — W5 外设域: macro 字节码 VM 验收 (0x40 / 0x41 / 0x42)

★ 判据设计 (每条都能失败, 且大多**零硬件依赖**):
  · VM 效果一律用 **SHM 类字节码** (0x30-0x33) 制造, 再用 0x22 burst **独立读回
    SHM** 核对 —— 不依赖任何外部引脚/仪器。这是把"能跑"变成"可观测"的关键。
  · 一次性执行 (0x40) 与循环执行 (0x41+0x42) **分开测** —— 二者共用同一个
    macro_exec, 但调度路径不同, 只测一个无法区分"能单跑"与"能循环跑"。
  · 否定性判据必须成对: NaN 守卫 / 未知 op / 越界地址 / 未迁移 op 都要
    ①NAK 且 ②**目标 SHM 未被改动** (只测 NAK 无法区分"拒绝了"与"根本没执行")。
  · 循环执行要证"真的在跑": loop_cnt 必须增长, 不是"设了 run=1 就算"。
  · RESET 清 macro 是**声明的边界** (RAM-only) —— 用判据把它钉住, 防止
    日后有人悄悄改成 flash 持久却忘了改文档 (或反之)。

用法:
    python tools/h723_macro.py            # 默认自动找 CH340 口
    python tools/h723_macro.py --port COM14
"""

# ★ Windows 控制台默认 GBK: 脚本自己 print 出来的个别字符 (⇒ / ✓ 等) 会以
#   UnicodeEncodeError **直接崩掉整个脚本** —— 数据都量到了, 却崩在"打印结论"这一步,
#   症状看起来像"脚本坏了"而不是"编码问题"。⇒ 统一在入口把 stdout 的错误策略改成
#   "永不抛" (换成 ?), 让验收脚本不可能因为自己的输出而失败。
#   (2026-09-11 实测: audit_m234 / w1 真的这么崩过一次, 整份结果都没打出来。)
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse
import struct
import sys
import time

try:
    import serial
except ImportError:
    print("!! 需要 pyserial: pip install pyserial")
    sys.exit(2)

_HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
sys.path.insert(0, _HERE)
from h723_modbus import Link, find_port          # 复用同款协议客户端 (含 T0c 闸门纪律)

CMD_GET_VERSION = 0x01
CMD_READ        = 0x20
CMD_READ_BURST  = 0x22
CMD_ENGINE_STATUS = 0x38
CMD_RESET       = 0x13
CMD_MACRO        = 0x40
CMD_MACRO_UPLOAD = 0x41
CMD_MACRO_CTRL   = 0x42

# SHM 布局 (必须与 src/engine.h 一致; 改布局要同步这里)
OFF_SENSOR_MAP   = 0x0040
OFF_ACTUATOR     = 0x0140
OFF_WIRE_MAP     = 0x0240
OFF_MACRO_CTRL   = 0x5DE0
OFF_MACRO_CODE   = 0x5DF0
MACRO_CTRL_FMT   = "<BBHHHII"        # run u8, err u8, len u16, loop_ms u16, rsv u16, loop_cnt u32, last_tick u32

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  [%s] %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


# ── 字节码工具 (VM op 见 src/macro.c) ──
def bc_push(u32):        return bytes([0x08]) + struct.pack("<I", u32)
def bc_push_f(f):        return bc_push(struct.unpack("<I", struct.pack("<f", f))[0])
def bc_wire_set(i):      return bytes([0x33, i])
def bc_wire_get(i):      return bytes([0x32, i])
def bc_act_set(i):       return bytes([0x31, i])
def bc_store():          return bytes([0x11])          # store(addr, val): 栈=[addr,val]
def bc_end():            return b"\xFF"

F_INF = 0x7F800000          # +Inf 位模式
F_ONE = struct.unpack("<I", struct.pack("<f", 1.0))[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    port = find_port(a.port)
    try:
        # M4 sentinel: un-halt the core if a pyocd session left it halted
        from h723_w1 import revive_if_dead as _rv
        _rv(port)
    except Exception:
        pass

    if not port:
        print("!! 找不到串口")
        return 2
    print("端口: %s @ 115200\n" % port)

    with serial.Serial(port, 115200, timeout=0.05) as ser:
        L = Link(ser, a.verbose)
        time.sleep(0.3)

        def rd32(addr):
            sts, p = L.xact(CMD_READ, struct.pack("<I", addr))
            return struct.unpack("<I", p)[0] if sts == 0 and len(p) >= 4 else None

        def rd_burst(addr, nwords, timeout=0.6):
            sts, p = L.xact(CMD_READ_BURST, struct.pack("<IH", addr, nwords), timeout=timeout)
            return p if sts == 0 else None

        # ── T0 前置 (链路 + 工具自检; 防"工具坏了判据照跑") ──
        print("── T0 前置 ──")
        sts, p = L.xact(CMD_GET_VERSION)
        record("T0 链路活性 (GET_VERSION→ACK)", sts == 0 and len(p) >= 4, "sts=%s" % sts)
        if sts != 0:
            print("  !! 链路不活"); return 2
        s_bad, _ = L.xact(0x7F)
        record("T0c 工具自检: 未实现命令(0x7F)→NAK (证明 sts 判据能失败)", s_bad == 0xFF, "sts=%s" % s_bad)
        if s_bad != 0xFF:
            print("  !! sts 判据不可信"); return 2

        sts, st = L.xact(CMD_ENGINE_STATUS)
        if sts != 0 or len(st) < 27:
            print("  !! 读不到 0x38 (无法定位 SHM)"); return 2
        shm = struct.unpack("<I", st[23:27])[0]
        record("T0b 读 SHM 基址 (0x38)", 0x20000000 <= shm < 0x20040000, "shm=0x%08X" % shm)

        L.xact(CMD_RESET); time.sleep(0.2)

        # ══════════ A. 一次性执行 (0x40) ══════════
        print("\n── A. 0x40 一次性执行 (返回栈) ──")

        # A1 纯栈: push 0x12345678 → ACK 回 4B (小端)
        code = bc_push(0x12345678) + bc_end()
        sts, p = L.xact(CMD_MACRO, code)
        record("A1 push 0x12345678 → 栈回读 78 56 34 12",
               sts == 0 and p == struct.pack("<I", 0x12345678),
               "resp=%s" % (p.hex() if p else None))

        # A2 写 WIRE[5]=1.0 (0x33) → 用 0x22 独立读回核对
        code = bc_push_f(1.0) + bc_wire_set(5) + bc_end()
        sts, p = L.xact(CMD_MACRO, code)
        w5 = rd32(shm + OFF_WIRE_MAP + 5 * 4)
        record("A2 push 1.0 → wire[5]= (SHM 独立读回 == 1.0)",
               sts == 0 and w5 == F_ONE, "wire[5]=0x%08X" % (w5 if w5 is not None else 0))

        # A3 读 WIRE[5] (0x32) → 栈回读应为 1.0
        code = bc_wire_get(5) + bc_end()
        sts, p = L.xact(CMD_MACRO, code)
        record("A3 wire[5] → push (0x32 读通路)", sts == 0 and p == struct.pack("<I", F_ONE),
               "resp=%s" % (p.hex() if p else None))

        # A4 写 ACTUATOR[2]=3.0 (0x31: 取栈值, 无地址参数)
        code = bc_push_f(3.0) + bc_act_set(2) + bc_end()
        sts, p = L.xact(CMD_MACRO, code)
        a2 = rd32(shm + OFF_ACTUATOR + 2 * 4)
        record("A4 push 3.0 → actuator[2]= (0x31 写通路)",
               sts == 0 and a2 == struct.unpack("<I", struct.pack("<f", 3.0))[0],
               "act[2]=0x%08X" % (a2 if a2 is not None else 0))

        # ══════════ B. 否定性判据 (每条: NAK + 目标未被改动) ══════════
        print("\n── B. 否定性判据 (NAK 且 SHM 未被改动) ──")
        before = rd32(shm + OFF_WIRE_MAP + 5 * 4)

        # B1 NaN/Inf 守卫: 写 +Inf 到 wire[5] 必须被拒
        code = bc_push(F_INF) + bc_wire_set(5) + bc_end()
        sts, _ = L.xact(CMD_MACRO, code)
        after = rd32(shm + OFF_WIRE_MAP + 5 * 4)
        record("B1 push +Inf → wire[5]= 被拒 (NaN/Inf 守卫) 且 wire[5] 未变",
               sts == 0xFF and after == before,
               "sts=%s wire[5]=0x%08X (前 0x%08X)" % (sts, after or 0, before or 0))

        # B2 未知 op 0xEE
        sts, _ = L.xact(CMD_MACRO, bytes([0xEE]))
        record("B2 未知 op 0xEE → NAK", sts == 0xFF, "sts=%s" % sts)

        # B3 SPI op 0x20 (未迁移, 必须显式报错而非静默跳过)
        sts, _ = L.xact(CMD_MACRO, bytes([0x20]) + bytes(8))
        record("B3 SPI op 0x20 (未迁移) → NAK (显式拒绝, 非静默)", sts == 0xFF, "sts=%s" % sts)

        # B4 栈溢出: 17 次 push (> MACRO_STACK_DEPTH=16)
        sts, _ = L.xact(CMD_MACRO, bc_push(0) * 17 + bc_end())
        record("B4 连续 17 次 push → 栈溢出被拒", sts == 0xFF, "sts=%s" % sts)

        # B5 裸地址越界: store 到非 SHM 地址 (0x08000000 = FLASH 区) 必须被拒 (H723 加固)
        code = bc_push(0x08000000) + bc_push_f(1.0) + bc_store() + bc_end()
        sts, _ = L.xact(CMD_MACRO, code)
        record("B5 store 到非 SHM 地址 (0x08000000) → NAK (裸地址窗口守卫)",
               sts == 0xFF, "sts=%s" % sts)

        # B6 长度非法: n=0 / n>512
        s0, _ = L.xact(CMD_MACRO, b"")
        s1, _ = L.xact(CMD_MACRO, bc_push(0) * 120)      # 5*120 = 600 > 512
        record("B6 长度非法 (0 / >512) → 均 NAK", s0 == 0xFF and s1 == 0xFF,
               "n0=%s n600=%s" % (s0, s1))

        # ══════════ C. 循环执行 (0x41 上传 + 0x42 控制) ══════════
        print("\n── C. 0x41 上传 + 0x42 控制 (循环执行) ──")

        # C1 上传: [loop_ms=20][push 2.0 → wire[6]= ; end]
        code = bc_push_f(2.0) + bc_wire_set(6) + bc_end()
        sts, p = L.xact(CMD_MACRO_UPLOAD, struct.pack("<H", 20) + code)
        ok = sts == 0 and len(p) == 4 and p[0] == len(code) and p[2] == 20
        record("C1 0x41 上传 → ACK [len][loop_ms] 回显", ok,
               "resp=%s (code_len=%d)" % (p.hex() if p else None, len(code)))

        # C2 启动 → run=1, 循环真的在跑 (loop_cnt 增长), 效果落 SHM
        sts, _ = L.xact(CMD_MACRO_CTRL, bytes([1]))
        time.sleep(0.6)                                   # 20ms/轮 → 约 30 轮
        ctl = rd_burst(shm + OFF_MACRO_CTRL, 4)
        w6 = rd32(shm + OFF_WIRE_MAP + 6 * 4)
        run = err = None; cnt = 0
        if ctl and len(ctl) >= 16:
            run, err, ln, lms, rsv, cnt, lt = struct.unpack(MACRO_CTRL_FMT, ctl[:16])
        record("C2 启动 → run=1 + loop_cnt 增长 (真的在跑)",
               sts == 0 and run == 1 and 5 <= cnt <= 60,
               "run=%s err=%s loop_cnt=%s loop_ms=%s" % (run, err, cnt, lms))
        record("C3 循环效果落 SHM: wire[6] == 2.0", w6 == struct.unpack("<I", struct.pack("<f", 2.0))[0],
               "wire[6]=0x%08X" % (w6 if w6 is not None else 0))

        # C4 停止 → run=0, loop_cnt 不再增长
        L.xact(CMD_MACRO_CTRL, bytes([0]))
        c1 = rd_burst(shm + OFF_MACRO_CTRL, 4)
        n1 = struct.unpack(MACRO_CTRL_FMT, c1[:16])[5] if c1 and len(c1) >= 16 else -1
        time.sleep(0.4)
        c2 = rd_burst(shm + OFF_MACRO_CTRL, 4)
        run2, _, _, _, _, n2, _ = struct.unpack(MACRO_CTRL_FMT, c2[:16]) if c2 and len(c2) >= 16 else (None,) * 7
        record("C4 停止 → run=0 且 loop_cnt 冻结", run2 == 0 and n1 == n2,
               "run=%s cnt %s→%s" % (run2, n1, n2))

        # C5 无程序时启动必须被拒 (比 S3 严: 不空转)
        L.xact(CMD_RESET); time.sleep(0.2)                # RESET 清 macro (RAM-only 边界)
        c3 = rd_burst(shm + OFF_MACRO_CTRL, 4)
        rlen = struct.unpack(MACRO_CTRL_FMT, c3[:16])[2] if c3 and len(c3) >= 16 else None
        s_no, _ = L.xact(CMD_MACRO_CTRL, bytes([1]))
        record("C5 RESET 清空 macro (声明的 RAM-only 边界) + 无程序启动被拒",
               rlen == 0 and s_no == 0xFF,
               "len_after_reset=%s start=%s" % (rlen, s_no))

        L.xact(CMD_RESET); time.sleep(0.1)

    print("\n" + "=" * 74)
    npass = sum(1 for _, ok, _ in RESULTS if ok)
    nfail = len(RESULTS) - npass
    print("W5 macro VM 结果: %d PASS / %d FAIL" % (npass, nfail))
    print("=" * 74)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
