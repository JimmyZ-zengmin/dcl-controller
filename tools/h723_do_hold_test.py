#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_do_hold_test.py —— **DO 0..7 保持判据**（"锁存链把 PE0~PE7 清零"的验收工具）

## 这条判据在验什么
2026-09-16 读码发现（并已在 src/do.c 修掉）：交付档 `DCL_DO_LATCH=0` 下，
`do_latch_init()` 仍**无条件**启动 MDMA 锁存链，而 0 档的 `do_poll()` 只写 BSRR、
**从不写影子** ⇒ 影子恒 0 ⇒ 每 100 µs(TIM2_UP) MDMA 把 **0** 锁进 `GPIOE_ODR` 低字节
⇒ **CPU 置起来的 PE0~PE7 会被静默清掉** ⇒ DO 0..7 永不可用。

★ 判据为什么必须是这个形状：
  `g_do_write_n` 会照涨、寄存器读回全对、`0x38` 一切正常 —— **只有引脚电平会说真话**。
  而"引脚电平"必须看 **`IDR`**（引脚实际电平），不是 `ODR`（我以为写了什么）。

## 判据（能失败的）
  ① 把 `GPIO_MASK` 置成 `0x00FF`（登记 PE0~PE7）；
  ② 把 `ACTUATOR[0..7]` 全写 1.0（`do_pack` 会让它们为 1）；
  ③ 等若干个拍（≥10 ms ≈ 100 拍）后读 `GPIOE_IDR`；
  ④ **判据：`IDR & 0x00FF == 0x00FF` 且连续 N 次采样都成立**。
  失败形态：读到 `0x00`（被 MDMA 每拍清零）或时高时低（锁存竞争）。
  ⑤ 收尾必须**恢复** `GPIO_MASK` 与 `ACTUATOR[0..7]`（不留副作用）。

## 用法
    python tools/h723_do_hold_test.py [--port COM21] [--samples 5] [--keep]
`--keep` = 跑完不复原（调试用，默认复原）。
退出码：0 = 通过；2 = 判据失败；1 = 环境/协议错误。
"""
import argparse
import struct
import sys
import time

sys.path.insert(0, "tools")
from h723_client import Dcl, engine_status          # noqa: E402

CMD_READ = 0x20
CMD_WRITE = 0x21
CMD_WRITE_BURST = 0x23
CMD_START = 0x11
CMD_PIN_PATTERN = 0x39

OFF_CTRL_GPIO_MASK = 0x34
OFF_ACTUATOR_STATUS = 0x0140
MASK_STEP = 0x0F00          # 上电默认（step_init 写的）
MASK_TEST = 0x00FF          # 本次要证的：PE0~PE7 能不能保持


def pin_pattern(d, op, sub=0, arg=0):
    sts, p = d.send(CMD_PIN_PATTERN, struct.pack("<BBI", op, sub, arg))
    if sts != "ACK" or len(p) < 96:
        return None
    return p


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                       # noqa: BLE001
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    d = Dcl(a.port)
    print(f"端口 = {d.port}")

    # ── 0) 0x38 长度（顺带验 ENG_STATUS_LEN 修复：越界过的应答这里会短/乱） ──
    sts, p38 = d.send(0x38)
    print(f"0x38: {sts}  len={len(p38)}   (契约 51)")
    if sts != "ACK" or len(p38) != 51:
        print("[FAIL] 0x38 应答长度不符契约 ⇒ ENG_STATUS_LEN 有问题")
        return 2
    st = engine_status(d)
    shm = st["shm"]
    print(f"SHM 基址 = 0x{shm:08X}  n_routes={st['n_routes']} run={st['run']}")

    if not st["run"]:
        print("引擎未在跑 ⇒ 发 0x11 START")
        d.send(CMD_START)
        time.sleep(0.05)

    # ── 1) 读当前 GPIO_MASK ──
    sts, p = d.send(CMD_READ, struct.pack("<I", shm + OFF_CTRL_GPIO_MASK))
    if sts != "ACK" or len(p) < 4:
        print(f"[FAIL] 0x20 读 GPIO_MASK 失败: {sts}")
        return 1
    mask0 = struct.unpack("<I", p[:4])[0]
    print(f"GPIO_MASK(改前) = 0x{mask0:04X}")

    ok = False
    try:
        # ── 2) 登记 PE0~PE7 ──
        sts, p = d.send(CMD_WRITE, struct.pack("<II", shm + OFF_CTRL_GPIO_MASK, MASK_TEST))
        if sts != "ACK":
            print(f"[FAIL] 0x21 写 GPIO_MASK 被拒: {sts} {p[:16]!r}")
            return 1
        sts, p = d.send(CMD_READ, struct.pack("<I", shm + OFF_CTRL_GPIO_MASK))
        mask_rb = struct.unpack("<I", p[:4])[0] if sts == "ACK" and len(p) >= 4 else 0
        print(f"GPIO_MASK(回读) = 0x{mask_rb:04X}   " + ("OK" if mask_rb == MASK_TEST else "**回读不符**"))

        # ── 3) ACTUATOR[0..7] = 1.0 ──
        vals = struct.pack("<8f", *([1.0] * 8))
        sts, p = d.send(CMD_WRITE_BURST,
                        struct.pack("<IH", shm + OFF_ACTUATOR_STATUS, 8) + vals)
        if sts != "ACK":
            print(f"[FAIL] 0x23 写 ACTUATOR 被拒: {sts} {p[:16]!r}")
            return 1

        # ── 4) 等足够多的拍，然后连续采样 IDR ──
        time.sleep(0.02)                       # 20 ms ≈ 200 拍
        hits = 0
        samples = []
        for i in range(a.samples):
            r = pin_pattern(d, 19, 0, 0)       # sub=0 = 只查询, 零副作用
            if r is None:
                print("[FAIL] 0x39 op=19 无应答")
                return 1
            odr_e = struct.unpack("<I", r[40:44])[0]
            idr_e = struct.unpack("<I", r[80:84])[0]
            lo = idr_e & 0xFF
            samples.append(lo)
            if lo == 0xFF:
                hits += 1
            print(f"  采样{i + 1}: GPIOE_IDR=0x{idr_e:04X} (低字节=0x{lo:02X})"
                  f"  ODR=0x{odr_e:04X}   {'✓ 保持' if lo == 0xFF else '✗ 被清零'}")
            time.sleep(0.01)

        print()
        print(f"判据: IDR 低字节连续 {a.samples} 次均 == 0xFF")
        print(f"实测: 命中 {hits}/{a.samples}  ({[hex(x) for x in samples]})")
        ok = (hits == a.samples)
    finally:
        if not a.keep:
            d.send(CMD_WRITE_BURST,
                   struct.pack("<IH", shm + OFF_ACTUATOR_STATUS, 8) + struct.pack("<8f", *([0.0] * 8)))
            d.send(CMD_WRITE, struct.pack("<II", shm + OFF_CTRL_GPIO_MASK, MASK_STEP))
            print(f"(已复原: ACTUATOR[0..7]=0, GPIO_MASK=0x{MASK_STEP:04X})")
        d.close()

    if ok:
        print("\n[PASS] DO 0..7 可置 1 并保持 ⇒ 锁存链未再清零 PE0~PE7")
        return 0
    print("\n[FAIL] PE0~PE7 未能保持为 1 ⇒ 仍被每拍清零（锁存链在跑而影子恒 0）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
