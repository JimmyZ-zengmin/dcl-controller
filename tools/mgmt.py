#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mgmt.py -- 板子**管理面**客户端 (常规非阻断通信, 不 halt / 不停引擎)

═══ 这是什么 ═══
  一条"管理通道": 用**普通协议帧**问板子内部状态, 像操作系统的管理面对内核那样。
  设计上有三条硬规矩:
    ① **地址全部来自板子的自报目录 (`0x64`)**, 本文件里**没有任何硬编码地址** ——
       布局怎么挪都不会读错。今天硬编码 SHM 基址读出过"台账 magic=0 的假故障"。
    ② **非侵入**: 只读 + 定长 + 不写 SD + 不等待外设 ⇒ 不会停引擎、不会改被测对象。
       能走协议就别走调试器 (铁律 0)。
    ③ **判读在工具里**: 读到的原始数按 kind 解释成"结论", 不是把十六进制甩给人。

═══ 用法 ═══
  python tools/mgmt.py --manifest            列出板子自报的诊断目录
  python tools/mgmt.py --read FAULTLOG       按名字读一个区 (按 kind 解释)
  python tools/mgmt.py --health              一次拿全套健检结论 (推荐起点)
  python tools/mgmt.py --boot                复位归因 (为什么复位了)
  python tools/mgmt.py --symptom comm        按症状取"该读什么" (comm|reset|faults|engine)
  python tools/mgmt.py --watch 5             连续 5 次健检 (看哪些量在动/不动)

