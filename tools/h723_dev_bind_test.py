#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_dev_bind_test.py — G6-4 **具名设备绑定表**的上机验收

  ★ 验收对象: `docs/REF-program-contract.md §3.8`（具名设备绑定表 / G6-4）
    实现侧: `src/dev_bind.c` / `src/dev_bind.h` / `src/engine.h` 的 `OFF_DEV_BIND` 段。
  ★ 只用**协议(串口)**，**不用 pyocd** —— 本项目纪律: 观测先走协议内通道
    （调试器会停引擎 ⇒ "观测改变被测对象"，铁律 0 不允许）。

## 为什么每条判据都能失败（这是本脚本存在的理由，不是套话）
逐条列出"它靠什么才能失败"，凡是没有失败路径的一律不写成判据:

  T1 区存在自证        —— 失败路径: `cold_start_reset()` 漏登记 ⇒ magic 变 0。
  T2 RESET 后 magic 仍在 —— 同上，但**只在 0x13 之后**才暴露。这是"新增 SHM 域
                          必须登记到单一入口"那条纪律的判据本体：
                          只在开机写一次的实现在 T1 通过、在 T2 失败。
  T3 合法表被接受       —— 失败路径: CRC 算法/字节序与固件不一致 ⇒ 永远 reject=1；
                          或 submit 压根没被主循环调用 ⇒ done_seq 不跟上(超时)。
                          ★ 先做一次**坏 CRC 的阳性对照**(T4)，否则"永远拒绝"
                            也能让 T3 看起来"有判据"。
  T4 坏 CRC 被拒        —— 失败路径: 固件不校验 CRC ⇒ reject=0（危险：半装载）。
                          同时判 `n_valid` **不变** ⇒ 抓"拒绝了但还是把表换了"。
                          再判 `done_seq == req_seq` ⇒ 抓"拒绝后不回 done_seq
                          ⇒ 上位机永远等下去"（判据不能终止）。
  T5 字段越界逐项       —— 每个码都是一条独立判据，且**每次都用递增 seq + 正确 CRC**
                          （否则失败的会是 T4 那条 CRC 判据，不是字段判据 —— 分不清）。
  T6 执行/失败两条路    —— 失败路径: 提交成功但服务方没跑（两个计数器都不动）。
                          ★ "err 在涨"与"什么都没发生"必须能区分，这正是契约
                            §3.8.3 要 `n_valid`(登记数) 与 `lanes_ok_n`(执行数)
                            两个观测量的理由。没接器件时 err 在涨 = PASS(合理)，
                            但必须**报出来是哪一种**，不许把前者当后者。
  T7 失败保旧值         —— 失败路径: 固件把读失败写成 0 ⇒ 哨兵被 0 覆盖。
                          ★ 前置条件: 必须确认 err_n **真的在涨** —— 否则"绑定从未
                            执行"也会让哨兵幸存，那条判据就是**空判据**。
  T8 在飞换表           —— 失败路径: 提交"立即换表"而不是"暂存待生效"⇒ 收尾时会用
                          **新表**的 `dst/len` 去解释**旧事务**的数据 ⇒ 读进**错的 SENSOR 槽**
                          （`reject=0`、`n_valid` 合法、计数照涨, **没有任何错误迹象**）。
                          ★ 构造: `DB_PERIOD=1`（合法值）+ 事务跨 8 拍 ⇒ 几乎总有事务在飞;
                            表 B 指向**不存在**的从机 ⇒ 它生效后永不写 `SENSOR[15]`,
                            于是 `SENSOR[15] != 哨兵` 就只可能是"用错 lane"。
                          ★ 本轮判据正方向也必须能失败: 每轮都要求 `err_n` 在涨
                            （证明 B 真的在执行, 而不是"B 没生效所以没写"）。
  T9 收尾副作用可观测    —— 失败路径: 任何 `acquire` 后不配对 `release` ⇒ 总线门
                          `refs != 0` / `owner != NONE`（由 B9 变异证明能红）。
                          ★ 为什么必须有它（**判据缺口, 由 regress-verify 实测暴露**）:
                            本脚本原先到 T7 为止**只复原了绑定表**, 没复原**总线门**。
                            若收尾把门留在 `owner=2(SM)`, 紧接着跑
                            `tools/h723_i2c_gate_test.py` 会 **4/5 FAIL**（起点占不住
                            ⇒ G2/G4/G5 连带倒）, 而单独复位后复跑 2 次均 5/5 —— **假失败**。
                            真因可能根本不在 dev_bind（本项目曾有 `i2c_sm_request` 先拿门
                            后验参 ⇒ 非法分支 return 不留门 的泄漏）。
                            ⇒ 本判据**只回答**"门干不干净"（外部可观测状态）, 并额外判
                              "0x13 RESET 能否清场"（下一个套件能不能自救）; 根因由
                              T9 的定案实验去分（纯 SM 30 次事务 refs 增量是否为 0）。
                          ★ 默认**收尾发一次 0x13 RESET**（`--no-reset-at-end` 可关）:
                            代价是清掉整个 SHM, 换来"不把副作用留给下一个套件"。

## 用法
    python tools/h723_dev_bind_test.py                  # 自动找板子（按能力字）
    python tools/h723_dev_bind_test.py --port COM7 -v
    python tools/h723_dev_bind_test.py --dst 7          # 目标 SENSOR 槽（默认 7，理由见下）
    python tools/h723_dev_bind_test.py --crc-mode bytes # 若契约按"逐字节 FNV"落地
    python tools/h723_dev_bind_test.py --skip-reset     # 不跑 0x13（T2 会 SKIP）
    python tools/h723_dev_bind_test.py --no-reset-at-end # 收尾也不复位（想看 SHM 残留时）
  ★ **跑完会发一次 0x13 RESET**（T9）: 本脚本会经过 `DB_PERIOD=1` / ghost 绑定等
    加速状态, 收尾把门与绑定都清干净, 后面的套件不必依赖执行顺序。
    若你要保留 SHM（SENSOR 历史值 / 计数器）做诊断, 加 `--no-reset-at-end`,
    但**下一个用同一条总线的套件要先自己复位**（T9 会把当时的 refs/owner 打出来）。

## 已知**未覆盖**（写在这里，免得被当成"全覆盖"）
  · `DB_RC_SEQ = 2`（req_seq 回退）**本脚本不测**。原因不是判不了，而是它有副作用：
    回退提交会被固件拒绝且**故意不回 `done_seq`**（保单调性），于是 `DB_REQ_SEQ`
    停在低位，固件此后**每一圈**都判"回退"并累加 `DB_REJ_N` —— 直到有更新的 seq 到来。
    要让"回退被拒"这条判据能终止，必须先约定"谁负责复位 REQ_SEQ"。在契约写清之前，
    本脚本不制造这个状态（会污染后面所有依赖 `rej_n` 归因的判据）。
  · `DB_RC_NOSEQ = 8`（req_seq == 0）在实现上是**静默 return**（视为"尚未提交"），
    不是拒绝 ⇒ 没有可观测的码，无法作为判据。
  · `DB_RC_DST = 6` 见上（不可构造）。

## 两个**刻意偏离**任务书的点（都是读源码后改的，附证据）
 ① 目标 SENSOR 槽默认用 **7**，不用 1。
    `src/as5600.c:39-40` 的阻塞轮询每 10ms 把 **SENSOR[0]=RAW / SENSOR[1]=DEG** 覆写一遍。
    拿 SENSOR[1] 做"保旧值"哨兵，在**真接了 AS5600 的板子上必然误 FAIL**
    （哨兵被真实角度覆盖），而在没接的板子上才通过 —— 那是"判据依赖被测环境"。
    槽 0=AS5600 RAW / 1=AS5600 DEG / 2=HIL 反馈 / 3..6=DI / 8..10=AI，
    空闲槽 7 与 11..15 无其他写者 ⇒ 默认 7（`--dst` 可改）。
 ② `dst=16` **不可构造**（字段只有 4 bit ⇒ 合法域 0..15），
    所以 `DB_RC_DST=6` 在两端都是**死码**。本脚本**不**为它造判据（那是空判据），
    只把它作为"未覆盖"明确打出来，并改用 **dst=15 合法边界**做正对照。

## CRC 算法（★ 这里与任务书的口述不一致，以源码为准）
  固件 `src/dev_bind.c:31-39` 的 `db_crc()` 是 **对 8 个 u32 逐"字"** 做 FNV-1a:
      `h ^= SHM_U32(entry_i); h *= 16777619u`
  而不是"把 8 个 u32 摊成 32 个 LE 字节逐字节做"。项目里既有先例是同一个：
  `src/blackbox.c:195-200` 的 `bb_map_sum()`（`h ^= map[i]`）与 PC 侧
  `tools/sd_log_read.py:130-135` 的 `map_sum()` 都是**逐字**。
  ⇒ 默认 `--crc-mode words`（照源码）；`bytes` 保留给"契约按逐字节落地"的分支。
     另: `auto` 模式会在 T3 用两种算法各试一次并**报告固件实际吃哪一种**
     （这本身是一个能让失败的判据: 两种都被拒 ⇒ CRC 通道或 submit 通路有问题）。
