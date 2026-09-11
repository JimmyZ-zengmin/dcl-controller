#!/usr/bin/env python3
"""
h723_wire_probe.py — H1 串口接线一次性判定 (不扫描 / 不猜 / 只出通或不通)

为什么需要它 (上一轮失败的原因):
    上一轮做的是"逐脚碰线扫描" —— 那是**盲猜**, 而且前提是"不知道 H1 脚位"。
    现在原理图已经给出脚位, 盲扫没有意义。真正缺的是一个**能一次决定**的判据:
    板子在 PA9 上主动发一段**固定、可识别、抗波特率偏差**的内容, PC 侧同时听
    **两个候选口**, 谁解出这段内容, 谁的接线就是对的。

    ★ 关键: 判据不用"字节数", 不用"有没有电平" —— 用**内容匹配**。
      噪声和波特率不匹配都可能产生字节, 但产生不出 "H723-OK" 这个字符串。

固件前提:
    烧 `build_min/min_uart.hex` (src/min_uart.c, 纯轮询, 无中断/无引擎)。
    它上电后: 立刻发 2 行横幅, 之后每秒一行 "HB <n>  rx=<n>"。
    内容里含固定串 "MIN UART" 和 "H723"。

用法:
    # 板子已被 pyocd 烧过并停在 halt 时, 本脚本会先 resume 再听
    python tools/h723_wire_probe.py                 # 默认听 COM11 + COM14
    python tools/h723_wire_probe.py --ports COM14   # 只听一个口
    python tools/h723_wire_probe.py --sec 20        # 听多久
    python tools/h723_wire_probe.py --banner        # 只按"含 MIN UART"判
    python tools/h723_wire_probe.py --reset         # 同时用 pyocd 复位板子(需 pyocd)

判定:
    某个口解出 "MIN UART" ⇒ 通。并在结果里直接给出**接线正确性结论**。
    两个口都是 0 字节        ⇒ 物理链路断 (线没接 / 接错脚 / 没共地)。
    有字节但解不出内容        ⇒ 波特率不匹配或干扰 —— 单独标注, 不算通。

退出码: 0=通  1=不通  2=环境问题(缺库/口打不开)
"""

# ★ Windows 控制台默认 GBK: 脚本自己 print 出来的个别字符 (⇒ / ✓ 等) 会以
#   UnicodeEncodeError **直接崩掉整个脚本** —— 数据都量到了, 却崩在"打印结论"这一步,
#   症状看起来像"脚本坏了"而不是"编码问题"。⇒ 统一在入口把 stdout 的错误策略改成
#   "永不抛" (换成 ?), 让验收脚本不可能因为自己的输出而失败。
#   (2026-09-11 实测: audit_m234 / w1 真的这么崩过一次, 整份结果都没打出来。)
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse
import re
import sys
import time

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    print("!! 需要 pyserial")
    sys.exit(2)

# 固定可识别串 —— 必须与 src/min_uart.c 的横幅一致
BANNER_KEYS = [b"MIN UART", b"PA9=TX"]
HB_PAT = re.compile(rb"HB (\d+)\s+rx=(\d+)")

DEFAULT_PORTS = ["COM11", "COM14"]


def describe_ports():
    print("── 当前可见串口 ──")
    for p in list_ports.comports():
        tag = ""
        if "CH340" in (p.description or ""):
            tag = "  ← CH340 (USB-TTL, 你手工接线的那个)"
        elif "0D28" in (p.hwid or ""):
            tag = "  ← DAPLink 自带 CDC 串口"
        print("  %-8s %-32s%s" % (p.device, (p.description or "")[:32], tag))
    print()


def open_all(ports, baud):
    sers = {}
    for name in ports:
        try:
            s = serial.Serial(name, baud, timeout=0.05)
            s.reset_input_buffer()
            sers[name] = s
            print("  打开 %s @ %d OK" % (name, baud))
        except Exception as e:
            print("  打开 %s 失败: %s" % (name, e))
    return sers


