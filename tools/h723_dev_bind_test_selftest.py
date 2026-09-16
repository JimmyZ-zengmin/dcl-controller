# -*- coding: utf-8 -*-
"""h723_dev_bind_test_selftest.py — 对 h723_dev_bind_test.py 的**判据做变异测试**。

★ 为什么要有这个文件: 验收脚本自己的判据也必须**能失败**，否则"PASS N 项"只是
  一句无法反驳的话。这里用一份**照 src/dev_bind.c 语义写的假固件**(word-wise CRC、
  拒绝不半装载、失败保留旧值、reset 恢复 magic、**在飞换表必须暂存**) 跑全套 ⇒ 必须 PASS；
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
# AS5600 reg 0x0C/0x0D 读回的字节（假器件固定值: raw=3347=0xD13）
AS_B0, AS_B1 = 0x0D, 0x13


class Fake:
    """照 `src/dev_bind.c` 写的一台假固件。

    ★ 关键建模: **事务要跨拍**（`inflight_left` 递减到 0 才收尾）—— 没有这个,
      T8（在飞换表）在假固件上根本无从复现, B8 变异也就验不出来。
      这里用"一次 loop() = 一拍"的粗粒度近似: 真实是 100µs/拍、主循环 ~0.37ms/圈,
      但"发起节奏快于完成节奏"这个**结构**是一样的 ⇒ T8 的窗口存在性等价。
    """

    def __init__(self, flaws=(), present=False, nodevbind=False):
        self.m = bytearray(SHM_SIZE)
        self.flaws = set(flaws)
        self.present = present
        self.nodevbind = nodevbind
        self.reset_state()

    # ---- SHM 访问 ----
    def u32(self, off):
        return struct.unpack_from("<I", self.m, off)[0]

    def set32(self, off, v):
        struct.pack_into("<I", self.m, off, v & 0xFFFFFFFF)

    def setf(self, off, v):
        self.set32(off, struct.unpack("<I", struct.pack("<f", v))[0])

    def reset_state(self):
        self.m[:] = b"\0" * SHM_SIZE
        self.set32(0x00, CTRL_MAGIC)
        self.setf(T.OFF_SENSOR_MAP + 4, 0.0)       # SENSOR[1]
        self.setf(T.OFF_SENSOR_MAP + 7 * 4, 0.0)   # SENSOR[7]
        self.setf(T.OFF_SENSOR_MAP + 15 * 4, 0.0)  # SENSOR[15]
        # 事务状态机（C 静态量, 不在 SHM 里）
        self.lane = []          # 已生效的绑定 [(dev,dst,len,reg,addr7)]
        self.busy = 0
        self.inflight_lane = None   # ★ 发起时用的 lane（正确实现收尾时用它）
        self.inflight_addr = None   # ★ 在飞事务真正去访问的从机地址
        self.inflight_ok = False    # ★ 在飞事务的**结果**（由它自己的地址决定）
        self.inflight_left = 0
        self.idx = 0
        self.pend = None        # 暂存的待生效表 (lane, seq)
        self.pend_seq = 0
        self.last_start = 0
        self.tick = 0
        # 总线门（G6-2）: 空闲必须 owner=NONE(0) 且 refs=0 —— 固件自述的泄漏判据
        # （src/i2c_bb.h:51）。★ 正常模型下"发起时 +1 / 收尾时还 0"是**配对**的,
        # 所以任何时刻 refs ∈ {0, 1}; 1 = 有事务在飞, 不是泄漏。
        if not (self.flaws & {"reset_keeps_refs"}):
            self.refs = 0
            self.owner = 0
        if self.flaws & {"noreset_reg"}:
            return                                  # 漏登记: magic 全 0
        self.set32(T.OFF_I2C_XACT, T.IX_MAGIC_VAL)
        self.set32(T.DB_MAGIC, T.DB_MAGIC_VAL)
        self.set32(T.DB_PERIOD, T.DB_PERIOD_DEF)
        self.set32(T.DB_N_VALID, 0)

    # ---- 提交面（对应 dev_bind_submit）----
    def loop_submit(self):
        if self.flaws & {"nosubmit"}:
            return                                  # 模拟"submit 没被主循环调用"
        rq = self.u32(T.DB_REQ_SEQ)
        dn = self.u32(T.DB_DONE_SEQ)
        if rq == 0 or rq == dn:
            return
        if self.pend is not None and self.pend_seq == rq:
            return
        if (rq - dn) & 0x80000000:                  # 有符号判负 = 回退
            self.set32(T.DB_REJECT, 2)
            self.set32(T.DB_REJ_N, self.u32(T.DB_REJ_N) + 1)
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

        if rc != 0:
            self.set32(T.DB_REJECT, rc)
            self.set32(T.DB_REJ_N, self.u32(T.DB_REJ_N) + 1)
            if self.flaws & {"done_seq_off"}:
                return                              # 拒绝不回 done_seq
            self.set32(T.DB_DONE_SEQ, rq)
            return

        self.set32(T.DB_REJECT, 0)
        if self.busy == 0:
            self.apply(tmp, rq)
            return
        if self.flaws & {"swap_now"}:
            # ★ B8 变异: 有事务在飞也**立即换表** —— 收尾时就会用新表的 dst/len
            #   去解释旧事务的数据。第一版实现的缺陷就是这个。
            self.apply(tmp, rq)
            return
        self.pend = (tmp, rq)                       # 正确实现: 暂存, 延后 done_seq
        self.pend_seq = rq

    def apply(self, lane, seq):
        self.lane = list(lane)
        self.set32(T.DB_N_VALID, len(self.lane))
        self.set32(T.DB_DONE_SEQ, seq)

    # ---- 服务面（对应 dev_bind_service）----
    def loop_service(self):
        period = self.u32(T.DB_PERIOD)
        if period == 0 or period > T.DB_PERIOD_MAX:
            period = T.DB_PERIOD_DEF

        # ① 安全点换表（旧 lane 已用完, 新 lane 尚未被引用）
        if self.busy == 0 and self.pend is not None:
            self.apply(self.pend[0], self.pend[1])
            self.pend = None

        # ② 在飞 ⇒ 只做收尾
        # ★★★ 这里必须把**两件事分开**（这是 T8 要抓的那条缺陷的全部要害）:
        #   · "事务成功了没有 + 数据是什么" 属于**在飞事务本身**（由 `inflight_addr` 决定）;
        #   · "值落进哪个 SENSOR 槽、按几个字节解释" 才是 `s_lane[s_idx]` 的事。
        #   真固件正是这样写的（`i2c_sm_status()`/`i2c_sm_result(d,…)` 是在飞事务的;
        #   `s_lane[s_idx].dst/.len` 是**当时的表**）。第一版把表**立即**换掉,
        #   于是"旧事务的数据"被按"新表的 dst/len"落下去 ⇒ 静默写错槽。
        #   ★ 若把这两件事混在一起建模(我第一版就是这么写的), T8 会变成空判据:
        #     因为"新表指向的从机不存在"会被误用来判**在飞事务**失败, 错槽写入就不会发生。
        if self.busy:
            if self.inflight_left > 0:
                self.inflight_left -= 1
                return
            self.busy = 0
            # ★ 收尾**放门**（真固件由主循环的 `i2c_sm_service()` 做）。
            #   B9 `leak_refs` 变异: 不还 ⇒ 每完成一次事务泄漏 1 个引用,
            #   累积后 owner 永久卡在 SM、阻塞路径再也拿不到总线。
            if self.flaws & {"leak_refs"}:
                self.owner = 2
            else:
                self.refs = 0
                self.owner = 0
            if self.flaws & {"swap_now"}:
                lane = self.lane[self.idx] if self.idx < len(self.lane) else None
            else:
                lane = self.inflight_lane          # ★ 用**发起时**的 lane 解释数据
            if lane is None:
                self.set32(T.DB_SKIP_N, self.u32(T.DB_SKIP_N) + 1)
                return
            _, dst, ln, _, a7 = lane
            if self.inflight_ok:                   # 在飞事务的结果, 与 lane 无关
                fv = float(AS_B0) if ln == 1 else float((AS_B0 << 8) | AS_B1)
                self.setf(T.OFF_SENSOR_MAP + dst * 4, fv)
                self.set32(T.DB_OK_N, self.u32(T.DB_OK_N) + 1)
            else:
                self.set32(T.DB_ERR_N, self.u32(T.DB_ERR_N) + 1)
                self.set32(T.DB_LAST_ERR, 3)
                if self.flaws & {"zero_on_fail"}:
                    self.setf(T.OFF_SENSOR_MAP + dst * 4, 0.0)
            return

        # ③ 无绑定 ⇒ 不动作
        if not self.lane:
            return
        # ④ 速率闸
        if self.last_start != 0 and (self.tick - self.last_start) < period:
            return
        # ⑤ 发起（事务跨 8 拍）
        self.idx = 0
        self.inflight_lane = self.lane[0]
        self.inflight_addr = self.lane[0][4]
        self.inflight_ok = bool(self.present and self.inflight_addr == T.AS5600_ADDR7)
        self.last_start = self.tick
        self.busy = 1
        self.inflight_left = 8

    def loop(self):
        self.tick += 1
        self.loop_submit()
        self.loop_service()

    # ---- IXAC 事务区（G6-3 独立路径）----
    def ix_loop(self):
        rq = self.u32(T.IX_REQ_SEQ)
        if rq == 0 or rq == self.u32(T.IX_DONE_SEQ):
            return
        p = self.u32(T.IX_REQ)
        a7 = p & 0xFF
        self.set32(T.IX_STATUS, 2 if (self.present and a7 == T.AS5600_ADDR7) else 3)
        if self.present and a7 == T.AS5600_ADDR7:
            struct.pack_into("<BB", self.m, T.IX_DATA, AS_B0, AS_B1)
        self.set32(T.IX_DONE_SEQ, rq)

    # ---- 协议面 ----
    def do_cmd(self, cmd, payload=b""):
        self.loop()          # ★ 模拟"主循环一直在跑"（真板上协议处理不占死主循环）
        self.ix_loop()
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
        if cmd == 0x39:                              # 0x39 op=22 sub=2: 只查询总线门
            op = payload[0] if len(payload) >= 1 else 0
            sub = payload[1] if len(payload) >= 2 else 0
            if op == 22 and sub == 2:
                st = 1 if self.busy else 2           # 1=BUSY(在飞) 2=OK
                return 0x00, struct.pack("<9I", 0, 0, 0, 0, st, 0, 0, 0, 0)
            return 0xFF, b"bad sub"
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


def run(flaws=(), present=False, nodevbind=False, mode="words", rounds=4, observe=0.03):
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
        # ★ 必须接住 t7_cleanup 返回的 seq —— 丢了它 T8 就会用**已生效的旧序号**提交,
        #   固件按"幂等"直接忽略 ⇒ T8 整段变成假判据（本次自测抓到过）。
        seq = T.t7_cleanup(b, seq, used, T.DB_PERIOD_DEF)
        T.t8_inflight_swap(b, seq, used, 7, T.DB_PERIOD_DEF, rounds, observe)
    return T._N_PASS, T._N_FAIL, T._N_SKIP, buf.getvalue(), True


CASES = [
    ("A 正确固件(未接器件)", (), False, False, 0),
    ("A 正确固件(接了 AS5600)", (), True, False, 0),
    ("B1 CRC 不校验(crc_off)", ("crc_off",), False, False, ">0"),
    ("B2 reset 漏登记(noreset_reg)", ("noreset_reg",), False, False, ">0"),
    ("B3 失败写 0(zero_on_fail)", ("zero_on_fail",), False, False, ">0"),
    ("B4 拒绝不回 done_seq(done_seq_off)", ("done_seq_off",), False, False, ">0"),
    ("B5 submit 未被主循环调用(nosubmit)", ("nosubmit",), False, False, ">0"),
    ("B8 立即换表(swap_now) ★T8 专测", ("swap_now",), True, False, ">0"),
]

if __name__ == "__main__":
    bad = 0
    for name, flaws, present, nodev, want in CASES:
        p, fl, sk, out, reached = run(flaws, present, nodev)
        verdict = (fl == 0 and reached) if want == 0 else (fl > 0)
        print("%-38s PASS=%2d FAIL=%2d SKIP=%2d  %s" % (
            name, p, fl, sk, "OK" if verdict else "★不符★"))
        if not verdict:
            bad += 1
            print("---- 输出 ----")
            print("\n".join(out.splitlines()[-40:]))

    # B6: cap 无 DEVBIND 位 ⇒ T0.2 SKIP 而非 FAIL
    p, fl, sk, out, reached = run((), False, True)
    ok = reached and sk >= 1
    print("%-38s PASS=%2d FAIL=%2d SKIP=%2d  %s" % (
        "B6 cap 缺 DEVBIND 位(nodevbind)", p, fl, sk, "OK" if ok else "★不符★"))
    if not ok:
        bad += 1

    # B7: 固件若是逐字节口径 ⇒ words 必红, auto 必须自证并跑通
    p, fl, sk, out, reached = run(("crc_bytes",), False, False, mode="words")
    ok1 = (fl > 0) and not reached
    p2, fl2, sk2, out2, reached2 = run(("crc_bytes",), False, False, mode="auto")
    ok2 = reached2 and fl2 == 0
    print("B7 固件=逐字节 CRC, mode=words     FAIL=%d reached=%s  %s"
          % (fl, reached, "OK" if ok1 else "★不符★"))
    print("B7 固件=逐字节 CRC, mode=auto      FAIL=%d reached=%s  %s"
          % (fl2, reached2, "OK(auto 自证并切换)" if ok2 else "★不符★"))
    if not (ok1 and ok2):
        bad += 1

    print("\n变异测试: %s" % ("全部符合预期" if bad == 0 else "%d 项不符" % bad))
    sys.exit(1 if bad else 0)
