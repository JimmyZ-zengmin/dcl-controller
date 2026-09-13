#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sdcfg_probe.py — 一次性触发固件里的硬件排障钩子, 并**在同一次会话内**读回结果。

为什么必须"同会话读回":
  本机 CMSIS-DAP 在 pyocd 会话**开始与结束都会复位**目标; 而 MbDiag 区在 DTCM
  (上电清零)。若先触发、再用协议口读, 中间隔着一次会话结束复位 ⇒ 结果必然被清零。
  ⇒ 触发 + 读回必须落在**同一个 pyocd 进程**里 (reset→go→写 SD_CFG→go→halt→read32→go)。
  ★ 这是对"诊断读不出 = 仪器伪影"那次事故的直接对策: 观测窗口包住被测事件。

会用到的 SD_CFG 索引 (见 src/sd.c / src/modbus.c):
  10 → mb_line_test : 整口输入+下拉, 读 IDR 位图 ⇒ 1 的位 = 被外部推挽驱动的脚
  11 → mb_line_probe: PD6 输入+上拉, 采样 40 万次, 统计低电平次数 ⇒ 线上有没有在翻转
结果落点: MbDiag[6]/[7] (line test) 与 [12]/[13]/[14] (probe)。

用法:
  python tools/sdcfg_probe.py --idx 10
  python tools/sdcfg_probe.py --idx 11 --send-port COM15 --send-ms 4000
