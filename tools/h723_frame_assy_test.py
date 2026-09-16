# -*- coding: utf-8 -*-
"""h723_frame_assy_test.py —— **帧组装超时**验收（2026-09-16）

## 它挡的是什么（已确定性复现的缺陷，不是理论风险）

1. 接收环原来只有 **512 B**（≈44 ms 的数据量 @115200），而主循环里的 `sd_log_poll()` 等会**停顿**
   ⇒ 环溢出、**丢字节**（`uart_drop` 实测累积到 767）⇒ 帧被截断。
2. 而 `fp_feed()` 在 PAYLOAD 状态**只存字节、不做重同步、也没有超时** ⇒ 解析器**停在半帧上**。
   实测（注入半帧后）：**等 2 s / 5 s 都不恢复**，只有把剩余字节喂完才恢复
   —— 而下一次大 deploy **正好把它喂完** ⇒ 表现为"设备偶尔失聪、下一次又好了"。

## 修复（两刀）
- `FRAME_ASSY_TIMEOUT_TICKS`（50 ms）：帧组装中**超过阈值没有新字节** ⇒ 复位解析器、重新等 SYNC。
- `RX_RING_SZ` 512 → **4096**（≈355 ms 容忍度），降低"真的溢出"的概率。

## 判据（每条都能失败）
| # | 判据 | 修复前 |
|---|---|---|
| T0 | 前置：链路活 | — |
| T1 | ★ **注入半帧 ⇒ 超过超时后必须自恢复**（探测 `0x01` 得 ACK）| **✗ 无应答** |
| T2 | ★ 恢复是因为走了**超时路径**（`0x39 op=25` 计数 +1）⇒ 可观测 | 计数不存在 |
| T3 | ★ **正常大帧不被误杀**：连续发 4102 B 合法 deploy ⇒ ACK，且超时计数**不涨** | — |
| T4 | 对照：正常小命令不受影响（`0x01` / `0x38`）| — |
| T5 | ★ **慢但连续**的帧不被误杀：合法帧分两段发、中间停 20 ms（< 50 ms 阈值）⇒ 仍 ACK | — |

★ T5 是**反向判据**：它保证阈值不会小到"任何停顿都杀"。
退出码：0 = 全通过；2 = 有 FAIL；1 = 环境/前置不成立（**"判据没跑到"必须与"通过"分开**）。
"""
import argparse
import struct
import sys
import time

sys.stdout.reconfigure(errors="replace")
sys.path.insert(0, "tools")
from h723_client import Dcl                              # noqa: E402
from h723_frame_attrib_test import crc16, build_q1        # noqa: E402  （v1 组帧，同仓库单一来源）

C_PIN_PATTERN = 0x39
OP_ASSY_TO = 25        # 0x39 op=25 → 帧组装超时次数
ASSY_TIMEOUT_TICKS = 500          # 与 src/transport.h 的 FRAME_ASSY_TIMEOUT_TICKS 一致
TICK_US = 100                     # 拍 = 100 µs


