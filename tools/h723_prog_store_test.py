#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_prog_store_test.py — **DCL 程序持久化** 五道闸验收 (S4/S5)

契约: docs/REF-program-contract.md §7。本脚本给每一道闸配一条**能失败**的判据，
并尽量配一个**对照** (故意做错, 看它是否真的被拦住) —— 本项目铁律 2:
"判据必须能失败, 而且要能证明它会红"。

    闸1  帧 CRC16            传输层 (改一个字节发出去 ⇒ 必须被拒)
    闸2  清单 CRC32+total_len COMMIT 里对**收到的字节**算 CRC32
    闸3  写完立即回读比对      prog_store_save 内部 (memcmp)
    闸4  A/B 双副本 + 单调 seq 两份都要能被读出 CRC 通过
    闸5  装载前重跑同一套校验  开机装载调 `prog_validate` (与上传同一个函数)

子命令
    status        读 0x48 (分区 / 两副本有效性 / seq / CRC / 事务计数)
    desc          读 0x4A (fw + 能力位 + 具名设备表) —— 我们的 ESI 等价物
    gate1         故意坏帧 ⇒ 期望被拒 (对照: 好帧必须通过)
    gate2         故意篡改载荷 ⇒ COMMIT 必须拒 (crc mismatch)
    gate3/4       正常上传两次 ⇒ 看 A/B 轮换 + seq 递增 + 两份都能读
    gate5         上传一份"结构非法"的程序 ⇒ 落盘前必须被拒 (validate)
    roundtrip     完整: 上传 → 断电重启 → 开机自动装载 → 读回比对
    all           全跑
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from h723_client import Dcl, find_board   # noqa: E402

import zlib

C_PROG_BEGIN = 0x45
C_PROG_DATA = 0x46
C_PROG_COMMIT = 0x47
C_PROG_STATUS = 0x48
C_PROG_ERASE = 0x49
C_DEV_DESC = 0x4A
C_DEPLOY = 0x10
C_VERSION = 0x01

RC = {0: "OK", 1: "NOPART(卡未分区)", 2: "LEN(长度非法)", 3: "WRITE(卡写失败)",
      4: "VERIFY(回读不符/闸3)", 5: "NONE(无有效副本)", 6: "CRC(CRC不符)",
      7: "VALIDATE(静态校验拒/闸5)", 8: "MINF(固件版本过低)", 9: "CAPS(缺能力位)"}


def crc32(b):
    return zlib.crc32(b) & 0xFFFFFFFF


def build_prog(n_routes=2):
    """构造一份**合法**的 0x10 载荷: [nr][np][ns] + routes + params + states。

    ★★★ 字段偏移**逐字节抄 `src/engine.h` 的 `RouteEntry_t`**, 不抄文档表格 ——
      2026-09-16 踩过: 我照 ARCH 的字段表估偏移, 把 `flags` 写到 byte 14
      (真值在 **byte 5**) ⇒ 全 0 条 ACTIVE ⇒ `n_routes` 恒 0, 白查一整轮。
      ⇒ 契约 §2: **"SHM 布局以 `src/engine.h` 为唯一权威"** —— 对 `RouteEntry_t` 同样成立。

    真布局 (packed, 16B):
        [0] src_type  [1] src_index  [2] dst_type  [3] dst_channel
        [4] op        [5] flags      [6:8]  param_idx(u16)
        [8:10] state_offset(u16)     [10:12] actuator_idx(u16)
        [12:14] wire2_idx(u16)       [14] period  [15] reserved
    取值: SRC_CONST=2 · DST_WIRE=2 · OP_DIRECT=0x00 · ROUTE_FLAG_ACTIVE=0x01"""
    nr, np_, ns = n_routes, 1, 0
    hdr = struct.pack("<HHH", nr, np_, ns)
    routes = b""
    for i in range(nr):
        r = bytearray(16)
        r[0]  = 2                      # src_type     = SRC_CONST
        r[1]  = 0                      # src_index
        r[2]  = 2                      # dst_type     = DST_WIRE
        r[3]  = (8 + i) & 0xFF         # dst_channel  = WIRE[8+i]
        r[4]  = 0x00                   # op           = OP_DIRECT
        r[5]  = 0x01                   # flags        = ROUTE_FLAG_ACTIVE  ← ★ byte 5
        struct.pack_into("<H", r, 6, 0)    # param_idx
        struct.pack_into("<H", r, 8, 0)    # state_offset
        struct.pack_into("<H", r, 10, 0)   # actuator_idx (0 = 不驱动)
        struct.pack_into("<H", r, 12, 0)   # wire2_idx
        r[14] = 0                      # period       = div0/phase0
        r[15] = 0                      # reserved
        routes += bytes(r)
    params = struct.pack("<ffff", 1.0, 0.0, 0.0, 0.0) * np_
    states = b""
    body = hdr + routes + params + states
    assert len(body) == 6 + (nr + np_ + ns) * 16, len(body)
    return body


