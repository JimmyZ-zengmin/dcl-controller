#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_loopback.py — 串口链路二分法第一段: PC 侧 TX<->RX 短接自收

用途
----
链路不通时, 先不碰板子, 单独验证 "PC + USB 转换器 + 线" 这一段是否可用。
把转换器(DAPLink VCP / CH340)的 TX 与 RX 两根杜邦线**短接**, 跑本脚本:

    python tools/h723_loopback.py            # 自动找 DAPLink VCP (0D28:0204)
    python tools/h723_loopback.py --port COM11

判据设计 (每条都必须能失败, 参考项目 "判据可失败性" 纪律)
------------------------------------------------------
T0  端口可打开                    —— 端口不存在/被占用 -> FAIL
T1  静默期噪声基线 = 0 字节        —— 悬空拾取串扰 / 上次残留 -> FAIL
T2  哨兵序列逐字节回环一致 (3 轮)  —— 线与转换器任一环断开 -> FAIL
T3  往返延迟随字节数线性增长 (参考, 不计分)
    · 真硬件回环: 每字节 ~87us@115200, 斜率应显著 > 0
    · 驱动层/软件回环: 斜率 ≈ 0
    => 这条用来区分 "真的从引脚上绕回来了" 和 "驱动自己回显"

为什么用哨兵序列而不是 0x00/0xFF
-------------------------------
短接线上若拾到环境噪声, 常见表现是 0x00 / 0xFF / 少量固定字节。
哨兵用 (i*37+11)^0x5A 生成的伪随机序列, "恰好收到" 的概率可忽略,
且长度 8/32/128 三轮不同, 避免把"只通了第一个字节"误判为全通。
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
import sys
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("FAIL: 需要 pyserial (pip install pyserial)")
    sys.exit(2)

DAPLINK_VID, DAPLINK_PID = 0x0D28, 0x0204


def find_daplink_port():
    for p in serial.tools.list_ports.comports():
        if p.vid == DAPLINK_VID and p.pid == DAPLINK_PID:
            return p.device
    return None


def list_all_ports():
    return [(p.device, p.description or "", p.vid, p.pid)
            for p in serial.tools.list_ports.comports()]


def sentinel(n):
    """不可能自然出现的序列: 与 0x00/0xFF/单字节噪声可区分"""
    return bytes(((i * 37 + 11) ^ 0x5A) & 0xFF for i in range(n))


def drain(ser):
    """排空输入缓冲 —— 打开端口瞬间的 DTR/RTS 翻转会在 RX 上感应毛刺,
    不排空会把毛刺当成"收到了数据"(历史上多次误判的源头)"""
    try:
        ser.reset_input_buffer()
    except Exception:
        pass
    deadline = time.time() + 0.3
    while time.time() < deadline:
        if not ser.read(1):
            break


def roundtrip(ser, payload, timeout=2.0):
    """发一包并收等长回包, 返回 (收到的字节, 往返秒数, 是否逐字节一致)"""
    drain(ser)
    t0 = time.perf_counter()
    n_written = ser.write(payload)
    ser.flush()
    # ★ 写入字节数必须等于请求长度: 证明数据真的进了驱动, 不是被本地吞掉
    #   (write 返回不足 = 驱动/缓冲有问题, 此时"收不到"不能归因到物理链路)
    if n_written != len(payload):
        raise RuntimeError("write 只接受了 %d/%d 字节" % (n_written, len(payload)))
    got = bytearray()
    deadline = time.time() + timeout
    while len(got) < len(payload) and time.time() < deadline:
        chunk = ser.read(len(payload) - len(got))
        if not chunk:
            break
        got.extend(chunk)
    dt = time.perf_counter() - t0
    return bytes(got), dt, (bytes(got) == payload)


