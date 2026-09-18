#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判定桶表矛盾: 读**路由表**逐条取 period, 自己算相位直方图, 与桶表对照。

## 为什么这样能判定
`engine_stage_program`（`engine.c:983-995`）做两件事：
  ① `r.period = (uint8_t)(dv | (uint8_t)(ph << 2))` 写进**路由表**（dst 区）
  ② `slot = cur2[ph]++` 把这条排进**桶表**（cnt2/off2）
⇒ **路由表里的 period 就是桶表分桶的依据**。
⇒ 所以：读回路由表 → 自己按 firmware 的同一套位域规则解出 (dv, ph) → 直方图，
   与桶表的 cnt2 对照。**两者必须一致**；不一致就说明有一侧我理解错了，
   而**先错的一定是我的解读**（因为两侧都是固件自己写的）。

## 已知事实（先摆出来, 免得又靠猜）
  · `PERIOD_DIV_MASK = 0x03`，`PERIOD_PHASE_SHIFT = 2`（engine.h:872-873）
  · `RouteEntry_t.period` 在 **offset 14**（engine.h:795），16 字节一条
  · `OFF_ROUTE_TABLE = 0x0840`，128 条 × 16 B
  · 预期（若相位真的只有 6 位）：ph = (period>>2) & 0x3F ∈ 0..63
  · 实测桶表 cnt2[64..99] = 1 ⇒ 若路由表解出的 ph 也只到 63，则**桶表与路由表不一致**
"""
import os, struct, sys, time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from h723_client import Dcl

cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst = 0x38, 0x10, 0x12, 0x11, 0x22
OFF_ROUTE_TABLE = 0x0840
OFF_ROUTE_BUCKETS = 0x4480
OFF_CTRL_N_ROUTES = 0x0E
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02


def mk(op, div, n):
    fl = FLAG_ACTIVE | FLAG_WIRE2
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  fl, i, (i % 64) + 1, 0, i, div, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    states = b"\x00" * (16 * (min(n, 64) + 1))
    return struct.pack("<HHH", n, n, min(n, 64) + 1) + routes + params + states + b"\x00" * 16


def rd(dcl, addr, nwords):
    out, off = b"", 0
    while off < nwords:
        k = min(200, nwords - off)
        sts, p = dcl.send(cmd_burst, struct.pack("<IH", addr + 4 * off, k), expect_len=None)
        if sts != "ACK" or len(p) < 4 * k:
            return None
        out += p[:4 * k]
        off += k
    return out


def main():
    dcl = Dcl(os.environ.get("DCL_PORT") or None)
    print("端口 = %s" % dcl.port); time.sleep(1.0)
    sts, p = dcl.send(cmd_status, expect_len=51)
    if sts != "ACK" or len(p) < 51:
        print("!! 0x38 失败"); return 2
    SHM = struct.unpack("<I", p[23:27])[0]
    print("SHM = 0x%08X" % SHM)
    ok = 0
    for _ in range(8):
        v = rd(dcl, SHM + 0x3880, 2)
        if v and struct.unpack("<2I", v[:8])[1] >= 200000:
            ok += 1
            if ok >= 3:
                break
        else:
            ok = 0
        time.sleep(0.25)
    if ok < 3:
        print("!! 板子不健康 ⇒ 判无效"); dcl.close(); return 2

    N = int(sys.argv[1]) if len(sys.argv) > 1 else 128
    DIV = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    print("=== n=%d div=%d ===" % (N, DIV))
    sts, pp = dcl.send(cmd_deploy, mk(5, DIV, N), expect_len=None)   # PID
    print("deploy: %s" % sts)
    if sts != "ACK":
        dcl.close(); return 2
    dcl.send(cmd_stop); time.sleep(0.12)
    dcl.send(cmd_start); time.sleep(0.25)

    raw = rd(dcl, SHM + OFF_ROUTE_TABLE, N * 4)      # 128 × 16B = 512 words... 实际 2048B = 512 words
    if raw is None:
        print("!! 路由表读失败"); dcl.close(); return 2
    print("路由表读到 %d 字节（期望 %d）" % (len(raw), N * 16))

    # period 在每条的 offset 14
    divs, ph6, ph8 = Counter(), Counter(), Counter()
    for i in range(N):
        period = raw[i * 16 + 14]
        dv = period & 0x03
        divs[dv] += 1
        ph6[(period >> 2) & 0x3F] += 1      # firmware 的 6 位读法
        ph8[(period >> 2) & 0xFF] += 1      # 全 8 位（看 bit6 是否被用到）
    print("\n=== 路由表解出的 div 分布 ===")
    print("  %s" % dict(divs))
    print("\n=== 相位直方图（两种读法）===")
    print("  6 位读法 (period>>2)&0x3F : 取值个数 %d, 范围 %d..%d"
          % (len(ph6), min(ph6), max(ph6)))
    print("  8 位读法 (period>>2)&0xFF : 取值个数 %d, 范围 %d..%d"
          % (len(ph8), min(ph8), max(ph8)))
    print("\n  ★ 判定: 若 8 位读法给出 0..99 且每个 1~2 次 ⇒ **相位确实用了 8 位字段**")
    print("         则 (uint8_t)(ph<<2) 在 ph>=64 时**不会**溢出到低位 ——")
    print("         因为那意味着 period 的 bit6/bit7 必须能装 (ph<<2) 的高位。")
    # 看 period 的原始值分布，直接判 bit6/bit7 是否出现
    b67 = Counter((raw[i * 16 + 14] >> 6) & 0x03 for i in range(N))
    print("\n  period 的 bit7..bit6 取值分布: %s" % dict(b67))
    # ★ 直接打原始 period 值的直方图（不再靠"范围/个数"推断）
    print("\n=== 原始 period 字节直方图（应为 dv|(ph<<2)，div=2 ⇒ period = 2|(ph<<2)）===")
    pc = Counter(raw[i * 16 + 14] for i in range(N))
    print("  不同 period 值 %d 个:" % len(pc))
    exp = sorted(set(2 | (p << 2) for p in range(100)))
    got = sorted(pc)
    print("  期望（ph=0..99 无截断）: %d 个值, 最小 %d 最大 %d"
          % (len(exp), exp[0], exp[-1]))
    print("  实测:                     %d 个值, 最小 %d 最大 %d"
          % (len(got), got[0], got[-1]))
    print("  实测 period 值: %s" % got)
    print("\n  ★ 逐条回代: 用 period>>2 当相位, 与固件的分桶循环对照")
    print("     固件数桶用 s2++%100 → 相位 0..99 ; 写路由用 (uint8_t)(ph<<2)")
    ok_lo = sum(1 for i in range(N) if (raw[i * 16 + 14] >> 2) < 64)
    print("     period>>2 < 64 的条数 = %d / %d" % (ok_lo, N))

    print("\n=== 桶表（对照）===")
    bk = rd(dcl, SHM + OFF_ROUTE_BUCKETS, 110)
    if bk:
        u = struct.unpack("<220H", bk)
        cnt2 = list(u[120:220])
        nz = sum(1 for c in cnt2 if c)
        print("  非空桶 %d ; cnt2 总和 %d" % (nz, sum(cnt2)))
        print("  cnt2[64..99] = %s" % cnt2[64:100])
    nr = rd(dcl, SHM + OFF_CTRL_N_ROUTES, 1)
    if nr:
        print("  OFF_CTRL_N_ROUTES = %d" % struct.unpack("<H", nr[:2])[0])
    dcl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
