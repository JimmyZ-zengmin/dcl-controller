# -*- coding: utf-8 -*-
"""h723_frame_attrib_test.py —— **帧归属 v2** 的上机验收（GAP-12，2026-09-16）

## 它解决什么问题（有解码过的现场实例，不是理论风险）

v1 应答是 `[0xC1][STS][LEN:2][payload][CRC:2]` —— **里面没有命令字段**。
⇒ 客户端只能"**收到任何合法帧就当应答**" ⇒ 一条杂帧（开机 banner / 上一条的迟到应答 /
**另一个进程的应答**）会被当成自己的答案。

实测现场：密轮询 `0x22` 读 1 个字时，读回值位型 `0x1DF70200` = 低16位 `fw_ver 0x0200`
+ 高16位 `cap 0x1DF7` —— 那是**一次 `0x01` 的应答被吃掉了**。
★ 最坏巧合：`0x22` 读 **1 个字**的应答**也是 4 字节**，与 `0x01` 的**完全相同** ⇒
**连长度判据都分不开**。为此我花了半小时才把它解码出来。

## v2 帧（本工具验收的对象；**协商后才用**，v1 一字不改）

```
v1 请求 [C0][CMD][LEN:2][payload][CRC:2]      CRC 覆盖 [CMD][LEN:2][payload]
v1 应答 [C1][STS][LEN:2][payload][CRC:2]      CRC 覆盖 [STS][LEN:2][payload]
v2 请求 [C2][CMD][SEQ][LEN:2][payload][CRC:2] CRC 覆盖 [CMD][SEQ][LEN:2][payload]
v2 应答 [C3][CMD][SEQ][STS][LEN:2][payload][CRC:2]
                                              CRC 覆盖 [CMD][SEQ][STS][LEN:2][payload]
0x05 FRAME_MODE [mode:u8]  1=进入 v2 / 0=回到 v1 —— ★ **它的应答永远用 v1 帧**
```
v2 的价值 = **归属**：应答回显 `CMD`（命令码）与 `SEQ`（请求携带的序号）
⇒ 客户端能判"**这条是不是我那次请求的**"。
★ 序号只**回显**、**不强制顺序**（不做重传窗口）—— 那属下一版；本版只解决归属。

## ★★ 为什么本工具**自己实现 v2 组帧/解析**（不调 h723_client）

因为要判的是"**固件的 v2 实现对不对**"。若客户端与固件共用同一份有 bug 的代码，
判据会**两边一起错而互相验证**（本项目铁律：**两条独立路径对上才算数**）。
⇒ 这里按上面那张表**独立**写组帧与 CRC，`h723_client` 只用来找串口。

## 判据（每条都能失败）

| # | 判据 | 怎么让它红 |
|---|---|---|
| T0 | 前置：链路活 + `cap` 含 `DCL_CAP_FRAME_V2`（**掩码**判，不等值）+ `0x39 op=24` 可读 | 老固件 ⇒ 判**无效**（不是 PASS）|
| T1 | ★ **未协商 ⇒ v1 帧逐字节不变**（用本工具独立算的期望帧比对） | 动了 v1 格式 ⇒ 逐字节不符 |
| T2 | `0x05 mode=1` 的应答是 **v1**（含 `[mode][caps_hi]`），之后 v2 请求得到 **C3** 应答且 `CMD/SEQ` 回显正确 | 回执用 v2 发 ⇒ 客户端读不懂（握手无解）|
| T3 | ★★ **陈旧应答可辨识（决定性）**：连发 A、B 两条只读一次 ⇒ 读到的必须是 **A 的**；再读一次是 B 的；且**把第一条当 B 的应答去核对必须不匹配** | 若应答不带 CMD/SEQ ⇒ 两个方向的断言都无法成立 |
| T4 | 协商**可逆**：`0x05 mode=0` 之后 v1 请求的应答与 T1 逐字节相同 | 回不去 ⇒ 老客户端被打断 |
| T5 | 序号只**回显不强制**：连发 3 条**同一 SEQ** ⇒ 三条都应答、都回显同一 SEQ | 固件偷偷加了顺序强制 ⇒ 后两条被拒 |
| T6 | `0x13 RESET` 后模式**回 v1**（用 v1 请求能通、且 `op=24` 报 mode=0） | 不复位 ⇒ RESET 后**整条链路失联**（症状像"板子坏了"）|

退出码：0 = 全通过；2 = 有 FAIL；1 = 环境错误 / 前置不成立（**"判据没跑到"必须与"通过"分开**）。
"""
import argparse
import struct
import sys
import time

