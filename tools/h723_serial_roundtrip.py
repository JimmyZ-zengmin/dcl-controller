#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""串口往返 20 s 错乱测试 —— 专打契约 §9.4 那个**已发生过的错帧现场**。

## 为什么这样设计
契约 §9.4 的标本：密轮询 `0x22` 读 1 个字时，读回位型是 `0x1DF70200` = **`0x01` 的应答载荷**。
★ 最坏巧合：**`0x22` 读 1 个字的应答也是 4 字节**，与 `0x01` **完全相同** ⇒ **长度判据分不开**。
⇒ 本测试就**故意交替**发这两条（应答同长），用**内容**判归属：
     0x01 的载荷 = [fw:u16=0x0200][cap:u16]      （小端 u32 的高 16 是 cap）
     0x22 的载荷 = [MAGIC:u32 = 0x44434C31]
   ⇒ 发出 A 收到 B 的载荷 = **mis**（v1 下这是可能的；v2 下不该发生）

## 两个相位（各 10 s，合计 20 s）
  A. **v1（默认）**：不协商，靠内容判归属
  B. **v2**：`0x05 [1]` 协商后，应答**显式带 CMD** ⇒ 严格判归属

## 判据（每条都能失败）
  C1 内容/mis == 0
  C2 超时率 == 0
  C3 `uart_ore` / `uart_drop` / `frame_bad` 增量 == 0（0x38 尾部）
  R  **正向对照**：主动注入一个 CRC 错帧 ⇒ `frame_bad` **必须涨**（否则 C3 是空判据）

