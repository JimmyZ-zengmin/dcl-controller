#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fault_suite.py -- 异常注入 + 长稳 统一套件 (单一入口)

动机 (2026-09-13, 用户定调):
  "要出问题了问题自己出来, 而不是找半天。"  +  "设计要符合架构规律。"
  485 那次事故的真正代价不是缺陷, 而是**找它的成本**(4 轮外部审计 + 一整天)。
  架构上的回答有两条, 本套件就是它们的使用者:
    ① **现场自己留案底** —— 固件侧 `FaultLedger`(首例冻结现场 + 分类计数 + 自洽式),
       我们只读它, 不去猜;
    ② **判据必须能失败 + 归因要分层** —— 每条注入都必须证明三件事:
         (a) 预期分类**涨了**      (打中了, 且检测没漏)
         (b) 注入后**链路仍可用**  (拒绝 ≠ 瘫痪)
         (c) 台账**自洽**          (total == Σcats, magic 正确)
       再加一条反向判据:
         (d) **不该涨的分类不许涨** (错站号这类"正常忽略"不许记成故障 —— 防噪声)
       若只做 (a), 一个"任何输入都记一笔故障"的坏固件也能全绿。

三种用途 (同一个入口, 避免工具碎片化):
  --show           打台账: 分类计数 + 首例/末例 + 自洽判定 + **已知外部条件的归类**
  --inject         故障注入矩阵 (默认全套 8 条)
  --soak MINUTES   长稳: 混合流量跑 N 分钟, 每 10s 采样, 事后断言不变量

★ 全程只走协议口: 读台账用 `0x22 READ_BURST` 读 SHM, **不开调试器**(铁律 0)。
  台账地址从 `.map` 读 `g_shm` —— ★ 不许硬编码: 加一个全局就可能挪动它
  (本套件开发时就因硬编码地址读到过"magic=0 的假故障")。

