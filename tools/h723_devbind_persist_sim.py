#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_devbind_persist_sim.py — **判据反空自检**（离线; 不接板子、不开串口）

## 它是什么, 不是什么
★ **不是**被测对象的第二份实现, **不是**固件的行为规格 —— 它唯一的作用是把
  `tools/h723_devbind_persist_test.py` 里的 T0..T5 **在故意做错的假设备上跑一遍**,
  回答一个问题: **"这些判据真的会红吗?"**
  本项目铁律: "判据必须能失败, 而且要能证明它会红"。没有这一步, T1..T5 只是一串
  在真板上"大概会 PASS"的断言 —— 而本项目已经栽过多次"空判据"（读数恒真、永远不红）。

★ 假设备里的 G6-4/GAP-11 语义是**照源码逐条抄**的（每条都标了出处）:
    `src/dev_bind.c`   db_crc / submit 的校验与拒绝 / service / pack / unpack
    `src/main.c`       h_prog_commit 里"闸2→闸5→pack→save"的顺序; prog_boot_load_apply
  ⇒ 若真板行为与它不一致, **以真板为准**（本文件没有任何权威性）。
  ⇒ 它**不能**代替上机验收, 只能证明"判据本身是活的"。

## 用法
    python tools/h723_devbind_persist_sim.py            # 全部 mutant 一遍
    python tools/h723_devbind_persist_sim.py -v         # 打印每条判据

退出码: 0 = 判据的行为符合预期（好设备全绿 + 每个 mutant 都能让**指定**判据变红）
        1 = 出现了"该红的没红"（= 判据是空的）或"不该红的红了"（= 判据脆弱/误判）