"""
import sys
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, os, re, subprocess, threading, time

SHM_DIAG_OFF = 0x4A10          # OFF_MB_DIAG
DIAG_WORDS = 32

SD_CFG_BASE = 0x24000400
SD_CFG_MAGIC = 0xF00DBEEF


def shm_addr(mapfile):
    pat = re.compile(r"\s+0x([0-9a-fA-F]+)\s+(g_shm)\s*$")
    for ln in open(mapfile, encoding="utf-8", errors="replace"):
        m = pat.match(ln)
        if m:
            return int(m.group(1), 16)
    return None


def run_pyocd(cmds, timeout=180):
    args = ["pyocd", "cmd", "-t", "stm32h723xx", "-O", "connect_mode=under-reset"]
    for c in cmds:
        args += ["-c", c]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def parse_reads(out):
    """pyocd 的行格式: `2000ceb0:  00000001   |....|` —— **冒号后才是值**。
    ★ 踩过的坑 (2026-09-13): 第一版用"全局扫 8 位十六进制"当退路, 把**地址**也当成值
      收进来了, 于是打印出来全是 0x2000CEB0+4i 这种"漂亮但完全错"的读数 ——
      而它看起来像一份正常的寄存器表。⇒ 解析器必须有格式锚 (冒号), 不能靠正则碰运气。"""
    vals = []
    for ln in out.splitlines():
        m = re.match(r"\s*[0-9a-fA-F]{8}\s*:\s*([0-9a-fA-F]{8})", ln)
        if m:
            vals.append(int(m.group(1), 16))
    return vals


class Sender(threading.Thread):
    """在另一个口上持续发东西, 给固件侧同时进行的测量制造"被测激励"。

    ★ mode:
      'mb'    → 发合法 Modbus 帧 (给 485 总线灌数据)
      'inject'→ 发 0x60 隧道注入帧 (DCL 协议口)。**这是让"板子自己在 PD5 上发"的唯一手段**:
                固件收到 0x60 后走完 Modbus 状态机, 从 USART2 把应答推到真总线。
                于是"板上 TX 有活动"这件事可以与"PD6 上量到的波形"**同时发生**,
                用来判 PD6 到底接在模块的 TXD 还是别的地方 (485 收发器驱动总线时会
                **本地回显** ⇒ 接对的话 PD6 必然跟着动)。
    """

    def __init__(self, port, ms, mode="mb", ser=None):
        super().__init__(daemon=True)
        self.port, self.ms, self.n = port, ms, 0
        self.mode, self.ser, self.own = mode, ser, (ser is None)

    def run(self):
        s = self.ser
        if s is None:
            try:
                import serial
                s = serial.Serial(self.port, 115200, timeout=0.05)
            except Exception as ex:
                print("  !! 发送口打开失败: %s" % ex)
                return
        if self.mode == "inject":
            def crc_mb(d):
                c = 0xFFFF
                for b in d:
                    c ^= b
                    for _ in range(8):
                        c = (c >> 1) ^ 0xA001 if (c & 1) else (c >> 1)
                return c
            r = bytes([1, 3, 0x9C, 0x41, 0x00, 0x0A])
            x = crc_mb(r)
            mbreq = r + bytes([x & 0xFF, x >> 8])
            body = bytes([0x60, len(mbreq) & 0xFF, (len(mbreq) >> 8) & 0xFF]) + mbreq
            c = 0xFFFF
            for b in body:
                c ^= (b << 8)
                for _ in range(8):
                    c = ((c << 1) ^ 0x1021) & 0xFFFF if (c & 0x8000) else (c << 1) & 0xFFFF
            f = bytes([0xC0]) + body + bytes([c & 0xFF, c >> 8])
        else:
            f = bytes([1, 3, 0x9C, 0x41, 0x00, 0x0A, 0xC5, 0xCD])  # 合法 Modbus 读请求
        t0 = time.time()
        while (time.time() - t0) * 1000 < self.ms:
            try:
                s.write(f)
                s.flush()
                self.n += 1
            except Exception:
                break
            if self.mode != "inject":
                time.sleep(0.01)
        if self.own:
            s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idx", type=int, required=True, help="SD_CFG 索引 (10=口线检, 11=PD6 波形)")
    ap.add_argument("--map", default="build/dcl_h723.map")
    ap.add_argument("--boot-ms", type=int, default=250)
    ap.add_argument("--run-ms", type=int, default=600)
    ap.add_argument("--cycles", type=int, default=1, help="同一会话内重复触发 N 次 (取多张位图)")
    ap.add_argument("--send-port", default=None, help="触发期间在另一个口持续发 Modbus 帧")
    ap.add_argument("--inject", action="store_true",
                    help="★ --send-port 改发 0x60 隧道注入帧: 让**板子自己在 PD5 上发**"
                         "(与 --idx 11 合用 ⇒ 判 PD6 是否接到模块 TXD: 驱动总线时本地回显)")
    ap.add_argument("--send-ms", type=int, default=4000)
    ap.add_argument("--show-raw", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.map):
        print("!! 找不到 %s" % a.map)
        return 2
    shm = shm_addr(a.map)
    if shm is None:
        print("!! map 里找不到 g_shm")
        return 2
    dg = shm + SHM_DIAG_OFF
    print("SHM=0x%08X  MbDiag=0x%08X" % (shm, dg))

    # ★ 多周期: 每周期都是 write SD_CFG[15]=魔数 + [idx]=1 → go → 主循环取走执行 → halt 读回。
    #   为什么值得做: 单张位图分不清"静态高"与"正在翻转" —— 而 115200 的数据线大约一半
    #   时间是低。**只有真在翻转的脚, 其位图列才会在不同周期里变** (静态脚永远是同一个值)。
    trig = ["write32 0x%08X 0x%08X" % (SD_CFG_BASE + 4 * 15, SD_CFG_MAGIC),
            "write32 0x%08X %d" % (SD_CFG_BASE + 4 * a.idx, 1),
            "go", "sleep %d" % a.run_ms, "halt"]

    cmds = ["reset", "go", "sleep %d" % a.boot_ms, "halt"]
    cycles = []
    for c in range(a.cycles):
        base = len(cmds)
        cmds += trig
        cycles.append(base)
        cmds += ["read32 0x%08X" % (dg + 4 * i) for i in range(DIAG_WORDS)]
    cmds += ["go"]

    snd = None
    if a.send_port:
        snd = Sender(a.send_port, a.send_ms, mode=("inject" if a.inject else "mb"))
        snd.start()
        time.sleep(0.4)
        print("  并发在 %s 上持续发%s (%d ms)…"
              % (a.send_port, "0x60 注入帧(板子会在 PD5 上发)" if a.inject else " Modbus 帧",
                 a.send_ms))

    print("  触发 SD_CFG[%d] × %d 次…" % (a.idx, a.cycles))
    out = run_pyocd(cmds)
    if a.show_raw:
        print(out)

    v = parse_reads(out)
    if len(v) < DIAG_WORDS * a.cycles:
        print("!! 读回 %d 个字 (期望 %d) —— pyocd 输出格式或内存可达性有问题"
              % (len(v), DIAG_WORDS * a.cycles))
        return 2
    if a.cycles > 1:
        snaps = [v[i * DIAG_WORDS:(i + 1) * DIAG_WORDS] for i in range(a.cycles)]
        print("\n=== 口线检位图 × %d (同一次会话) ===" % a.cycles)
        print("    周期: " + " ".join("%2d" % i for i in range(a.cycles)))
        print("    mk  : " + " ".join("%02X" % (s[7] & 0xFF) for s in snaps))
        varying = []
        for b in range(16):
            bits = [(s[6] >> b) & 1 for s in snaps]
            col = " ".join(" %d" % x for x in bits)
            flag = ""
            if len(set(bits)) > 1:
                flag = "  ★ 在翻转!"
                varying.append(b)
            tag = " ←USART2_RX" if b == 6 else (" ←USART2_TX" if b == 5 else "")
            print("    PD%-2d :%s%s%s" % (b, col, tag, flag))
        print("\n  判据: 静态脚 (没接东西 / 恒高 / 恒低) 的列**恒定不变**;")
        print("        只有真在翻转的脚才会在多次采样间变 ⇒ 找到它就等于找到'对方那根线'。")
        if varying:
            print("  ⇒ 有活动的脚: %s" % ", ".join("PD%d" % b for b in varying))
        else:
            print("  ⇒ 没有任何 PD 脚在翻转 —— 模块的 TXD 根本没接到本口上 (或模块没在发)")
        if snd:
            print("  (并发发送线程发了 %d 帧)" % snd.n)
        return 0

    v = v[-DIAG_WORDS:]      # 取最后一批

    nm = {0: "bytes", 1: "maxrx", 2: "short", 3: "last_isr", 4: "erracc", 5: "last_byte",
          6: "line_map", 7: "line_mk", 8: "MODER", 9: "AFRL", 10: "PUPDR", 11: "cfg_mk",
          12: "pd6_low", 13: "pd6_tot", 14: "prb_mk", 16: "CR1", 17: "CR2", 18: "CR3",
          19: "BRR", 20: "ISR", 21: "PRESC", 22: "reg_mk"}
    print("\n=== MbDiag ===")
    for i in range(DIAG_WORDS):
        if v[i] or i in nm:
            print("  [%2d] %-11s = 0x%08X (%d)" % (i, nm.get(i, ""), v[i], v[i]))

    if a.idx == 10:
        if v[7] != 0xC0DEF00D:
            print("\n  !! line_mk != 0xC0DEF00D → 口线检没跑 (索引/魔数没被取走?)")
        else:
            bm = v[6]
            print("\n  口线检位图 0x%02X:" % bm)
            for b in range(16):
                tag = []
                if b == 5: tag.append("PD5=USART2_TX(我们驱动)")
                if b == 6: tag.append("PD6=USART2_RX(←应挂模块TXD)")
                print("     PD%-2d = %d  %s" % (b, (bm >> b) & 1, " ".join(tag)))
            print("  判据: PD6=1 ⇒ 那根线确实挂着外部推挽高 (模块 TXD 空闲就是高)")
            print("        PD6=0 ⇒ 没东西驱动 PD6 (线没接上 / 接触不良 / 模块 TXD 高阻)")
    if a.idx == 11:
        if v[14] != 0xA5A50001:
            print("\n  !! prb_mk != 0xA5A50001 → 波形采样没跑")
        elif v[13]:
            pct = 100.0 * v[12] / v[13]
            print("\n  PD6 采样: %d/%d 低 = %.2f%%" % (v[12], v[13], pct))
            print("  判据: 低电平比例明显 >0 %% ⇒ 线上确有在翻转的信号 (对方在发)")
            print("        ==0 %% ⇒ 一直高 ⇒ 对方没发 / 没接到这根线")
        else:
            print("\n  !! 采样总数 0")
    if snd:
        print("\n  (并发发送线程发了 %d 帧)" % snd.n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
