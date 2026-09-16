# -*- coding: utf-8 -*-
"""h723_dev_bind_test_selftest.py — 对 h723_dev_bind_test.py 的**判据做变异测试**。

★ 为什么要有这个文件: 验收脚本自己的判据也必须**能失败**，否则"PASS 36 项"只是
  一句无法反驳的话。这里用一份**照 src/dev_bind.c 语义写的假固件**(word-wise CRC、
  拒绝不半装载、失败保留旧值、reset 恢复 magic) 跑全套 ⇒ 必须 PASS；
  然后逐项注入缺陷(mutation) ⇒ 对应判据必须 FAIL。注入后仍 PASS = 那条是**空判据**。

用法:  python tools/h723_dev_bind_test_selftest.py     # 不需要板子
"""
import io, os, struct, sys, contextlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h723_dev_bind_test as T

SHM_SIZE = 0x8000
CTRL_MAGIC = 0x5A5A1234
SHM_BASE = 0x20000000
CAP_FULL = 0x1DF7


class Fake:
    def __init__(self, flaws=(), present=False, nodevbind=False):
        self.m = bytearray(SHM_SIZE)
        self.flaws = set(flaws)
        self.present = present
        self.nodevbind = nodevbind
        self.lane = []          # (dev,dst,len,reg,addr7)
        self.busy = 0
        self.reset_state()

    # ---- SHM 访问 ----
    def u32(self, off):
        return struct.unpack_from("<I", self.m, off)[0]

    def set32(self, off, v):
        struct.pack_into("<I", self.m, off, v & 0xFFFFFFFF)

    def reset_state(self):
        self.m[:] = b"\0" * SHM_SIZE
        self.set32(0x00, CTRL_MAGIC)
        self.set32(T.OFF_SENSOR_MAP + 4, 0)      # SENSOR[1]
        self.set32(T.OFF_SENSOR_MAP + 7 * 4, 0)  # SENSOR[7]
        self.set32(0x7200, 0)                     # IX_DONE_SEQ=0
        self.set32(0x7204, 0)                     # IX_REQ_SEQ
        if self.flaws & {"noreset_reg"}:
            return                                # 漏登记: magic 全 0
        self.set32(T.OFF_I2C_XACT, T.IX_MAGIC_VAL)
        self.set32(T.DB_MAGIC, T.DB_MAGIC_VAL)
        self.set32(T.DB_PERIOD, T.DB_PERIOD_DEF)
        self.lane = []
        self.set32(T.DB_N_VALID, 0)

    # ---- 固件语义 ----
    def loop_submit(self):
        if self.flaws & {"nosubmit"}:
            return                                # 模拟"submit 没被主循环调用"
        rq = self.u32(T.DB_REQ_SEQ)
        dn = self.u32(T.DB_DONE_SEQ)
        if rq == 0 or rq == dn:
            return
        rc = 0
        if not (self.flaws & {"crc_off"}):
            ents = [self.u32(T.DB_ENTRIES + i * 4) for i in range(8)]
            f = T.db_crc_bytes if (self.flaws & {"crc_bytes"}) else T.db_crc_words
            if f(ents) != self.u32(T.DB_CRC):
                rc = 1
        tmp = []
        if rc == 0:
            for i in range(8):
                e = self.u32(T.DB_ENTRIES + i * 4)
                dev = (e >> 28) & 0xF
                if dev == 0:
                    continue
                dst = (e >> 24) & 0xF
                ln = (e >> 16) & 0xFF
                reg = (e >> 8) & 0xFF
                a7 = e & 0xFF
                if dev > 1:
                    rc = 3; break
                if a7 > 0x7F:
                    rc = 4; break
                if ln == 0 or ln > 2:
                    rc = 5; break
                if dst > 15:
                    rc = 6; break
                tmp.append((dev, dst, ln, reg, a7))
        if rc == 0:
            self.lane = tmp
            self.set32(T.DB_N_VALID, len(tmp))
            self.set32(T.DB_REJECT, 0)
        else:
            self.set32(T.DB_REJECT, rc)
            self.set32(T.DB_REJ_N, self.u32(T.DB_REJ_N) + 1)
        if self.flaws & {"done_seq_off"}:
            return
        self.set32(T.DB_DONE_SEQ, rq)

    def loop_service(self):
        if not self.lane:
            return
        _, dst, ln, _, a7 = self.lane[0]
        if self.present and a7 == T.AS5600_ADDR7:
            raw = 2048
            v = float(raw)
            self.set32(T.OFF_SENSOR_MAP + dst * 4,
                       struct.unpack("<I", struct.pack("<f", v))[0])
            self.set32(T.DB_OK_N, self.u32(T.DB_OK_N) + 1)
        else:
            self.set32(T.DB_ERR_N, self.u32(T.DB_ERR_N) + 1)
            self.set32(T.DB_LAST_ERR, 3)
            if self.flaws & {"zero_on_fail"}:
                self.set32(T.OFF_SENSOR_MAP + dst * 4,
                           struct.unpack("<I", struct.pack("<f", 0.0))[0])

    def loop(self):
        self.loop_submit()
        self.loop_service()

    # ---- 协议面 ----
    def do_cmd(self, cmd, payload=b""):
        self.loop()          # ★ 模拟"主循环一直在跑"（真板上协议处理不占死主循环）
        if cmd == T.CMD_GET_VERSION:
            cap = CAP_FULL & ~T.CAP_DEVBIND if self.nodevbind else CAP_FULL
            return 0x00, struct.pack("<HH", 0x0200, cap)
        if cmd == T.CMD_ENGINE_STATUS:
            return 0x00, struct.pack("<IIIII", 0, 0, 0, 0, 0) + struct.pack("<H", 0) \
                + b"\0" + struct.pack("<I", SHM_BASE)
        if cmd == T.CMD_RESET:
            self.reset_state()
            return 0x00, b""
        if cmd == T.CMD_START:
            return 0x00, b""
        if cmd == T.CMD_READ_BURST:
            a, c = struct.unpack("<IH", payload[:6])
            off = a - SHM_BASE
            return 0x00, b"".join(struct.pack("<I", self.u32(off + i * 4)) for i in range(c))
        if cmd == T.CMD_WRITE_BURST:
            a, c = struct.unpack("<IH", payload[:6])
            off = a - SHM_BASE
            for i in range(c):
                v, = struct.unpack_from("<I", payload, 6 + i * 4)
                self.set32(off + i * 4, v)
            self.loop()
            return 0x00, struct.pack("<I", a)
        if cmd == T.CMD_WRITE:
            a, v = struct.unpack("<II", payload[:8])
            off = a - SHM_BASE
            self.set32(off, v)
            self.loop()
            return 0x00, struct.pack("<I", a)
        return 0xFF, b"bad cmd"