用法: DCL_PORT=COM21 python .tmpctl/serial_roundtrip.py --secs 20
"""
import argparse, os, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import serial  # noqa: E402

SYNC_REQ_V1, SYNC_ACK_V1 = 0xC0, 0xC1
SYNC_REQ_V2, SYNC_ACK_V2 = 0xC2, 0xC3
CMD_VERSION, CMD_FRAME_MODE, CMD_READ, CMD_STATUS = 0x01, 0x05, 0x20, 0x38


def crc16(b):
    """CRC16-CCITT: poly 0x1021, init 0xFFFF, MSB-first，无 final xor。"""
    c = 0xFFFF
    for x in b:
        c ^= (x << 8)
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if (c & 0x8000) else (c << 1) & 0xFFFF
    return c


def build_v1(cmd, payload=b"", seq=0):
    body = bytes([cmd, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    return bytes([SYNC_REQ_V1]) + body + struct.pack("<H", crc16(body))


def build_v2(cmd, payload=b"", seq=0):
    body = bytes([cmd, seq & 0xFF, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    return bytes([SYNC_REQ_V2]) + body + struct.pack("<H", crc16(body))


def read_frame(sp, v2, to=0.25):
    """返回 (cmd|None, sts, payload, ok)。v1 时 cmd 为 None（**这正是问题**）。"""
    t0 = time.time()
    buf = b""
    while time.time() - t0 < to:
        b = sp.read(1)
        if not b:
            continue
        if not buf:
            want = SYNC_ACK_V2 if v2 else SYNC_ACK_V1
            if b[0] != want:
                continue
            buf = b
            continue
        buf += b
        need = 8 if v2 else 6          # 最短帧长（空载荷）
        if len(buf) < need:
            continue
        if v2:
            cmd, seq, sts, lo, hi = buf[1], buf[2], buf[3], buf[4], buf[5]
            n = lo | (hi << 8)
            tot = 8 + n
            if len(buf) < tot:
                continue
            body, crc = buf[1:tot - 2], buf[tot - 2:tot]
            return cmd, sts, buf[6:6 + n], (crc16(body) == struct.unpack("<H", crc)[0])
        else:
            sts, lo, hi = buf[1], buf[2], buf[3]
            n = lo | (hi << 8)
            tot = 6 + n
            if len(buf) < tot:
                continue
            body, crc = buf[1:tot - 2], buf[tot - 2:tot]
            return None, sts, buf[4:4 + n], (crc16(body) == struct.unpack("<H", crc)[0])
    return None, None, b"", False


def get_shm(sp):
    """★ 从 0x38 的 +23 自取 g_shm —— 绝不写死（重新烧录后它会变）。
    ★★ 必须**两种帧都试**：帧模式是"逐链路"状态，上一次测试可能把板子留在 v2，
       此时用 v1 帧去问会**完全失联**（契约 §9.4 规则 3 的现场：症状像"板子不响应"）。"""
    for v2 in (True, False):
        sp.write(build_v2(CMD_STATUS) if v2 else build_v1(CMD_STATUS)); sp.flush()
        _, sts, pl, ok = read_frame(sp, v2)
        if sts == 0 and len(pl) >= 51:
            return struct.unpack("<I", pl[23:27])[0], v2
    return None, None


def status_tail(sp, v2, seq):
    """读 0x38 取尾部三个计数器（uart_ore/drop/frame_bad @ 39/43/47）。"""
    sp.write(build_v2(CMD_STATUS, seq=seq & 0xFF) if v2 else build_v1(CMD_STATUS))
    sp.flush()
    cmd, sts, pl, ok = read_frame(sp, v2)
    if sts != 0 or len(pl) < 51:
        return None
    return dict(ore=struct.unpack("<I", pl[39:43])[0],
                drop=struct.unpack("<I", pl[43:47])[0],
                bad=struct.unpack("<I", pl[47:51])[0],
                ov=struct.unpack("<I", pl[27:31])[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("DCL_PORT") or "COM21")
    ap.add_argument("--secs", type=float, default=20.0)
    a = ap.parse_args()
    global SHM
    sp = serial.Serial(a.port, 115200, timeout=0.02)
    time.sleep(0.5)

    # 开机默认 v1（且上电/复位都会回 v1）—— 从干净状态开始
    sp.reset_input_buffer()
    r = {}
    bad_crc_sent = 0
    try:
        _s, _v2 = get_shm(sp)
        assert _s, "两种帧都读不到 0x38 ⇒ 链路失联（先喂一个 v1 的 0x05[0] 或复位板子）"
        # ★ 读到后**强制回到 v1**，让相位 A 从干净状态开始（顺带验证"回 v1"这条规则）
        sp.write(build_v2(CMD_FRAME_MODE, bytes([0])) if _v2 else build_v1(CMD_FRAME_MODE, bytes([0])))
        sp.flush(); read_frame(sp, _v2); time.sleep(0.15)
        _s2, _ = get_shm(sp)
        SHM = [_s2]
        print("  g_shm = 0x%08X（自 0x38 读，非写死；已强制回 v1）" % SHM[0])
        # ★ C3 必须**实测增量**：先取一份基线，否则"增量==0"只是推断（累计值可能早就 >0）
        pre = status_tail(sp, False, 0x11)
        if pre:
            print("  基线（本相位流量开始前）: ore=%d drop=%d bad=%d ov=%d"
                  % (pre["ore"], pre["drop"], pre["bad"], pre["ov"]))
        half = a.secs / 2.0
        for phase, v2 in (("A · v1（默认）", False), ("B · v2（协商后）", True)):
            if v2:
                # ★ 规则 1：0x05 的应答**永远用 v1**
                sp.write(build_v1(CMD_FRAME_MODE, bytes([1])))
                sp.flush()
                _, sts, _, _ = read_frame(sp, False)
                print("  协商 0x05[1] ⇒ sts=%s（应答按规则用 v1）" % sts)
                time.sleep(0.1)
            print("\n────── %s · %.0f s ──────" % (phase, half))
            n = ack = nak = to =mis = cbad = 0
            seq = 0
            t_end = time.time() + half
            while time.time() < t_end:
                seq = (seq + 1) & 0xFF
                # 交替：0x01（载荷 4B）与 0x20 读 MAGIC（载荷 4B）——★ 应答同长
                if seq & 1:
                    req = build_v2(CMD_VERSION, seq=seq) if v2 else build_v1(CMD_VERSION)
                    want = "VER"
                else:
                    req = build_v2(CMD_READ, struct.pack("<I", SHM[0]), seq=seq) if v2 \
                        else build_v1(CMD_READ, struct.pack("<I", SHM[0]))
                    want = "MAGIC"
                sp.write(req); sp.flush()
                cmd, sts, pl, ok = read_frame(sp, v2)
                n += 1
                if sts is None:
                    to += 1; continue
                if sts == 0xFF:
                    nak += 1; continue
                if not ok:
                    cbad += 1; continue
                ack += 1
                if v2 and cmd is not None:
                    exp = CMD_VERSION if want == "VER" else CMD_READ
                    if cmd != exp:
                        mis += 1
                        continue
                if want == "MAGIC":
                    if len(pl) != 4 or struct.unpack("<I", pl)[0] != 0x44434C31:
                        mis += 1
                else:
                    if len(pl) != 4 or struct.unpack("<H", pl[:2])[0] != 0x0200:
                        mis += 1
            r[phase] = dict(n=n, ack=ack, nak=nak, to=to, mis=mis, crc=cbad)
            print("  请求 %-6d ACK %-6d NAK %-4d 超时 %-4d **归属/内容错 %-4d CRC错 %d**"
                  % (n, ack, nak, to, mis, cbad))

        # ── C3：整轮业务流量后的计数器增量（**实测**，非推断）──
        mid = status_tail(sp, True, 0x12)      # 此刻板子已在 v2（相位 B 协商过）
        print("\n────── C3：%.0f s 业务流量的计数器增量 ──────" % a.secs)
        if pre and mid:
            d = {k: mid[k] - pre[k] for k in ("ore", "drop", "bad", "ov")}
            print("  uart_ore  %d → %d  (Δ=%d)" % (pre["ore"], mid["ore"], d["ore"]))
            print("  uart_drop %d → %d  (Δ=%d)" % (pre["drop"], mid["drop"], d["drop"]))
            print("  frame_bad %d → %d  (Δ=%d)" % (pre["bad"], mid["bad"], d["bad"]))
            print("  ov        %d → %d  (Δ=%d)" % (pre["ov"], mid["ov"], d["ov"]))
            print("  ⇒ C3 %s（%d 次请求 / 0 增量）"
                  % ("PASS" if all(v == 0 for v in d.values()) else "FAIL",
                     sum(x["n"] for x in r.values())))
        else:
            print("  ✗ 读不到计数器 ⇒ C3 判 SKIP（**不许当 PASS**）")

        # ── 正向对照：注入 CRC 错帧 ⇒ frame_bad 必须涨 ──
        v2 = True
        s0 = mid if mid else status_tail(sp, v2, 0x7E)
        bad = build_v2(CMD_VERSION, seq=0x7F)
        bad = bad[:-1] + bytes([bad[-1] ^ 0xFF])       # 破坏 CRC
        for _ in range(5):
            sp.write(bad); sp.flush(); time.sleep(0.05)
        time.sleep(0.3)
        s1 = status_tail(sp, v2, 0x7D)
        print("\n────── 正向对照：注入 5 个 CRC 错帧 ──────")
        if s0 and s1:
            print("  frame_bad %d → %d  (Δ=%d)  %s" % (s0["bad"], s1["bad"], s1["bad"] - s0["bad"],
                  "✓ 判据能失败" if s1["bad"] > s0["bad"] else "✗ **空判据**"))
            print("  uart_ore %d→%d   uart_drop %d→%d   ov %d→%d"
                  % (s0["ore"], s1["ore"], s0["drop"], s1["drop"], s0["ov"], s1["ov"]))
        if s0:
            print("  ★ 全程累计: ore=%d drop=%d bad=%d ov=%d" % (s0["ore"], s0["drop"], s0["bad"], s0["ov"]))

        # ── 收尾：把帧模式**放回 v1**（契约 §9.4 规则 3）──
        # ★ 上一次就是"把板子留在 v2"导致下一轮开局读不到 0x38（症状像"板子失联"）
        # ★★ 注意：请求可以用 v2（此刻板子在 v2），但 **0x05 的应答恒为 v1 帧**（规则 1）
        sp.write(build_v2(CMD_FRAME_MODE, bytes([0]))); sp.flush()
        _, sts_r, _, _ = read_frame(sp, False)     # ← 必须按 v1 读，否则恒 None
        time.sleep(0.1)
        _sr, _vr = get_shm(sp)
        print("\n  收尾：v2 → v1（应答按规则用 v1，sts=%s），复读 0x38 ⇒ %s（帧模式 v%s）"
              % (sts_r, "OK" if _sr else "**失联**", "1" if _vr is False else "2/?"))
    finally:
        sp.close()

    print("\n══════ 汇总 ══════")
    ok_all = True
    for k, v in r.items():
        bad = v["mis"] + v["crc"] + v["nak"]
        print("  %-18s 请求 %-5d  **错乱 %d**  超时 %d  %s"
              % (k, v["n"], bad, v["to"], "PASS" if bad == 0 and v["to"] == 0 else "FAIL"))
        ok_all &= (bad == 0 and v["to"] == 0)
    print("\n  [%s] C1/C2 内容与归属" % ("PASS" if ok_all else "FAIL"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
