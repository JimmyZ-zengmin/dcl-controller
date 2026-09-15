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

用法:
  python h723_i2c_diag.py [采样秒数]      # 读计数器 (默认 6s)
  python h723_i2c_diag.py --swdio         # ★ 挂核后用 SWD **自己手摇 I2C** 读 AS5600
                                          #   ⇒ 把"器件不应答"与"固件驱动有问题"彻底分开
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


def load_syms(path, names=None):
    txt = open(path, encoding="utf-8", errors="replace").read()
    out = {}
    for n in (names if names is not None else [x[0] for x in WANT]):
        m = re.search(r"0x([0-9a-fA-F]{8,16})\s+" + re.escape(n) + r"\b", txt)
        if m:
            out[n] = int(m.group(1), 16)
    return out


def cmd_swdio():
    """★ 挂核后用 SWD 自己手摇 I2C 读 AS5600 —— 完全绕开固件驱动。

    为什么这是决定性的:
      固件报 100% 地址 NAK, 但这可能是
        ① AS5600 本身不应答 (器件/接线/供电), 或
        ② 固件驱动的时序/状态有问题
      两者在串口侧**完全同形**。本实验用**另一套主控**(SWD + GPIO 寄存器)去问同一个器件,
      谁的问题立刻分开。

    安全性: 先 halt(固件不会碰总线) → 改 PB10/PB11 配置 → 收发 → **恢复原配置** → resume。
            全程 try/finally 保证恢复。
    """
    GPIOB = 0x58020400
    MODER, OTYPER, OSPEEDR, PUPDR, IDR_R, BSRR = (GPIOB + 0x00, GPIOB + 0x04, GPIOB + 0x08,
                                                  GPIOB + 0x0C, GPIOB + 0x10, GPIOB + 0x18)
    SCL, SDA = 10, 11
    ADDR = 0x36

    from pyocd.core.helpers import ConnectHelper
    sess = ConnectHelper.session_with_chosen_probe(
        target_override="stm32h723xx",
        options={"connect_mode": "halt", "frequency": 2000000}, blocking=False)
    if sess is None:
        print("✗ 找不到探针")
        return 1

    print("=" * 78)
    print("SWD 手摇 I2C —— 独立主控问 AS5600 (地址 0x36)")
    print("=" * 78)

    with sess:
        t = sess.target
        t.halt()
        # ------------------------------------------------------------------
        # ★★★ 先自检: 证明"读/写通路 + 挂核"都真的有效, 再谈任何结论
        #     (血证: 四个不同 GPIO 寄存器读出**同一个值**, 说明读数不可信)
        # ------------------------------------------------------------------
        print()
        print("  [自检] 目标状态: %s" % t.get_state())
        cpuid = t.read32(0xE000ED00)
        print("  [自检] SCB->CPUID      = 0x%08X   (Cortex-M7 应为 0x410FC2xx)" % cpuid)
        print("  [自检] DBGMCU_IDCODE   = 0x%08X   (STM32H7 应为 0x0000045x/0x0000048x)"
              % t.read32(0x5C001004))
        print("  [自检] FLASH_SIZE(1FF1E880) = 0x%08X  (H723 应为 0x00000200 = 512KB)"
              % t.read32(0x1FF1E880))
        moder_b = t.read32(0x58020400)
        moder_c = t.read32(0x58020800)
        moder_d = t.read32(0x58020C00)
        moder_e = t.read32(0x58021000)
        print("  [自检] MODER GPIOB/C/D/E = 0x%08X 0x%08X 0x%08X 0x%08X"
              % (moder_b, moder_c, moder_d, moder_e))
        # ★★ 关键的旁证: 外设时钟到底开了没有
        ahb4 = t.read32(0x580244E0)
        print("  [自检] RCC_AHB4ENR = 0x%08X" % ahb4)
        for bit, nm in ((0, "GPIOA"), (1, "GPIOB"), (2, "GPIOC"), (3, "GPIOD"), (4, "GPIOE")):
            print("         %sEN(bit%d) = %d" % (nm, bit, (ahb4 >> bit) & 1))
        print("  [自检] TIM3_CR1=0x%08X TIM3_ARR=0x%08X (外设空间是否可读的旁证)"
              % (t.read32(0x40000400), t.read32(0x4000042C)))
        print("  [自检] USART2_CR1=0x%08X" % t.read32(0x40004400))
        if moder_b == moder_c == moder_d == moder_e:
            print("     ⇒ ✗✗ **四组 GPIO 读出同一个值 ⇒ 读通路不可信** ⇒ 本工具结论全部作废")
        if ((ahb4 >> 1) & 1) == 0:
            print("     ⇒ ★★★ **GPIOB 时钟未开!** ⇒ 这既解释'寄存器读写无效',")
            print("        也解释固件为什么 100% NAK: PB10/PB11 停在上电默认的模拟态, 根本推不动")
        # 写/读回校验 (用 g_as_scan_lo 当草稿, 读完恢复)
        extra = load_syms(MAP, ["g_as_scan_lo", "g_as_scan_hi", "g_as_raw_v"])
        addr = extra.get("g_as_scan_lo")
        if addr:
            old = t.read32(addr)
            t.write32(addr, 0xA5A5A5A5)
            back = t.read32(addr)
            t.write32(addr, old)
            print("  [自检] 写读回 g_as_scan_lo: 写 0xA5A5A5A5 → 读 0x%08X  %s"
                  % (back, "✓ 写通路有效" if back == 0xA5A5A5A5 else "✗✗ **写通路无效**"))
        else:
            print("  [自检] ⚠ 解析不到 g_as_scan_lo ⇒ 跳过写通路校验")
        print()
        save = {n: t.read32(a) for n, a in
                (("MODER", MODER), ("OTYPER", OTYPER), ("OSPEEDR", OSPEEDR), ("PUPDR", PUPDR))}
        print("  已挂核; 原 GPIOB 配置 MODER=0x%08X OTYPER=0x%08X OSPEEDR=0x%08X PUPDR=0x%08X"
              % (save["MODER"], save["OTYPER"], save["OSPEEDR"], save["PUPDR"]))

        nacc = [0]
        tacc = [0.0]

        def rd(a):
            t0 = time.time()
            v = t.read32(a)
            nacc[0] += 1
            tacc[0] += time.time() - t0
            return v

        def wr(a, v):
            t0 = time.time()
            t.write32(a, v)
            nacc[0] += 1
            tacc[0] += time.time() - t0

        def scl_hi():
            wr(BSRR, 1 << SCL)

        def scl_lo():
            wr(BSRR, 1 << (SCL + 16))

        def sda_hi():
            wr(BSRR, 1 << SDA)

        def sda_lo():
            wr(BSRR, 1 << (SDA + 16))

        def sda_v():
            return (rd(IDR_R) >> SDA) & 1

        def scl_v():
            return (rd(IDR_R) >> SCL) & 1

        try:
            # ---- 配置成开漏输出 + 内部上拉 ----
            m = (3 << (SCL * 2)) | (3 << (SDA * 2))
            wr(MODER, (save["MODER"] & ~m) | (1 << (SCL * 2)) | (1 << (SDA * 2)))
            wr(OTYPER, save["OTYPER"] | (1 << SCL) | (1 << SDA))
            wr(OSPEEDR, save["OSPEEDR"] & ~m)
            wr(PUPDR, (save["PUPDR"] & ~m) | (1 << (SCL * 2)) | (1 << (SDA * 2)))
            print("  写入后回读 MODER=0x%08X OTYPER=0x%08X PUPDR=0x%08X  (PB10/11 期望 MODER 位 = 01)"
                  % (rd(MODER), rd(OTYPER), rd(PUPDR)))
            print("     PB10[21:20]=%d%d  PB11[23:22]=%d%d  %s"
                  % ((rd(MODER) >> 21) & 1, (rd(MODER) >> 20) & 1,
                     (rd(MODER) >> 23) & 1, (rd(MODER) >> 22) & 1,
                     "⇒ 已是输出 ✓" if (((rd(MODER) >> 20) & 3) == 1 and ((rd(MODER) >> 22) & 3) == 1)
                     else "⇒ ✗✗ **仍不是输出 ⇒ 写没生效**"))

            # ---- 阶段 1: 引脚级拉低/释放自检 (判"有没有外部上拉") ----
            print()
            print("  阶段1 引脚级自检:")
            scl_lo(); sda_lo()
            lo = rd(IDR_R) & ((1 << SCL) | (1 << SDA))
            scl_hi(); sda_hi()
            hi = rd(IDR_R) & ((1 << SCL) | (1 << SDA))
            print("    拉低时 IDR(PB10|PB11) = 0x%03X   (期望 0x000)" % lo)
            print("    释放时 IDR(PB10|PB11) = 0x%03X   (期望 0x%03X = 两脚都被上拉)"
                  % (hi, (1 << SCL) | (1 << SDA)))
            if lo != 0:
                print("    ⇒ ✗✗ **拉不低** ⇒ 输出通路不通 ⇒ 后面时序全是空谈")
            if hi != ((1 << SCL) | (1 << SDA)):
                print("    ⇒ ✗✗ **释放后有脚不是高** ⇒ 没有外部上拉, 或有东西把线拉住"
                      " (器件未供电/短路/接反) ⇒ 这**直接解释了地址 NAK**")

            # ---- 阶段 2: 完整 I2C 事务 ----
            def start():
                sda_hi(); scl_hi(); sda_lo(); scl_lo()

            def stop():
                sda_lo(); scl_hi(); sda_hi()

            def wr_byte(b):
                for i in range(8):
                    if b & 0x80:
                        sda_hi()
                    else:
                        sda_lo()
                    b = (b << 1) & 0xFF
                    scl_hi()
                    scl_lo()
                sda_hi()                      # 释放 SDA 收 ACK
                scl_hi()
                ack = 0 if sda_v() == 0 else 1
                scl_lo()
                return ack                    # 0 = 收到 ACK

            def rd_byte(ack):
                v = 0
                sda_hi()
                for _ in range(8):
                    scl_hi()
                    v = (v << 1) | sda_v()
                    scl_lo()
                if ack:
                    sda_lo()
                else:
                    sda_hi()
                scl_hi()
                scl_lo()
                sda_hi()
                return v

            print()
            print("  阶段2 完整 I2C 读 (reg 0x0C 角度高字节+低字节):")
            start()
            a1 = wr_byte(ADDR << 1)
            print("    写地址 0x%02X(写)  ⇒ %s" % (ADDR << 1, "ACK ✓" if a1 == 0 else "**NAK ✗**"))
            a2 = wr_byte(0x0C)
            print("    写寄存器 0x0C     ⇒ %s" % ("ACK ✓" if a2 == 0 else "**NAK ✗**"))
            start()
            a3 = wr_byte((ADDR << 1) | 1)
            print("    写地址 0x%02X(读)  ⇒ %s" % ((ADDR << 1) | 1, "ACK ✓" if a3 == 0 else "**NAK ✗**"))
            if a1 == 0 and a2 == 0 and a3 == 0:
                bh = rd_byte(1)
                bl = rd_byte(0)
                stop()
                raw = ((bh << 8) | bl) & 0x0FFF
                print("    读到 RAW = 0x%02X%02X ⇒ **%d (%.2f°)**" % (bh, bl, raw, raw * 360.0 / 4096.0))
            else:
                stop()
                # 再读一次 IDR, 看总线停在哪
                print("    事务失败后 IDR: SCL=%d SDA=%d" % (scl_v(), sda_v()))
                print("    (SDA=0 且不释放 ⇒ 有器件把线拉住; 两线都=1 ⇒ 无人应答)")
            el = tacc[0]
            print()
            print("  SWD 访问统计: %d 次读写 / %.2fs ⇒ 每次 %.2fms"
                  % (nacc[0], el, el / max(nacc[0], 1) * 1000))
            print("  (所以本实验的 I2C 位速率约 %.1f kHz —— 远低于 AS5600 的 1MHz 上限, 时序不是瓶颈)"
                  % (1.0 / max(el / max(nacc[0], 1), 1e-9) / 1000.0))
        finally:
            wr(MODER, save["MODER"])
            wr(OTYPER, save["OTYPER"])
            wr(OSPEEDR, save["OSPEEDR"])
            wr(PUPDR, save["PUPDR"])
            t.resume()
            print()
            print("  ✓ 已恢复 GPIOB 原配置并 resume (固件继续跑)")

    print()
    print("★ 判读:")
    print("  · SWD 手摇 **能读到 RAW** ⇒ AS5600 与接线**都是好的** ⇒ 问题在固件的 I2C 驱动/状态机")
    print("  · SWD 手摇 **同样 NAK** ⇒ AS5600 侧问题 (供电/接线/器件) ⇒ 别在固件里找了")
    print("  · 阶段1 释放后有脚不为高 ⇒ 没有外部上拉/线被拉住 ⇒ 这就是 NAK 的直接原因")
    return 0


def main():
    if "--swdio" in sys.argv:
        return cmd_swdio()
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