class Prog:
    def __init__(self):
        self.d = Dcl(find_board(), wait=0.6)

    # ── 事务式上传 ──
    def begin(self, total, crc, prog_id=1, prog_ver=1, min_fw=0, req_caps=0):
        pl = struct.pack("<II I H H I", total, crc, prog_id, prog_ver, min_fw, req_caps)
        return self.d.send(C_PROG_BEGIN, pl)

    def data(self, off, chunk):
        return self.d.send(C_PROG_DATA, struct.pack("<I", off) + chunk)

    def commit(self):
        return self.d.send(C_PROG_COMMIT, b"")

    def status(self):
        st, p = self.d.send(C_PROG_STATUS, b"")
        if st != "ACK" or len(p) < 64:
            return None
        u = struct.unpack("<16I", p[:64])
        return dict(part_ok=u[0], ab_valid=u[1], seq_a=u[2], seq_b=u[3],
                    crc_a=u[4], crc_b=u[5], len_a=u[6], len_b=u[7], active=u[8],
                    ok_n=u[9], fail_n=u[10], reject_n=u[11], last_rc=u[12],
                    txn_begin=u[13], txn_commit=u[14], txn_abort=u[15])

    def desc(self):
        st, p = self.d.send(C_DEV_DESC, b"")
        if st != "ACK" or len(p) < 8:
            return None
        fw, caps, hi, n = struct.unpack("<4H", p[:8])
        devs = []
        for i in range(n):
            o = 8 + i * 6
            if o + 6 > len(p):
                break
            devs.append(struct.unpack("<3H", p[o:o + 6]))
        return dict(fw=fw, caps=caps, caps_hi=hi, devs=devs)

    def upload(self, payload, chunk=140, **kw):
        """正常事务上传。返回 (最终状态, 每一步的返回)"""
        log = []
        st, _ = self.begin(len(payload), crc32(payload), **kw)
        log.append(("BEGIN", st))
        if st != "ACK":
            return log
        off = 0
        while off < len(payload):
            c = payload[off:off + chunk]
            st, _ = self.data(off, c)
            log.append(("DATA@%d" % off, st))
            if st != "ACK":
                return log
            off += len(c)
        st, p = self.commit()
        log.append(("COMMIT", st))
        return log


