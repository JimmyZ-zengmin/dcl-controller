#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_persist_win.py — **落盘窗口**的协议侧端到端验收 (不需要调试器)。

## 为什么需要它 (而不是只用 h723_persist.py)
`h723_persist.py` 是 **pyocd 直驱**的, 而 pyocd 会话会把核 **HALT 数百 ms**
⇒ 喂狗停 ⇒ **IWDG 复位** ⇒ 它自己就把被测对象动了 ("铁律 0: 观测不得改变被测对象")。
本工具**全程走协议** (`0x22` 读 AXI / `0x43` 查询与落盘 / `0x12` 停机 / `0x01` 探活),
观测动作对被测对象**无副作用**。

## 被测特性
`persist_save()` 在擦写段临时把 IWDG 超时放大到 `WDT_PERSIST_WINDOW_MS`,
完成后在**单一出口**恢复。背景(实测): 擦 flash 期间拍 ISR 的 `wdt_feed()`
(写 `IWDG_KR`)**无法完成** ⇒ ISR 卡住 ⇒ 喂狗停 ⇒ 200ms 后 IWDG 复位
⇒ "保存"变"重启"且配置从未落盘。详见 docs/audit/H723-PERSIST-WDT-DEFECT.md §11/§12。

## 判据 (缺一不可)
  P1 `writes > 0`            —— 落盘真的完成 (不是"回了 ACK")
  P2 启动次数不变            —— 落盘期间没复位 (读 AXI `BOOT_AXI[0]`)
  P3 ISR 检查点 = ⑦          —— ISR 完整跑完, 没卡在喂狗那条语句
  P4 ★反向: 落盘后空等 12s (>8s 窗口) 板子**仍活着**(tick 仍推进)
         —— 证明窗口**确实被关回去了**。若窗口没恢复(永久 8s), ISR 会一直卡在喂狗
            ⇒ 8s 后必然复位 ⇒ P4 抓得到。**这条是"保护没被废掉"的判据。**
  P5 `0x01` 仍 ACK           —— 板子可用

## 用法
    python tools/h723_persist_win.py [COMxx]
    # 对照档 (应当 FAIL, 用来证明判据能失败):
    #   bash build.sh -DDCL_PERSIST_SAVE=1 -DDCL_WDT_PERSIST_WINDOW=0
    #   ⇒ 预期 P1/P2/P3 FAIL (落盘被 200ms 看门狗打断)