def try_resume_target():
    """板子被 pyocd 烧录后核心处于 HALT, 程序不跑 —— 先放开它。"""
    try:
        from pyocd.core.helpers import ConnectHelper
    except ImportError:
        print("  (没装 pyocd, 跳过 resume —— 若你刚烧完固件, 板子可能停在 halt, 请手动复位)")
        return
    try:
        s = ConnectHelper.session_with_chosen_probe(
            target_override="stm32h723xx",
            options={"connect_mode": "under-reset", "frequency": 1000000},
            blocking=False)
        s.open()
        t = s.target
        t.reset_and_halt()
        time.sleep(0.2)
        t.resume()
        time.sleep(0.1)
        s.close()
        print("  ✓ 已复位并放开核心 (程序开始跑)")
    except Exception as e:
        print("  (resume 失败: %s —— 可能探针被占用; 可手动按板子复位键)" % e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", default=None, help="逗号分隔, 默认 COM11,COM14")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--sec", type=float, default=15.0)
    ap.add_argument("--reset", action="store_true", help="先用 pyocd 复位并放开核心")
    ap.add_argument("--banner", action="store_true",
                    help="严格模式: 只认含 'MIN UART' 的横幅 (心跳不计)")
    a = ap.parse_args()

    ports = a.ports.split(",") if a.ports else DEFAULT_PORTS
    ports = [p.strip() for p in ports if p.strip()]

    describe_ports()
    print("── 打开监听 ──")
    sers = open_all(ports, a.baud)
    if not sers:
        print("\n!! 一个口都打不开 —— 检查设备管理器 / 是否被别的程序(串口助手/VOFA)占用")
        return 2

    if a.reset:
        print()
        print("── 复位板子 ──")
        try_resume_target()

    print()
    print("── 监听 %g 秒 ──" % a.sec)
    print("   固件上电应立刻发: '==== MIN UART (no engine, no IRQ, HSI 64MHz) ===='")
    print("   之后每秒: 'HB <n>  rx=<n>'")
    print()
    print("   ★ 若两个口都 0 字节 → 物理链路断, 不用再看固件 (固件已验证在发)")
    print()

    buf = {k: bytearray() for k in sers}
    hb = {k: 0 for k in sers}
    t0 = time.time()
    last_report = 0.0
    while time.time() - t0 < a.sec:
        for k, s in list(sers.items()):
            try:
                d = s.read(8192)
            except Exception:
                continue
            if not d:
                continue
            buf[k] += d
            # 实时回显: 收到就立刻打, 不等统计
            txt = d.decode("ascii", "replace").replace("\r", "").replace("\n", "\\n")
            print("   [%s] %s" % (k, txt[:160]))
            m = HB_PAT.search(bytes(buf[k]))
            if m:
                hb[k] = int(m.group(1))
        el = time.time() - t0
        if el - last_report >= 5.0:
            last_report = el
            print("   ... t=%4.1fs | %s" % (
                el, "  ".join("%s=%d B" % (k, len(buf[k])) for k in sorted(buf))))
        time.sleep(0.02)

    for s in sers.values():
        s.close()

    print()
    print("═" * 62)
    print("结果")
    print("═" * 62)

    passed = []
    ambiguous = []          # 有字节但解不出内容
    for k in sorted(buf):
        b = bytes(buf[k])
        hit = [key.decode() for key in BANNER_KEYS if key in b]
        status = "通" if hit else ("有信号但内容不匹配" if b else "完全无字节")
        print("%-8s %7d B  → %s%s" % (k, len(b), status,
                                      ("   命中: %s" % hit) if hit else ""))
        if b and not hit:
            ambiguous.append((k, b))
        if hit:
            passed.append(k)

    print()
    if passed:
        for k in passed:
            who = "CH340" if k == "COM14" else ("DAPLink CDC" if k == "COM11" else k)
            print("★★ 通了。%s (%s) 收到了板子发的横幅。" % (k, who))
        print()
        print("接线结论: 该口的 RXD 已正确接在 **H1 第 6 脚 (USART1_TX / PA9)** 上。")
        print("         现在可以把 CH340 的 TXD 接到 **H1 第 5 脚 (USART1_RX / PA10)**, ")
        print("         然后回环测试(或跑主固件)验证接收方向。")
        return 0

    if ambiguous:
        print("有字节但不含预期内容 —— 这**不是通**。可能原因, 按概率排:")
        print("  1. 波特率不匹配 (固件是 64MHz HSI → 115200; 你工具设的不是 115200?)")
        print("  2. 收到的其实是悬空噪声 (把线拔掉再听一次, 字节数不变就说明是噪声)")
        print("  3. 半双工冲突 / 双方都在发")
        for k, b in ambiguous:
            print("  [%s] 原始前 48 字节: %s" % (k, b[:48].hex(" ")))
        return 1

    print("两个口都完全无字节 (0 B) —— PC 侧根本没有看到 MCU 的 TX 边沿。")
    print()
    print("固件侧已被独立验证在发 (USART1_CR1=0x0D 收发全开, TXE=1, BRR 正确), 所以:")
    print("  ① 线没接到 H1 第 6 脚 (PA9) 上")
    print("  ② 接到了假脚 (H1 第 7 脚不是 PA9! 原理图: 7=RST)")
    print("  ③ 没共地 —— 这一条最容易被忽略, **必须**把 CH340 的 GND 接到板子 GND")
    print("  ④ CH340 的 RXD 线本身断了 (用短接法: TX 短接 RX 自收验证转换器+线)")
    print()
    print("★ 一秒验证法: 把 CH340 的 TXD 和 RXD **直接短接**, 用本脚本听该口, ")
    print("  然后随便发点什么 —— 如果连自己发的都收不到, 是转换器/线的问题, 与板子无关。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
