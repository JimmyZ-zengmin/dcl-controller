#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_do_mask_owner_test.py —— 判据: **上位机掩码剔除 PE8..PE11 时, 步进引脚会不会被丢在半空**

## 这条判据要回答的那个问题
`g_step_ena_mismatch_n` 曾涨到 **327616 后冻结**。当时只能读到这一个数 ⇒ 被解读成
"有别的代码在驱 PE9"（听起来像野写者）。真因是：
  · `tools/h723_do_hold_test.py` 把 `GPIO_MASK` 换成 **0x00FF**（只登记 PE0..PE7）
  · `tools/h723_w1.py` 注入 **0x7FC00000**（`&0xFFFF = 0`）
⇒ `do_poll()` 的管辖范围不含 bit9 ⇒ **PE9 从此没人驱动**，冻结在旧电平 ⇒
  与 `g_step_ena_pin_intent` 永久不一致 ⇒ 主循环每圈 +1。
★ 真因离读数很远，**因为读数没说清**：一个计数器只能回答一个问题。

## 判据（两档，**必须打两份** —— 缺了对照档就证明不了"这条判据能失败"）
| 档 | `DCL_DO_MASK_UNION` | 期望 |
|---|---|---|
| **交付** | 1 | 注入后 `mismatch_n` 增量 **== 0**；`PE9 实读 == intent`；`step_drop_n` **+1** |
| **对照** | 0 | 注入后 `mismatch_n` 增量 **> 0**（且 hi/lo 分类与实读一致）|

★ 工具**自己识别跑在哪一档**（按注入后 PE9 是否仍跟随 intent），并按那一档判 —— 但**显式打印**，
  不让"档位判错"伪装成 PASS。
★ 收尾必须复原掩码（放 finally）。

用法: python tools/h723_do_mask_owner_test.py [--port COM22] [--keep]
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from h723_client import Dcl, find_board, engine_status      # noqa: E402

CMD_READ  = 0x20          # 0x20 READ  —— 绝对地址
CMD_WRITE = 0x21          # 0x21 WRITE —— 绝对地址
OFF_CTRL_GPIO_MASK = 0x34
STEP_DO_MASK = 0x0F00
MASK_EVIL    = 0x00FF     # ★ 与 h723_do_hold_test 同一个值: 剔掉 PE8..PE11
SUB11_LEN, SUB24_LEN = 32, 32

_tick = 0


def rec(ok, name, detail=""):
    global _tick
    _tick += 1
    print("  [%s] %-46s %s" % ("PASS" if ok else "FAIL", name, detail))
    return ok


def rd(d, addr):
    st, p = d.send(CMD_READ, struct.pack("<I", addr))
    if st != "ACK" or len(p) < 4:
        return None
    return struct.unpack("<I", p[:4])[0]


def wr(d, addr, v):
    st, p = d.send(CMD_WRITE, struct.pack("<II", addr, v))
    return st == "ACK"


def s11(d):
    st, p = d.send(0x39, bytes([19, 11]) + struct.pack("<I", 0))
    if st != "ACK" or len(p) < 8:
        return None
    v = struct.unpack("<8I", p[:SUB11_LEN])
    return dict(rc=v[0], pol_set=v[1], pol=v[2], hold=v[3], rej=v[4],
                mismatch_n=v[5], intent=v[6], actual=v[7])


def s24(d):
    st, p = d.send(0x39, bytes([19, 24]) + struct.pack("<I", 0))
    if st != "ACK" or len(p) != SUB24_LEN:
        return None
    v = struct.unpack("<8I", p[:SUB24_LEN])
    return dict(mismatch_n=v[0], hi=v[1], lo=v[2], drop_n=v[3],
                eff_mask=v[4], last_idr=v[5], last_tick=v[6], host_mask=v[7])