退出码: 0 = 全过 / 1 = 有 FAIL
"""
import sys
import time
import os
import serial

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # 与本文件同目录的 h723_proto
from h723_proto import crc16

BOOT = 0x24000500          # BOOT_AXI 基址 (与 manifest.h 的 MF_ENTRY 一致)
WINDOW_WAIT_S = 12.0       # > 8s 窗口: 用来证明窗口被关回去了


def mk(cmd, p=b""):
    """构帧。★ len 字段必须 = 载荷长度 —— 不要用 h723_proto.build_frame:
    它在 force_len=None 时把 len 写成 0 (只为无载荷用例设计) ⇒ 带载荷的帧退化成空载荷。"""
    b = bytes([cmd, len(p) & 0xFF, (len(p) >> 8) & 0xFF]) + p
    c = crc16(b)
    return bytes([0xC0]) + b + bytes([c & 0xFF, c >> 8])


def talk(ser, fr, to=8.0):
    ser.reset_input_buffer()
    ser.write(fr)
    ser.flush()
    buf = b""
    end = time.time() + to
    while time.time() < end:
        ch = ser.read(1)
        if not ch:
            continue
        buf += ch
        i = buf.find(b"\xC1")
        if i < 0:
            buf = b""
            continue
        if i > 0:
            buf = buf[i:]
        if len(buf) < 4:
            continue
        n = buf[2] | (buf[3] << 8)
        need = 4 + n + 2
        while len(buf) < need and time.time() < end:
            m = ser.read(need - len(buf))
            if m:
                buf += m
        if len(buf) >= need:
            return buf[1], buf[4:4 + n]
    return None, None


def rd(ser, addr, cnt):
    """0x22 READ_BURST [addr:u32][count:u16] (绝对地址; AXI 诊断窗已放行)"""
    sts, r = talk(ser, mk(0x22, addr.to_bytes(4, "little") + cnt.to_bytes(2, "little")))
    if sts != 0 or not r or len(r) < cnt * 4:
        return None
    return [int.from_bytes(r[i * 4:i * 4 + 4], "little") for i in range(cnt)]


def stat(ser, tag):
    sts, pl = talk(ser, mk(0x43))
    if sts != 0 or not pl or len(pl) < 24:
        print("  [%s] 0x43 失败 sts=%s" % (tag, sts))
        return None
    d = dict(writes=int.from_bytes(pl[20:24], "little"), dirty=pl[7] & 1,
             ab=pl[12], nr=int.from_bytes(pl[1:3], "little"),
             err=int.from_bytes(pl[14:16], "little"))
    print("  [%s] dirty=%d writes=%d ab=%d nr=%d err=0x%04X"
          % (tag, d["dirty"], d["writes"], d["ab"], d["nr"], d["err"]))
    return d


def live(ser):
    """活体: (启动次数, stage, 主循环 tick)"""
    r = rd(ser, BOOT, 4)
    r2 = rd(ser, BOOT + 30 * 4, 2)
    return (r[0] if r else None,
            r2[0] if r2 else None,
            r2[1] if r2 else None)


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM18"
    ser = serial.Serial(port, 115200, timeout=0.2)
    print("=== 落盘窗口验收 (协议侧, 无调试器) @ %s ===" % port)

    b0, st0, t0 = live(ser)
    print("\n[0] 基线: 启动次数=%s stage=%s tick=%s" % (b0, st0, t0))
    if b0 is None:
        print("!! 读不到 BOOT_AXI —— 链路或固件不对, 后续判据无效")
        return 2

    print("\n[1] 0x43[1] (RUN 态) ⇒ 预期被 PERSISTENT 门跳过并置 dirty")
    talk(ser, mk(0x43, b"\x01"))
    stat(ser, "run-skip")

    print("\n[2] 0x12 STOP")
    sts, _ = talk(ser, mk(0x12))
    print("    sts=%s" % sts)
    time.sleep(0.5)

    print("\n[3] ★ 0x43[1] 显式落盘 (STOP 态 ⇒ 真擦 128KB)")
    t = time.time()
    sts, pl = talk(ser, mk(0x43, b"\x01"), to=25.0)
    print("    sts=%s 耗时 %.2fs" % (sts, time.time() - t))
    if pl and len(pl) >= 24:
        print("    应答: writes=%d dirty=%d ab=%d nr=%d err=0x%04X"
              % (int.from_bytes(pl[20:24], "little"), pl[7] & 1, pl[12],
                 int.from_bytes(pl[1:3], "little"),
                 int.from_bytes(pl[14:16], "little")))

    print("\n[4] 等 3s 看状态")
    time.sleep(3.0)
    b1, st1, t1 = live(ser)
    print("    启动次数=%s stage=%s tick=%s" % (b1, st1, t1))
    r2 = rd(ser, BOOT + 0x80, 11)
    seg = (r2[8] >> 28) if r2 else None
    if r2:
        print("    ISR 检查点: 本轮段=%d 上轮段=%d  停滞自愈=%d  最大间隔=%d拍(%.0fms)"
              % (r2[8] >> 28, r2[9] >> 28, r2[2], r2[3], r2[3] / 10.0))

    print("\n[5] ★ 反向判据 P4: 空等 %.0fs (>8s 窗口) —— 板子必须仍活着" % WINDOW_WAIT_S)
    time.sleep(WINDOW_WAIT_S)
    b2, st2, t2 = live(ser)
    print("    启动次数=%s stage=%s tick=%s" % (b2, st2, t2))
    s1 = stat(ser, "final")
    sts, _ = talk(ser, mk(0x01), to=3.0)

    print("\n===== 判定 =====")
    ok = True
    if s1 and s1["writes"] > 0:
        print("  ✓ P1 writes=%d > 0 ⇒ 落盘完成" % s1["writes"])
    else:
        print("  ✗ P1 writes=%s ⇒ 没落盘" % (s1 and s1["writes"]))
        ok = False
    if b0 == b2:
        print("  ✓ P2 启动次数 %s 不变 ⇒ 全程未复位" % b0)
    else:
        print("  ✗ P2 启动次数 %s→%s ⇒ 复位了" % (b0, b2))
        ok = False
    if seg == 7:
        print("  ✓ P3 ISR 检查点 = ⑦ (出口) ⇒ 没卡在喂狗那条语句")
    else:
        print("  ✗ P3 ISR 停在段 %s (⑬=喂狗前)" % seg)
        ok = False
    if t1 is not None and t2 is not None and t2 > t1:
        print("  ✓ P4 12s 后 tick 仍推进 (%d→%d) ⇒ 窗口已关回, 保护没被废" % (t1, t2))
    else:
        print("  ✗ P4 tick 停滞 (%s→%s) ⇒ 窗口可能没恢复" % (t1, t2))
        ok = False
    if sts == 0:
        print("  ✓ P5 板子仍可用")
    else:
        print("  ✗ P5 板子无响应")
        ok = False
    print("\n%s" % ("★★ 通过 —— 落盘窗口成立" if ok else "未通过"))
    ser.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
