#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rs485_probe.py — 485/串口链路"二分定位"工具: 一发两帧, 直接判断点在哪一段。

原理: 板子上有**两个** UART, 各自有独立的"收到过东西"的证据, 所以一条命令就能把
      "信号到底到了哪一侧 / 有没有到板子"分开:

  · 往 USART2 (PA2/PA3, Modbus) 发一条**合法 Modbus 帧**
      ⇒ 固件侧 `frames_rx++` ⇒ 到了 PA3
      ⇒ 或 `err_crc++`      ⇒ 到了 PA3 但内容不对 (波特率/噪声)
  · 往 USART1 (PA9/PA10, DCL 协议口) 发一条**合法 DCL 帧**
      ⇒ 固件侧 `g_frame_ok++` 且**会当场回 ACK** ⇒ 到了 PA9

于是:
  A. 两个计数都不动        ⇒ 信号根本没到板子 (接线/电平/方向/共地)
  B. Modbus 侧动了         ⇒ 链路是通的, 换成 Modbus 协议正常对话
  C. DCL 侧动了(收到 ACK)  ⇒ 接错脚了: 接在 PA9/PA10 上, 要挪到 PA2/PA3

用法:
    # 只发不收: 看有没有任何东西回来
    python rs485_probe.py --port COM15

    # 带固件侧对账 (推荐): 协议口正常时用 --proto 读 0x61, 不用 pyocd
    python rs485_probe.py --port COM15 --proto COM14

    # 没有协议口时退回 pyocd 读 (注意: 本机探针会在会话结束时复位板子)
    python rs485_probe.py --port COM15 --pyocd

    # 参考: 487 端模块 TTL 侧 TXD-RXD 短接的自环测试后, 用本工具看能不能收到回显
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import serial
except ImportError:
    serial = None


def crc_modbus(d):
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if (c & 1) else (c >> 1)
    return c


def crc_ccitt(d):
    c = 0xFFFF
    for b in d:
        c ^= (b << 8)
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if (c & 0x8000) else (c << 1) & 0xFFFF
    return c


def mb_frame(addr, pdu):
    b = bytes([addr]) + bytes(pdu)
    x = crc_modbus(b)
    return b + bytes([x & 0xFF, (x >> 8) & 0xFF])