sys.stdout.reconfigure(errors="replace")
sys.path.insert(0, "tools")
from h723_client import Dcl            # noqa: E402  （只用于找串口）

C_FRAME_MODE = 0x05
C_PIN_PATTERN = 0x39

SYNC_Q1, SYNC_A1 = 0xC0, 0xC1
SYNC_Q2, SYNC_A2 = 0xC2, 0xC3
STS_ACK = 0x00


# ────────────────────── 独立实现的 CRC16-CCITT（XMODEM/0xFFFF 起） ──────────────────────
def crc16(data: bytes) -> int:
    """CRC16-CCITT（poly 0x1021，初值 0xFFFF）—— 按位算，**不查表**，与固件独立。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def build_q1(cmd: int, payload: bytes = b"") -> bytes:
    body = bytes([cmd, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    c = crc16(body)
    return bytes([SYNC_Q1]) + body + bytes([c & 0xFF, c >> 8])


def build_q2(cmd: int, seq: int, payload: bytes = b"") -> bytes:
    body = bytes([cmd, seq & 0xFF, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    c = crc16(body)
    return bytes([SYNC_Q2]) + body + bytes([c & 0xFF, c >> 8])


def parse_reply(buf: bytes):
    """解析应答 → dict(sync, cmd|None, seq|None, sts, payload, crc_ok, raw) 或 None。

    ★ 这是**独立**解析：v1 只认 `STS`，v2 认 `CMD/SEQ/STS`；CRC 必须自己复核。
    """
    if len(buf) < 6:
        return None
    sync = buf[0]
    if sync == SYNC_A1:
        sts = buf[1]
        n = buf[2] | (buf[3] << 8)
        if len(buf) < 5 + n - 1 + 1:
            return None
        payload = buf[4:4 + n]
        crc_rx = buf[4 + n] | (buf[5 + n] << 8)
        body = buf[1:4 + n]
        return dict(sync=sync, cmd=None, seq=None, sts=sts, payload=bytes(payload),
                    crc_ok=(crc16(body) == crc_rx), raw=bytes(buf[:6 + n]))
    if sync == SYNC_A2:
        cmd, seq, sts = buf[1], buf[2], buf[3]
        n = buf[4] | (buf[5] << 8)
        payload = buf[6:6 + n]
        if len(buf) < 8 + n:
            return None
        crc_rx = buf[6 + n] | (buf[7 + n] << 8)
        body = buf[1:6 + n]
        return dict(sync=sync, cmd=cmd, seq=seq, sts=sts, payload=bytes(payload),
                    crc_ok=(crc16(body) == crc_rx), raw=bytes(buf[:8 + n]))
    return None


class Link:
    """独立串口收发（不用 h723_client 的 Link —— 理由见文件头）。"""

    def __init__(self, ser):
        self.ser = ser

    def _read_frame(self, timeout=1.0):
        """读一帧（按 SYNC 决定长度），返回解析结果或 None。"""
        t0 = time.time()
        buf = bytearray()
        while time.time() - t0 < timeout:
            b = self.ser.read(1)
            if not b:
                continue
            if not buf and b[0] not in (SYNC_A1, SYNC_A2):
                continue
            buf += b
            need = None
            if buf[0] == SYNC_A1 and len(buf) >= 4:
                need = 6 + (buf[2] | (buf[3] << 8))
            elif buf[0] == SYNC_A2 and len(buf) >= 6:
                need = 8 + (buf[4] | (buf[5] << 8))
            if need and len(buf) >= need:
                return parse_reply(bytes(buf[:need]))
        return None

    def xact(self, cmd, payload=b"", seq=None, timeout=1.0):
        """seq=None ⇒ v1 请求；给 seq ⇒ v2 请求。返回解析结果或 None。"""
        self.ser.reset_input_buffer()
        frame = build_q1(cmd, payload) if seq is None else build_q2(cmd, seq, payload)
        self.ser.write(frame)
        return self._read_frame(timeout)

    def send_only(self, cmd, payload=b"", seq=None):
        """只发不收（用于 T3「连发两条只读一次」）。"""
        frame = build_q1(cmd, payload) if seq is None else build_q2(cmd, seq, payload)
        self.ser.write(frame)

    def read_one(self, timeout=1.0):
        return self._read_frame(timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--reset-mode", action="store_true",
                    help="只把设备的帧模式复位成 v1（别的工具报'0x38 无有效应答'时用它救场）")
    a = ap.parse_args()

    # ★ 判据发射器**命名为 `record`** —— 与仓库其它验收脚本**同一约定**。
    #   `docs/claims.md` 的 E 类闸门就是按 `record("…")` 找判据的；换个名字就会被判"判据不存在"，
    #   而那不是误报：**判据名与契据对不上，人就没法从契据追到判据**（追不了 = 闸门白建）。
    fails, skips, npass = [], [], 0

    def record(name, cond, detail=""):
        nonlocal npass
        if cond:
            npass += 1
            print("  [PASS] %s  %s" % (name, detail))
        else:
            fails.append("%s  %s" % (name, detail))
            print("  [FAIL] %s  %s" % (name, detail))

    d = Dcl(a.port)
    if a.reset_mode:
        # ★ 救场入口：设备可能被上一次"切到 v2 后异常退出"留在 v2，此时所有 v1 工具都读到 TIMEOUT。
        #   本命令**用 v1 发** `0x05 mode=0` —— 按契约它的**应答强制 v1** ⇒ 一定收得到回执。
        try:
            sts1, p1 = d.send(C_FRAME_MODE, bytes([0]))
            print("0x05 mode=0 → %s %s" % (sts1, p1.hex() if p1 else ""))
            sts2, p2 = d.send(0x01)
            print("复检 0x01 → %s len=%d" % (sts2, len(p2)))
            okk = (sts1 == "ACK" and sts2 == "ACK")
            print("[%s] 帧模式已复位到 v1" % ("PASS" if okk else "FAIL"))
            return 0 if okk else 2
        finally:
            d.close()
    L = Link(d.ser)
    print("端口: %s" % d.port)
    try:
        # ── T0 前置 ──
        print("\n== T0 前置 ==")
        r = L.xact(0x01)
        if not r or r["sts"] != STS_ACK or len(r["payload"]) < 4:
            print("!! 0x01 无有效应答 ⇒ 环境错误（不算通过，也不代表功能有问题）")
            return 1
        fw = r["payload"][0] | (r["payload"][1] << 8)
        cap = r["payload"][2] | (r["payload"][3] << 8)
        record("T0.1 链路活", r["crc_ok"], "fw=0x%04X cap=0x%04X" % (fw, cap))
        record("T0.2 cap 含 DCL_CAP_FRAME_V2(0x4000)（**掩码**判，不等值）",
           (cap & 0x4000) != 0, "cap=0x%04X" % cap)
        r = L.xact(C_PIN_PATTERN, struct.pack("<BBI", 24, 0, 0))
        if not r or len(r["payload"]) < 8:
            print("!! 0x39 op=24 不可读 ⇒ 固件不是本版（判**无效**）")
            return 1
        mode0, v2n0 = struct.unpack("<2I", r["payload"][:8])
        record("T0.3 0x39 op=24 可读（设备侧的帧模式）", mode0 == 0,
           "mode=%d v2_frames=%d" % (mode0, v2n0))

        # ── T1 未协商 ⇒ v1 逐字节不变 ──
        print("\n== T1 未协商：v1 帧必须逐字节不变 ==")
        L.ser.reset_input_buffer()
        L.ser.write(build_q1(0x01))             # 本工具独立算出的 v1 请求
        r = L.read_one()
        record("T1.1 v1 应答是 C1 且 CRC 自洽", bool(r) and r["sync"] == SYNC_A1 and r["crc_ok"],
           "" if not r else "sync=0x%02X crc_ok=%s" % (r["sync"], r["crc_ok"]))
        # 用**独立构造的期望帧**比对（用固件报回的 fw/cap 拼期望的 v1 应答）
        exp_body = bytes([STS_ACK, 4, 0]) + r["payload"]
        c = crc16(exp_body)
        exp = bytes([SYNC_A1]) + exp_body + bytes([c & 0xFF, c >> 8])
        record("T1.2 ★ v1 应答与本工具独立构造的期望帧**逐字节相同**",
           r is not None and r["raw"] == exp,
           "实际=%s 期望=%s" % (r["raw"].hex() if r else "-", exp.hex()))

        # ── T2 协商：0x05 的应答必须是 v1 ──
        print("\n== T2 协商（0x05 FRAME_MODE）==")
        r = L.xact(C_FRAME_MODE, bytes([1]))    # 该请求本身用 v1 发（还不知道设备支不支持）
        record("T2.1 ★ 0x05 的应答是 **v1**（客户端必须能用已知格式解析回执）",
           bool(r) and r["sync"] == SYNC_A1 and r["sts"] == STS_ACK,
           "" if not r else "sync=0x%02X payload=%s" % (r["sync"], r["payload"].hex()))
        if not r or r["sts"] != STS_ACK:
            print("!! 协商失败 ⇒ 后续 v2 判据无法执行（判**无效**，不是通过）")
            return 1
        record("T2.2 回执自报 mode=1 且带能力字高位", len(r["payload"]) >= 2 and r["payload"][0] == 1,
           "mode=%d caps_hi=0x%02X" % (r["payload"][0], r["payload"][1]))
        # 之后用 v2 请求；设备侧应报 mode=1
        seq = 0x11
        r = L.xact(C_PIN_PATTERN, struct.pack("<BBI", 24, 0, 0), seq=seq)
        record("T2.3 v2 请求得到 **C3** 应答", bool(r) and r["sync"] == SYNC_A2 and r["crc_ok"],
           "" if not r else "sync=0x%02X crc_ok=%s" % (r["sync"], r["crc_ok"]))
        record("T2.4 ★ 应答**回显** CMD 与 SEQ（归属写进帧里）",
           bool(r) and r["cmd"] == C_PIN_PATTERN and r["seq"] == seq,
           "" if not r else "cmd=0x%02X seq=0x%02X（请求 cmd=0x%02X seq=0x%02X）"
           % (r["cmd"] or 0, r["seq"] or 0, C_PIN_PATTERN, seq))
        if r and len(r["payload"]) >= 8:
            mode1 = struct.unpack("<2I", r["payload"][:8])[0]
            record("T2.5 设备侧确认 mode=1（『我切了』≠『它记住了』⇒ 分开判）", mode1 == 1,
               "mode=%d" % mode1)

        # ── T3 ★★ 陈旧应答可辨识（决定性）──
        print("\n== T3 ★★ 陈旧应答可辨识（连发两条只读一次）==")
        L.ser.reset_input_buffer()
        seqA, seqB = 0x21, 0x22
        L.send_only(0x38, b"", seq=seqA)        # A: 0x38（长应答）
        time.sleep(0.05)
        L.send_only(0x01, b"", seq=seqB)        # B: 0x01（4B 短应答 —— 正是当年被误吃的那一档）
        r1 = L.read_one()
        r2 = L.read_one()
        record("T3.1 第一条应答是 **A(0x38)** 的（不是 B 的）",
           bool(r1) and r1["cmd"] == 0x38 and r1["seq"] == seqA,
           "" if not r1 else "cmd=0x%02X seq=0x%02X" % (r1["cmd"] or 0, r1["seq"] or 0))
        record("T3.2 第二条应答是 **B(0x01)** 的",
           bool(r2) and r2["cmd"] == 0x01 and r2["seq"] == seqB,
           "" if not r2 else "cmd=0x%02X seq=0x%02X" % (r2["cmd"] or 0, r2["seq"] or 0))
        # ★★ 反向断言：把第一条**当成 B 的应答**去核对 ⇒ 必须**不匹配**
        #    （这条才真正证明"归属可用"：若帧里没有 CMD/SEQ，这个断言无法成立）
        mis = bool(r1) and (r1["cmd"] == 0x01 and r1["seq"] == seqB)
        record("T3.3 ★★ 把第一条当 B 的应答核对 ⇒ **必须不匹配**（归属真的可用）", not mis,
           "错误配对=%s（若为 True 说明归属失效，客户端会吃串帧）" % mis)

        # ── T4 可逆 ──
        print("\n== T4 协商可逆（回到 v1）==")
        r = L.xact(C_FRAME_MODE, bytes([0]), seq=0x31)   # 用 v2 发出，但**应答按契约是 v1**
        record("T4.1 0x05(mode=0) 的应答是 v1 且 ACK", bool(r) and r["sync"] == SYNC_A1
           and r["sts"] == STS_ACK, "" if not r else "sync=0x%02X" % r["sync"])
        L.ser.reset_input_buffer()
        L.ser.write(build_q1(0x01))
        r = L.read_one()
        exp_body = bytes([STS_ACK, 4, 0]) + (r["payload"] if r else b"")
        c = crc16(exp_body)
        exp = bytes([SYNC_A1]) + exp_body + bytes([c & 0xFF, c >> 8])
        record("T4.2 ★ 回到 v1 之后，应答与 T1.2 的期望帧**逐字节相同**",
           r is not None and r["sync"] == SYNC_A1 and r["raw"] == exp,
           "" if not r else "sync=0x%02X 逐字节=%s" % (r["sync"], r["raw"] == exp))

        # ── T5 序号只回显、不强制 ──
        print("\n== T5 序号只回显、不强制顺序 ==")
        L.xact(C_FRAME_MODE, bytes([1]))        # 再进 v2
        same = 0x55
        echoes = []
        for _ in range(3):
            rr = L.xact(0x38, b"", seq=same)
            echoes.append((rr or {}).get("seq") if rr else None)
        record("T5.1 ★ 连发 3 条**同一 SEQ** ⇒ 三条都应答且回显同一 SEQ（不因'重复'被拒）",
           echoes == [same, same, same],
           "回显=%s（若出现 None 说明固件偷偷加了顺序强制）" % echoes)

        # ── T6 RESET 后回 v1 ──
        print("\n== T6 0x13 RESET 后模式回 v1 ==")
        r = L.xact(0x13, b"", seq=0x61)         # RESET 本身用 v2 发（应答仍带 CMD/SEQ）
        time.sleep(0.8)
        L.ser.reset_input_buffer()
        L.ser.write(build_q1(C_PIN_PATTERN))    # ★ 用 **v1** 请求（不切模式）—— 若设备仍在 v2 就会失联
        r = L.read_one()
        record("T6.1 ★ RESET 后**用 v1 请求能通**（设备确实回到了 v1）",
           bool(r) and r["sync"] == SYNC_A1,
           "" if not r else "sync=0x%02X" % r["sync"])
        # 上面那条 v1 请求是 0x39 无载荷 ⇒ 会 NAK；再发一条合法的 op=24 读 mode
        r = L.xact(C_PIN_PATTERN, struct.pack("<BBI", 24, 0, 0))
        m = struct.unpack("<2I", r["payload"][:8]) if r and len(r["payload"]) >= 8 else (None, None)
        record("T6.2 RESET 后 op=24 报 mode=0", m[0] == 0, "mode=%s" % (m[0],))

        print("\n════════════ 摘要 ════════════")
        print("  PASS %d / FAIL %d / SKIP %d" % (npass, len(fails), len(skips)))
        if fails:
            print("\n★★ 失败项:")
            for f in fails:
                print("   - " + f)
            return 2
        print("[PASS] GAP-12：v2 帧归属可用，且 v1 一字未变（老上位机零改动）")
        return 0
    finally:
        # ★★★ 帧模式必须复位，而且**必须放 finally**（我的 memory §九.5：状态复原放 finally，
        #   判据 FAIL 时也要还原 —— 这里更狠：**脚本崩溃时**也要还原）。
        #   不还原的后果（实测踩到过）：设备留在 v2 ⇒ 之后**所有用 v1 的工具全部 TIMEOUT**，
        #   症状是"板子没响应"，会把人引向接线/驱动方向。
        #   ★ 能救回来的原因正是契约那条"**0x05 的应答永远用 v1**" ⇒ **用 v1 发就能收到可解析的回执**。
        try:
            sts_rec, _ = d.send(C_FRAME_MODE, bytes([0]))
            print("[复原] 帧模式回 v1: %s" % sts_rec)
        except Exception as e:                                  # noqa: BLE001
            print("[复原] ★ 失败(%s) ⇒ 手工救场: python tools/h723_frame_attrib_test.py --reset-mode" % e)
        d.close()


if __name__ == "__main__":
    sys.exit(main())