★ 退出码: 0 = 健康; 1 = 有结论判为"异常"; 2 = 通道读不到 (先查链路)。
"""
import sys
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse
import struct
import time

try:
    import serial
except ImportError:
    print("!! need pyserial")
    sys.exit(2)

SYNC_MCU2PC = 0xC1
CMD_READ_BURST = 0x22
CMD_MANIFEST = 0x64

MF_K_U32, MF_K_BITS, MF_K_BYTES, MF_K_STRUCT = 0, 1, 2, 3
MF_F_SHM = 1

FAULT_NAMES = {
    1: "MB_ORE", 2: "MB_CRC", 3: "MB_SHORT", 4: "MB_RX_FULL", 5: "MB_EXC",
    6: "MB_BUILD_OVF", 7: "ISR_OVER", 8: "SCAN_DIV0", 9: "SHM_GUARD",
    10: "DEPLOY_REJ", 11: "PROTO_NAK", 12: "MACRO_ERR", 13: "CTRL_WHILE_STOP",
    14: "TIMEBASE",
}
FAULT_EXTERNAL = {14: "DWT 时基被外部停掉 (调试器会话) ⇒ DWT 计时统计不可用; 复位即恢复"}
MB_ST = {0: "IDLE", 1: "RX", 2: "EXEC", 3: "BUILD", 4: "TX"}


def crc_ccitt(d):
    c = 0xFFFF
    for b in d:
        c ^= (b << 8)
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if (c & 0x8000) else (c << 1) & 0xFFFF
    return c


def dcl_frame(cmd, payload=b""):
    body = bytes([cmd, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    x = crc_ccitt(body)
    return bytes([0xC0]) + body + bytes([x & 0xFF, x >> 8])


class Board:
    def __init__(self, port):
        self.s = serial.Serial(port, 115200, timeout=0.05)
        self._manifest = None

    def _ack(self, cmd, payload=b"", wait=0.4):
        self.s.reset_input_buffer()
        self.s.write(dcl_frame(cmd, payload))
        self.s.flush()
        t0 = time.time()
        buf = bytearray()
        while time.time() - t0 < wait:
            n = self.s.in_waiting
            if n:
                buf += self.s.read(n)
            else:
                time.sleep(0.002)
        if len(buf) < 6 or buf[0] != SYNC_MCU2PC:
            return None, None
        ln = buf[2] | (buf[3] << 8)
        if len(buf) < 4 + ln + 2:
            return None, None
        if crc_ccitt(buf[1:4 + ln]) != (buf[4 + ln] | (buf[5 + ln] << 8)):
            return None, None
        return buf[1], buf[4:4 + ln]          # (status, payload)

    # ---- 目录: 板子自报, 本工具不硬编码任何地址 ----
    def manifest(self, refresh=False):
        if self._manifest is not None and not refresh:
            return self._manifest
        out, page = [], 0
        total = None
        while True:
            st, pl = self._ack(CMD_MANIFEST, bytes([page]))
            if st is None:
                raise SystemExit("!! 0x64 无应答 —— 固件没带管理面 (或链路不通)")
            if st != 0x00:
                raise SystemExit("!! 0x64 NAK: %s" % (pl or b"").decode("latin-1"))
            total = pl[0]
            n = pl[1]
            for i in range(n):
                o = pl[2 + i * 20:2 + (i + 1) * 20]
                name = o[0:12].split(b"\x00")[0].decode("latin-1")
                out.append({"name": name,
                            "addr": struct.unpack_from("<I", o, 12)[0],
                            "words": struct.unpack_from("<H", o, 16)[0],
                            "kind": o[18], "flags": o[19]})
            page += 1
            if len(out) >= total or n == 0:
                break
        self._manifest = out
        return out

    def find(self, name):
        for e in self.manifest():
            if e["name"].upper() == name.upper():
                return e
        return None

    def read(self, name):
        e = self.find(name)
        if e is None:
            raise SystemExit("!! 目录里没有 %s (用 --manifest 看有哪些)" % name)
        p = struct.pack("<I", e["addr"]) + struct.pack("<H", e["words"])
        st, pl = self._ack(CMD_READ_BURST, p)
        if st != 0x00 or pl is None:
            raise SystemExit("!! 读 %s (0x%08X, %d 字) 被拒/无应答 —— 这是 bug, 不是环境问题"
                             % (name, e["addr"], e["words"]))
        n = e["words"]
        return e, [struct.unpack_from("<I", pl, i * 4)[0] for i in range(n)]


# ============================ 判读层 (把原始数变成结论) ============================

def verdict(name, e, w):
    """返回 (是否异常, 结论字符串列表)。★ 判据尽量做成"能失败"的。"""
    out, bad = [], False
    if name == "SHM_CTRL":
        magic, ver, hb = w[0], w[1], w[2]
        out.append("MAGIC=0x%08X %s / VERSION=%d / HEARTBEAT=%d" %
                   (magic, "OK" if magic else "★空", ver, hb))
        # ★ ENGINE_RUN 在 SHM+0x0D ⇒ 是 w[3](=SHM+0x0C) 的**高 8 位**。
        #   第一版取 `w[3] & 0xFF`(=0x0C) ⇒ 永远读到 0, 于是把"引擎在跑"报成"没跑"。
        #   (同族: 偏移错一位, 结论完全相反 —— 这类错必须靠"读回已知值"才抓得住。)
        run = (w[3] >> 8) & 0xFF
        out.append("ENGINE_RUN 字节(SHM+0x0D)=0x%02X ⇒ 引擎 %s"
                   % (run, "在跑" if (run & 1) else "已停"))
    elif name == "MB_DIAG":
        out.append("bytes=%d maxrx=%d short=%d erracc=0x%02X last=0x%02X ERRCLR=%d"
                   % (w[0], w[1], w[2], w[4], w[5], w[15]))
        out.append("USART2 CR1=0x%08X BRR=0x%X ISR=0x%08X"
                   % (w[16], w[19], w[20]))
        if w[16] & (1 << 29):
            out.append("  FIFO 已开 (bit29=1) ✓")
        else:
            out.append("  ★ FIFO 未开 (bit29=0) ⇒ 100µs 轮询撑不住 86.8µs 字节间隔")
            bad = True
        if w[26]:      # LAT_N
            out.append("响应延迟(板内): min=%d 拍 max=%d 拍 (1 拍=100µs) n=%d FASTOK=%d RX_FULL=%d"
                       % (w[24], w[25], w[26], w[28], w[29]))
    elif name == "MB_CTRL":
        b = b"".join(struct.pack("<I", x) for x in w)
        st = b[0]
        out.append("state=%s(%d) rx_len=%d silent=%d enabled=%d src=%s tx_uart=%d"
                   % (MB_ST.get(st, "?"), st, b[2], b[6], b[7],
                      "隧道" if b[25] else "物理口", b[26]))
        out.append("frames_rx=%d frames_tx=%d err_crc=%d err_exc=%d"
                   % (struct.unpack_from("<I", b, 9)[0], struct.unpack_from("<I", b, 13)[0],
                      struct.unpack_from("<I", b, 17)[0], struct.unpack_from("<I", b, 21)[0]))
        if not b[7]:
            out.append("★ enabled=0 ⇒ 通信域没开, 什么都不收")
            bad = True
    elif name == "FAULTLOG":
        magic, total, cats = w[0], w[1], w[2:2 + 24]
        first, last = w[26:30], w[30:34]
        if magic != 0x464C4F47:
            out.append("★ magic=0x%08X 不是 FLOG ⇒ 台账没登记" % magic)
            return True, out
        s = sum(cats)
        out.append("total=%d sane=%s" % (total, "OK" if total == s else "★BAD(Σ=%d)" % s))
        if total != s:
            bad = True
        ext = 0
        for i in range(1, 24):
            if cats[i]:
                tag = ("  [外部条件] " + FAULT_EXTERNAL[i]) if i in FAULT_EXTERNAL else ""
                if i not in FAULT_EXTERNAL:
                    ext += cats[i]
                out.append("  %-16s = %-8d%s" % (FAULT_NAMES.get(i, "cat%d" % i), cats[i], tag))
        out.append("  首例: code=%d(%s) tick=%d c0=0x%08X c1=%d"
                   % (first[0], FAULT_NAMES.get(first[0], "-"), first[1], first[2], first[3]))
        out.append("  末例: code=%d(%s) tick=%d c0=0x%08X c1=%d"
                   % (last[0], FAULT_NAMES.get(last[0], "-"), last[1], last[2], last[3]))
        if ext:
            out.append("  ⇒ 非外部条件故障 %d 笔" % ext)
    elif name == "RTC_DIAG":
        out.append("BDCR=0x%08X ISR=0x%08X TR=0x%X 状态判定=%d(1=日历可信)"
                   % (w[0], w[1], w[2], w[3]))
        if w[3] != 1:
            out.append("  ★ 日历未判为可信 ⇒ 时间戳不可用 (见 rtc.c 的启动判据)")
            bad = True
    elif name == "BB_DIAG":
        out.append("映射表绑到 %d 槽 (应=60) / FNV=0x%08X" % (w[36], w[37]))
        if w[36] != 60:
            out.append("★ 绑到的槽数不是 60 ⇒ 表里有越界项")
            bad = True
    elif name == "BOOT_AXI":
        rsr, bdcr = w[2], w[3]
        names = []
        for bit, nm in ((16, "PORR 上电"), (17, "SFTRSTF 软件复位"), (18, "IWDG1 独立看门狗"),
                        (19, "WWDG1 窗口看门狗"), (20, "LPWR 低功耗"), (21, "BORR 欠压"),
                        (22, "PINR 复位脚")):
            if rsr & (1 << bit):
                names.append(nm)
        known = 0x7F << 16
        stray = rsr & ~known
        out.append("启动次数=%d RSR=0x%08X BDCR=0x%08X" % (w[0], rsr, bdcr))
        out.append("复位原因位: %s" % (", ".join(names) if names else "无 (首次上电或未置位)"))
        # ★ 不许照抄"多因同时成立"这种物理上不可能的结论:
        #   一次复位只可能有一个主因 ⇒ 同时置多位 = 标志没被清 / 位定义不符, 要么就是复位源真在反复抖。
        if len(names) > 1:
            out.append("  ★ 同时置 %d 位 ⇒ **不是单一复位原因**。可能: ①RSR 未被清(RMVF) "
                       "②位定义与实际不符 ③复位源在反复抖动。需要单独查, 别照抄成'多个原因'。"
                       % len(names))
            bad = True
        if stray:
            out.append("  ★ RSR 里有本项目未定义的位: 0x%08X ⇒ 位定义需重核" % stray)
            bad = True
    else:
        out.append("(无专用解析器, 原始 %d 字)" % len(w))
        for i in range(0, min(len(w), 16), 4):
            out.append("  +%02X: %s" % (i * 4, " ".join("%08X" % x for x in w[i:i + 4])))
    return bad, out


# ============================ 命令实现 ============================

def cmd_manifest(b):
    m = b.manifest()
    print("板子自报诊断目录: %d 条" % len(m))
    print("  %-12s %-12s %6s  %-6s  %s" % ("名字", "地址", "字数", "kind", "flags"))
    for e in m:
        print("  %-12s 0x%08X %6d  %-6s  %s"
              % (e["name"], e["addr"], e["words"],
                 ["u32", "bits", "bytes", "struct"][e["kind"]],
                 "SHM相对" if e["flags"] & MF_F_SHM else "绝对(AXI)"))
    print("  ★ 本工具不硬编码任何地址: 全部来自上面这张表 (0x64)。")
    return 0


def cmd_read(b, name):
    e, w = b.read(name)
    print("== %s @ 0x%08X (%d 字, kind=%s) ==" % (name, e["addr"], e["words"],
                                                  ["u32", "bits", "bytes", "struct"][e["kind"]]))
    if e["kind"] == MF_K_BYTES:
        raw = b"".join(struct.pack("<I", x) for x in w)
        for i in range(0, min(len(raw), 64), 16):
            print("  +%03X  %s" % (i, " ".join("%02X" % c for c in raw[i:i + 16])))
    else:
        for i in range(0, len(w), 4):
            print("  [%2d] %s" % (i, " ".join("%08X" % x for x in w[i:i + 4])))
    bad, lines = verdict(name, e, w)
    print("  判读:")
    for l in lines:
        print("    " + l)
    return 1 if bad else 0


def cmd_health(b):
    print("=" * 74)
    print("健检 (全部按名字读, 无硬编码地址; 只读 ⇒ 不停引擎)")
    print("=" * 74)
    bad = 0
    # 先判活: HEARTBEAT 必须推进
    e, w1 = b.read("SHM_CTRL")
    hb1 = w1[2]
    time.sleep(0.25)
    _, w2 = b.read("SHM_CTRL")
    hb2 = w2[2]
    alive = hb2 != hb1
    print("① 存活: HEARTBEAT %d → %d %s" % (hb1, hb2, "推进 ✓" if alive else "★不动 ⇒ ISR 没跑"))
    if not alive:
        bad += 1
    print("   引擎: ENGINE_RUN 字节=0x%02X (bit0=%d)" % (w2[3] & 0xFF, w2[3] & 1))
    for nm in ("FAULTLOG", "MB_DIAG", "MB_CTRL", "BB_DIAG", "RTC_DIAG", "BOOT_AXI"):
        try:
            e, w = b.read(nm)
        except SystemExit as ex:
            print("② %-9s ★读失败: %s" % (nm, ex)); bad += 1; continue
        vbad, lines = verdict(nm, e, w)
        print("%s %-9s %s" % ("②" if nm == "FAULTLOG" else " ", nm, lines[0]))
        for l in lines[1:]:
            print("      " + l)
        if vbad:
            bad += 1
    print("=" * 74)
    print("结论: %s" % ("全部健康" if bad == 0 else "有 %d 项异常 (见上面的 ★)" % bad))
    return 1 if bad else 0


def cmd_boot(b):
    e, w = b.read("BOOT_AXI")
    _, lines = verdict("BOOT_AXI", e, w)
    for l in lines:
        print("  " + l)
    print("  ★ 用法: 反复读同一个数 —— 若'启动次数'在涨, 说明板子在**反复复位**;")
    print("     复位原因位告诉你是什么触发的 (看门狗/掉电/复位脚/软件)。")
    return 0


def cmd_symptom(b, s):
    """回答"哪里出问题读什么" —— 把排障路径写进代码, 而不是靠记忆。"""
    plan = {
        "comm": [("MB_DIAG", "字节级: 到底到了多少字节 / 有没有 ORE / FIFO 开没开"),
                 ("MB_CTRL", "状态机: 是否锁死在某状态 / enabled / 收发计数"),
                 ("MB_RX", "收到的原始字节 (最硬证据: 内容对不对)"),
                 ("MB_TX", "待发/已发的响应字节")],
        "reset": [("BOOT_AXI", "复位次数 + 复位原因 (看门狗/掉电/复位脚/软件)"),
                  ("SHM_CTRL", "复位后 HEARTBEAT 是否在推进 (ISR 活着)")],
        "faults": [("FAULTLOG", "首例(冻结现场) + 24 类计数 + 自洽式"),
                   ("MB_DIAG", "通信类故障的上下文")],
        "engine": [("SHM_CTRL", "HEARTBEAT / ENGINE_RUN / N_ROUTES"),
                   ("FAULTLOG", "ISR_OVER / SCAN_DIV0 说明超载或时基问题"),
                   ("RTC_DIAG", "时间基座是否可信")],
    }
    if s not in plan:
        print("可用症状: %s" % ", ".join(sorted(plan)))
        return 2
    print("症状 '%s' ⇒ 依次读:" % s)
    bad = 0
    for nm, why in plan[s]:
        e, w = b.read(nm)
        vbad, lines = verdict(nm, e, w)
        bad += 1 if vbad else 0
        print("  ── %s  (%s)" % (nm, why))
        for l in lines:
            print("      " + l)
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM14")
    ap.add_argument("--manifest", action="store_true")
    ap.add_argument("--read", default=None, metavar="NAME")
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--boot", action="store_true")
    ap.add_argument("--symptom", default=None)
    ap.add_argument("--watch", type=int, default=0)
    a = ap.parse_args()
    b = Board(a.port)
    try:
        b.manifest()          # T0: 通道活性 (拿不到目录 = 链路/固件问题)
    except SystemExit as ex:
        print(ex)
        return 2
    if a.manifest:
        return cmd_manifest(b)
    if a.read:
        return cmd_read(b, a.read)
    if a.boot:
        return cmd_boot(b)
    if a.symptom:
        return cmd_symptom(b, a.symptom)
    if a.watch:
        rc = 0
        for i in range(a.watch):
            print("\n########## 第 %d/%d 次 ##########" % (i + 1, a.watch))
            rc |= cmd_health(b)
            if i + 1 < a.watch:
                time.sleep(2.0)
        return rc
    return cmd_health(b)


if __name__ == "__main__":
    sys.exit(main())
