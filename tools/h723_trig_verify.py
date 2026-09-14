#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_trig_verify.py — MDMA 锁存触发链的**自证仪器** (0x39 op=6)

为什么需要它
------------
方案②(把 MDMA 锁存触发源从 TIM2_UP 换成 TIM2_CH4)第一次**静默失效**:
`DMAMUX1_C8` 改成了 21, 但 `do.c` 只置了 `TIM_DIER.UDE` —— 定时器的 DMA 请求
被 DIER 逐位门控, `CC4DE` 没开 ⇒ **请求永远不产生**。而当时唯一的观测手段是
LA 波形, 只能"反推"没切成功, 绕了一大圈。

★ 本文件的立场: **凡"写进去就不再读一眼"的配置量, 都必须有自证面。**
  (与 pyocd 无关 —— pyocd 读 AHB 外设返回垃圾, 且 `-c reset` 会毁掉运行态。)

用法
----
  python h723_trig_verify.py                # 只读自证块
  python h723_trig_verify.py --sel 1        # 切到 CC4 (ccr4 默认 10000 = 50µs) 再读
  python h723_trig_verify.py --sel 0        # 切回 TIM2_UP
  python h723_trig_verify.py --sel 1 --ccr4 4000   # CC4 = 20µs 处锁存
  python h723_trig_verify.py --on           # 顺带开诊断 (op=1) 并确保 MDMA EN=1