class FakeDcl:
    def __init__(self, f):
        self.f = f

    def send(self, cmd, payload=b""):
        sts, p = self.f.do_cmd(cmd, payload)
        return ("ACK" if sts == 0 else "NAK"), p

    def close(self):
        pass


def make_board(f):
    b = T.Board.__new__(T.Board)
    b.d = FakeDcl(f)
    b.verbose = False
    b.shm = SHM_BASE
    return b


def run(flaws=(), present=False, nodevbind=False, quiet=True, mode="words"):
    T._N_PASS = T._N_FAIL = T._N_SKIP = 0
    f = Fake(flaws, present=present, nodevbind=nodevbind)
    b = make_board(f)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        if not T.t0_link(b, False):
            return T._N_PASS, T._N_FAIL, T._N_SKIP, buf.getvalue(), False
        T.t1_zone(b)
        T.t2_reset(b)
        seq, used = T.t3_crc_and_accept(b, 0, 7, mode, T.DB_PERIOD_DEF)
        if used is None:
            return T._N_PASS, T._N_FAIL, T._N_SKIP, buf.getvalue(), False
        seq = T.t4_reject_fields(b, seq, used, 7, T.DB_PERIOD_DEF)
        seq, pres = T.t5_execute(b, seq, used, 7, T.DB_PERIOD_DEF)
        seq = T.t6_keep_old(b, seq, used, 7, T.DB_PERIOD_DEF, pres)
        T.t7_cleanup(b, seq, used, T.DB_PERIOD_DEF)
    return T._N_PASS, T._N_FAIL, T._N_SKIP, buf.getvalue(), True


CASES = [
    ("A 正确固件(未接器件)", (), False, False, 0),
    ("A 正确固件(接了 AS5600)", (), True, False, 0),
    ("B1 CRC 不校验(crc_off)", ("crc_off",), False, False, "必须>0(CRC 判据)"),
    ("B2 reset 漏登记(noreset_reg)", ("noreset_reg",), False, False, "必须>0(区存在自证)"),
    ("B3 失败写 0(zero_on_fail)", ("zero_on_fail",), False, False, "必须>0(保旧值)"),
    ("B4 拒绝不回 done_seq(done_seq_off)", ("done_seq_off",), False, False, "必须>0(判据可终止)"),
    ("B5 submit 未被主循环调用(nosubmit)", ("nosubmit",), False, False, "必须>0"),
]

if __name__ == "__main__":
    bad = 0
    for name, flaws, present, nodev, want in CASES:
        p, fl, sk, out, reached = run(flaws, present, nodev)
        if want == 0:
            verdict = (fl == 0) and reached
        else:
            verdict = (fl > 0)
        print("%-40s PASS=%2d FAIL=%2d SKIP=%2d  %s" % (
            name, p, fl, sk, "OK" if verdict else "★不符★"))
        if not verdict:
            bad += 1
            print("---- 输出 ----")
            print("\n".join(out.splitlines()[:40]))
    # B6: cap 无 DEVBIND 位 ⇒ T0.2 必须 SKIP 而不是 FAIL
    p, fl, sk, out, reached = run((), False, True)
    ok = reached and sk >= 1
    print("%-40s PASS=%2d FAIL=%2d SKIP=%2d  %s" % (
        "B6 cap 缺 DEVBIND 位(nodevbind)", p, fl, sk, "OK" if ok else "★不符★"))
    if not ok:
        bad += 1
    print("\n变异测试: %s" % ("全部符合预期" if bad == 0 else "%d 项不符" % bad))

    # B7: 固件若是**逐字节**口径 ⇒ words 模式必须被拒(T3 FAIL)，auto 模式必须自证并跑通
    p, fl, sk, out, reached = run(("crc_bytes",), False, False, mode="words")
    ok1 = (fl > 0) and not reached
    p2, fl2, sk2, out2, reached2 = run(("crc_bytes",), False, False, mode="auto")
    ok2 = reached2 and fl2 == 0 and "逐字节" not in out2
    print("B7 固件=逐字节 CRC, --crc-mode words: FAIL=%d reached=%s  %s"
          % (fl, reached, "OK" if ok1 else "★不符★"))
    print("B7 固件=逐字节 CRC, --crc-mode auto : FAIL=%d reached=%s  %s"
          % (fl2, reached2, "OK(auto 自证并切换)" if ok2 else "★不符★"))
    if not (ok1 and ok2):
        bad += 1
    print("\n最终: %s" % ("全部符合预期" if bad == 0 else "%d 项不符" % bad))
    sys.exit(1 if bad else 0)