def slow_frame_bytes(cmd: int, payload: bytes) -> bytes:
    """建一个 v1 帧，但**故意声明更大的载荷**（用于注入"半帧"）。"""
    body = bytes([cmd, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    c = crc16(body)
    return bytes([0xC0]) + body + bytes([c & 0xFF, c >> 8])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    a = ap.parse_args()
    fails, npass = [], 0

    def record(name, cond, detail=""):
        nonlocal npass
        if cond:
            npass += 1
            print("  [PASS] %s  %s" % (name, detail))
        else:
            fails.append("%s  %s" % (name, detail))
            print("  [FAIL] %s  %s" % (name, detail))

    d = Dcl(a.port)
    ser = d.ser
    print("端口: %s（帧组装超时阈值 = %d 拍 = %d ms）" %
          (d.port, ASSY_TIMEOUT_TICKS, ASSY_TIMEOUT_TICKS * TICK_US // 1000))

    def assy_to_count():
        sts, p = d.send(C_PIN_PATTERN, struct.pack("<BBI", OP_ASSY_TO, 0, 0), expect_len=4)
        return struct.unpack("<I", p)[0] if sts == "ACK" and len(p) >= 4 else None

    def probe(timeout=1.0):
        ser.reset_input_buffer()
        ser.write(build_q1(0x01))
        t0 = time.time()
        buf = b""
        while time.time() - t0 < timeout:
            b = ser.read(1)
            if b:
                buf += b
                if buf[:1] == b"\xC1" and len(buf) >= 6:
                    return True
        return False

    try:
        # ── T0 ──
        print("\n== T0 前置 ==")
        record("T0.1 链路活", probe(), "")
        if fails:
            print("!! 前置不成立 ⇒ 本测**无效**（不是通过）")
            return 1
        c0 = assy_to_count()
        record("T0.2 0x39 op=25（组装超时计数）可读", c0 is not None, "count=%s" % c0)

        # ── T1 ★ 半帧 ⇒ 超时自恢复 ──
        print("\n== T1 ★ 注入半帧后必须自恢复 ==")
        half = bytes([0xC0, 0x10, 0x00, 0x10]) + b"\xAA" * 200      # 声明 4096 载荷, 只发 200
        ser.reset_input_buffer()
        ser.write(half)
        time.sleep(0.05)                                            # 让它进 PAYLOAD 状态
        record("T1.1 注入之后的**瞬间**应当是失聪的（否则本判据没测到那一步）",
               not probe(timeout=0.4), "（这一条红 ⇒ 说明注入没生效, 不是修复成功）")
        # ★ 等过超时窗口（阈值 50 ms, 给 3 倍余量）
        time.sleep(ASSY_TIMEOUT_TICKS * TICK_US / 1e6 * 3.0)
        record("T1.2 ★★ 超过超时窗口后**必须自恢复**（探测 0x01 得 ACK）", probe(),
               "阈值 %d ms, 等了 %.0f ms" % (ASSY_TIMEOUT_TICKS * TICK_US // 1000,
                                            ASSY_TIMEOUT_TICKS * TICK_US / 1000.0 * 3))
        c1 = assy_to_count()
        record("T1.3 ★ 恢复走的是**超时路径**（计数 +1 ⇒ 可观测, 不是碰巧）",
               (c0 is not None and c1 is not None and c1 > c0), "count %s → %s" % (c0, c1))

        # ── T3 ★ 正常大帧不被误杀 ──
        print("\n== T3 ★ 正常大帧（4102 B 连续发）不被误杀 ==")
        c2 = assy_to_count()
        hdr = struct.pack("<HHH", 128, 128, 0)
        rs = b"".join(struct.pack("<BBBBBBHHHHB", 2, i & 0xFF, 2, i % 128, 0, 0x01,
                                  i & 0xFFFF, 0, 0, 0, 0) + b"\x00" for i in range(128))
        ps = b"".join(struct.pack("<ffff", 1.0, 0.0, 0.0, 0.0) for _ in range(128))
        P = hdr + rs + ps
        d.send(0x13)
        time.sleep(0.15)
        st, pl = d.send(0x10, P)
        record("T3.1 合法大 deploy 仍被接受（阈值没有小到打断正常大帧）",
               st == "ACK", "deploy=%s %s" % (st, pl[:24]))
        c3 = assy_to_count()
        record("T3.2 期间**没有**触发组装超时（计数不涨）",
               (c2 is not None and c3 is not None and c3 == c2), "count %s → %s" % (c2, c3))

        # ── T4 对照：小命令 ──
        print("\n== T4 对照：正常小命令不受影响 ==")
        record("T4.1 0x01 正常", probe(), "")
        st4, p4 = d.send(0x38)
        record("T4.2 0x38 正常", st4 == "ACK" and len(p4) >= 51, "len=%d" % len(p4))

        # ── T5 ★ 反向判据：慢但连续 ⇒ 不杀 ──
        print("\n== T5 ★ 慢但连续的帧不被误杀（阈值不能小到'停顿就杀'）==")
        c4 = assy_to_count()
        fr = build_q1(0x38)                       # 合法帧
        cut = 3                                    # 头部之后断开
        ser.reset_input_buffer()
        ser.write(fr[:cut])
        time.sleep(0.020)                          # 20 ms < 50 ms 阈值
        ser.write(fr[cut:])
        t0 = time.time()
        got = False
        buf = b""
        while time.time() - t0 < 1.0:
            b = ser.read(1)
            if b:
                buf += b
                if buf[:1] == b"\xC1" and len(buf) >= 6:
                    got = True
                    break
        record("T5.1 分两段发（中间停 20 ms）⇒ 仍应答（阈值 > 20 ms）", got, "")
        c5 = assy_to_count()
        record("T5.2 且未触发组装超时", (c4 is not None and c5 is not None and c5 == c4),
               "count %s → %s" % (c4, c5))

        print("\n════════════ 摘要 ════════════")
        print("  PASS %d / FAIL %d" % (npass, len(fails)))
        if fails:
            print("\n★★ 失败项:")
            for f in fails:
                print("   - " + f)
            return 2
        print("[PASS] 帧组装超时生效：半帧会自愈、正常帧与慢帧都不被误杀")
        return 0
    finally:
        d.close()


if __name__ == "__main__":
    sys.exit(main())