"""

# ★ Windows 控制台默认 GBK: 脚本 print 的个别字符 (⇒ / ⊇ 等) 会让脚本在**打印结论**
#   那一步抛 UnicodeEncodeError 而崩掉 —— 数据都量到了却看不到。统一改成"永不抛"。
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from h723_client import Dcl, find_board, engine_status, link_alive
except ImportError as e:
    print("!! 需要 tools/ 下的 h723_client.py / h723_modbus.py (pyserial): %s" % e)
    sys.exit(2)

# ══════════════════ 协议常量 ══════════════════
CMD_GET_VERSION   = 0x01
CMD_START         = 0x11
CMD_RESET         = 0x13
CMD_WRITE         = 0x21      # [addr:u32][value:u32] → ACK [addr:u32]
CMD_READ_BURST    = 0x22      # [addr:u32][count:u16] → ACK [count×u32]
CMD_WRITE_BURST   = 0x23      # [addr:u32][count:u16][count×u32] → ACK [addr:u32]
CMD_ENGINE_STATUS = 0x38

# ★ 必备位掩码（认口用；**不是**等值 —— 见 h723_client.link_alive 的 docstring）
CAP_REQUIRED      = 0x0DF7
CAP_DEVBIND       = 0x1000    # G6-4 新增位（宣称值应为 0x1DF7）

# ══════════════════ SHM 布局（必须与 src/engine.h 一致）══════════════════
OFF_SENSOR_MAP = 0x0040

OFF_I2C_XACT     = 0x7200                     # IXAC 事务区（G6-3）
IX_MAGIC       = OFF_I2C_XACT + 0
IX_REQ_SEQ     = OFF_I2C_XACT + 4
IX_DONE_SEQ    = OFF_I2C_XACT + 8
IX_STATUS      = OFF_I2C_XACT + 12
IX_PHASE       = OFF_I2C_XACT + 16
IX_TICKS       = OFF_I2C_XACT + 20
IX_LAST_OK_SEQ = OFF_I2C_XACT + 24
IX_REQ         = OFF_I2C_XACT + 28
IX_DATA        = OFF_I2C_XACT + 32             # u8[8]
IX_REQ_N       = OFF_I2C_XACT + 40
IX_OK_N        = OFF_I2C_XACT + 44
IX_NAK_N       = OFF_I2C_XACT + 48
IX_STUCK_N     = OFF_I2C_XACT + 52
IX_GATE_N      = OFF_I2C_XACT + 56
IX_BUSY_N      = OFF_I2C_XACT + 60
IX_MAGIC_VAL   = 0x43415849                    # 'IXAC'

OFF_DEV_BIND = 0x7300                          # G6-4 具名设备绑定表
DB_MAGIC    = OFF_DEV_BIND + 0
DB_N_VALID  = OFF_DEV_BIND + 4
DB_CRC      = OFF_DEV_BIND + 8
DB_REQ_SEQ  = OFF_DEV_BIND + 12
DB_DONE_SEQ = OFF_DEV_BIND + 16
DB_REJECT   = OFF_DEV_BIND + 20
DB_PERIOD   = OFF_DEV_BIND + 24
DB_OK_N     = OFF_DEV_BIND + 28
DB_ENTRIES  = OFF_DEV_BIND + 32                # u32[8]
DB_ERR_N    = OFF_DEV_BIND + 64
DB_LAST_ERR = OFF_DEV_BIND + 68
DB_SKIP_N   = OFF_DEV_BIND + 72                # 轮空数（总线门忙 / 被抢先收尾）
DB_REJ_N    = OFF_DEV_BIND + 76                # 表被拒次数（上传面）
OFF_DEV_BIND_SZ = 0x50
DB_SLOTS    = 8
DB_MAGIC_VAL  = 0x444E4244                     # 'DBND'
DB_PERIOD_DEF = 10
DB_PERIOD_MAX = 1000

# 拒绝原因码（src/dev_bind.h）
DB_RC_NAME = {0: "OK", 1: "CRC", 2: "SEQ", 3: "DEV", 4: "ADDR",
              5: "LEN", 6: "DST", 8: "NOSEQ"}

# 条目字段
DB_DEV_EMPTY  = 0
DB_DEV_I2C_RD = 1
DB_MAX_ADDR7  = 0x7F

# 目标器件：AS5600 RAW ANGLE（契约 §3.8.2 的具名例子）
AS5600_ADDR7, AS5600_REG, AS5600_LEN = 0x36, 0x0C, 2
# 一个**必然不存在**的从机地址（用于 T7）
GHOST_ADDR7 = 0x50

# i2c_sm 状态码（src/i2c_sm.h）
I2C_SM_ST_BUSY = 1
I2C_SM_ST_OK = 2
I2C_SM_ST_NAK = 3
I2C_SM_ST_GATE_BUSY = 6
I2C_SM_ST = {0: "IDLE", 1: "BUSY", 2: "OK", 3: "NAK",
             4: "SCL_STUCK", 5: "BADARG", 6: "GATE_BUSY"}

# 总线门持有者（src/i2c_bb.h:44-46）
I2C_OWNER_NAME = {0: "NONE", 1: "BLOCKING", 2: "SM"}

# ══════════════════ 判据记账 ══════════════════
_N_PASS = _N_FAIL = _N_SKIP = 0


def record(name, ok, detail=""):
    global _N_PASS, _N_FAIL
    if ok is None:
        globals()["_N_SKIP"] += 1
        print("  [SKIP] %s%s" % (name, ("  %s" % detail) if detail else ""))
        return None
    if ok:
        _N_PASS += 1
    else:
        _N_FAIL += 1
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  %s" % detail) if detail else ""))
    return ok


def skip(name, detail=""):
    global _N_SKIP
    _N_SKIP += 1
    print("  [SKIP] %s%s" % (name, ("  %s" % detail) if detail else ""))


# ══════════════════ FNV-1a：两种口径，都与固件对齐过 ══════════════════
def db_crc_words(entries):
    """★ 与 `src/dev_bind.c:db_crc()` **逐字同构**的实现。

    固件源码（照抄）:
        uint32_t h = 2166136261u;
        for (i = 0..7) { h ^= SHM_U32(entries + i*4u); h *= 16777619u; }
    `SHM_U32` = `*(volatile uint32_t*)` ⇒ 取的是**一个 u32 值**（不是 4 个字节），
    该值的字节序由 CPU 决定，但**异或的是这个 32 位值本身** ⇒ PC 侧照 u32 取值即可，
    不需要关心 LE/BE。这是"逐字"口径（= bb_map_sum 的口径）。
    """
    h = 2166136261
    for w in entries:
        h = ((h ^ (w & 0xFFFFFFFF)) * 16777619) & 0xFFFFFFFF
    return h


def db_crc_bytes(entries):
    """★ 另一种口径：把 8 个 u32 按 **LE 摊成 32 字节**后逐字节 FNV-1a。

    ⚠ 这**不是**当前固件的算法（见 db_crc_words 的说明）。保留它只是为了
      "契约若按逐字节落地"时能一个开关切过去，并能在 T3 里**实测**固件吃哪一种。
    """
    h = 2166136261
    for b in struct.pack("<%dI" % len(entries), *entries):
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


CRC_FN = {"words": db_crc_words, "bytes": db_crc_bytes}


def entry(dev, dst, ln, reg, addr7):
    """按契约 §3.8.2 打包 u32。★ 越界字段在这里就被掩掉（4/8 bit 字段）——
    所以 `dst=16` 这种"越界值"根本**进不了编码**（见文件头的偏离②）。"""
    return (((dev & 0xF) << 28) | ((dst & 0xF) << 24) | ((ln & 0xFF) << 16)
            | ((reg & 0xFF) << 8) | (addr7 & 0xFF))


def entry_note(e):
    dev = (e >> 28) & 0xF
    dst = (e >> 24) & 0xF
    ln = (e >> 16) & 0xFF
    reg = (e >> 8) & 0xFF
    a7 = e & 0xFF
    if dev == DB_DEV_EMPTY:
        return "*空槽*"
    return "dev=%d dst=%d len=%d reg=0x%02X addr7=0x%02X" % (dev, dst, ln, reg, a7)


# ══════════════════ 总线 IO（全部走协议）══════════════════
class Board:
    def __init__(self, port, verbose=False):
        self.d = Dcl(port)
        self.verbose = verbose
        self.shm = None

    def send(self, cmd, payload=b""):
        sts, p = self.d.send(cmd, payload)
        if self.verbose:
            print("      %02X -> %s %s" % (cmd, sts, p[:32].hex()))
        return sts, p

    def locate_shm(self):
        st = engine_status(self.d)
        if not st or not st.get("shm"):
            return None
        self.shm = st["shm"]
        return self.shm

    def rd(self, off, count):
        """0x22 读 count 个 u32（off = SHM 偏移）"""
        assert count <= 256
        sts, p = self.send(CMD_READ_BURST,
                           struct.pack("<IH", self.shm + off, count))
        if sts != "ACK" or len(p) < count * 4:
            return None
        return list(struct.unpack("<%dI" % count, p[:count * 4]))

    def wr(self, off, words):
        """0x23 写 n 个 u32"""
        assert len(words) <= 256
        sts, p = self.send(CMD_WRITE_BURST,
                           struct.pack("<IH", self.shm + off, len(words))
                           + struct.pack("<%dI" % len(words), *words))
        return sts == "ACK"

    def wr1(self, off, val):
        """0x21 写 1 个 u32"""
        sts, p = self.send(CMD_WRITE, struct.pack("<II", self.shm + off, val))
        return sts == "ACK"

    def rd_f(self, off):
        w = self.rd(off, 1)
        return None if w is None else struct.unpack("<f", struct.pack("<I", w[0]))[0]

    def wr_f(self, off, v):
        return self.wr1(off, struct.unpack("<I", struct.pack("<f", v))[0])

    def close(self):
        self.d.close()


# ══════════════════ 绑定表快照 / 提交 ══════════════════
def snap(b):
    """一次 0x22 读回整个提交块（80B = 20 字）—— 保证各字段同一次采样。"""
    w = b.rd(OFF_DEV_BIND, OFF_DEV_BIND_SZ // 4)
    if w is None or len(w) < 20:
        return None
    return dict(magic=w[0], n_valid=w[1], crc=w[2], req_seq=w[3], done_seq=w[4],
                reject=w[5], period=w[6], ok_n=w[7], entries=list(w[8:16]),
                err_n=w[16], last_err=w[17], skip_n=w[18], rej_n=w[19])


def snap_str(s):
    if s is None:
        return "(读不到)"
    return ("magic=0x%08X n_valid=%d crc=0x%08X req=%d done=%d reject=%d(%s) "
            "period=%d ok_n=%d err_n=%d last_err=%d(%s) rej_n=%d" % (
                s["magic"], s["n_valid"], s["crc"], s["req_seq"], s["done_seq"],
                s["reject"], DB_RC_NAME.get(s["reject"], "?"), s["period"],
                s["ok_n"], s["err_n"], s["last_err"],
                I2C_SM_ST.get(s["last_err"], "?"), s["rej_n"]))


def submit(b, entries, seq, mode="words", period=None, corrupt=False):
    """把表写进 SHM 并提交。★ 写序: entries → crc → (period) → **req_seq 最后**。

    为什么要这个顺序: 固件的提交触发条件是 `req_seq != done_seq`。若先写 req_seq，
    固件可能在 entries 只写了一半时就受理 —— 而那种"半个表"正是 CRC 要防的东西。
    seq **最后**写 ⇒ 固件看到 seq 变化时, 表与 CRC 一定都已就位。
    """
    ents = (list(entries) + [0] * DB_SLOTS)[:DB_SLOTS]
    crc = CRC_FN[mode](ents)
    if corrupt:
        crc ^= 0xDEADBEEF          # ★ 只改 CRC，其它字段都是合法值:
        #                            这样"被拒"只可能是 CRC 判据的功劳（可归因）
    if not b.wr(DB_ENTRIES, ents):
        return None, "0x23 写 entries 被拒"
    if not b.wr1(DB_CRC, crc):
        return None, "0x21 写 crc 被拒"
    if period is not None and not b.wr1(DB_PERIOD, period):
        return None, "0x21 写 period 被拒"
    if not b.wr1(DB_REQ_SEQ, seq):
        return None, "0x21 写 req_seq 被拒"
    return crc, None


def wait_done(b, seq, timeout=2.0):
    """等 `done_seq == seq`。★ 有上界 —— 绝不出现"等一个永远不来的条件"。
    返回 (ok, 最后一次快照)。"""
    end = time.time() + timeout
    s = None
    while True:
        s = snap(b)
        if s is not None and s["done_seq"] == seq:
            return True, s
        if time.time() >= end:
            return False, s
        time.sleep(0.01)


def wait_counter_growth(b, key, base, timeout=3.0, step=0.05):
    """等 snap()[key] > base（有上界）。返回 (是否增长, 最后一次快照)"""
    end = time.time() + timeout
    s = None
    while time.time() < end:
        s = snap(b)
        if s is not None and s[key] > base:
            return True, s
        time.sleep(step)
    if s is None:
        s = snap(b)
    return False, s


# ══════════════════ 独立第二路径: IXAC 事务区（G6-3）══════════════════
_ix_seq = 0


def ix_read(b, addr7, reg, ln, timeout=2.0):
    """走 G6-3 的 SHM 事务区独立做一次 I2C 读，返回 (status, bytes) 或 (None, None)。

    ★ 为什么值得写这段: 它是与 dev_bind **完全独立的第二条路径**（不同的服务函数、
      不同的握手字段）。两条路径读到同一个值才算数 —— 这是本项目"两条独立路径
      对上才算数"的既有纪律，也把"dev_bind 只是把某个常量写进了 SENSOR"这种
      假 PASS 排除掉。
    """
    global _ix_seq
    _ix_seq += 1
    seq = _ix_seq
    packed = (addr7 & 0xFF) | (1 << 8) | ((reg & 0xFF) << 16) | ((ln & 0xFF) << 24)
    if not b.wr1(IX_REQ, packed):
        return None, None
    if not b.wr1(IX_REQ_SEQ, seq):
        return None, None
    end = time.time() + timeout
    while time.time() < end:
        w = b.rd(IX_DONE_SEQ, 6)          # done/status/phase/ticks/last_ok/data[0..1]
        if w is not None and w[0] == seq:
            status = w[1]
            dw = b.rd(IX_DATA, 2)         # u8[8] → 2 个 u32
            if dw is None:
                return status, None
            raw = struct.pack("<II", dw[0], dw[1])
            return status, raw[:max(0, min(8, ln))]
        time.sleep(0.01)
    return None, None


# ══════════════════ 各项验收 ══════════════════
def t0_link(b, want_start):
    print("\n── T0 链路活性（后续全部判据的前提）──")
    sts, p = b.send(CMD_GET_VERSION)
    fw = cap = None
    if sts == "ACK" and len(p) >= 4:
        fw, cap = struct.unpack("<HH", p[:4])
    ok = (fw is not None) and ((cap & CAP_REQUIRED) == CAP_REQUIRED)
    record("T0.1 0x01 → ACK + cap 含全部必备位(掩码 0x%04X)" % CAP_REQUIRED, ok,
           "fw=0x%04X cap=0x%04X" % (fw or 0, cap or 0))
    # ★ 这条是"新增能力位不破坏认口"的现场自证: 固件若已加 DEVBIND 位，
    #   cap 应为 0x1DF7；掩码语义下 T0.1 仍须通过（等值语义下会 FAIL）。
    if cap is not None:
        has = (cap & CAP_DEVBIND) != 0
        print("        能力字 0x%04X: %s" % (cap, ", ".join(
            n for bit, n in ((0x0001, "MULTICYCLE"), (0x0002, "HOTRELOAD"),
                             (0x0004, "PERSISTENT"), (0x0010, "WIRE2_FLAG"),
                             (0x0020, "VERINFO"), (0x0040, "SEQ"),
                             (0x0080, "FORCE"), (0x0100, "COMM"),
                             (0x0400, "AI"), (0x0800, "MACRO"),
                             (0x1000, "DEVBIND")) if cap & bit) or "(无)"))
        print("        DCL_CAP_DEVBIND(0x1000): %s"
              % ("已声明（= 契约 §3.8.7 要求）" if has else "★未声明★"))
        if not has:
            skip("T0.2 cap 含 DCL_CAP_DEVBIND(0x1000)",
                 "未声明 —— 契约 §3.8.7 要求三处同步; 本脚本其余项仍可跑")
    if not ok:
        print("  !! 链路不活 ⇒ 后续无法判定（先跑 tools/h723_revive.py 看诊断）")
        return False
    st = engine_status(b.d)
    if not st:
        record("T0.3 0x38 ENGINE_STATUS 可解析", False, "读不到")
        return False
    if b.locate_shm() is None:
        record("T0.3 0x38 给出 SHM 基址", False, "shm=0")
        return False
    record("T0.3 0x38 给出 SHM 基址", True, "g_shm=0x%08X" % b.shm)
    if want_start:
        print("        (--start 给定 ⇒ 发 0x11 START；注意空程序下 START 不产生输出)")
        b.send(CMD_START)
        time.sleep(0.05)
    return True


def t1_zone(b):
    print("\n── T1 区存在自证（两条 magic）──")
    w = b.rd(OFF_I2C_XACT, 1)
    ix = None if w is None else w[0]
    record("T1.1 IX_MAGIC == 'IXAC'（G6-3 事务区存在）",
           ix == IX_MAGIC_VAL, "读到 0x%08X" % (ix or 0))
    s = snap(b)
    record("T1.2 DB_MAGIC == 'DBND'（G6-4 绑定表存在）",
           s is not None and s["magic"] == DB_MAGIC_VAL,
           "读到 0x%08X" % (s["magic"] if s else 0))
    record("T1.3 DB_PERIOD 默认值 = %d 拍" % DB_PERIOD_DEF,
           s is not None and s["period"] == DB_PERIOD_DEF,
           "读到 %s" % (s["period"] if s else "?"))
    if s:
        print("        %s" % snap_str(s))
    return s


def t2_reset(b):
    print("\n── T2 ★ 0x13 RESET 后两条 magic 仍须在（'登记到 cold_start_reset 单一入口'的判据）──")
    print("        理由: 只在开机写一次 magic 的实现在 T1 通过、**只在 RESET 之后**暴露。")
    sts, _ = b.send(CMD_RESET)
    record("T2.1 0x13 RESET 被受理", sts == "ACK", "sts=%s" % sts)
    time.sleep(0.3)                      # 等主循环跑完 cold_start_reset 之后的几圈
    w = b.rd(OFF_I2C_XACT, 1)
    ix = None if w is None else w[0]
    record("T2.2 RESET 后 IX_MAGIC == 'IXAC'", ix == IX_MAGIC_VAL,
           "读到 0x%08X" % (ix or 0))
    s = snap(b)
    record("T2.3 RESET 后 DB_MAGIC == 'DBND'",
           s is not None and s["magic"] == DB_MAGIC_VAL,
           "读到 0x%08X" % (s["magic"] if s else 0))
    record("T2.4 RESET 后 DB_PERIOD 回到默认 %d（dev_bind_reset 真的跑过）"
           % DB_PERIOD_DEF,
           s is not None and s["period"] == DB_PERIOD_DEF,
           "读到 %s" % (s["period"] if s else "?"))
    record("T2.5 RESET 后 req_seq/done_seq 归 0（干净的提交基线）",
           s is not None and s["req_seq"] == 0 and s["done_seq"] == 0,
           "req=%s done=%s" % (s["req_seq"] if s else "?", s["done_seq"] if s else "?"))
    record("T2.6 RESET 后 n_valid == 0（旧绑定已解除）",
           s is not None and s["n_valid"] == 0,
           "n_valid=%s" % (s["n_valid"] if s else "?"))
    return s


def t3_crc_and_accept(b, seq, dst, mode, period):
    """T3 = CRC 阳性对照 + 合法提交。返回 (下一个可用 seq, 实际吃下的 crc 模式)"""
    print("\n── T3 ★ 坏 CRC 的阳性对照 + 合法表被接受（'CRC 判据真的在门' + 'CRC 口径自证'）──")
    # ① 阳性对照: 先故意错 CRC。若连它都被接受 ⇒ CRC 判据不存在（危险）。
    #   ★ mode 可能是 'auto'（还没定口径）⇒ 探针必须落在一个**具体**口径上。
    #     这里选哪个都无所谓: CRC 已被 corrupt 标志破坏, 两种口径都会错。
    probe_mode = mode if mode in CRC_FN else "words"
    bad_entries = [entry(DB_DEV_I2C_RD, dst, AS5600_LEN, AS5600_REG, AS5600_ADDR7)]
    dead_entries = [entry(DB_DEV_I2C_RD, dst, 2, 0x0C, GHOST_ADDR7)]
    base_rej = (snap(b) or {}).get("rej_n", 0)
    seq += 1
    crc, err = submit(b, dead_entries, seq, mode=probe_mode, period=period, corrupt=True)
    if err:
        record("T3.1 提交坏 CRC 表（协议写入本身成功）", False, err)
        return seq, None
    ok, s = wait_done(b, seq, timeout=2.0)
    record("T3.1 坏 CRC 表在 2s 内被受理/回过（拒绝也要终止）", ok,
           "req=%d done=%s reject=%s(%s)" % (seq, s["done_seq"] if s else "?",
                                             s["reject"] if s else "?",
                                             DB_RC_NAME.get(s["reject"], "?") if s else "?"))
    if not ok:
        print("        !! done_seq 不跟上 —— 固件的提交通路根本没在跑（见下方诊断提示）")
        return seq, None
    record("T3.2 ★ 坏 CRC 必须被拒不静默接受（reject == 1 CRC）",
           s["reject"] == 1 and s["rej_n"] > base_rej,
           "reject=%d(%s), rej_n %d→%d（计数也在涨 ⇒ 这个码是**本次**产生的, 不是旧值）"
           % (s["reject"], DB_RC_NAME.get(s["reject"], "?"), base_rej, s["rej_n"]))
    record("T3.3 坏 CRC 被拒后 n_valid 仍为 0（初始态未被半装载）",
           s["n_valid"] == 0, "n_valid=%d" % s["n_valid"])

    # ② 合法表: 用请求的口径；若没指定（auto）就逐个候选口径试，并**报告固件吃哪一种**。
    #   ★ auto 模式下"某个候选口径被拒"是**过程信息**，不是 FAIL —— 口径此刻还没定，
    #     把它记成 FAIL 会让"auto 自证成功"的跑法平白带一个红项（本次变异测试抓到过）。
    #     只有**所有候选都被拒**才算 FAIL（那时判据是"提交通路/口径一致"这件事本身）。
    modes = [mode] if mode in CRC_FN else ["words", "bytes"]
    used = None
    rejected = []
    for m in modes:
        seq += 1
        crc, err = submit(b, bad_entries, seq, mode=m, period=period)
        if err:
            rejected.append((m, "协议写入失败: %s" % err))
            continue
        ok, s = wait_done(b, seq, timeout=2.0)
        if not ok:
            rejected.append((m, "done_seq 未跟上 (req=%d done=%s)"
                             % (seq, s["done_seq"] if s else "?")))
            continue
        if s["reject"] == 0:
            used = m
            record("T3.4 合法表（%s 口径）被接受 reject==0" % m, True, snap_str(s))
            record("T3.5 n_valid == 1（登记数回填）", s["n_valid"] == 1,
                   "n_valid=%d" % s["n_valid"])
            break
        rejected.append((m, "被拒 reject=%d(%s)" % (s["reject"],
                                                    DB_RC_NAME.get(s["reject"], "?"))))

    if used:
        if len(modes) > 1:
            for m, why in rejected:
                print("        · 候选口径 %s 未被固件接受（%s）—— auto 模式据此排除" % (m, why))
        print("        ⇒ 固件实际吃下的 CRC 口径 = **%s**" % used)
    else:
        for m, why in rejected:
            record("T3.4 合法表（%s 口径）被接受 reject==0" % m, False, why)
        print("        !! 没有任何 CRC 口径被接受 —— 先分清是『CRC 算法不一致』还是"
              "『提交通路没跑』（前者 reject==1，后者 done_seq 不跟上）")
        print("           契约 §3.8.3 说 'FNV-1a 与 bb_map_sum 同算法' ⇒ 逐字（src/dev_bind.c:db_crc）")
    return seq, used


def t4_reject_fields(b, seq, mode, dst, period):
    """T5（任务书编号）= 字段越界逐项拒绝。每次都用**递增 seq + 正确 CRC**，
    这样失败的只可能是字段判据，不会是 CRC/SEQ 判据 —— 可归因。"""
    print("\n── T4 字段越界逐项拒绝（每个码一条独立判据；每次递增 seq + 正确 CRC）──")
    cases = [
        ("addr7=0x80 (> 0x7F)", entry(DB_DEV_I2C_RD, dst, 2, 0x0C, 0x80), 4),
        ("len=0",               entry(DB_DEV_I2C_RD, dst, 0, 0x0C, AS5600_ADDR7), 5),
        ("len=3 (> 2)",         entry(DB_DEV_I2C_RD, dst, 3, 0x0C, AS5600_ADDR7), 5),
        ("dev=9 (非法设备码)",   entry(9, dst, 2, 0x0C, AS5600_ADDR7), 3),
    ]
    for name, e, want in cases:
        seq += 1
        pre = snap(b) or {}
        base_rej = pre.get("rej_n", 0)
        crc, err = submit(b, [e], seq, mode=mode, period=period)
        if err:
            record("T4 %s → reject=%d(%s)" % (name, want, DB_RC_NAME[want]), False, err)
            continue
        ok, s = wait_done(b, seq, timeout=2.0)
        if not ok:
            record("T4 %s → reject=%d" % (name, want), False,
                   "done_seq 未跟上 —— 拒绝也必须回 done_seq（判据须能终止）")
            continue
        # ★ reject 是"最近一次"的值 —— 只判它可能读到**上一次**留下的旧码。
        #   加上 `rej_n` 增量 ⇒ "这个码是本次提交产生的" 才成立（判据可归因）。
        record("T4 %s → reject=%d(%s)" % (name, want, DB_RC_NAME[want]),
               s["reject"] == want and s["rej_n"] > base_rej,
               "读到 reject=%d(%s), rej_n %d→%d" % (
                   s["reject"], DB_RC_NAME.get(s["reject"], "?"), base_rej, s["rej_n"]))
        record("T4 %s：n_valid 未被改动（不半装载，仍=1）" % name,
               s["n_valid"] == 1, "n_valid=%d" % s["n_valid"])

    # ★ dst=16 **不可构造**（4 bit 字段 ⇒ 0..15）⇒ DB_RC_DST=6 是两端死码。
    #   不为它造判据（那是空判据）—— 明确打出来，并用合法边界做正对照。
    skip("T4 dst=16 → reject=6(DST)",
         "★ 空判据: dst 只有 4 bit（契约 §3.8.2 [27:24]）⇒ 合法域 0..15, "
         "编码层就掩掉 ⇒ 固件 `if (dst > 15u)` 与 DB_RC_DST=6 均为死码。"
         "下面的 dst=15 正对照证明'不是什么都拒'。")

    # 正对照: 边界值必须被接受（否则"全拒"也会让上面的判据全绿）
    seq += 1
    e = entry(DB_DEV_I2C_RD, 15, 2, 0x0C, DB_MAX_ADDR7)
    crc, err = submit(b, [e], seq, mode=mode, period=period)
    if err:
        record("T4 正对照 addr7=0x7F & dst=15 被接受", False, err)
        return seq
    ok, s = wait_done(b, seq, timeout=2.0)
    record("T4 ★ 正对照 addr7=0x7F & dst=15 被接受 (reject=0, n_valid=1)",
           ok and s["reject"] == 0 and s["n_valid"] == 1,
           "reject=%s(%s) n_valid=%s" % (
               s["reject"] if s else "?", DB_RC_NAME.get(s["reject"], "?") if s else "?",
               s["n_valid"] if s else "?"))
    record("T4 正对照: SENSOR[15] 之外的槽未被这次提交改动",
           s is not None and s["entries"][0] == e and all(x == 0 for x in s["entries"][1:]),
           "entries[0]=%s" % (entry_note(s["entries"][0]) if s else "?"))
    return seq


def t5_execute(b, seq, mode, dst, period):
    """执行路 + 两条独立路径互证。返回 (seq, 是否确认器件在场)"""
    print("\n── T5 执行/失败两条路都要观测，且必须能区分是否接了器件 ──")
    good = [entry(DB_DEV_I2C_RD, dst, AS5600_LEN, AS5600_REG, AS5600_ADDR7)]
    seq += 1
    crc, err = submit(b, good, seq, mode=mode, period=period)
    if err:
        record("T5.1 提交 AS5600 一行（addr7=0x36 reg=0x0C len=2）", False, err)
        return seq, False
    ok, s0 = wait_done(b, seq, timeout=2.0)
    record("T5.1 AS5600 行被接受 (reject=0, n_valid=1)",
           ok and s0["reject"] == 0 and s0["n_valid"] == 1,
           "reject=%s n_valid=%s" % (s0["reject"] if s0 else "?",
                                     s0["n_valid"] if s0 else "?"))
    if not ok:
        return seq, False

    print("        等执行（err_n 或 ok_n 必须有一个在涨；两者都不动 = 服务方没跑）...")
    got = False
    s = s0
    for key in ("ok_n", "err_n"):
        got, s = wait_counter_growth(b, key, s0[key], timeout=3.0)
        if got:
            break
    record("T5.2 ★ 计数器在动（区分'提交成功但服务方没跑'）", got,
           "ok_n %d→%s, err_n %d→%s" % (s0["ok_n"], s["ok_n"] if s else "?",
                                        s0["err_n"], s["err_n"] if s else "?"))
    if not got:
        print("        !! 提交被受理（done_seq 跟上）但 ok_n/err_n 都不动 ⇒")
        print("           最可能: `dev_bind_submit()`/`dev_bind_service()` 没有在主循环被调用")
        print("           （核对 src/main.c 的主循环；这不是接线问题）。")
        print("           若怀疑是引擎未 RUN 导致，可加 --start 重跑（本脚本默认不动执行器）。")
        return seq, False

    if s["ok_n"] > s0["ok_n"]:
        # 真的读到了器件 ⇒ 用**独立第二路径**核对同一个值
        st, raw = ix_read(b, AS5600_ADDR7, AS5600_REG, AS5600_LEN)
        v = b.rd_f(OFF_SENSOR_MAP + dst * 4)
        print("        ⇒ 实际读到器件（ok_n 在涨）。SENSOR[%d]=%s；"
              "IXAC 独立路径 status=%s(%s) bytes=%s" % (
                  dst, v, st, I2C_SM_ST.get(st, "?"), raw.hex() if raw else "无"))
        record("T5.3 ★ ok 路: ok_n 在涨（I2C 确实读到器件）", True,
               "ok_n %d→%d" % (s0["ok_n"], s["ok_n"]))
        if raw and len(raw) >= AS5600_LEN:
            ref = float((raw[0] << 8) | raw[1])         # 与 dev_bind.c 的值转换同式(大端)
            record("T5.4 ★ 两条独立路径（dev_bind SENSOR 槽 vs IXAC 事务区）读到同一个值",
                   v is not None and abs(v - ref) < 1e-6,
                   "SENSOR[%d]=%s  IX 独立读数=%s" % (dst, v, ref))
            record("T5.5 读回的原始角度在 AS5600 12 位域内 (0..4095)",
                   0.0 <= ref <= 4095.0, "ref=%s" % ref)
        else:
            skip("T5.4 两条路径互证", "IXAC 独立读没拿到数据（status=%s）"
                 " —— 可能是总线被 dev_bind 占用，见契约 §3.6 约束③" % st)
    else:
        # 只有 err 在涨 ⇒ 未接器件（合理结果），必须明确标注
        print("        ⇒ **未接器件**: 只有 err_n 在涨, last_err=%d(%s)" % (
            s["last_err"], I2C_SM_ST.get(s["last_err"], "?")))
        record("T5.3 ★ err 路: err_n 在涨（绑定被真的执行过）", True,
               "err_n %d→%d, last_err=%d(%s)" % (s0["err_n"], s["err_n"],
                                                 s["last_err"],
                                                 I2C_SM_ST.get(s["last_err"], "?")))
        record("T5.4 err 路: last_err==3 (I2C_SM_ST_NAK, 地址无应答) —— 这是未接器件的**合理**结果",
               s["last_err"] == 3, "last_err=%d(%s)" % (s["last_err"],
                                                        I2C_SM_ST.get(s["last_err"], "?")))
        print("        ★ 结论: 判 PASS, 但**未接器件**（不要把这个结果读成'读到 0 度'）")
    return seq, (s["ok_n"] > s0["ok_n"])


def t6_keep_old(b, seq, mode, dst, period, present):
    """T6（任务书）= 失败保旧值。★ 必须确认 err_n 真的在涨，否则是空判据。"""
    print("\n── T6 ★ 失败必须保留旧值（'没读到'不得伪装成'值=0'）──")
    ghost = [entry(DB_DEV_I2C_RD, dst, 2, 0x0C, GHOST_ADDR7)]
    seq += 1
    crc, err = submit(b, ghost, seq, mode=mode, period=period)
    if err:
        record("T6.1 把绑定改到不存在的从机 addr7=0x%02X" % GHOST_ADDR7, False, err)
        return seq
    ok, s = wait_done(b, seq, timeout=2.0)
    record("T6.1 改绑定到 addr7=0x%02X 被接受（这本身也证明'表上位机可写'）" % GHOST_ADDR7,
           ok and s["reject"] == 0 and s["n_valid"] == 1,
           "reject=%s n_valid=%s" % (s["reject"] if s else "?",
                                     s["n_valid"] if s else "?"))
    if not ok:
        return seq

    SENTINEL = 1234.0
    if not b.wr_f(OFF_SENSOR_MAP + dst * 4, SENTINEL):
        record("T6.2 写哨兵到 SENSOR[%d]" % dst, False, "0x21 被拒")
        return seq
    back = b.rd_f(OFF_SENSOR_MAP + dst * 4)
    record("T6.2 哨兵已就位 SENSOR[%d] == %s" % (dst, SENTINEL),
           back is not None and abs(back - SENTINEL) < 1e-6, "读回 %s" % back)

    base = s["err_n"]
    grew, s2 = wait_counter_growth(b, "err_n", base, timeout=3.0)
    record("T6.3 ★ 该绑定真的在被执行（err_n 必须涨，否则本判据是空判据）",
           grew, "err_n %d→%s, last_err=%s" % (
               base, s2["err_n"] if s2 else "?", s2["last_err"] if s2 else "?"))
    if not grew:
        if present:
            skip("T6.4 失败保旧值", "err_n 不涨（可能是器件在场、该地址也被应答 —— "
                 "本次跑确认过器件在场）；无法构造'读失败'")
            return seq
        record("T6.4 失败保旧值 SENSOR[%d] == 哨兵" % dst, False,
               "err_n 不涨 ⇒ 无法构造失败 ⇒ 该判据无法判定（不当作 PASS）")
        return seq

    v = b.rd_f(OFF_SENSOR_MAP + dst * 4)
    record("T6.4 ★ 失败后 SENSOR[%d] 仍 == 哨兵 %s（未被写成 0）" % (dst, SENTINEL),
           v is not None and abs(v - SENTINEL) < 1e-6,
           "读回 %s%s" % (v, "" if v is None or abs(v - SENTINEL) < 1e-6
                          else "  ← 被改动了: '没读到'被伪装成了别的值"))
    return seq


def sm_gate_query(b):
    """`0x39 op=22 sub=2` = **只查询**总线门（不占门, 无副作用）。

    为什么本脚本需要它: T8 的构造依赖"提交那一刻服务方**有事务在飞**"。这个窗口
    只有 ~800µs, 从串口**抓不到瞬时**, 但**能把占比量出来** —— 若采样里几乎总是
    `sm_status==BUSY`, 那么一次随机落点的提交就有同等概率落在窗口里。
    ⇒ 把"几乎总有事务在飞"从**论断**变成一个**数**，并作为 T8 是否真的覆盖了
    在飞路径的判据。返回 dict 或 None。
    （op=22 的语义照 `src/main.c` 的 G6-2 诊断段: sub=2 只查询; sub=0 才占住。）
    """
    sts, p = b.send(0x39, struct.pack("<BBI", 22, 2, 0))
    if sts != "ACK" or len(p) < 36:
        return None
    f = struct.unpack("<9I", p[:36])
    return dict(got=f[0], owner=f[1], busy_n=f[2], sm_gate_n=f[3],
                sm_status=f[4], sm_phase=f[5], bb_tx=f[6], bb_nak=f[7], refs=f[8])


def t8_inflight_swap(b, seq, mode, dst, period_restore, rounds, observe):
    """T8 ★ 有事务在飞时换表 ⇒ 新表必须**暂存待生效**（不得用新表的 lane 解释旧事务的数据）。

    ## 这条判据在抓什么（P0）
    `dev_bind_service` 的收尾代码用 `s_lane[s_idx]` 解释**在飞事务**的数据。若提交被
    "立即换表"处理（并重置 `s_rr`），收尾时 `s_idx` 指的就是**新表**的条目 ⇒ 新表的
    `dst`/`len` 会被用来落**旧事务**的数据：读数被写进**另一个 SENSOR 槽**，
    而 `reject=0`、`n_valid` 合法、`done_seq` 跟上 —— **没有任何错误迹象**。

    ## 怎么把它变成可观测（构造成"几乎必然落在在飞窗口里"）
      · `DB_PERIOD = 1` 拍（**合法值**，不是靠越界兜底）—— 而一次 2 字节读事务跨
        `6+n = 8` 拍 ⇒ 发起节奏永远快于完成节奏 ⇒ 服务方**几乎总是**有事务在飞。
      · 表 B 指向**不存在**的从机（`addr7=0x50`）⇒ 它生效后只会 NAK，**永不写** `SENSOR[15]`。
      · 表 A = AS5600（`len=2`），在飞时若被 B 的 lane（`dst=15, len=1`）解释 ⇒
        `SENSOR[15] = d[0]`（AS5600 首字节，0..15；或 len 不匹配时的其它值）。
    ⇒ **指纹**: `SENSOR[15]` 一旦 ≠ 哨兵，就只可能是"用错 lane"。
       ★ 而且这个错值是**粘住**的（B 生效后没有任何东西会写 `SENSOR[15]`），
         所以不靠"抓瞬时"，一次读就能判 —— 观测窗口只用来给足"坏事发生"的机会。
       ★ 哨兵取 4321.0 而不是 0.0: 否则"被写成 0" 与 "从未被写" 分不开。

    ## 正方向也必须能失败
    每轮都要求 `err_n` 在观测窗口内增长（B 对 0x50 必然 NAK）—— 否则"`SENSOR[15]` 没变"
    可能只是"B 根本没生效/服务方没跑"，那条判据就成了空判据。
    """
    print("\n── T8 ★ 在飞换表：新表必须暂存待生效（不得用新表的 dst/len 解释旧事务的数据）──")
    print("        DB_PERIOD=1 拍(合法) + 事务跨 8 拍 ⇒ 几乎总有事务在飞; "
          "表 B 指向不存在的 0x50 ⇒ 它永不写 SENSOR[15]")
    A = [entry(DB_DEV_I2C_RD, dst, AS5600_LEN, AS5600_REG, AS5600_ADDR7)]
    Bt = [entry(DB_DEV_I2C_RD, 15, 1, 0x00, GHOST_ADDR7)]
    SENT2 = 4321.0
    SLOT15 = OFF_SENSOR_MAP + 15 * 4

    if not b.wr1(DB_PERIOD, 1):
        record("T8.1 DB_PERIOD 写 1 拍被接受", False, "0x21 被拒")
        return seq
    record("T8.1 DB_PERIOD 写成 1 拍（加速到远快于一次事务的 8 拍）", True,
           "这是一次**合法**的提交前配置, 判据不依赖任何越界")

    # ★★ 必须是 try/finally: 把 period 留在 1 拍会让服务方**每 1 拍就抢一次总线** ——
    #    T8 中途任何异常（协议错、Ctrl-C、断言）都会把板子留在那个状态上, 影响
    #    后面所有用同一条总线的人。"复原"是**副作用清理**, 不能只写在正常路径末尾。
    try:
        return _t8_body(b, seq, mode, dst, A, Bt, SENT2, SLOT15, rounds, observe)
    finally:
        if b.wr1(DB_PERIOD, period_restore):
            print("        (已复原 DB_PERIOD = %d)" % period_restore)
        else:
            record("T8.8 复原 DB_PERIOD（副作用清理）", False, "0x21 被拒")


def _t8_body(b, seq, mode, dst, A, Bt, SENT2, SLOT15, rounds, observe):
    """T8 主体（DB_PERIOD 已被置 1 拍, 由调用方的 `finally` 负责复原）。"""
    # ② 提交 A 并证明确实在跑（否则"窗口"根本不存在）
    seq += 1
    crc, err = submit(b, A, seq, mode=mode, period=None)
    if err:
        record("T8.2 提交表 A（AS5600）", False, err)
        return seq
    ok, s = wait_done(b, seq, timeout=2.0)
    record("T8.2 表 A 生效 (reject=0, n_valid=1)",
           ok and s["reject"] == 0 and s["n_valid"] == 1,
           "reject=%s n_valid=%s" % (s["reject"] if s else "?",
                                     s["n_valid"] if s else "?"))
    if not ok:
        return seq
    # ★ 判"事务流已建立"必须**两条路都算**: 接了 AS5600 ⇒ ok_n 涨; 没接 ⇒ err_n 涨。
    #   只认 ok_n 会让这条判据在"没接器件的板子"上必红（那是把环境当判据, 不是判据）。
    s0 = s
    grew = False
    for key in ("ok_n", "err_n"):
        grew, s2 = wait_counter_growth(b, key, s0[key], timeout=2.0)
        if grew:
            break
    record("T8.3 事务流已建立（ok_n/err_n 在涨 ⇒ 后面确实有'在飞'窗口可落）", grew,
           "ok_n %d→%s, err_n %d→%s" % (s0["ok_n"], s2["ok_n"] if s2 else "?",
                                        s0["err_n"], s2["err_n"] if s2 else "?"))

    # ⓪ ★ 把"几乎总有事务在飞"量出来（否则 T8 的 PASS 不能说明它覆盖了在飞路径）
    n_s = n_busy_s = 0
    t0 = time.time()
    while time.time() - t0 < 0.4:
        q = sm_gate_query(b)
        if q is not None:
            n_s += 1
            n_busy_s += (q["sm_status"] == I2C_SM_ST_BUSY)
    frac = (100.0 * n_busy_s / n_s) if n_s else 0.0
    record("T8.0 在飞窗口占比实测 > 0（否则本判据没有覆盖到要考的那条路径）",
           n_s > 0 and n_busy_s > 0,
           "%d/%d 次采样 sm_status==BUSY(1) = %.0f%%（period=1 拍 vs 事务 8 拍; "
           "op=22 sub=2 只查询, 不占门）" % (n_busy_s, n_s, frac))

    hit = None
    n_rounds_ok = 0
    n_busy_rounds = 0
    for r in range(1, rounds + 1):
        # ① 重新武装哨兵，然后**立刻**提交 B（中间不 sleep —— 要的就是落在在飞窗口里）
        if not b.wr_f(SLOT15, SENT2):
            record("T8.4 第 %d 轮: 哨兵写入 SENSOR[15]" % r, False, "0x21 被拒")
            break
        back = b.rd_f(SLOT15)
        # ★ 提交**前一刻**的 SM 状态: BUSY 就是"提交会落在在飞窗口"的**直接旁证**
        q = sm_gate_query(b)
        busy_now = bool(q is not None and q["sm_status"] == I2C_SM_ST_BUSY)
        n_busy_rounds += busy_now
        seq += 1
        crc, err = submit(b, Bt, seq, mode=mode, period=None)
        if err:
            record("T8.4 第 %d 轮: 提交表 B" % r, False, err)
            break
        ok, sb = wait_done(b, seq, timeout=2.0)
        base_err = sb["err_n"] if sb else 0

        # ② 密轮询：哨兵必须**始终**不被改动（错值是粘的，但窗口给足"坏事发生"的机会）
        t0 = time.time()
        n_rd = n_err = 0
        while True:
            v = b.rd_f(SLOT15)
            n_rd += 1
            if v is None:
                n_err += 1
                if n_err > 20:
                    break
            elif abs(v - SENT2) > 1e-6:
                hit = (r, v)
                break
            if time.time() - t0 >= observe:
                break
        if hit:
            break
        sm = snap(b)
        er_delta = (sm["err_n"] - base_err) if sm else 0
        record("T8.4 第 %d 轮: SENSOR[15] 仍 == 哨兵 %s（换表落在在飞窗口）" % (r, SENT2),
               back is not None and abs(back - SENT2) < 1e-6,
               "提交前 SM status=%s%s; 轮询 %d 次/%.2fs（读错 %d）; err_n +%d; done_seq %s" % (
                   q["sm_status"] if q else "?", "(BUSY=在飞)" if busy_now else "",
                   n_rd, observe, n_err, er_delta,
                   ("跟上 %d" % seq) if ok else "未跟上"))
        record("T8.5 第 %d 轮: err_n 在涨（B 真的在执行 ⇒ 上一行不是空判据）" % r,
               er_delta > 0, "err_n +%d" % er_delta)
        if not ok:
            record("T8.6 第 %d 轮: 表 B 的 done_seq 在 2s 内跟上（暂存后必须补回执）" % r,
                   False, "req=%d done=%s" % (seq, sm["done_seq"] if sm else "?"))
        n_rounds_ok += 1
        # ③ 回到 A，为下一轮重建事务流
        seq += 1
        submit(b, A, seq, mode=mode, period=None)
        wait_done(b, seq, timeout=1.0)

    if hit:
        record("T8.6 ★ 在飞换表时 SENSOR[15] 被改动 ⇒ **用错 lane**"
               "（新表的 dst/len 被用于旧事务的数据）",
               False, "第 %d 轮读到 SENSOR[15]=%s（哨兵 %s, 而表 B 指向**不存在**的 0x50）"
               % (hit[0], hit[1], SENT2))
        record("T8.7 全部 %d 轮哨兵均未被改动" % rounds, False,
               "第 %d 轮即失败" % hit[0])
    elif n_busy_rounds == 0:
        # ★ 构造没成立就不算 PASS —— 否则"没抓到窗口"会被读成"固件没问题"。
        skip("T8.6 哨兵未被动过", "★ 构造未成立: %d/%d 轮的提交时刻 SM 都不是 BUSY ⇒ "
             "本轮没有覆盖'在飞换表'这条路径, PASS 无意义" % (n_busy_rounds, rounds))
        record("T8.7 ★ 至少一轮的提交落在在飞窗口（否则本判据未覆盖要考的路径）",
               False, "%d/%d 轮提交时 SM 为 BUSY（实测窗口占比见 T8.0）"
               % (n_busy_rounds, rounds))
    else:
        record("T8.6 ★ 全部 %d 轮哨兵均未被改动（换表没有把旧事务的读数落进新表的槽）"
               % rounds, n_rounds_ok == rounds,
               "%d/%d 轮通过, 每轮观测 ≥%.2fs（覆盖数十次事务）; 其中 %d/%d 轮"
               "**提交时刻 SM 正在 BUSY** ⇒ 断言用在飞路径上" % (
                   n_rounds_ok, rounds, observe, n_busy_rounds, rounds))
        record("T8.7 在飞换表未产生任何错槽写入", n_rounds_ok == rounds,
               "错槽写入是**粘住**的（B 生效后无人写 SENSOR[15]）⇒ 一次观测即可判")
    return seq


def t7_cleanup(b, seq, mode, period):
    """复原: 提交空表 ⇒ n_valid=0，不留一个一直读不存在从机的绑定。"""
    print("\n── T7 复原（空表 ⇒ n_valid=0，避免留下持续读 ghost 地址的绑定）──")
    seq += 1
    crc, err = submit(b, [], seq, mode=mode, period=period)
    if err:
        record("T7.1 提交空表复原", False, err)
        return seq
    ok, s = wait_done(b, seq, timeout=2.0)
    record("T7.1 空表被接受 (reject=0, n_valid=0)", ok and s["reject"] == 0
           and s["n_valid"] == 0,
           "reject=%s n_valid=%s" % (s["reject"] if s else "?",
                                     s["n_valid"] if s else "?"))
    return seq


def t9_teardown(b, want_reset=True):
    """T9 ★ 收尾必须把"我留下了什么"变成可观测 —— 不许让下一个套件去背锅。

    ## 为什么需要这条（regress-verify 的实测, 不是理论风险）
    本脚本到 T7 为止**只复原了绑定表**（`n_valid=0`）, **没有复原总线门**。
    实测后果: 收尾若 `owner=2(SM)` / `refs != 0`（门被未释放的引用占着）, 紧接着跑
    `tools/h723_i2c_gate_test.py` 会 **4/5 FAIL** —— 起点 owner=2 ⇒ G1 占不住
    ⇒ G2/G4/G5 连带倒; 而**单独复位后复跑 2 次均 5/5**。**这是假失败**，
    且现象离真因极远: 所有人都会去查那条套件, 而不是去查"谁把门留下了"。

    固件自己已把判据写死 ⇒ 收尾 `refs != 0` **就是违约**, 不是风格问题:
      · `src/i2c_bb.h:51`  `i2c_bus_refs()` —— "正常空闲必须 == 0（泄漏判据）"
      · `src/i2c_sm.h:124` 观测量 `0x39 op=22 +32` = refs（空闲必须 0）

    ## 一个计数只回答一个问题
    · **T9.1** 回答「本套件离开时, 总线门是否干净」—— 纯外部可观测状态, 与根因无关。
    · **T9.2** 回答「0x13 RESET 是不是有效的清场手段」—— 下一个套件能否靠它自救。

    ## 失败路径（两条都能失败, 由 selftest 的 B9/B10 证明）
    · T9.1: 任何 `acquire` 之后不配对 `release` ⇒ refs 只增不减（B9 `leak_refs`）。
    · T9.2: RESET 不清门引用 ⇒ 复位后 refs 仍 != 0（B10 `reset_keeps_refs`）。

    ## ★ 本判据**刻意不回答**根因
    refs != 0 **不等于**"dev_bind 有缺陷"。可能的来源至少三处: SM 自身的延迟释放
    记账 / G6-3 `i2c_shm_service` / G6-4 `dev_bind_service`。
    ★ 定案实验（2026-09-16 本机实测, 结论已同步 team-lead 与 regress-verify）:
      a) 干净复位后**只**反复发 `0x39 op=20 sub=0`（纯 SM, 不碰 dev_bind）**30 次**
         ⇒ `owner=0 refs=0`, **增量 +0**（`sm_status=2(OK)` 证明事务真跑完了, 不是没跑）
         ⇒ **泄漏不在 SM 自身**（`g_i2c_sm_release_pending` 的配对计数修复是有效的）。
      b) 提交一张 AS5600 表并连续跑 ~4300 次事务 ⇒ `refs` **恒为 1 且不增长**
         （那 1 是"事务在飞"的正常占用, 不是泄漏）。
      c) 跑完整套件（T0..T8, 含 T8 的 `period=1` 密发）后**立即**读 ⇒ `owner=0 refs=0`。
      ⇒ 在本机当前固件上**无法复现** regress-verify 记录的 `refs=56~64`; 该记录很可能
        来自**更早的固件**或**其它套件的路径**。需要时用 a) 那一步复现判定。

    ## 为什么要"等一会儿才判定"（否则这条判据自己会假红）
    稳态下服务方**恒持有 1 个引用**（有一个事务在飞）。T7 提交空表后, 最后一个在飞事务
    还要几拍才收尾 ⇒ **立刻读会读到 refs=1** —— 那是时序抖动, 不是泄漏。
    ⇒ 判据给一个**上界 1.0s**, 只等「归零」: 真泄漏时 refs 不会自己变 0, 等满即判红。
    """
    print("\n── T9 ★ 收尾副作用可观测（总线门 refs/owner；不许让下一个套件背锅）──")
    q0 = sm_gate_query(b)
    if q0 is None:
        record("T9.1 收尾读 0x39 op=22 sub=2", False, "无应答")
        return
    refs0, owner0 = q0["refs"], q0["owner"]
    print("        收尾瞬间: owner=%d(%s) refs=%d sm_status=%d busy_n=%d"
          % (owner0, I2C_OWNER_NAME.get(owner0, "?"), refs0,
             q0["sm_status"], q0["busy_n"]))

    deadline = time.time() + 1.0
    while (refs0 != 0 or owner0 != 0) and time.time() < deadline:
        time.sleep(0.05)
        q = sm_gate_query(b)
        if q is not None:
            refs0, owner0 = q["refs"], q["owner"]

    clean = (refs0 == 0 and owner0 == 0)
    record("T9.1 ★ 收尾时总线门 refs==0 且 owner==NONE"
           "（固件自述的空闲判据: src/i2c_bb.h:51 / src/i2c_sm.h:124）",
           clean,
           "owner=%d(%s) refs=%d%s" % (
               owner0, I2C_OWNER_NAME.get(owner0, "?"), refs0,
               "" if clean else
               "  ← **违约**: 门被占着 ⇒ 下一个用这条总线的套件会**假失败**"
               "（实测 h723_i2c_gate_test 4/5 FAIL）。本判据只报『门不干净』, "
               "不回答根因（定案实验见 docstring）"))

    if not want_reset:
        skip("T9.2 0x13 RESET 后门归零（清场手段有效性）", "--no-reset-at-end 给定")
        return
    sts, _ = b.send(CMD_RESET, b"")
    if sts != "ACK":
        record("T9.2 0x13 RESET 被受理（清场手段）", False, "sts=%s" % sts)
        return
    q1 = None
    deadline = time.time() + 1.0
    while time.time() < deadline:
        q1 = sm_gate_query(b)
        if q1 is not None and q1["refs"] == 0 and q1["owner"] == 0:
            break
        time.sleep(0.05)
    if q1 is None:
        record("T9.2 0x13 RESET 后门归零", False, "RESET 后无应答")
        return
    record("T9.2 ★ 0x13 RESET 后门归零（⇒ 『跑前先复位』是可行约定, 下一个套件可自救）",
           q1["refs"] == 0 and q1["owner"] == 0,
           "owner %d→%d, refs %d→%d" % (owner0, q1["owner"], refs0, q1["refs"]))


# ══════════════════ main ══════════════════
def main():
    ap = argparse.ArgumentParser(description="H723 G6-4 具名设备绑定表 上机验收（只用协议）")
    ap.add_argument("--port", default=None, help="串口号（不给则按能力字自动找板子）")
    ap.add_argument("--dst", type=int, default=7,
                    help="目标 SENSOR 槽（默认 7 —— 见文件头'偏离①'：槽 1 被 AS5600 每 10ms 覆写）")
    ap.add_argument("--period", type=int, default=DB_PERIOD_DEF,
                    help="DB_PERIOD 拍数（默认 %d）" % DB_PERIOD_DEF)
    ap.add_argument("--crc-mode", choices=["words", "bytes", "auto"], default="words",
                    help="CRC 口径: words=逐个 u32(与 dev_bind.c/bb_map_sum 一致, 默认); "
                         "bytes=把 8 个 u32 摊成 32 个 LE 字节; auto=两种都试并报告")
    ap.add_argument("--skip-reset", action="store_true", help="跳过 T2 的 0x13 RESET")
    ap.add_argument("--start", dest="want_start", action="store_true",
                    help="先发 0x11 START（默认不发: 本脚本不动执行器）")
    ap.add_argument("--no-cleanup", action="store_true", help="不跑 T7 复原")
    ap.add_argument("--no-reset-at-end", action="store_true",
                    help="收尾**不**发 0x13 RESET。默认会发 —— 理由见 T9: 本脚本只复原"
                         "绑定表(n_valid=0), 若把 owner/refs 留在非 0, 下一个用同一条"
                         "总线的套件会**假失败**（实测 h723_i2c_gate_test 4/5 FAIL）。"
                         "代价: 收尾会清掉整个 SHM（含 SENSOR 历史值）。")
    ap.add_argument("--t8-rounds", type=int, default=6,
                    help="T8（在飞换表）的轮数；每轮 ~%.1fs 观测。默认 6 轮 ⇒ "
                         "单轮命中在飞窗口的概率 ~3/4 时, 漏检概率 < 1e-4" % 0.6)
    ap.add_argument("--t8-observe", type=float, default=0.6,
                    help="T8 每轮哨兵观测时长(秒), 默认 0.6")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每帧")
    a = ap.parse_args()

    if not (0 <= a.dst <= 15):
        print("!! --dst 必须 0..15（dst 是 4 bit 字段）")
        return 2

    port = a.port
    if not port:
        try:
            port = find_board()
        except Exception as e:
            print("!! 自动找板子失败: %s" % e)
            print("   先跑 tools/h723_revive.py 看诊断, 或用 --port 指定。")
            return 2
    print("端口: %s @ 115200   dst=SENSOR[%d]  crc-mode=%s  period=%d"
          % (port, a.dst, a.crc_mode, a.period))

    try:
        b = Board(port, verbose=a.verbose)
    except Exception as e:
        print("!! 打不开串口 %s: %s" % (port, e))
        print("   先跑 tools/h723_revive.py 看诊断, 或用 --port 指定其它端口。")
        return 2
    seq = 0
    mode = a.crc_mode
    present = False
    try:
        if not t0_link(b, a.want_start):
            return 2
        if a.skip_reset:
            skip("T2 RESET 后 magic 仍在", "--skip-reset 给定")
            s = snap(b)
            seq = s["req_seq"] if s else 0
        else:
            t1_zone(b)
            t2_reset(b)
        seq, used = t3_crc_and_accept(b, seq, a.dst, mode, a.period)
        if used is None:
            print("\n!! T3 未通过 —— 后续判据全部无法判定（先解决提交通路/CRC 口径）")
            return 1
        mode = used
        seq = t4_reject_fields(b, seq, mode, a.dst, a.period)
        seq, present = t5_execute(b, seq, mode, a.dst, a.period)
        seq = t6_keep_old(b, seq, mode, a.dst, a.period, present)
        if not a.no_cleanup:
            seq = t7_cleanup(b, seq, mode, a.period)
        seq = t8_inflight_swap(b, seq, mode, a.dst, a.period,
                               max(1, a.t8_rounds), max(0.1, a.t8_observe))
        if not a.no_cleanup:
            seq = t7_cleanup(b, seq, mode, a.period)   # T8 后又立了表 ⇒ 再清一次
        t9_teardown(b, want_reset=not a.no_reset_at_end)
    except KeyboardInterrupt:
        print("\n!! 用户中断")
    finally:
        b.close()

    print("\n════════════ 摘要 ════════════")
    print("  PASS %d / FAIL %d / SKIP %d" % (_N_PASS, _N_FAIL, _N_SKIP))
    if present:
        print("  器件: **实际读到**（ok_n 在涨）")
    else:
        print("  器件: 未读到（只有 err_n 在涨）—— 判 PASS 但**未接器件**")
    return 1 if _N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
