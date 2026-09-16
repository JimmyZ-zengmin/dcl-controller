# -*- coding: utf-8 -*-
"""h723_i2c_leak_check.py —— **总线独占门的引用计数泄漏判据**（G6-2 族，2026-09-16）

## 它挡的是什么（真实缺陷，不是理论风险）
`i2c_sm_request()` 原实现是"**先 `i2c_bus_acquire()`、再校验参数**"，而三条"参数非法"分支
直接 `return 0` —— **不放门** ⇒ **引用计数泄漏** ⇒ `owner` 永久卡在 `I2C_OWNER_SM`，
**阻塞路径（AS5600）再也拿不到总线**。

为什么它特别值得一条专用判据：**症状离真因极远**。
实测那次，所有依赖阻塞路径的判据一起变红（AS5600 轮询、G6-2 的 G1/G4/G5、G6-3 的 S6），
所有人都会先去查"总线被谁占了"，而真因是**很久以前一次 `len=9` 的非法请求**。

## 怎么判（两步，都能失败）
1. **基线干净**：静息态 `owner == 0`（NONE）且 `refs == 0`。
   ★ 基线不干净就直接判**无效**（不是 PASS）—— 板子已被别的测试污染，判据无意义。
2. **制造一次参数非法的请求**（`len=9 > I2C_SM_MAX_DATA(8)` ⇒ 必须 `IX_STATUS == 5(BADARG)`）
   ⇒ 之后 `owner` 与 `refs` **必须仍为 0**。
   ★ 中间那条断言很重要：**"没跑到该分支 ⇒ 本判据无效，不是通过"** ——
     否则"请求根本没被受理"会被读成"没有泄漏"。

## 观测量
- `0x39 op=22 sub=2`（只查询，不占门）：`+4` owner（0=NONE 1=BLOCKING 2=SM）、`+32` refs
- SHM 事务区：`IX_REQ`(+28) / `IX_REQ_SEQ`(+4) / `IX_DONE_SEQ`(+8) / `IX_STATUS`(+12)

## 用法
    python tools/h723_i2c_leak_check.py [--port COM21]
退出码：0 = 通过；2 = 判据失败；1 = 环境/协议错误（含"基线不干净 ⇒ 判据无效"）。
"""
import argparse
import struct
import sys
import time

sys.stdout.reconfigure(errors="replace")          # ★ GBK 控制台上 print 一个 ⇒ 就崩（本项目踩过）
sys.path.insert(0, "tools")
from h723_client import Dcl                        # noqa: E402

OFF_IX          = 0x7200
IX_REQ_SEQ      = OFF_IX + 4
IX_DONE_SEQ     = OFF_IX + 8
IX_STATUS       = OFF_IX + 12
IX_REQ          = OFF_IX + 28

ST_BADARG = 5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    a = ap.parse_args()

    d = Dcl(a.port)
    print("端口 = %s" % d.port)
    try:
        sts, p = d.send(0x38)
        if sts != "ACK" or len(p) < 27:
            print("[FAIL] 0x38 无有效应答 ⇒ 环境错误")
            return 1
        shm = struct.unpack("<I", p[23:27])[0]
        print("SHM = 0x%08X" % shm)

        def rd(off, w):
            # ★ 用 `expect_len` 防串帧：应答**没有命令码回显**（GAP-12），
            #   长度不符即判"这条不属于本次请求"（本仓 2026-09-16 真的被吃过一次 0x01 的应答）。
            sts, r = d.send(0x22, struct.pack("<IH", shm + off, w), expect_len=w * 4)
            if sts != "ACK" or len(r) < w * 4:
                raise IOError("0x22 应答异常: %s len=%d" % (sts, len(r)))
            return list(struct.unpack("<%dI" % w, r[:w * 4]))

        def wr(off, vals):
            pay = struct.pack("<IH", shm + off, len(vals)) + \
                  struct.pack("<%dI" % len(vals), *vals)
            sts, _ = d.send(0x23, pay)
            if sts != "ACK":
                raise IOError("0x23 被拒: %s" % sts)

        def gate():
            """0x39 op=22 sub=2 = 只查询（不占门）"""
            sts, r = d.send(0x39, struct.pack("<BBI", 22, 2, 0))
            if sts != "ACK" or len(r) < 36:
                raise IOError("0x39 op=22 应答异常: %s len=%d" % (sts, len(r)))
            return struct.unpack("<9I", r[:36])

        g0 = gate()
        print("基线: owner=%d refs=%d" % (g0[1], g0[8]))
        if g0[1] != 0 or g0[8] != 0:
            print("[FAIL] 基线就不干净（owner=%d refs=%d）⇒ **本判据无效**，不是通过。"
                  "先复位板子再跑。" % (g0[1], g0[8]))
            return 1

        seq = rd(IX_REQ_SEQ, 1)[0]
        # len=9 > I2C_SM_MAX_DATA(8)：必须是 BADARG（本判据要的就是"参数非法"这条出口）
        bad = 0x36 | (1 << 8) | (0x0C << 16) | (9 << 24)
        seq += 1
        wr(IX_REQ, [bad])
        wr(IX_REQ_SEQ, [seq])                       # ★ seq **最后**写（它才是"提交"）

        for _ in range(80):
            if rd(IX_DONE_SEQ, 1)[0] == seq:
                break
            time.sleep(0.05)

        ix = rd(OFF_IX, 4)
        print("非法请求后: IX_STATUS=%d (应 5=BADARG)  done_seq=%d" % (ix[3], ix[2]))
        if ix[3] != ST_BADARG:
            print("[FAIL] 请求没有被判为 BADARG ⇒ **本判据没跑到要考的那条出口，判为无效**"
                  "（不是通过）")
            return 1

        g1 = gate()
        print("owner=%d refs=%d  (都应为 0)" % (g1[1], g1[8]))
        if g1[1] != 0 or g1[8] != 0:
            print("[FAIL] ★ 引用计数泄漏: owner=%d refs=%d ⇒ 阻塞路径将永久拿不到总线"
                  % (g1[1], g1[8]))
            return 2

        print("[PASS] 参数非法路径不再泄漏总线门（先验参、再拿门）")
        return 0
    finally:
        d.close()


if __name__ == "__main__":
    sys.exit(main())