def main():
    port = None
    if "--port" in sys.argv:
        port = sys.argv[sys.argv.index("--port") + 1]
    keep = "--keep" in sys.argv
    d = Dcl(port or find_board())
    es = engine_status(d)
    shm = es["shm"]
    print("=== DO 掩码归属判据 (shm=0x%08X) ===" % shm)

    ok_all = True
    mask0 = rd(d, shm + OFF_CTRL_GPIO_MASK)
    print("  掩码(改前) = 0x%04X" % mask0)
    try:
        # ── 让 PE9 处于一个**确定的意图**, 且与注入后的实际值可能不同 ──
        #    沿用 §4.15 的纪律: 极性先声明, 再使能, 并回读断言。
        d.send(0x39, bytes([19, 5]) + struct.pack("<I", 1))     # 极性声明
        time.sleep(0.2)
        d.send(0x39, bytes([19, 3]) + struct.pack("<I", 1))     # 使能
        time.sleep(0.4)
        a0 = s11(d); b0 = s24(d)
        if a0 is None or b0 is None:
            print("  ✗ sub=11/24 读不到 ⇒ 本判据无效(不是通过)"); return 1
        rec(a0["pol_set"] == 1, "前置: 极性已声明", "pol=%d" % a0["pol"])
        rec(a0["intent"] == a0["actual"], "前置: 注入前 intent == 实读",
            "PE9=%d" % a0["actual"])

        # ── 注入: 把保留位剔出掩码 ──
        if not wr(d, shm + OFF_CTRL_GPIO_MASK, MASK_EVIL):
            print("  ✗ 写掩码被拒 ⇒ 本判据无效"); return 1
        print("  已注入 GPIO_MASK = 0x%04X (剔掉 PE8..PE11)" % MASK_EVIL)
        time.sleep(0.4)

        # ── ★★★ 关键一步: 注入后**改变意图** ──
        #   为什么必须改: 注入**本身**不会造成失配 —— PE9 刚才就是 1, 而"没人驱动"会把它
        #   **冻结在 1**, 恰好等于旧意图 ⇒ 这时看 mismatch 是 0, **看起来"没事"**。
        #   ⇒ 只有把意图翻成 0, 才逼出"引脚到底跟不跟得上"。★ 第一版漏了这步,
        #     于是对照档会**假绿** —— 这正是"判据必须能失败"要防的形态。
        d.send(0x39, bytes([19, 3]) + struct.pack("<I", 0))     # 失能 ⇒ intent = 0
        time.sleep(1.2)                    # 让主循环跑够几百圈

        a1 = s11(d); b1 = s24(d)
        if a1 is None or b1 is None:
            print("  ✗ 注入后读不到 ⇒ 本判据无效"); return 1
        dm = a1["mismatch_n"] - a0["mismatch_n"]
        dd = b1["drop_n"] - b0["drop_n"]
        follows = (a1["actual"] == a1["intent"])
        print("  翻意图后: intent=%d actual=%d   mismatch_n +%d   step_drop_n +%d"
              % (a1["intent"], a1["actual"], dm, dd))
        print("  生效掩码 = 0x%04X   上位机掩码 = 0x%04X" % (b1["eff_mask"], b1["host_mask"]))

        # ── 恒等式: mismatch_n ≡ hi + lo（这一条本身就是判据, 且与档位无关）──
        ok_all &= rec(b1["mismatch_n"] == b1["hi"] + b1["lo"],
                      "恒等式 mismatch_n == hi_n + lo_n",
                      "%d vs %d+%d" % (b1["mismatch_n"], b1["hi"], b1["lo"]))

        if follows:
            # 交付档 (DCL_DO_MASK_UNION=1)
            print("  ⇒ 档位识别: **交付档** (保留位不可剔)")
            ok_all &= rec(a1["intent"] == 0 and a1["actual"] == 0,
                          "交付档: 掩码被剔后 PE9 仍跟随意图", "actual=%d" % a1["actual"])
            ok_all &= rec(dm == 0, "交付档: 翻意图后 mismatch 仍不涨", "+%d" % dm)
            ok_all &= rec(dd >= 1, "交付档: step_drop_n 记到了这次剔除", "+%d" % dd)
            ok_all &= rec((b1["eff_mask"] & STEP_DO_MASK) == STEP_DO_MASK,
                          "交付档: 生效掩码含全部保留位", "0x%04X" % b1["eff_mask"])
            ok_all &= rec((b1["host_mask"] & 0xFFFF) == MASK_EVIL,
                          "交付档: 上位机原值**原样保留**（不偷改其字段）",
                          "0x%04X" % b1["host_mask"])
        else:
            # 对照档 (DCL_DO_MASK_UNION=0) —— 此处**必须红**, 否则说明判据量不出问题
            print("  ⇒ 档位识别: **对照档** (掩码说了算) —— 本档的期望是 mismatch 必须涨")
            ok_all &= rec(a1["intent"] == 0 and a1["actual"] == 1,
                          "对照档: PE9 被丢在半空（冻结在旧值 1）",
                          "intent=%d actual=%d" % (a1["intent"], a1["actual"]))
            ok_all &= rec(dm > 0, "对照档: mismatch 必须涨（判据能失败）", "+%d" % dm)
            ok_all &= rec(b1["hi"] > 0 and b1["lo"] == 0,
                          "对照档: 归因到位 —— 记在 hi_n（实读=1）",
                          "hi=%d lo=%d" % (b1["hi"], b1["lo"]))
            ok_all &= rec(b1["eff_mask"] == MASK_EVIL, "对照档: 生效掩码 == 上位机掩码",
                          "0x%04X" % b1["eff_mask"])
    finally:
        if not keep:
            wr(d, shm + OFF_CTRL_GPIO_MASK, STEP_DO_MASK)     # ★ 复原
            d.send(0x39, bytes([19, 3]) + struct.pack("<I", 0))   # 失能
            print("  已复原: 掩码=0x%04X, 已失能" % STEP_DO_MASK)
    d.close()
    print("=== %s ===" % ("全部通过" if ok_all else "有 FAIL —— 见上"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
