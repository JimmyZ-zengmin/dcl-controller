#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mb_holistic.py — 通信域"整体视角"运行时取证 (2026-09-13)

回答三个问题, 全部用**同一次 pyocd 会话内**的读数:
  ① 状态机在推进吗     → g_tick_count / g_mb_ticks / HEARTBEAT 在两批读数间是否增长
  ② 通信域是活的吗     → MbCtrl_t.enabled / src / state / tick_budget / tx_uart
  ③ 字节到底进没进 DTCM → RX 缓冲 (SHM+OFF_MB_RX) 的原始字节 + rx_len/maxrx

★ 为什么必须"同会话": 本机 CMSIS-DAP 会话开始/结束都会复位目标, 而这些都是
  DTCM 上电清零的量 ⇒ 跨会话读到的只是"我刚清零后的值"(2026-09-12 事故)。
★ 为什么带并发发送: 读第一批之后立刻在 485 口持续发帧, 再读第二批 ——
  两批的差值才是"数据有没有到"的证据 (绝对量没有意义)。

用法:
  python tools/mb_holistic.py --send-port COM15
"""
import sys
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, os, re, subprocess, threading, time

SHM_DIAG_OFF = 0x4A10
OFF_MB_CTRL  = 0x4B20
OFF_MB_RX    = 0x4B60
OFF_MB_TX    = 0x4C60
OFF_CTRL_HEARTBEAT = 0x08

# 全局观测符号 (从 build/dcl_h723.map 核出来的固定地址, 都在 DTCM)
G_TICK_COUNT     = 0x20000254
G_MB_TICKS       = 0x20000160
G_ENG_TICKS      = 0x200000E8
G_ENGINE_GATE    = 0x200000EC
G_ENGINE_RUN_SEEN= 0x200000F0


def shm_addr(mapfile):
    pat = re.compile(r"\s+0x([0-9a-fA-F]+)\s+(g_shm)\s*$")
    for ln in open(mapfile, encoding="utf-8", errors="replace"):
        m = pat.match(ln)
        if m:
            return int(m.group(1), 16)
    return None


def parse_reads(out):
    """pyocd 行格式: `2000ceb0:  00000001   |....|` —— 冒号后才是值 (见 sdcfg_probe 的坑)"""
    vals = []
    for ln in out.splitlines():
        m = re.match(r"\s*[0-9a-fA-F]{8}\s*:\s*([0-9a-fA-F]{8})", ln)
        if m:
            vals.append(int(m.group(1), 16))
    return vals


def run_pyocd(cmds, timeout=180):
    args = ["pyocd", "cmd", "-t", "stm32h723xx", "-O", "connect_mode=under-reset"]
    for c in cmds:
        args += ["-c", c]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


class Sender(threading.Thread):
    def __init__(self, port, ms):
        super().__init__(daemon=True)
        self.port, self.ms, self.n = port, ms, 0

    def run(self):
        import serial
        try:
            s = serial.Serial(self.port, 115200, timeout=0.05)
        except Exception as ex:
            print("  !! 发送口打开失败: %s" % ex)
            return
        f = bytes([1, 3, 0x9C, 0x41, 0x00, 0x0A, 0xC5, 0xCD])
        t0 = time.time()
        while (time.time() - t0) * 1000 < self.ms:
            s.write(f); s.flush(); self.n += 1
            time.sleep(0.01)
        s.close()


def mbctrl_words(base):
    """返回 (读命令列表, 字段) —— 读 MbCtrl_t 10 个字"""
    return ["read32 0x%08X" % (base + 4 * i) for i in range(10)]


def decode_mbctrl(w):
    """从 10 个字解出字段 (packed 40B, 偏移见 engine.h 的 MbCtrl_t)"""
    b = bytearray()
    for x in w:
        b += x.to_bytes(4, "little")
    g8 = lambda o: b[o]
    g32 = lambda o: int.from_bytes(b[o:o + 4], "little")
    g16 = lambda o: int.from_bytes(b[o:o + 2], "little")
    return {
        "state": g8(0), "slave_addr": g8(1), "rx_len": g8(2), "rx_pos": g8(3),
        "tx_len": g8(4), "tx_sent": g8(5), "silent": g8(6), "enabled": g8(7),
        "tick_budget": g8(8), "frames_rx": g32(9), "frames_tx": g32(13),
        "err_crc": g32(17), "err_exc": g32(21), "src": g8(25), "tx_uart": g8(26),
        "b_func": g8(27), "b_start": g16(28), "b_qty": g16(30), "b_pos": g16(32),
        "b_len": g16(34), "crc_acc": g16(36),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default="build/dcl_h723.map")
    ap.add_argument("--send-port", default=None)
    ap.add_argument("--send-ms", type=int, default=3000)
    ap.add_argument("--boot-ms", type=int, default=1200)
    ap.add_argument("--gap-ms", type=int, default=800)
    ap.add_argument("--show-raw", action="store_true")
    a = ap.parse_args()

    shm = shm_addr(a.map)
    if shm is None:
        print("!! map 里找不到 g_shm")
        return 2
    ctrl = shm + OFF_MB_CTRL
    rx   = shm + OFF_MB_RX
    tx   = shm + OFF_MB_TX
    hb   = shm + OFF_CTRL_HEARTBEAT
    print("SHM=0x%08X  MbCtrl=0x%08X  RXbuf=0x%08X  TXbuf=0x%08X" % (shm, ctrl, rx, tx))

    def batch():
        return (["read32 0x%08X" % G_TICK_COUNT,
                 "read32 0x%08X" % G_MB_TICKS,
                 "read32 0x%08X" % G_ENG_TICKS,
                 "read32 0x%08X" % G_ENGINE_GATE,
                 "read32 0x%08X" % G_ENGINE_RUN_SEEN,
                 "read32 0x%08X" % hb]
                + mbctrl_words(ctrl)
                + ["read32 0x%08X" % (rx + 4 * i) for i in range(32)]
                + ["read32 0x%08X" % (tx + 4 * i) for i in range(8)])

    N1 = 6 + 10 + 32 + 8     # 第一批字数

    cmds = (["reset", "go", "sleep %d" % a.boot_ms, "halt"]
            + batch()
            + ["go", "sleep %d" % a.gap_ms, "halt"]
            + batch()
            + ["go"])

    snd = None
    if a.send_port:
        snd = Sender(a.send_port, a.send_ms)
        snd.start()
        time.sleep(0.3)
        print("  并发在 %s 上持续发合法 Modbus 帧 (%d ms)…" % (a.send_port, a.send_ms))

    print("  一次会话内取两批读数 (中间跑 %.0f ms)…" % a.gap_ms)
    out = run_pyocd(cmds)
    if a.show_raw:
        print(out)

    v = parse_reads(out)
    if len(v) < 2 * N1:
        print("!! 读回 %d 个值, 期望 %d" % (len(v), 2 * N1))
        return 2
    p1, p2 = v[:N1], v[N1:2 * N1]

    def val(p, i):
        return p[i]

    names = ["g_tick_count", "g_mb_ticks", "g_eng_ticks", "g_engine_gate",
             "g_engine_run_seen", "HEARTBEAT"]
    print("\n=== ① 计数器推进 (两批之间差) ===")
    alive = False
    for i, nm in enumerate(names):
        d = (val(p2, i) - val(p1, i)) & 0xFFFFFFFF
        if nm in ("g_tick_count", "g_mb_ticks", "HEARTBEAT") and d:
            alive = True
        print("   %-17s %10d → %-10d  Δ=%d" % (nm, val(p1, i), val(p2, i), d))
    print("   ⇒ %s" % ("ISR/状态机在推进" if alive else
                       "★ 计数器完全不动 ⇒ ISR 没在跑 / 核被 halt / mb_tick 未被调用"))

    print("\n=== ② MbCtrl_t 控制块 (DTCM) ===")
    c1 = decode_mbctrl(p1[6:16])
    c2 = decode_mbctrl(p2[6:16])
    for k in c1:
        mark = ""
        if c1[k] != c2[k]:
            mark = "   ← Δ %d→%d" % (c1[k], c2[k])
        print("   %-12s %10d%s" % (k, c2[k], mark))

    print("\n=== ③ RX 缓冲 (DTCM 原始字节) ===")
    rb1 = b"".join(x.to_bytes(4, "little") for x in p1[16:48])
    rb2 = b"".join(x.to_bytes(4, "little") for x in p2[16:48])
    print("   第一批: %s" % rb1[:32].hex(" "))
    print("   第二批: %s" % rb2[:32].hex(" "))
    nz1 = sum(1 for x in rb1 if x)
    nz2 = sum(1 for x in rb2 if x)
    print("   非零字节数: %d → %d" % (nz1, nz2))
    if nz2 > nz1:
        print("   ⇒ ★ 收发的字节**确实落进了 DTCM**")
    elif nz2 == 0:
        print("   ⇒ RX 缓冲全 0: 没有任何字节进入过通信域缓冲")
    else:
        print("   ⇒ 缓冲里有旧字节, 但两批之间没有新增")

    print("\n=== ④ 判据小结 ===")
    print("   mb_tick 在跑   : %s" % ("是" if (val(p2, 1) - val(p1, 1)) & 0xFFFFFFFF else "否"))
    print("   通信域使能     : %s" % ("是" if c2["enabled"] else "★ 否"))
    print("   RX 源          : %s" % ("隧道注入 (0x60)" if c2["src"] else "物理口 USART2"))
    print("   frames_rx      : %d (Δ=%d)" % (c2["frames_rx"], c2["frames_rx"] - c1["frames_rx"]))
    if snd:
        print("   (并发发送线程发了 %d 帧, 期望 Δrx_len/缓冲有变化)" % snd.n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