def dcl_frame(cmd, payload=b""):
    """[0xC0][cmd][len_lo][len_hi][payload][crc_lo][crc_hi]，CRC16-CCITT"""
    body = bytes([cmd, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    x = crc_ccitt(body)
    return bytes([0xC0]) + body + bytes([x & 0xFF, (x >> 8) & 0xFF])


def xfer(s, f, wait=0.4):
    s.reset_input_buffer()
    s.write(f)
    s.flush()
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < wait:
        n = s.in_waiting
        if n:
            buf += s.read(n)
        else:
            time.sleep(0.002)
    return bytes(buf)


def read_counters_via_proto(proto):
    """用协议口读 0x61 → (frames_rx, frames_tx, err_crc, err_exc, state, tx_len)"""
    r = xfer(proto, dcl_frame(0x61))
    if len(r) < 6 or r[0] != 0xC1 or r[1] != 0x00:
        return None, "0x61 无有效 ACK: %s" % (r.hex(" ") if r else "(无)")
    ln = r[2] | (r[3] << 8)
    p = r[4:4 + ln]
    if len(p) < 2:
        return None, "0x61 载荷太短"
    tl = p[1]
    o = 2 + tl
    if len(p) < o + 16:
        return None, "0x61 载荷缺计数"
    g = lambda i: int.from_bytes(p[o + 4 * i:o + 4 * i + 4], "little")
    return (g(0), g(1), g(2), g(3), p[0], p[1]), None


def pyocd_counters():
    """经 pyocd 读 MbCtrl_t → (frames_rx, frames_tx, err_crc, err_exc, state, tx_len)。
    ★ 本机探针会在**会话结束时复位板子**, 所以调用它会重置计数 —— 既是缺点也是优点:
      把"发帧前读一次"当基线, 之后读到的那份天然就是增量。"""
    try:
        import re
        from pyocd.core.helpers import ConnectHelper
        sym = {}
        for ln in open("build/dcl_h723.map", encoding="utf-8", errors="replace"):
            m = re.match(r"\s+0x([0-9a-fA-F]+)\s+(g_shm_addr)\s*$", ln)
            if m:
                sym["shm"] = int(m.group(1), 16)
        s = ConnectHelper.session_with_chosen_probe(
            target_override="stm32h723xx", options={"connect_mode": "halt"})
        s.open()
        try:
            shm = s.target.read32(sym["shm"])
            cb = bytes(s.target.read_memory_block8(shm + 0x4B20, 40))
            g = lambda a, b: int.from_bytes(cb[a:b], "little")
            return (g(9, 13), g(13, 17), g(17, 21), g(21, 25), cb[0], cb[4])
        finally:
            try:
                s.target.resume()
            except Exception:
                pass
            s.close()
    except Exception as e:
        print("[!] pyocd 读取失败: %s" % e)
        return None


def board_tx_burst(proto_port, link_port, seconds):
    """让板子**连续从 PA2 真发应答**, 同时监听 485 口。

    用途: 数据流起来时才能看 LED —— dongle 的 RX 灯闪 ⇒ 板子的数据真的过总线到了 PC。
      · 002 直接看灯: 让用户盯着 USB-485 的 TX/RX 灯与 485 模块的灯。
      · 同时统计 485 口收到的字节数 (若真到了, 会收到 N 条 Modbus 应答)。
    """
    proto = serial.Serial(proto_port, 115200, timeout=0.3)
    link = serial.Serial(link_port, 115200, timeout=0.3)
    mb = mb_frame(1, [0x03, 0x9C, 0x41, 0x00, 0x01])
    print("① 切 隧道RX + 物理TX: %s" % xfer(proto, dcl_frame(0x62, bytes([1, 1]))).hex(" "))
    print("② 连发 %d 秒 —— ★ 现在盯着 USB-485 的 LED 与 485 模块的 LED" % seconds)
    t0 = time.time()
    n = 0
    got = bytearray()
    while time.time() - t0 < seconds:
        xfer(proto, dcl_frame(0x60, mb), 0.05)        # 注入 → 板子从 PA2 发应答
        n += 1
        nwait = link.in_waiting
        if nwait:
            got += link.read(nwait)
        time.sleep(0.30)
    print("   注入了 %d 次; 485 口累计收到 %d 字节 %s"
          % (n, len(got), got[:48].hex(" ") if got else "(无)"))
    print("③ 恢复物理RX: %s" % xfer(proto, dcl_frame(0x62, bytes([0, 1]))).hex(" "))
    link.close()
    proto.close()
    print("\n判读 (结合你看到的灯):")
    print("  dongle RX 灯跟着闪 + 上面收到 01 03 02.. ⇒ **板子→模块→总线→PC 全通**")
    print("  dongle TX 灯闪但 RX 灯不闪            ⇒ 发送出去了, 回不来 (A/B 极性/模块接收)")
    print("  两个灯都不闪                          ⇒ dongle 侧就没动 (或灯不是数据灯)")
    return 0


def loopback_test(proto_port, cycles=4):
    """★ PA2 ↔ PA3 外部短接时的**自环**测试: 板子自己发、自己收。

    这是"USART2 收发硬件 + 固件物理通路"唯一可靠的功能性验证 ——
    因为本机 pyocd **读不了外设寄存器**(GPIOA 读回 0xABFFFFFF = 陈旧 SRAM;
    USART2 读回 0 = 读失败), 所以只能让硬件**真的走一遍数据**。

    ★★ 时序要点 (不这么做就一定失败): `0x60` 注入会把 `src` 置成**隧道模式**,
       而回环回来的字节只有在 `src=0`(物理口) 时才会被状态机读走。
       办法: 请求用 **qty=125** ⇒ 应答长达 255 字节, 固件按 ≤4 字节/拍推,
       要 ~6.4 ms 才推完 ⇒ **注入后立刻把 src 切回物理口, 就能抢到应答的尾巴**。
    ★★ 判据只能用 err_crc / err_exc, **不能用 frames_rx** —— 这一条是负对照抓出来的:
       `frames_rx` **两种字节源都计数**(隧道注入 + 物理口各算一帧), 所以"每轮 +1"
       只是**我注入的那一帧自己**, 与回环毫无关系 (第一版就这么报了假成功)。
       而**注入的请求是合法帧** ⇒ 它永远不会产生 err_crc/err_exc。
       回环回来的应答被当成"请求"解析 ⇒ 要么片段 CRC 失败 (err_crc++),
       要么完整但地址非法/长度不足 (err_exc++) ⇒ **这两个量只要动, 就一定来自物理口**。
    """
    proto = serial.Serial(proto_port, 115200, timeout=0.3)
    long_req = mb_frame(1, [0x03, 0x9C, 0x41, 0x00, 0x7D])   # qty=125 → 255B 应答
    tot = 0
    print("自环测试: PA2↔PA3 应已短接; 注入长应答(255B) 并在途中把 src 切回物理口")
    print("(判据 = Δerr_crc + Δerr_exc; frames_rx 两种字节源都算, 不作数)")
    for k in range(cycles):
        xfer(proto, dcl_frame(0x62, bytes([0, 1])), 0.05)
        base, err = read_counters_via_proto(proto)
        if err:
            print("  [X] 读计数失败: %s" % err)
            break
        xfer(proto, dcl_frame(0x60, long_req), 0.004)        # 只等 4ms → 应答还在推
        xfer(proto, dcl_frame(0x62, bytes([0, 1])), 0.03)    # 抢在推完前切回物理口
        time.sleep(0.15)
        now, err = read_counters_via_proto(proto)
        if err:
            continue
        df = now[0] - base[0]
        de = now[2] - base[2]
        dx = now[3] - base[3]
        tot += de + dx
        print("  第%d轮: Δframes_rx=%+d(忽略)  Δerr_crc=%+d  Δerr_exc=%+d  state=%d rx_len=%d"
              % (k + 1, df, de, dx, now[4], now[5]))
    proto.close()
    print("\n判定: %s" % ("**自环成功** ⇒ USART2 收发硬件 + 固件物理通路都正常 ⇒ "
                        "问题一定在 485 链路 (模块/A-B/线)" if tot > 0 else
                        "**自环失败** ⇒ 若 PA2↔PA3 **确实已短接**, 则板子侧有问题 "
                        "(USART2 硬件/引脚配置/固件路径); 若还没短接, 这一条只是负对照 (本就该失败)"))
    return 0 if tot > 0 else 1


def main():
    argv = sys.argv[1:]
    if serial is None:
        print("[X] 缺 pyserial")
        return 2
    port = argv[argv.index("--port") + 1] if "--port" in argv else None
    proto = argv[argv.index("--proto") + 1] if "--proto" in argv else None
    use_pyocd = "--pyocd" in argv

    if "--loopback" in argv:                     # 自环只需协议口, 不需要 --port
        if not proto:
            print("[X] --loopback 需要 --proto COMxx (协议口, 用它注入/查计数)")
            return 2
        return loopback_test(proto)

    if not port:
        print(__doc__)
        return 2

    if "--board-tx" in argv:
        i = argv.index("--board-tx") + 1
        secs = int(argv[i]) if (i < len(argv) and argv[i].isdigit()) else 20
        if not proto:
            print("[X] --board-tx 需要同时给 --proto COMxx (协议口, 用它驱动板子)")
            return 2
        return board_tx_burst(proto, port, secs)

    f_mb = mb_frame(1, [0x03, 0x9C, 0x41, 0x00, 0x01])        # 测 USART2 (PA2/PA3)
    f_dcl = dcl_frame(0x62, bytes([0x00, 0x01]))               # 测 USART1 (PA9/PA10), 幂等
    print("Modbus 帧 = %s   (测 USART2/Modbus 通路; 引脚由 DCL_MB_UART_ALT 决定)" % f_mb.hex(" "))
    print("DCL 帧    = %s   (测 USART1/协议口, cmd=0x62 幂等)\n" % f_dcl.hex(" "))

    # ★★ 必须先取**基线**: 固件里的 frames_rx/err_crc 是**自启动以来累计**的,
    #    直接看绝对值会把"之前隧道注入(0x60)留下的 1"当成"这次物理请求的成功证据"
    #    —— 第一版就这么报了假阳性。判据只能建立在**增量**上。
    pser = None
    base = None
    if proto:
        pser = serial.Serial(proto, 115200, timeout=0.3)
        base, err = read_counters_via_proto(pser)
        if err:
            print("[!] 协议口基线读取失败: %s" % err)
            base = None
        else:
            print("基线: frames_rx=%d err_crc=%d err_exc=%d" % (base[0], base[2], base[3]))

    # ★ pyocd 路径: 本机探针**会话结束会复位板子** ⇒ 先在发帧之前读一次 (它的 close
    #   会把板子复位), 这样发帧之后读到的那份计数就是**本次探测独有的增量**。
    base_py = None
    if use_pyocd and not proto:
        base_py = pyocd_counters()
        if base_py:
            print("pyocd 基线(会话结束会复位板子): frames_rx=%d err_crc=%d" % (base_py[0], base_py[2]))

    link = serial.Serial(port, 115200, timeout=0.3)
    got_mb = got_dcl = b""
    for name, f in (("Modbus", f_mb), ("DCL", f_dcl)):
        r = xfer(link, f)
        print("  %-6s → 收到 %2d 字节: %s" % (name, len(r), r.hex(" ") if r else "(无)"))
        if name == "Modbus":
            got_mb = r
        else:
            got_dcl = r
    link.close()

    cnt = None
    d_frx = d_ecrc = None
    if pser is not None and base is not None:
        cnt, err = read_counters_via_proto(pser)
        pser.close()
        if err:
            print("\n[!] 协议口复读失败: %s" % err)
        else:
            d_frx = cnt[0] - base[0]
            d_ecrc = cnt[2] - base[2]
            print("增量: frames_rx %+d, err_crc %+d, err_exc %+d"
                  % (d_frx, d_ecrc, cnt[3] - base[3]))
    elif use_pyocd:
        cnt = pyocd_counters()
        if cnt is None:
            print("\n[!] pyocd 读取失败")
        else:
            # 前面那次 pyocd close 已把板子复位 ⇒ 这份计数就是本次探测的增量
            d_frx = cnt[0] - (base_py[0] if base_py else 0)
            d_ecrc = cnt[2] - (base_py[2] if base_py else 0)
            print("增量: frames_rx %+d, err_crc %+d" % (d_frx, d_ecrc))

    if cnt:
        print("\n固件侧: frames_rx=%d frames_tx=%d err_crc=%d err_exc=%d  state=%d tx_len=%d" % cnt)

    print("\n=== 判定 (一律基于**增量**, 历史累计值不作数) ===")
    if d_frx is None:
        print("  ? 没有固件侧对账 ⇒ 只能看回显: %s" % ("有回显" if got_dcl else "无回显(疑似不通)"))
        print("     强烈建议加 --proto COMxx(=协议口) 做增量对账, 否则'没响应'说明不了任何事")
    elif got_dcl:
        print("  C. DCL 帧被当场应答 ⇒ 线接在 **协议口(USART1)** 上, 要挪到 Modbus 口(USART2)")
    elif d_frx:
        print("  B. 固件收到了 Modbus 帧 (Δframes_rx=%+d) ⇒ **链路正常**, 可直接跑 "
              "tools/mb_master_test.py" % d_frx)
    elif d_ecrc:
        print("  B'. 固件收到字节但 CRC 错 (Δerr_crc=%+d) ⇒ 到了 PA3, 查波特率/噪声" % d_ecrc)
    else:
        print("  A. 增量全为 0, 也没回显 ⇒ **信号根本没到板子**")
        print("     按这个顺序查: ① 共地了吗 ② A/B 对调试过吗 ③ TXD/RXD 有没有接反")
        print("     ④ 485 模块供电了吗/是自动收发吗 ⑤ 到底接在哪个脚 (PA2/PA3?)")
        print("     ⑥ 最省事的判据: 把模块 TTL 侧 **TXD-RXD 短接**, 再跑本工具 —— 能收到回显")
        print("        就说明 dongle→A/B→模块 这条全通, 问题在板子侧接线; 收不到就是链路本身")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