"""
import argparse
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from h723_client import Dcl, find_board   # noqa: E402

CMD_PIN_PATTERN = 0x39

# DMAMUX1 请求号 (三源核对: ST 官方头 / stm32-hal2 / H730 HAL)
REQ_NAME = {21: "TIM2_CH4", 22: "TIM2_UP"}

# TIM2_DIER 位
DIER_BIT = {0: "UIE", 8: "UDE", 9: "CC1DE", 10: "CC2DE", 11: "CC3DE", 12: "CC4DE"}
# TIM2_SR 位
SR_BIT = {0: "UIF", 1: "CC1IF", 2: "CC2IF", 3: "CC3IF", 4: "CC4IF"}


def bits(v, table):
    out = []
    for b, name in sorted(table.items()):
        if (v >> b) & 1:
            out.append(name)
    return ",".join(out) if out else "(none)"


def read_block(d):
    sts, p = d.send(CMD_PIN_PATTERN, bytes([0x06]))
    if sts != "ACK" or len(p) < 32:
        return None
    w = struct.unpack("<8I", p[:32])
    return dict(dmamux_c8=w[0], dier=w[1], sr=w[2], ccer=w[3],
                trig_sel=w[4], trig_ccr4=w[5], dma2s0_cr=w[6], mdma_ccr=w[7])


def dwt_rearm(d):
    """op=7: 重新 dwt_enable() + 清引脚码型统计。

    ★ 为什么每次量之前都要调它: pyocd/调试器会话退出时**调试域断电 ⇒ DWT 寄存器复位
      ⇒ CYCCNTENA 被清 ⇒ CYCCNT 冻结在 0**。此时 ISR 照跑、wr_n 照涨, 但所有
      DWT 派生的量 min/max/分档 **全是 0** —— 判据变成**空判据**(永远通过)。
      本项目 modbus.c:79 / faultlog.h:75 早就记录过这条陷阱, 这里是它的"解药"。
    """
    sts, p = d.send(CMD_PIN_PATTERN, bytes([0x07]))
    if sts != "ACK" or len(p) < 24:
        return None
    w = struct.unpack("<6I", p[:24])
    return dict(dwt_ctrl=w[0], demcr=w[1], c1=w[2], c2=w[3], delta=w[4], alive=w[5])


def latch_alive(d, wait=0.35):
    """★ 锁存活判据 (固件侧, **不依赖 LA**)。

    原理: `op=2` 会**先读 ODR** 再用 `GPIO_BSRR = 0x7F` 把 PE0..PE6 强制置 1
    (所以同一应答里 `odr_after` 恒为 0x7F)。
    ⇒ 若 MDMA 锁存还活着, wait 之后影子里的值一定被重新锁存进 ODR, 于是
      第二次读到的 ODR ≠ 0x7F; 若锁存死了, ODR 会**一直停在 0x7F**。

    ★ 这个判据能失败 (0x7F 不是合法码型吗? 是 —— 所以还要加"且 == val"这一条),
      并且不需要任何外部仪器。用来在 LA 之前先确认"触发源到底通不通"。
    返回 (odr2, val2, alive:bool)
    """
    d.send(CMD_PIN_PATTERN, bytes([0x02]))          # 第一次: 污染 ODR=0x7F
    time.sleep(wait)
    sts, p = d.send(CMD_PIN_PATTERN, bytes([0x02]))
    if sts != "ACK" or len(p) < 64:
        return None, None, None
    a = struct.unpack("<16I", p[:64])
    odr, val = a[6] & 0xFF, a[5] & 0xFF
    return odr, val, (odr != 0x7F and odr == val)


def raw_inject(d, req, dier):
    """op=8: 直接注入 DMAMUX1_C8 请求号 + TIM2_DIER 原始值。"""
    sts, _ = d.send(CMD_PIN_PATTERN, bytes([0x08, req & 0xFF]) + struct.pack("<I", dier))
    return sts


def show(tag, b):
    if b is None:
        print("  %-14s ✗ 无应答 (0x39 op=6)" % tag)
        return False
    ok_route = b["dmamux_c8"] in REQ_NAME
    want = 21 if b["trig_sel"] else 22
    ok_match = (b["dmamux_c8"] == want)
    ok_gate = bool(b["dier"] & ((1 << 8) | (1 << 12)))
    # ★ 门控必须**恰好**与路由同侧: 路由到 CC4 就必须 CC4DE=1 且 UDE=0
    if b["trig_sel"]:
        ok_gate_exact = bool(b["dier"] & (1 << 12)) and not (b["dier"] & (1 << 8))
    else:
        ok_gate_exact = bool(b["dier"] & (1 << 8)) and not (b["dier"] & (1 << 12))
    print("  %-14s DMAMUX1_C8=%-3d(%-9s) DIER=%s SR=%s CCER=%s" %
          (tag, b["dmamux_c8"], REQ_NAME.get(b["dmamux_c8"], "?"),
           bits(b["dier"], DIER_BIT), bits(b["sr"], SR_BIT), bits(b["ccer"], {12: "CC4E"})))
    print("  %-14s sel=%d ccr4=%d(%dµs) DMA2S0_CR=0x%X(EN=%d) MDMA_CCR=0x%X(EN=%d)" %
          ("", b["trig_sel"], b["trig_ccr4"], b["trig_ccr4"] * 5 // 1000,
           b["dma2s0_cr"], b["dma2s0_cr"] & 1, b["mdma_ccr"], b["mdma_ccr"] & 1))
    flag = "✓" if (ok_route and ok_match and ok_gate_exact) else "✗"
    print("  %-14s %s 路由%s 门控%s 桥/锁存%s" %
          ("", flag,
           "对" if ok_match else "**错 (期望 %d)**" % want,
           "对" if ok_gate_exact else "**错/半配置**",
           "在" if ((b["dma2s0_cr"] & 1) and (b["mdma_ccr"] & 1)) else "**掉了**"))
    if b["trig_sel"]:
        cc4if = (b["sr"] >> 4) & 1
        print("  %-14s ★ CC4IF=%d %s" %
              ("", cc4if,
               "⇒ CC4 事件确实在产生 (FROZEN 模式也能发事件)" if cc4if
               else "⇒ **CC4 事件从未产生** ⇒ 请求源其实不存在 (需换 OC4M)"))
    return ok_route and ok_match and ok_gate_exact


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--sel", type=int, default=None, choices=[0, 1])
    ap.add_argument("--ccr4", type=int, default=10000)
    ap.add_argument("--on", action="store_true", help="开诊断 op=1 (并确保 MDMA EN=1)")
    ap.add_argument("--off", action="store_true", help="关诊断 op=0")
    ap.add_argument("--dwt", action="store_true",
                    help="op=7 重新校时 (★ 每次被 pyocd 碰过之后都必须先做, 否则统计恒 0)")
    ap.add_argument("--odr", action="store_true", help="顺便打印 op=2 的引脚/统计快照")
    ap.add_argument("--latch", action="store_true",
                    help="跑锁存活判据 (固件侧, 不依赖 LA) —— ★ LA 之前先跑这个")
    ap.add_argument("--raw", type=lambda s: int(s, 0), default=None,
                    help="op=8: 直接注入 DMAMUX1_C8 请求号 (如 0/21/22)")
    ap.add_argument("--dier", type=lambda s: int(s, 0), default=None,
                    help="op=8: 直接注入 TIM2_DIER (如 0x1001=UIE|CC4DE)")
    args = ap.parse_args()

    d = Dcl(args.port or find_board())
    try:
        if args.raw is not None:
            dier = args.dier if args.dier is not None else 0
            print("op=8 原始注入: DMAMUX1_C8=%d  DIER=0x%X → %s"
                  % (args.raw, dier, raw_inject(d, args.raw, dier)))
            time.sleep(0.3)
            odr, val, ok = latch_alive(d)
            print("   锁存活? %s  (BSRR 污染后 0.35s, ODR=0x%02X 期望=val=0x%02X)"
                  % ({True: "✓ 是", False: "✗ **否 ⇒ 锁存停了**", None: "? 无应答"}[ok], odr or 0, val or 0))
        if args.latch:
            odr, val, ok = latch_alive(d)
            print("锁存活判据: ODR=0x%02X  期望 val=0x%02X  ⇒ %s"
                  % (odr or 0, val or 0,
                     {True: "✓ 锁存在工作", False: "✗ **锁存停了**", None: "? 无应答"}[ok]))
        if args.dwt:
            r = dwt_rearm(d)
            if r is None:
                print("op=7 重新校时: ✗ 无应答")
            else:
                print("op=7 重新校时: DWT_CTRL=0x%X DEMCR=0x%X  c1=%u c2=%u Δ=%u" %
                      (r["dwt_ctrl"], r["demcr"], r["c1"], r["c2"], r["delta"]))
                if r["alive"]:
                    print("   ✓ **CYCCNT 正在走** ⇒ 本次 DWT 派生的统计有效")
                else:
                    print("   ✗ **CYCCNT 冻结 (Δ=0)** ⇒ DWT 派生的统计全部无效, 不要读它们!")
        if args.on:
            sts, _ = d.send(CMD_PIN_PATTERN, bytes([0x01]))
            print("op=1 (开诊断 + 确保 MDMA EN=1): %s" % sts)
        if args.off:
            sts, _ = d.send(CMD_PIN_PATTERN, bytes([0x00]))
            print("op=0 (关诊断): %s" % sts)

        if args.sel is not None:
            pl = bytes([0x05, args.sel & 0xFF]) + struct.pack("<I", args.ccr4 & 0xFFFFFFFF)
            sts, _ = d.send(CMD_PIN_PATTERN, pl)
            print("op=5 sel=%d ccr4=%d: %s" % (args.sel, args.ccr4, sts))

        print("\n── 触发链自证 (0x39 op=6) ──")
        show("切换后", read_block(d))

        if args.odr:
            sts, p = d.send(CMD_PIN_PATTERN, bytes([0x02]))
            if sts == "ACK" and len(p) >= 64:
                a = struct.unpack("<16I", p[:64])
                print("\n── 引脚/统计快照 (0x39 op=2) ──")
                print("  wr_n=%d  ODR=0x%02X  val=%d  min=%d  max=%d  last=%d  first=%d" %
                      (a[1], a[6] & 0xFF, a[5], a[2], a[3], a[4], a[15]))
                print("  分档 short/low/ok/high/long = %d/%d/%d/%d/%d" %
                      (a[10], a[11], a[12], a[13], a[14]))
                if a[4] == 0 and a[1] > 1000:
                    print("  ✗ **last 恒 0 而 wr_n 在涨 ⇒ DWT_CYCCNT 冻结** ⇒ 分档统计无效"
                          " (先跑 --dwt 重新校时)")
    finally:
        d.close()


if __name__ == "__main__":
    main()
