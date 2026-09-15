#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_i2c_diag.py — 经 SWD 直读固件里的 I2C / AS5600 诊断计数器
================================================================
为什么需要它:
  串口应答(0x39 op=19) **没有暴露** i2c_bb.c / as5600.c 里的失败计数器。
  于是"编码器读失败 ⇒ 保留上一次值"这个故障在主机侧**与"轴不动"完全同形**。
  血证: raw 冻结在 107 时, 我判成"轴不转", 而现场轴在转。
⇒ 用 SWD 直接读全局量 (从 .map 解析地址, 不写死), 并**以固件自己的计数**判定:
     g_as_err_n 在涨  ⇒ I2C 读在失败 (就是它)
     g_i2c_stuck_n 在涨 ⇒ SCL 被拉住(时钟延展/总线卡死)
     g_i2c_nak_n 在涨  ⇒ 器件不应答 (线/供电/地址)

★ 纪律: connect_mode=halt (挂核但**不复位**), 读全局量前绝不 reset;
        采样时**必须 resume**(挂核时固件不跑, 计数器不会动)。

用法:  python h723_i2c_diag.py [采样秒数]
"""
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAP = r"D:\STM\8.29 AIAutoFactior\9.10 H723newest\build\dcl_h723.map"

WANT = [
    # AS5600 层
    ("g_as_ok_n",      "AS5600 读成功次数"),
    ("g_as_err_n",     "AS5600 读失败次数"),
    ("g_as_last_err",  "最后一次失败码 (1=地址NAK 2=寄存器NAK 3=读地址NAK)"),
    ("g_as_raw_v",     "★ 固件缓存的编码器 raw (串口读的就是它)"),
    ("g_as_status",    "AS5600 STATUS 寄存器"),
    ("g_as_mag_ok",    "磁状态 MD (1=磁场在量程内)"),
    # I2C 位操作层
    ("g_i2c_tx_n",     "I2C 事务总数"),
    ("g_i2c_ok_n",     "I2C 事务成功数"),
    ("g_i2c_nak_n",    "I2C NAK 次数"),
    ("g_i2c_stuck_n",  "★ SCL 被拉住次数 (总线卡死)"),
    ("g_i2c_timeout_n", "I2C 超时次数"),
]


def load_syms(path):
    txt = open(path, encoding="utf-8", errors="replace").read()
    out = {}
    for n, _d in WANT:
        m = re.search(r"0x([0-9a-fA-F]{8,16})\s+" + re.escape(n) + r"\b", txt)
        if m:
            out[n] = int(m.group(1), 16)
    return out


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    if not os.path.exists(MAP):
        print("✗ 找不到 %s" % MAP)
        return 1
    addr = load_syms(MAP)
    miss = [n for n, _ in WANT if n not in addr]
    print("=" * 78)
    print("H723 I2C/AS5600 诊断 (SWD 直读)")
    print("=" * 78)
    print("  从 .map 解析到 %d/%d 个符号" % (len(addr), len(WANT)))
    if miss:
        print("  ⚠ 未解析到 (可能在当前 .map 里被优化掉了 / 固件没重编): %s" % ", ".join(miss))
    if not addr:
        return 1

    from pyocd.core.helpers import ConnectHelper
    sess = ConnectHelper.session_with_chosen_probe(
        target_override="stm32h723xx",
        options={"connect_mode": "halt", "frequency": 2000000},
        blocking=False)
    if sess is None:
        print("✗ 找不到探针 (DAPLink/CMSIS-DAP)")
        return 1

    with sess:
        t = sess.target
        t.halt()
        snap = {n: t.read32(a) for n, a in addr.items()}
        t.resume()                                     # ★ 必须恢复, 否则固件不跑
        print()
        print("  初始快照:")
        for n, d in WANT:
            if n in snap:
                print("    %-16s 0x%08X  %10u    %s" % (n, snap[n], snap[n], d))
        print()
        print("  连续采样 %.1fs (固件运行中, SWD 只读不干扰):" % secs)
        t0 = time.time()
        hist = []
        while time.time() - t0 < secs:
            try:
                hist.append({n: t.read32(a) for n, a in addr.items()})
            except Exception as ex:
                print("    读失败: %s" % ex)
                break
        el = time.time() - t0
        if len(hist) < 2:
            print("    采样点太少")
            return 1
        print("    %d 个采样点 / %.2fs ⇒ **%.0f 采样/s** (这条通道比串口 27Hz 快 %.0f 倍)"
              % (len(hist), el, len(hist) / el, len(hist) / el / 27.0))
        print()
        print("  增量 (末 − 首):")
        print("    %-16s %14s | %14s | %s" % ("量", "增量", "速率/s", "判读"))
        for n, d in WANT:
            if n not in addr:
                continue
            a0, a1 = hist[0][n], hist[-1][n]
            dl = a1 - a0
            rate = dl / el
            if n in ("g_as_raw_v", "g_as_status"):
                uniq = len(set(h[n] for h in hist))
                extra = "唯一值 %d %s" % (uniq, "(★ 冻结, 读路径没在更新)" if uniq == 1 else "(在更新)")
                print("    %-16s %14s | %14s | %s" % (n, "—", "—", extra))
                continue
            verdict = ""
            if n == "g_as_err_n" and rate > 0.5:
                verdict = "★★ **I2C 读在失败** ⇒ 'raw 冻结' 的真因"
            elif n == "g_as_ok_n" and abs(rate) < 0.5:
                verdict = "★★ **成功次数不涨** ⇒ 读路径已停"
            elif n == "g_i2c_stuck_n" and rate > 0.5:
                verdict = "★★ **SCL 被拉住** ⇒ 总线卡死, 需要 9 时钟 + STOP 恢复"
            elif n == "g_i2c_nak_n" and rate > 0.5:
                verdict = "★ 器件不应答 ⇒ 查接线/供电/地址"
            elif rate > 0.5:
                verdict = "在涨"
            else:
                verdict = "静止"
            print("    %-16s %14d | %14.1f | %s" % (n, dl, rate, verdict))
        print()
        print("★ 判读要点:")
        print("  · `g_as_raw_v` 唯一值=1 ⇒ 编码器值冻结 ⇒ **任何运动结论都不可信**")
        print("  · `g_as_err_n` 在涨 且 `g_as_last_err` ≠ 0 ⇒ I2C 读失败(真因)")
        print("  · `g_i2c_stuck_n` 在涨 ⇒ SCL 卡死 ⇒ i2c_bb_read 里**没有 bus_recover**,")
        print("    一旦卡住就永远失败 ⇒ 值冻结在最后一次成功读到的数")
    return 0


if __name__ == "__main__":
    sys.exit(main())