退出码: 0 = 全 PASS; 1 = 有 FAIL (可直接当 CI 门)。
"""
import sys
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse
import os
import re
import struct
import threading
import time

try:
    import serial
except ImportError:
    print("!! need pyserial")
    sys.exit(2)

# ---------------- 台账口径 (必须与 src/faultlog.h 逐字一致) ----------------
FAULT_MAGIC = 0x464C4F47           # "FLOG"
FAULT_N_CATS = 24
LEDGER_BYTES = 136
OFF_FAULT_LOG = 0x7020
FAULT_NAMES = {
    1: "MB_ORE",           # USART2 溢出/帧错/噪声/校验错
    2: "MB_CRC",           # 请求 CRC 校验失败
    3: "MB_SHORT",         # 太短帧被丢
    4: "MB_RX_FULL",       # RX 缓冲被填满
    5: "MB_EXC",           # 回异常响应
    6: "MB_BUILD_OVF",     # 响应组装越界
    7: "ISR_OVER",         # ISR 超拍预算
    8: "SCAN_DIV0",        # 扫描测得 0 周期 (测量坏了)
    9: "SHM_GUARD",        # SHM 护栏被踩
    10: "DEPLOY_REJ",      # 部署被静态校验拒
    11: "PROTO_NAK",       # 协议命令被拒
    12: "MACRO_ERR",       # 宏字节码错误
    13: "CTRL_WHILE_STOP",  # 停机态收到控制类命令
    14: "TIMEBASE",        # ★ DWT 时基没在走 (多半是调试器停的)
}
# ★ 归类: 哪些分类属于"外部条件/仪器造成的", 不该算功能故障。
#   把它们显示出来(不隐藏), 但从"未知故障"计数里剔除 —— 否则噪声会淹掉真信号。
EXTERNAL = {
    14: "DWT 时基被外部停掉 (如调试器会话) ⇒ DWT 计时统计不可用; 复位即可恢复",
}

SYNC_MCU2PC = 0xC1


def crc_ccitt(d):
    c = 0xFFFF
    for b in d:
        c ^= (b << 8)
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if (c & 0x8000) else (c << 1) & 0xFFFF
    return c


def crc_modbus(d):
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if (c & 1) else (c >> 1)
    return c


def dcl_frame(cmd, payload=b""):
    body = bytes([cmd, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    x = crc_ccitt(body)
    return bytes([0xC0]) + body + bytes([x & 0xFF, x >> 8])


def mb_frame(addr, pdu, corrupt_crc=False):
    b = bytes([addr]) + bytes(pdu)
    c = crc_modbus(b)
    if corrupt_crc:
        c ^= 0xFFFF
    return b + bytes([c & 0xFF, c >> 8])


def shm_addr(mapfile):
    pat = re.compile(r"\s+0x([0-9a-fA-F]+)\s+(g_shm)\s*$")
    for ln in open(mapfile, encoding="utf-8", errors="replace"):
        m = pat.match(ln)
        if m:
            return int(m.group(1), 16)
    return None


class Dut:
    """协议口: 台账读 + 通信域计数读 + 合法请求。**只读观测面用, 不开调试器。**"""

    def __init__(self, proto_port, mb_port, mapfile):
        self.s = serial.Serial(proto_port, 115200, timeout=0.05)
        self.mb = serial.Serial(mb_port, 115200, timeout=0.02)
        self.shm = shm_addr(mapfile)
        if self.shm is None:
            raise SystemExit("!! .map 里找不到 g_shm")

    def _x(self, f, wait=0.3):
        self.s.reset_input_buffer()
        self.s.write(f)
        self.s.flush()
        t0 = time.time()
        buf = bytearray()
        while time.time() - t0 < wait:
            n = self.s.in_waiting
            if n:
                buf += self.s.read(n)
            else:
                time.sleep(0.002)
        return bytes(buf)

    def _ack(self, cmd, payload=b"", wait=0.3):
        r = self._x(dcl_frame(cmd, payload), wait)
        if len(r) < 6 or r[0] != SYNC_MCU2PC:
            return None
        ln = r[2] | (r[3] << 8)
        if len(r) < 4 + ln + 2:
            return None
        if crc_ccitt(r[1:4 + ln]) != (r[4 + ln] | (r[5 + ln] << 8)):
            return None
        return r[4:4 + ln]

    def burst(self, addr, count_words):
        p = struct.pack("<I", addr) + struct.pack("<H", count_words)
        pl = self._ack(0x22, p)
        if pl is None or len(pl) < count_words * 4:
            return None
        return b"".join(pl[i:i + 4] for i in range(0, count_words * 4, 4))

    # ---- 台账 ----
    def ledger(self):
        b = self.burst(self.shm + OFF_FAULT_LOG, LEDGER_BYTES // 4)
        if b is None:
            return None
        w = [int.from_bytes(b[i:i + 4], "little") for i in range(0, LEDGER_BYTES, 4)]
        return {
            "magic": w[0], "total": w[1], "cats": w[2:2 + FAULT_N_CATS],
            "f_code": w[26], "f_tick": w[27], "f_c0": w[28], "f_c1": w[29],
            "l_code": w[30], "l_tick": w[31], "l_c0": w[32], "l_c1": w[33],
        }

    def sane(self, lg):
        return (lg is not None and lg["magic"] == FAULT_MAGIC
                and lg["total"] == sum(lg["cats"]))

    # ---- 通信域计数 ----
    def diag(self):
        p = self._ack(0x63)
        if p is None:
            return None
        return {"bytes": int.from_bytes(p[0:4], "little"),
                "maxrx": int.from_bytes(p[4:8], "little"),
                "short": int.from_bytes(p[8:12], "little"),
                "erracc": int.from_bytes(p[16:20], "little"),
                "last": int.from_bytes(p[20:24], "little"),
                "errclr": int.from_bytes(p[60:64], "little")}

    def ctr(self):
        p = self._ack(0x61)
        if p is None:
            return None
        o = 2 + p[1]
        if len(p) < o + 16:
            return None
        g = lambda i: int.from_bytes(p[o + 4 * i:o + 4 * i + 4], "little")
        return {"frames_rx": g(0), "frames_tx": g(1),
                "err_crc": g(2), "err_exc": g(3), "state": p[0]}

    def heartbeat(self):
        b = self.burst(self.shm + 0x08, 1)
        return None if b is None else int.from_bytes(b[0:4], "little")

    # ---- 一条合法 Modbus 请求 (链路活性判据; 内容级: 响应必须 CRC 合法且回显序号) ----
    def mb_valid(self, seq=0, timeout=0.5):
        b = bytes([1, 0x06, 0x9C, 0x81, (seq >> 8) & 0xFF, seq & 0xFF])
        c = crc_modbus(b)
        req = b + bytes([c & 0xFF, c >> 8])
        self.mb.reset_input_buffer()
        self.mb.write(req)
        self.mb.flush()
        t0 = time.time()
        buf = bytearray()
        while len(buf) < 8 and time.time() - t0 < timeout:
            d = self.mb.read(8 - len(buf))
            if d:
                buf += d
        if len(buf) < 8:
            return False
        return (bytes(buf[:8]) == req
                and crc_modbus(bytes(buf[:6])) == (buf[6] | (buf[7] << 8)))

    def mb_send_raw(self, raw):
        self.mb.write(raw)
        self.mb.flush()


# ============================ 命令: --show ============================

def cmd_show(dut):
    lg = dut.ledger()
    if lg is None:
        print("!! 台账读失败 (0x22 无有效应答)")
        return 1
    ok = dut.sane(lg)
    print("故障台账 @ SHM+0x%04X   magic=%s  sane=%s" % (
        OFF_FAULT_LOG, "FLOG" if lg["magic"] == FAULT_MAGIC else hex(lg["magic"]),
        "OK" if ok else "★ BAD"))
    if lg["magic"] != FAULT_MAGIC:
        print("   ★ magic 不对 ⇒ 该域没被登记 (应写在 cold_start_reset 里)")
        return 1
    if lg["total"] != sum(lg["cats"]):
        print("   ★ total(%d) != Σcats(%d) ⇒ 部分写入/重入/布局漂移"
              % (lg["total"], sum(lg["cats"])))
        return 1
    print("   total = %d" % lg["total"])
    unknown = 0
    for i in range(1, FAULT_N_CATS):
        if not lg["cats"][i]:
            continue
        tag = "  [外部条件] " + EXTERNAL[i] if i in EXTERNAL else ""
        if i not in EXTERNAL:
            unknown += lg["cats"][i]
        print("   %-16s = %-10d%s" % (FAULT_NAMES.get(i, "cat%d" % i),
                                      lg["cats"][i], tag))
    print("   首例: code=%d(%s) tick=%d c0=0x%08X c1=%d"
          % (lg["f_code"], FAULT_NAMES.get(lg["f_code"], "-"), lg["f_tick"],
             lg["f_c0"], lg["f_c1"]))
    print("   末例: code=%d(%s) tick=%d c0=0x%08X c1=%d"
          % (lg["l_code"], FAULT_NAMES.get(lg["l_code"], "-"), lg["l_tick"],
             lg["l_c0"], lg["l_c1"]))
    if unknown == 0:
        print("   ⇒ **除外部条件外, 零故障**")
    else:
        print("   ⇒ 有 %d 笔非外部条件故障, 看上面的首例上下文定位" % unknown)
    return 0


# ============================ 命令: --inject ============================

def cmd_inject(dut):
    fails, skips = [], []

    def snap():
        return dut.ledger()

    def inject_case(name, expect_cat, payload, gap=0.15, raw_limit=None):
        """注入 → 断言 (a) 预期涨 (b) 链路可用 (c) 台账自洽"""
        lg0 = snap()
        if lg0 is None:
            print("  %-26s SKIP (台账读不到)" % name)
            skips.append(name)
            return
        if raw_limit is not None:
            # 连续灌 raw_limit 字节 (无帧间隔, 用于打满 RX 缓冲)
            for _ in range(raw_limit // len(payload) + 1):
                dut.mb_send_raw(payload)
            time.sleep(0.5)
        else:
            dut.mb_send_raw(payload)
            time.sleep(gap)
        lg1 = snap()
        if lg1 is None:
            print("  %-26s FAIL (注入后台账读不到 ⇒ 可能锁死)" % name)
            fails.append(name)
            return
        d = lg1["cats"][expect_cat] - lg0["cats"][expect_cat]
        alived = dut.mb_valid(max(1, lg1["total"] & 0xFFFF))
        sane = dut.sane(lg1)
        verdict = "PASS" if (d > 0 and alived and sane) else "FAIL"
        if verdict == "FAIL":
            fails.append(name)
        print("  %-26s %s  Δ%-14s=%d  链路%s  台账%s"
              % (name, verdict, FAULT_NAMES.get(expect_cat, expect_cat), d,
                 "OK" if alived else "★断", "OK" if sane else "★坏"))

    print("=" * 78)
    print("故障注入矩阵 —— 每条必须 (a) 预期分类涨 (b) 链路仍可用 (c) 台账自洽")
    print("=" * 78)
    lg = snap()
    if lg is None:
        print("!! 台账读不到, 无法开始")
        return 1
    print("基线: total=%d sane=%s\n" % (lg["total"], "OK" if dut.sane(lg) else "BAD"))

    # (a) 坏 CRC → MB_CRC
    inject_case("坏 CRC 帧", 2, mb_frame(1, [0x03, 0x9C, 0x41, 0x00, 0x01], True))
    # (b) 太短帧 → MB_SHORT
    inject_case("太短帧 (2B)", 3, bytes([0x01, 0x03]))
    # (c) 非法功能码 → MB_EXC
    inject_case("非法功能码 0x99", 5, mb_frame(1, [0x99, 0x00, 0x00, 0x00, 0x01]))
    # (d) 越界地址 40000 → MB_EXC
    inject_case("越界地址 40000", 5, mb_frame(1, [0x03, 0x9C, 0x40, 0x00, 0x01]))
    # (e) qty=0 → MB_EXC
    inject_case("qty=0", 5, mb_frame(1, [0x03, 0x9C, 0x41, 0x00, 0x00]))
    # (f) 帧间零间隔灌满 → MB_RX_FULL (且必须不锁死)
    inject_case("无帧间隔灌满", 4, mb_frame(1, [0x06, 0x9C, 0x81, 0x11, 0x22]),
                raw_limit=600)
    # (g) 持续垃圾流 → 通信类分类须涨 (ORE 或 CRC 或 SHORT 都可)
    lg0 = snap()
    for _ in range(120):
        dut.mb_send_raw(bytes([0xA5, 0x5A, 0xC3, 0x3C, 0x0F, 0xF0]))
    time.sleep(0.5)
    lg1 = snap()
    got = 0
    if lg0 and lg1:
        for i in (1, 2, 3, 4):
            got += lg1["cats"][i] - lg0["cats"][i]
    alived = dut.mb_valid(7)
    v = "PASS" if (got > 0 and alived and dut.sane(lg1)) else "FAIL"
    if v == "FAIL":
        fails.append("持续垃圾流")
    print("  %-26s %s  Δ通信类故障=%d  链路%s"
          % ("持续垃圾流", v, got, "OK" if alived else "★断"))

    # ★ 反向判据 (d): "正常忽略" 不许记成故障 —— 防噪声
    lg0 = snap()
    for _ in range(10):
        dut.mb_send_raw(mb_frame(2, [0x03, 0x9C, 0x41, 0x00, 0x01]))   # 错站号
        time.sleep(0.02)
    time.sleep(0.4)
    lg1 = snap()
    noise = (lg1["total"] - lg0["total"]) if (lg0 and lg1) else -1
    v = "PASS" if noise == 0 else "FAIL"
    if v == "FAIL":
        fails.append("错站号不应记账")
    print("  %-26s %s  台账新增=%d (期望 0 —— 正常忽略不是故障)"
          % ("错站号(反向判据)", v, noise))

    lg = snap()
    print("\n最终: total=%d sane=%s" % (lg["total"], "OK" if dut.sane(lg) else "BAD"))
    if not dut.sane(lg):
        fails.append("台账自洽")
    print("=" * 78)
    print("注入矩阵: %d 条, FAIL=%d %s" % (9, len(fails), fails if fails else ""))
    if skips:
        print("SKIP=%d %s (SKIP 不计 PASS)" % (len(skips), skips))
    return 1 if fails else 0


# ============================ 命令: --soak ============================

def cmd_soak(dut, minutes, gap_ms):
    """长稳: 混合流量跑 N 分钟; 每 10s 采样; 事后断言不变量。

    ★ 长稳真正要抓的是"**慢慢变坏**": 计数器倒退(意外复位)、链路锁死、
      台账自洽被破坏、时基丢失、以及"故障增长率"随时间上升。
      单次短测看不到这些 —— 它们是时间的函数。"""
    print("=" * 78)
    print("长稳: %.1f 分钟, 混合流量 (合法+周期性非法), 每 10s 采样" % minutes)
    print("=" * 78)
    stop = threading.Event()
    st = {"sent": 0, "ok": 0, "seq": 0, "illegal": 0}

    req = mb_frame(1, [0x06, 0x9C, 0x81, 0x00, 0x00])
    bad = mb_frame(1, [0x03, 0x9C, 0x40, 0x00, 0x01])   # 越界 ⇒ 周期性非法注入

    def writer():
        while not stop.is_set():
            st["seq"] += 1
            r = bytearray(req)
            r[4] = (st["seq"] >> 8) & 0xFF
            r[5] = st["seq"] & 0xFF
            c = crc_modbus(bytes(r[:6]))
            r[6] = c & 0xFF
            r[7] = c >> 8
            dut.mb_send_raw(bytes(r))
            st["sent"] += 1
            if st["sent"] % 20 == 0:          # 每 20 条掺一条非法的
                dut.mb_send_raw(bad)
                st["illegal"] += 1
            time.sleep(gap_ms / 1000.0)

    samples = []
    th = threading.Thread(target=writer, daemon=True)
    th.start()
    t_end = time.time() + minutes * 60.0
    nxt = time.time()
    try:
        while time.time() < t_end:
            if time.time() >= nxt:
                nxt += 10.0
                d, c, lg = dut.diag(), dut.ctr(), dut.ledger()
                hb = dut.heartbeat()
                if None in (d, c, lg) or hb is None:
                    samples.append(None)
                    print("  采样失败 (协议口读不到) —— 可能就是故障")
                else:
                    samples.append({"hb": hb, "bytes": d["bytes"],
                                    "frx": c["frames_rx"], "ftx": c["frames_tx"],
                                    "ecrc": c["err_crc"], "eexc": c["err_exc"],
                                    "state": c["state"], "total": lg["total"],
                                    "sane": dut.sane(lg),
                                    "tb": lg["cats"][14], "errclr": d["errclr"]})
                    s = samples[-1]
                    print("  t=%4ds hb=%u bytes=%u frx=%u ftx=%u ecrc=%u eexc=%u "
                          "state=%u 台账=%u%s"
                          % (int(time.time() - t_end + minutes * 60), s["hb"],
                             s["bytes"], s["frx"], s["ftx"], s["ecrc"], s["eexc"],
                             s["state"], s["total"], "" if s["sane"] else " ★台账坏"))
            time.sleep(0.05)
    finally:
        stop.set()
        th.join(timeout=2.0)

    # ---- 事后断言 ----
    print("\n" + "=" * 78)
    print("长稳不变量")
    print("=" * 78)
    ok_s = [s for s in samples if s]
    fails = []
    if len(ok_s) < 2:
        print("  ★ 采样不足 ⇒ FAIL")
        return 1
    # ★★ 测"锁死"必须**在流量停下来之后**测: 每 5ms 一条请求, 采样瞬间状态机
    #    本来就可能正处在 RX —— 用"末态必须 IDLE"是**瞬态判据**, 不成立
    #    (本套件第一版就栽在这: 报出 state=1 这条假故障)。
    #    "锁死"的定义是"有界时间内回不到 IDLE", 所以要静默后再判。
    time.sleep(0.4)
    c_quiet = dut.ctr()
    state_quiet = None if c_quiet is None else c_quiet["state"]

    def check(name, cond, detail=""):
        print("  %-34s %s %s" % (name, "PASS" if cond else "FAIL", detail))
        if not cond:
            fails.append(name)

    # (1) 计数器不许倒退 —— 倒退 = 意外复位 (单次短测抓不到的东西)
    def mono(key):
        seq = [s[key] for s in ok_s]
        bad = [(i, seq[i - 1], seq[i]) for i in range(1, len(seq)) if seq[i] < seq[i - 1]]
        return bad
    for k in ("hb", "bytes", "frx", "ftx", "total"):
        b = mono(k)
        check("单调不漏: %s 不倒退" % k, not b, ("首处倒退 %s" % str(b[0])) if b else "")
    # (2) 台账自洽 全程
    check("台账全程自洽 (total==Σcats)", all(s["sane"] for s in ok_s))
    # (3) 通信域不锁死 (静默后判)
    check("静默后通信域回到 IDLE (未锁死)", state_quiet == 0,
          "state=%s" % ("读失败" if state_quiet is None else state_quiet))
    # (4) 链路仍可用 (内容级)
    alive = dut.mb_valid(0x1234)
    check("压力后链路仍可用 (内容级)", alive)
    # (5) ★ 应答覆盖 —— **恒等式必须从固件语义推出来** (本套件第一版推错了):
    #     固件里 `frames_rx++` 在"任何被判为完整帧"时发生; `frames_tx++` 在"响应发完"时发生。
    #     一条被判完整的帧**不产生响应**只有两种情形: 非本站/广播、**CRC 校验失败**。
    #     ⇒ 正确恒等式:  Δframes_tx ≈ Δframes_rx − Δerr_crc − Δ(非本站帧数)
    #     ✗ 错误版本(第一版): 减去"非法注入数" —— 而**非法地址帧是会回异常响应的**,
    #       它照样 frames_tx++ ⇒ 那条判据把正确行为判成了缺陷 (差 89 条)。
    d_frx = c_quiet["frames_rx"] - ok_s[0]["frx"]
    d_ftx = c_quiet["frames_tx"] - ok_s[0]["ftx"]
    d_ecc = c_quiet["err_crc"] - ok_s[0]["ecrc"]
    check("应答覆盖: Δftx ≈ Δfrx − Δerr_crc",
          abs(d_ftx - (d_frx - d_ecc)) <= 5,
          "Δfrx=%d Δftx=%d Δecrc=%d ⇒ 缺口 %d"
          % (d_frx, d_ftx, d_ecc, d_ftx - (d_frx - d_ecc)))
    # (6) 故障增长率: 后半程不许明显快于前半程 (慢速劣化探测器)
    half = len(ok_s) // 2
    g1 = ok_s[half]["total"] - ok_s[0]["total"]
    g2 = ok_s[-1]["total"] - ok_s[half]["total"]
    check("故障增长率不发散 (后半 ≤ 前半×3)", g2 <= g1 * 3 + 5,
          "前半+%d 后半+%d" % (g1, g2))
    # (7) 外部条件点名 (不算失败, 但必须说清)
    tb = ok_s[-1]["tb"]
    print("  %-34s %s" % ("[点名] DWT 时基故障计数",
                          ("%d —— 时基被外部停掉, DWT 计时统计不可用" % tb)
                          if tb else "0 (时基正常)"))
    print("\n  流量: 发出 %d 条 (其中非法注入 %d)" % (st["sent"], st["illegal"]))
    print("=" * 78)
    print("长稳: %s" % ("全 PASS" if not fails else "FAIL: %s" % fails))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", default="COM14")
    ap.add_argument("--mb", default="COM15")
    ap.add_argument("--map", default="build/dcl_h723.map")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--inject", action="store_true")
    ap.add_argument("--soak", type=float, default=None, metavar="MINUTES")
    ap.add_argument("--gap-ms", type=float, default=5.0)
    a = ap.parse_args()
    if not os.path.exists(a.map):
        print("!! 找不到 %s" % a.map)
        return 2
    dut = Dut(a.proto, a.mb, a.map)
    print("SHM = 0x%08X   台账 = 0x%08X" % (dut.shm, dut.shm + OFF_FAULT_LOG))
    rc = 0
    if a.show or not (a.inject or a.soak):
        rc |= cmd_show(dut)
    if a.inject:
        rc |= cmd_inject(dut)
    if a.soak:
        rc |= cmd_soak(dut, a.soak, a.gap_ms)
    dut.s.close()
    dut.mb.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