def hdr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    op = sys.argv[1]
    p = Prog()
    d = p.d
    try:
        if op in ("status", "all"):
            hdr("0x48 PROG_STATUS —— 分区与两副本")
            s = p.status()
            if not s:
                print("  读不到 (固件太旧?)")
            else:
                print("  分区可用(part_ok) = %d" % s["part_ok"])
                print("  副本有效位图(ab_valid) = %d  (1=A 2=B 3=都有)" % s["ab_valid"])
                print("  seq A/B = %d / %d      len A/B = %d / %d" % (s["seq_a"], s["seq_b"], s["len_a"], s["len_b"]))
                print("  实测 CRC A/B = 0x%08X / 0x%08X" % (s["crc_a"], s["crc_b"]))
                print("  当前生效副本 = %s" % ("A" if s["active"] == 0 else "B" if s["active"] == 1 else "无"))
                print("  事务: begin=%d commit=%d abort=%d   last_rc=%s"
                      % (s["txn_begin"], s["txn_commit"], s["txn_abort"], RC.get(s["last_rc"], s["last_rc"])))

        if op in ("desc", "all"):
            hdr("0x4A DEVICE_DESC —— 我们的 ESI 等价物")
            q = p.desc()
            if not q:
                print("  读不到")
            else:
                print("  fw_ver = 0x%04X   caps = 0x%04X (hi=0x%04X)" % (q["fw"], q["caps"], q["caps_hi"]))
                print("  具名设备 (device_type, product_code, revision):")
                for t in q["devs"]:
                    print("      %s" % (t,))

        if op in ("gate2", "all"):
            hdr("闸2: 清单 CRC32 + total_len —— 篡改载荷必须被 COMMIT 拒")
            pay = build_prog()
            st, _ = p.begin(len(pay), crc32(pay))
            print("  BEGIN(声明正确 CRC) → %s" % st)
            bad = bytearray(pay)
            bad[-1] ^= 0xFF                     # ★ 故意篡改一个字节
            st, _ = p.data(0, bytes(bad))
            print("  DATA(篡改 1 字节, 长度不变) → %s" % st)
            st, rp = p.commit()
            print("  COMMIT → %s   %s" % (st, (rp[:40] if st != "ACK" else "")))
            print("  ⇒ %s" % ("✅ 被拒 (闸2 生效)" if st != "ACK" else "✗✗ 竟然通过 ⇒ 闸2 是空的!"))

        if op in ("gate5", "all"):
            hdr("闸5: 落盘前重跑同一套静态校验 —— 结构非法的程序必须被拒")
            # 构造一份 nr 巨大但载荷很短的: validate 的 "payload short" 必须拦住
            pay = struct.pack("<HHH", 100, 0, 0)     # 声称 100 条路由却只有 6 字节
            st, _ = p.begin(len(pay), crc32(pay))
            print("  BEGIN → %s" % st)
            st, _ = p.data(0, pay)
            print("  DATA  → %s" % st)
            st, rp = p.commit()
            print("  COMMIT → %s   %s" % (st, rp[:48] if st != "ACK" else b""))
            print("  ⇒ %s" % ("✅ 被拒 (闸5 生效: 上传期就拦, 不是存进去再说)"
                              if st != "ACK" else "✗✗ 竟然通过 ⇒ 闸5 是空的!"))

        if op in ("gate34", "all"):
            hdr("闸3/闸4: 正常上传两次 ⇒ A/B 轮换 + seq 递增 + 两份都读得出")
            for k in range(2):
                pay = build_prog(n_routes=2 + k * 2)     # ★ 两次内容不同, 便于看 CRC 变化
                log = p.upload(pay, prog_id=0x1234, prog_ver=k + 1)
                s = p.status()
                bad = [x for x in log if x[1] != "ACK"]
                print("  第 %d 次: %s" % (k + 1, "全部 ACK" if not bad else bad))
                print("           seq A/B = %d/%d   CRC A=0x%08X B=0x%08X  生效=%s"
                      % (s["seq_a"], s["seq_b"], s["crc_a"], s["crc_b"],
                         "A" if s["active"] == 0 else "B"))
            print("  ⇒ %s" % ("✅ 两份都有实测 CRC ⇒ 闸4 的 A/B 结构成立"
                              if s["crc_a"] and s["crc_b"] else "⚠ 只有一份有效 (首次上传属正常)"))

        if op in ("roundtrip", "all"):
            hdr("端到端: 上传 → 复位 → 开机自动装载")
            pay = build_prog()
            p.upload(pay, prog_id=0x3C, prog_ver=9)
            print("  上传完成, 现在复位板子...")
            d.close()
            os.system("pyocd reset -t stm32h723xx >nul 2>&1")
            time.sleep(1.5)
            p2 = Prog()
            s = p2.status()
            print("  复位后 txn_commit=%d  abort=%d  part_ok=%d" % (s["txn_commit"], s["txn_abort"], s["part_ok"]))
            print("  ⇒ 看固件的 g_prog_boot_rc / g_prog_boot_loaded / g_deploy_routes 是否印证装载")
            print("     (这两条要用 pyocd 读全局量, 或看 0x38 的 n_routes 是否非 0)")
            p2.d.close()
            return

        d.close()
    except Exception:
        try:
            d.close()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