def main():
    ap = argparse.ArgumentParser(description="串口 TX<->RX 短接自收测试")
    ap.add_argument("--port", default=None,
                    help="串口号; 不给则自动找 DAPLink VCP (VID:PID=0D28:0204)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--quiet", type=float, default=2.0,
                    help="T1 静默监听秒数 (噪声基线)")
    args = ap.parse_args()

    print("=" * 62)
    print("串口链路二分 · 第一段: PC + 转换器 + 线 (TX<->RX 短接自收)")
    print("=" * 62)

    port = args.port
    if port is None:
        port = find_daplink_port()
        if port is None:
            print("\n当前系统串口:")
            for d, desc, vid, pid in list_all_ports():
                print("  %-8s %-40s VID:%04X PID:%04X"
                      % (d, desc[:40], vid or 0, pid or 0))
            print("\n[FAIL] T0 未找到 DAPLink VCP (0D28:0204), 请用 --port 显式指定")
            return 1
        print("自动定位 DAPLink VCP: %s" % port)

    # ---- T0 端口可打开 ----
    try:
        ser = serial.Serial(port=port, baudrate=args.baud, timeout=1.0,
                            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                            stopbits=serial.STOPBITS_ONE,
                            dsrdtr=False, rtscts=False, xonxoff=False)
    except Exception as e:
        print("\n[FAIL] T0 打不开 %s: %s" % (port, e))
        return 1
    print("[PASS] T0 端口打开成功  %s @ %d 8N1" % (port, args.baud))

    results = []

    try:
        # ---- T1 静默噪声基线 ----
        drain(ser)
        noise = bytearray()
        t_end = time.time() + args.quiet
        while time.time() < t_end:
            b = ser.read(256)
            if b:
                noise.extend(b)
            else:
                time.sleep(0.05)
        ok1 = (len(noise) == 0)
        results.append(("T1 静默 %.1fs 噪声基线 = 0 字节" % args.quiet, ok1,
                        "%d 字节" % len(noise)))
        if noise:
            print("       噪声样本(前16): %s" % noise[:16].hex(" "))

        # ---- T2 哨兵回环 (3 轮不同长度) ----
        lat = []
        for n in (8, 32, 128):
            pay = sentinel(n)
            got, dt, same = roundtrip(ser, pay)
            lat.append((n, dt, same))
            results.append(("T2 哨兵回环 %3d 字节逐字节一致" % n, same,
                            "%d/%d 字节, %.1f ms" % (len(got), n, dt * 1000)))
            if not same and got:
                print("       期望: %s" % pay[:16].hex(" "))
                print("       实收: %s" % got[:16].hex(" "))

        # ---- T2b 多波特率扫描 ----
        # 短接自收不跨芯片, 波特率理论上无关; 但如果 9600 能通而 115200 不通,
        # 说明波特率发生器/驱动侧有问题, 而不是"线没接上"。便宜且能失败。
        for baud in (9600, 57600, 115200):
            if baud == args.baud:
                continue
            try:
                ser.baudrate = baud
                drain(ser)
                pay = sentinel(8)
                got, _dt, same = roundtrip(ser, pay, timeout=1.5)
                results.append(("T2b @%d 8 字节回环" % baud, same,
                                "%d/8 字节" % len(got)))
            except Exception as e:
                results.append(("T2b @%d 8 字节回环" % baud, False, str(e)[:20]))
            finally:
                ser.baudrate = args.baud

        # ---- T4 break 探针 ----
        # ★ 注意: Windows 的 usbser.sys 会**静默丢弃**带 framing error 的字节,
        #   所以 T4 收不到**不能**证明没短接 (判据只能证真, 不能证伪)。
        #   真正能证伪的是 T2 —— 发正常数据收不回来才是硬证据。
        # 强制把 TX 拉低 duration 秒。若 TX<->RX 真的短接, RX 必然看到长低电平
        # (BREAK) => 驱动至少会交出一个 0x00 或 framing error 字节。
        # 收不到 => 要么 TX 没在动, 要么短接无效。代价 0 硬件, 判据能失败。
        drain(ser)
        brk_got = b""
        try:
            ser.send_break(duration=0.5)
            time.sleep(0.15)
            brk_got = ser.read(256)
        except Exception as e:
            brk_got = b"EXC:" + str(e).encode()[:40]
        ok4 = len(brk_got) > 0
        results.append(("T4 break 探针 (TX 拉低 0.5s 有回响)", ok4,
                        "%d 字节" % len(brk_got)))

        # ---- T3 延迟斜率 (参考) ----
        if len(lat) >= 2:
            (n0, t0_, ok0), (n2, t2_, ok2) = lat[0], lat[-1]
            slope = (t2_ - t0_) / (n2 - n0) * 1e6  # us/字节
            print("\n[T3 参考] 往返延迟 %d 字节 %.2f ms -> %d 字节 %.2f ms, 斜率 %.1f us/字节"
                  % (n0, t0_ * 1000, n2, t2_ * 1000, slope))
            # ★★ 斜率只在"回环真的成功"时才有意义。全部超时的话三轮延迟都 ≈ timeout,
            #   斜率是纯随机噪声 —— 曾据此误判"数据从引脚绕回来了"(实为超时噪声)。
            #   判据宁可不出, 也不能出一个会骗人的数。
            if not (ok0 and ok2):
                print("  [不判读] 回环未成功, 三轮延迟均为超时值, 斜率无物理意义")
            else:
                print("  真硬件回环 @%d 期望 ~%.0f us/字节 (10 位/字节)"
                      % (args.baud, 10.0 / args.baud * 1e6))
                if slope > 30:
                    print("  判读: 斜率显著 > 0 => 数据真的从引脚绕回来了 (硬件回环)")
                else:
                    print("  判读: 斜率 ≈ 0 => 可能是驱动层回显, 未经过物理引脚"
                          " (需结合下一步接板子实测确认)")
    finally:
        ser.close()

    print()
    print("-" * 62)
    for name, ok, detail in results:
        print("[%s] %-36s %s" % ("PASS" if ok else "FAIL", name, detail))
    n_pass = sum(1 for r in results if r[1])
    print("-" * 62)
    print("%d/%d PASS" % (n_pass, len(results)))
    if n_pass == len(results):
        print("=> PC + 转换器 + 线 这一段可用。下一步: 接板子, 烧 min_uart.bin 复测。")
    else:
        print("=> 这一段本身就断了, 板子侧无嫌疑。先修 PC/转换器/线。")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