"""
import argparse
import io
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import h723_devbind_persist_test as T   # noqa: E402

SHM_BASE = 0x24000000
FW_VER = 0x0200
CAP = 0x3DF7          # 与 src/transport.h 的 DCL_CAP_H723_IMPL 同口径（含 0x2000）


# ══════════════════ 假设备 ══════════════════
class SimDevice:
    """照 `src/dev_bind.c` / `src/main.c` 的语义跑的**可注入故障**的假设备。

    mutant 列表（每个都是"实现里少了一句/多了一句"的真实可能）:
        ""               忠实实现
        no_restore       `dev_bind_unpack` 永远返回 NONE（= 没实现恢复）
        always_pack      `dev_bind_pack` 不看 `s_n`（= 老包也被加段, 破坏兼容）
        no_fnv_check     不校验段内 fnv（= 坏表被接受）
        silent_reject    段坏时既不计数也不写 DB_REJECT（= 拒绝不可观测）
        nonidempotent    重装载叠加副作用（每次把 entries 右移一位 = 表被改坏）
        no_service       `dev_bind_service` 没被主循环调用（= 恢复了但不轮询）
    """

    def __init__(self, mutant=""):
        self.m = mutant
        self.mem = {}                       # abs_addr -> u32
        self.w(SHM_BASE + T.DB_MAGIC, T.DB_MAGIC_VAL)
        self.w(SHM_BASE + T.DB_PERIOD, T.DB_PERIOD_DEF)
        # 已生效的表（内部态, = dev_bind.c 的 s_lane/s_n）
        self.lane = [0] * T.DB_SLOTS
        self.s_n = 0
        self.req = self.done = self.reject = 0
        self.ok_n = self.err_n = self.skip_n = self.rej_n = 0
        self.load_ok = self.load_bad = 0
        self.stored = None                  # 卡上那份载荷（含可选段）
        self.stored_seq = 0
        self.deploy_seq = 0
        self.txn = None                     # [total, crc, buf]
        self.present = True                 # 板上是否接了 AS5600
        self.last_err = ""

    # ── 内存 ──
    def r(self, a):
        return self.mem.get(a, 0)

    def w(self, a, v):
        self.mem[a] = v & 0xFFFFFFFF

    def off(self, a):
        return a - SHM_BASE

    # ── 主循环: 照 dev_bind_service 的轮询语义（每被问一次推进一拍）──
    def service(self):
        if self.m == "no_service":
            return
        if self.s_n == 0:
            return
        if self.present:
            self.ok_n += 1
            self.w(SHM_BASE + T.DB_OK_N, self.ok_n)      # 照 dev_bind_service: 两个计数各自回写
        else:
            self.err_n += 1
            self.w(SHM_BASE + T.DB_ERR_N, self.err_n)
            self.w(SHM_BASE + T.DB_LAST_ERR, T.I2C_SM_ST_NAK)

    # ── 照 dev_bind.c: dev_bind_submit ──
    def submit(self):
        rq = self.r(SHM_BASE + T.DB_REQ_SEQ)
        dn = self.r(SHM_BASE + T.DB_DONE_SEQ)
        if rq == 0 or rq == dn:
            return
        if dn != 0 and ((rq - dn) & 0xFFFFFFFF) >= 0x80000000:      # 有符号差 < 0
            self.reject = T.DB_RC_SEQ
            self.rej_n += 1
            self.w(SHM_BASE + T.DB_REJ_N, self.rej_n)
            self.w(SHM_BASE + T.DB_REJECT, self.reject)
            return
        ents = [self.r(SHM_BASE + T.DB_ENTRIES + 4 * i) for i in range(T.DB_SLOTS)]
        if self.m == "crc_bytes":
            h = 2166136261
            for _b in struct.pack("<%dI" % T.DB_SLOTS, *ents):
                h = ((h ^ _b) * 16777619) & 0xFFFFFFFF
            mine = h
        else:
            mine = T.db_crc_words(ents)
        if mine != self.r(SHM_BASE + T.DB_CRC):
            rc = T.DB_RC_CRC
        else:
            rc = T.DB_RC_OK
            for e in ents:
                dev = (e >> 28) & 0xF
                if dev == 0:
                    continue
                if dev > 1:
                    rc = T.DB_RC_DEV
                    break
                if (e & 0xFF) > 0x7F:
                    rc = T.DB_RC_ADDR
                    break
                ln = (e >> 16) & 0xFF
                if ln == 0 or ln > 2:
                    rc = T.DB_RC_LEN
                    break
        if rc != T.DB_RC_OK:
            self.rej_n += 1
            self.reject = rc
            self.w(SHM_BASE + T.DB_REJ_N, self.rej_n)
            self.w(SHM_BASE + T.DB_REJECT, rc)
            self.w(SHM_BASE + T.DB_DONE_SEQ, rq)        # ★ 拒绝也回 done_seq
            return
        self.lane = list(ents)
        self.s_n = sum(1 for e in ents if ((e >> 28) & 0xF) != 0)
        self.reject = T.DB_RC_OK
        self.w(SHM_BASE + T.DB_REJECT, T.DB_RC_OK)
        self.w(SHM_BASE + T.DB_N_VALID, self.s_n)
        self.w(SHM_BASE + T.DB_DONE_SEQ, rq)

    # ── 照 dev_bind.c: dev_bind_pack（main.c 在闸2/闸5 之后调它）──
    def pack(self, payload):
        if self.s_n == 0 and self.m != "always_pack":
            return payload
        period = self.r(SHM_BASE + T.DB_PERIOD)
        if period == 0 or period > T.DB_PERIOD_MAX:
            period = T.DB_PERIOD_DEF
        ents = [self.r(SHM_BASE + T.DB_ENTRIES + 4 * i) for i in range(T.DB_SLOTS)]
        return payload + T.build_segment(period, ents)

    # ── 照 dev_bind.c: dev_bind_unpack ──
    def unpack(self, payload):
        if self.m == "no_restore":
            return "none"
        if len(payload) < T.DB_SEG_LEN:
            return "none"
        seg = payload[len(payload) - T.DB_SEG_LEN:]
        if T.u32(seg, 0) != T.DB_MAGIC_VAL:
            return "none"
        bad = T.u32(seg, 4) != T.DB_SEG_LEN
        if self.m == "nonidempotent":
            base = [self.r(SHM_BASE + T.DB_ENTRIES + 4 * i) for i in range(T.DB_SLOTS)]
            ents = base[1:] + [base[0]]
        else:
            ents = [T.u32(seg, 16 + 4 * i) for i in range(T.DB_SLOTS)]
        for i, e in enumerate(ents):
            self.w(SHM_BASE + T.DB_ENTRIES + 4 * i, e)
        if not bad and self.m != "no_fnv_check":
            if T.db_crc_words(ents) != T.u32(seg, 12):
                bad = True
        if bad:
            for i in range(T.DB_SLOTS):
                self.w(SHM_BASE + T.DB_ENTRIES + 4 * i, 0)
            self.w(SHM_BASE + T.DB_CRC, T.db_crc_words([0] * T.DB_SLOTS))
            if self.m != "silent_reject":
                self.load_bad += 1
                self.w(SHM_BASE + T.DB_LOAD_BAD_N, self.load_bad)
                self.w(SHM_BASE + T.DB_REJECT, T.DB_RC_CRC)
            return "bad"
        self.w(SHM_BASE + T.DB_PERIOD, T.u32(seg, 8))
        self.w(SHM_BASE + T.DB_CRC, T.db_crc_words(ents))
        self.w(SHM_BASE + T.DB_REQ_SEQ, (self.r(SHM_BASE + T.DB_REQ_SEQ) + 1) & 0xFFFFFFFF)
        self.submit()
        if self.r(SHM_BASE + T.DB_REJECT) != T.DB_RC_OK:
            self.load_bad += 1
            self.w(SHM_BASE + T.DB_LOAD_BAD_N, self.load_bad)
            return "bad"
        self.load_ok += 1
        self.w(SHM_BASE + T.DB_LOAD_OK_N, self.load_ok)
        return "ok"

    # ── 照 main.c: prog_boot_load_apply（op=23 与开机同一条路径）──
    def boot_load_apply(self):
        if self.stored is None:
            return 5, 0                      # PROG_RC_NONE
        pl = self.stored
        nr, np_, ns = T.u32(pl, 0) & 0xFFFF, (T.u32(pl, 0) >> 16) & 0xFFFF, T.u32(pl, 4) & 0xFFFF
        need = 6 + (nr + np_ + ns) * 16
        if len(pl) < need:
            return 7, 0                      # PROG_RC_VALIDATE
        self.deploy_seq += 1
        self.unpack(pl)                      # ★ 程序先装, 再恢复绑定表（源码顺序）
        return 0, 1

    # ── 协议面 ──
    def send(self, cmd, payload=b"", expect_len=None):
        p = b""
        if cmd == T.C_VERSION:
            p = struct.pack("<HH", FW_VER, CAP)
        elif cmd == T.C_ENGINE_STATUS:
            b = bytearray(T.ENG_STATUS_LEN)
            struct.pack_into("<I", b, 23, SHM_BASE)
            p = bytes(b)
        elif cmd == T.C_READ_BURST:
            a, c = struct.unpack_from("<IH", payload, 0)
            p = b"".join(struct.pack("<I", self.r(a + 4 * i)) for i in range(c))
            self.service()
        elif cmd == T.C_WRITE:
            a, v = struct.unpack_from("<II", payload, 0)
            self.w(a, v)
            p = struct.pack("<I", a)
            self.submit()
        elif cmd == T.C_WRITE_BURST:
            a, c = struct.unpack_from("<IH", payload, 0)
            for i in range(c):
                self.w(a + 4 * i, struct.unpack_from("<I", payload, 6 + 4 * i)[0])
            p = struct.pack("<I", a)
            self.submit()
        elif cmd == T.C_PIN_PATTERN:
            assert payload[0] == T.OP_RELOAD
            rc, loaded = self.boot_load_apply()
            p = struct.pack("<II", rc, loaded)
        elif cmd == T.C_PROG_BEGIN:
            total, crc = struct.unpack_from("<II", payload, 0)
            self.txn = [total, crc, bytearray()]
            p = struct.pack("<I", total)
        elif cmd == T.C_PROG_DATA:
            off = struct.unpack_from("<I", payload, 0)[0]
            assert off == len(self.txn[2]), "0x46 off != got（假设备也照这条拒）"
            self.txn[2] += payload[4:]
            p = struct.pack("<I", len(self.txn[2]))
        elif cmd == T.C_PROG_COMMIT:
            total, crc, buf = self.txn
            assert len(buf) == total and (zlib.crc32(bytes(buf)) & 0xFFFFFFFF) == crc, "闸2"
            packed = self.pack(bytes(buf))          # ★ 闸2/闸5 之后才附加（源码顺序）
            self.stored = packed
            self.stored_seq += 1
            p = struct.pack("<II", 0, 0)
        elif cmd == T.C_PROG_STATUS:
            b = bytearray(96)
            struct.pack_into("<I", b, 0, 1)                      # part_ok
            struct.pack_into("<I", b, 8, self.stored_seq)
            struct.pack_into("<I", b, 24, len(self.stored) if self.stored else 0)
            struct.pack_into("<I", b, 32, 0 if self.stored else 0xFFFFFFFF)
            struct.pack_into("<I", b, 56, self.stored_seq)
            struct.pack_into("<I", b, 64, 0)
            struct.pack_into("<I", b, 68, 1 if self.stored else 0)
            struct.pack_into("<I", b, 76, self.deploy_seq)
            p = bytes(b)
        elif cmd == T.C_PROG_ERASE:
            self.stored = None
            p = struct.pack("<I", 0)
        else:
            return "NAK", b""
        if expect_len is not None and len(p) != expect_len:
            return "TIMEOUT", b""
        return "ACK", p


class SimBoard:
    """与工具用的 `Board` **同形状**（只实现工具真正调用的那些方法）。"""

    def __init__(self, mutant=""):
        self.dev = SimDevice(mutant)
        self.shm = SHM_BASE
        self.verbose = False
        self.last_err = ""

    def send(self, cmd, payload=b"", expect_len=None):
        return self.dev.send(cmd, payload, expect_len)

    def rd(self, off, count):
        sts, p = self.send(T.C_READ_BURST, struct.pack("<IH", self.shm + off, count),
                           expect_len=count * 4)
        if sts != "ACK":
            return None
        return list(struct.unpack("<%dI" % count, p))

    def wr(self, off, words):
        sts, _ = self.send(T.C_WRITE_BURST,
                           struct.pack("<IH", self.shm + off, len(words))
                           + struct.pack("<%dI" % len(words), *words), expect_len=4)
        return sts == "ACK"

    def wr1(self, off, val):
        sts, _ = self.send(T.C_WRITE, struct.pack("<II", self.shm + off, val), expect_len=4)
        return sts == "ACK"

    def prog_begin(self, total, crc, prog_id=0, prog_ver=1, min_fw=0, req_caps=0):
        return self.send(T.C_PROG_BEGIN,
                         struct.pack("<IIIHH I", total, crc, prog_id, prog_ver, min_fw, req_caps),
                         expect_len=4)

    def prog_data(self, off, chunk):
        return self.send(T.C_PROG_DATA, struct.pack("<I", off) + chunk, expect_len=4)

    def upload(self, payload, chunk=140):
        log = []
        sts, _ = self.prog_begin(len(payload), T.crc32(payload))
        log.append(("BEGIN", sts))
        if sts != "ACK":
            return False, log, "BEGIN 未被受理"
        for off, n in T.chunk_plan(len(payload), chunk):
            sts, _ = self.prog_data(off, payload[off:off + n])
            log.append(("DATA@%d" % off, sts))
            if sts != "ACK":
                return False, log, "DATA@%d 未被受理" % off
        sts, p = self.send(T.C_PROG_COMMIT, b"", expect_len=8)
        log.append(("COMMIT", sts))
        if sts != "ACK":
            return False, log, "COMMIT 被拒"
        return True, log, "ok"

    def prog_status(self):
        sts, p = self.send(T.C_PROG_STATUS, b"", expect_len=96)
        if sts != "ACK":
            return None
        w = struct.unpack("<24I", p)
        return dict(part_ok=w[0], ab_valid=w[1], seq_a=w[2], seq_b=w[3], crc_a=w[4],
                    crc_b=w[5], len_a=w[6], len_b=w[7], active=w[8], ok_n=w[9],
                    fail_n=w[10], reject_n=w[11], last_rc=w[12], txn_begin=w[13],
                    txn_commit=w[14], txn_abort=w[15], boot_rc=w[16], boot_loaded=w[17],
                    deploy_routes=w[18], deploy_seq=w[19], pl0=w[20], pl1=w[21],
                    pl2=w[22], pl3=w[23])

    def stored_len(self, st):
        if not st:
            return None
        if st["active"] == 0:
            return st["len_a"]
        if st["active"] == 1:
            return st["len_b"]
        return None

    def snap(self):
        return T.Board.snap(self)

    def reload(self):
        self.last_err = ""
        sts, p = self.send(T.C_PIN_PATTERN, bytes([T.OP_RELOAD, 0]))
        if sts != "ACK" or len(p) != 8:
            self.last_err = "op=23 应答 %s" % sts
            return None, None
        return T.u32(p, 0), T.u32(p, 4)

    def close(self):
        pass



# ══════════════════ 跑一遍 ══════════════════
def run_once(mutant, verbose=False, present=True):
    """在指定 mutant 的假设备上跑完 T0..T5, 返回 (FAIL 的判据名, PASS 数, SKIP 数, 报告)"""
    T._N_PASS = T._N_FAIL = T._N_SKIP = 0
    T.PROG_BODY = T.build_prog(2, 1)
    T.TABLE_A = [T.entry(1, T.DST_A, T.AS5600_LEN, T.AS5600_REG, T.AS5600_ADDR7),
                 T.entry(1, T.DST_B, 1, T.AS5600_REG, T.AS5600_ADDR7)]
    b = SimBoard(mutant)
    b.dev.present = present                  # True=接有 AS5600(走 ok_n) / False=未接(走 err_n)
    buf = io.StringIO()
    saved, sys.stdout = sys.stdout, buf
    fails = []
    old_record = T.record

    def spy(name, ok, detail=""):
        if ok is False:
            fails.append(name)
        return old_record(name, ok, detail)

    T.record = spy
    try:
        if T.t0_preflight(b, 0x2000):
            ok1, post = T.t1_restore(b)
            T.t2_polling(b, post)
            T.t3_no_segment(b)
            T.t4_bad_fnv(b)
            T.t5_idempotent(b)
    finally:
        T.record = old_record
        sys.stdout = saved
    return fails, T._N_PASS, T._N_SKIP, buf.getvalue()


# (mutant, 期望**必须**变红的判据前缀, 板上是否接有 AS5600)
SCENARIOS = [
    ("", [], True),
    ("", [], False),                      # ★ 未接器件: err_n 那条路也必须能判 PASS
    ("no_restore", ["T1.4", "T1.5", "T2.1"], True),
    ("always_pack", ["T3.1"], True),
    ("no_fnv_check", ["T4.2", "T4.3", "T4.4"], True),
    ("silent_reject", ["T4.4"], True),
    ("nonidempotent", ["T1.4", "T5.2"], True),
    ("no_service", ["T2.1"], True),
    ("crc_bytes", ["T1.0"], True),        # FNV 口径若不同 ⇒ 工具的表会被 CRC 拒（判据是活的）
]


def main():
    ap = argparse.ArgumentParser(description="判据反空自检（离线, 不接板子）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每条判据的输出")
    a = ap.parse_args()
    bad = 0
    print("=" * 78)
    n_mut = len(set(m for m, _e, _p in SCENARIOS if m))
    print("判据反空自检: %d 个场景（%d 个故意做错的固件 + 接/不接器件两种环境）" % (
        len(SCENARIOS), n_mut))
    print("=" * 78)
    for mut, expect, present in SCENARIOS:
        fails, npass, nskip, out = run_once(mut, present=present)
        if a.verbose:
            print(out)
        hit = [e for e in expect if any(f.startswith(e) for f in fails)]
        ok_expect = len(hit) == len(expect)
        no_extra = True
        if mut == "":
            no_extra = len(fails) == 0
        else:
            # 允许连带红（同一根因的级联），但不允许「期望的没红」
            no_extra = True
        good = ok_expect and no_extra
        bad += 0 if good else 1
        print("  [%s] %-14s 器件=%-6s FAIL=%s  PASS=%d SKIP=%d%s" % (
            "OK  " if good else "BAD ",
            mut or "(忠实实现)", "接有" if present else "未接",
            ",".join(sorted(set(f.split()[0] for f in fails))) or "-", npass, nskip,
            "" if good else "   ← 期望 %s 变红" % (expect or "全绿")))
    print("-" * 78)
    print("  结论: %s" % ("全部符合预期（判据是活的）" if not bad else
                          "%d 个 mutant 的判据行为不符合预期 ⇒ **判据是空的或误判**" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
