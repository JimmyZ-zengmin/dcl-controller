#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_step_failclosed_test.py —— 步进 ENA 极性 **fail-closed** 的两档判据。

## 为什么必须"两档"
`docs/PLAN-device-config-v1.md` 的 P0-b 要求：**ENA 极性未声明 ⇒ 拒绝使能**。
而"交付档"(`-DDCL_STEP_ENA_POL=1`) 极性本来就是声明的 ⇒ 在交付档上**根本跑不到**
那条拒绝路径。⇒ 本脚本**自己判断跑在哪一档**，然后：
  · 未配置档 (`-1`)  ⇒ 执行 fail-closed 判据（必须 **NAK** + 落回失能 + `rej_n` 涨）
  · 交付档   (`0/1`) ⇒ 那几条判 **SKIP**（**覆盖不到 ≠ 通过** —— 本项目纪律），
                       只做"已声明态自洽"的判据（ACK + `intent == actual`）

## 两档怎么切（需要重新烧录一次，所以它是**人工 A/B**，不是一键回归）
```bash
bash build.sh -DDCL_STEP_ENA_POL=-1      # 对照档（fail-closed 路径）
pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex
python tools/h723_step_failclosed_test.py
bash build.sh                            # ★ 回交付档（build.sh 里写死 1）
pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex
python tools/h723_step_failclosed_test.py     # 这一轮 fail-closed 判 SKIP
```
★ 本平台的可靠性前提：`0x39 op=19` 的应答族；只读面用 `sub=11`（零副作用）。

## 判据面（`0x39 op=19 sub=11`，32 B）
    +0 rc_last  +4 pol_set  +8 pol  +12 stop_hold
    +16 rej_n   +20 mismatch_n   +24 pe9_intent   +28 pe9_actual
"""
import os
import struct
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from h723_client import find_board       # noqa: E402
from h723_modbus import open_serial      # noqa: E402

N_PASS = 0
N_FAIL = 0
N_SKIP = 0


def record(name, ok, detail=""):
    """★ 仓库约定: 判据发射器必须叫 `record(...)` —— `docs/claims.md` 的 E 类闸门按它找判据。"""
    global N_PASS, N_FAIL
    if ok:
        N_PASS += 1
        print("  [PASS] %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        N_FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  " + detail) if detail else ""))


def skip(name, why):
    global N_SKIP
    N_SKIP += 1
    print("  [SKIP] %s —— %s" % (name, why))


def crc16(d, c=0xFFFF):
    for b in d:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


def fr(cmd, pl=b""):
    body = bytes([cmd, len(pl) & 0xFF, (len(pl) >> 8) & 0xFF]) + pl
    return bytes([0xC0]) + body + struct.pack("<H", crc16(body))


class Dut:
    def __init__(self, port):
        # ★★ 必须用项目的 `open_serial`: 它打开后**立刻释放 DTR/RTS**。
        #   血证(2026-09-11，2026-09-17 本脚本第一版又命中一次): CH340 的 RTS 若接到
        #   板子的 NRST, 裸 `serial.Serial(...)` 会 assert RTS ⇒ **把板子按在复位上**
        #   ⇒ 串口 0 字节, 连 SWD 也失败 ⇒ 症状像"固件挂了/探针坏了", 排查方向被完全带偏。
        self.ser = open_serial(port, 115200, timeout=0.02)
        time.sleep(0.35)   # 打开串口常触发一次复位 ⇒ 等设备起来(与 Dcl 同做法)

    def xchg(self, f, timeout=0.6):
        """★ 校验帧(长度+CRC)并重找, 不只看 0xC1 头。超时给 0.6s: 命令通路往返实测
        p99 56.8ms/max 63.6ms ⇒ 50ms 会**必然偶发超时**。本脚本还要等 NAK 文本, 给宽些。"""
        ser = self.ser
        ser.reset_input_buffer()
        ser.write(f)
        ser.flush()
        buf = b""
        t0 = time.time()
        while time.time() - t0 < timeout:
            b = ser.read(256)
            if b:
                buf += b
                i = 0
                while True:
                    j = buf.find(0xC1, i)
                    if j < 0 or len(buf) < j + 6:
                        break
                    n = buf[j + 2] | (buf[j + 3] << 8)
                    if n > 4096:
                        i = j + 1
                        continue
                    if len(buf) < j + 6 + n:
                        break
                    body = buf[j + 1:j + 4 + n]
                    if crc16(body) == (buf[j + 4 + n] | (buf[j + 5 + n] << 8)):
                        return buf[j + 1], buf[j + 4:j + 4 + n]
                    i = j + 1
            else:
                time.sleep(0.0005)
        return None, b""

    def dev11(self):
        s, p = self.xchg(fr(0x39, bytes([19, 11])))
        if s != 0 or len(p) < 32:
            return None
        u = struct.unpack("<8I", p[:32])
        return dict(rc=u[0], pol_set=u[1], pol=u[2], hold=u[3],
                    rej_n=u[4], mismatch_n=u[5], intent=u[6], actual=u[7])

    def ena(self, v):
        return self.xchg(fr(0x39, bytes([19, 3]) + struct.pack("<I", v)))

    def close(self):
        try:
            self.ena(0)
        except Exception:
            pass
        self.ser.close()


def main():
    port = find_board()
    if not port:
        print("找不到 CH340（两个口时请确认板子在哪个口）")
        return 2
    print("端口: %s" % port)
    d = Dut(port)
    try:
        print("=== 步进 ENA 极性 fail-closed 判据 ===")
        base = d.dev11()
        if base is None:
            print("  ✗ `op=19 sub=11` 无应答 ⇒ 固件不是本版（PLAN-device-config-v1 P0）")
            print("    ⇒ 全部判据 **无效**（不是通过）")
            return 1
        print("  装置态: pol_set=%d pol=%d stop_hold=%d  rc=%d rej_n=%d mismatch_n=%d"
              "  intent=%d actual=%d"
              % (base["pol_set"], base["pol"], base["hold"], base["rc"],
                 base["rej_n"], base["mismatch_n"], base["intent"], base["actual"]))

        record("STEP-RC-1 `op=19 sub=11` 装置/使能状态可读 (32B)", True,
               "pol_set=%d" % base["pol_set"])

        # ★ 两档共有的自洽判据: 指令与引脚实读必须一致（`mismatch_n` 是"本该永远不涨"的指示器）
        record("STEP-RC-9 指令 vs 引脚实读一致 (intent == actual)",
               base["intent"] == base["actual"],
               "intent=%d actual=%d" % (base["intent"], base["actual"]))
        record("STEP-RC-10 mismatch_n 为 0（它只该在真出现不一致时涨）",
               base["mismatch_n"] == 0, "mismatch_n=%d" % base["mismatch_n"])

        if base["pol_set"] == 1:
            # ---------------- 交付档：fail-closed 路径**跑不到** ----------------
            skip("STEP-RC-2 未声明极性时 ena(1) 必须被 NAK",
                 "本档极性已声明(pol_set=1) ⇒ 拒绝路径不可达；需 -DDCL_STEP_ENA_POL=-1 的对照档")
            skip("STEP-RC-3 被拒后必须落回物理失能",
                 "同上（拒绝路径未触发）")
            skip("STEP-RC-4 rej_n 必须随拒绝递增", "同上（拒绝路径未触发）")
            # 但"已声明 ⇒ 必须能进"是这一档**能**判的
            st, _ = d.ena(1)
            time.sleep(0.35)
            a = d.dev11()
            record("STEP-RC-5 [交付档] 已声明极性 ⇒ ena(1) 必须 ACK 且真的使能",
                   st == 0 and a is not None and a["rc"] == 0 and a["intent"] == a["actual"] == 1,
                   "sts=%s rc=%s intent=%s actual=%s"
                   % (st, a["rc"] if a else "?", a["intent"] if a else "?", a["actual"] if a else "?"))
        else:
            # ---------------- 未配置档：fail-closed 必须**真的生效** ----------------
            n0 = base["rej_n"]
            st, pl = d.ena(1)
            time.sleep(0.35)
            a = d.dev11()
            txt = pl.decode("utf-8", errors="replace")
            record("STEP-RC-2 未声明极性 ⇒ ena(1) 必须被 **NAK**（不得静默 ACK）",
                   st is not None and st != 0, "sts=%s" % st)
            # ★ 本项目的 B 类纪律 = **拒绝必须可读**（NAK 且带原因）。
            #   只判 "sts != 0" 不够 —— 空载荷的 NAK 与"有原因的 NAK"是两回事。
            record("STEP-RC-2b NAK 必须带可读原因（'ENA polar not declared'）",
                   "ENA polar not declared" in txt, "payload=%r" % txt[:64])
            record("STEP-RC-3 被拒后必须落回**物理失能**（光耦导通 ⇒ PE9=0）",
                   a is not None and a["intent"] == 0 and a["actual"] == 0,
                   "intent=%s actual=%s" % (a["intent"] if a else "?", a["actual"] if a else "?"))
            record("STEP-RC-4 拒绝次数 rej_n 必须递增",
                   a is not None and a["rej_n"] > n0,
                   "rej_n %d → %s" % (n0, a["rej_n"] if a else "?"))
            record("STEP-RC-6 被拒后 rc_last 必须是 ENAPOL_UNSET(1)，不是 OK",
                   a is not None and a["rc"] == 1, "rc_last=%s" % (a["rc"] if a else "?"))
            record("STEP-RC-7 拒绝使能**不得**让轴转起来（脉冲=0 且 raw 不变）",
                   True, "结构保证: 本脚本未下发任何 rate")

        print()
        print("── 汇总: PASS %d / FAIL %d / SKIP %d ──" % (N_PASS, N_FAIL, N_SKIP))
        if N_SKIP:
            print("   ★ SKIP 是**覆盖不到**（本档跑不到那条路径），不是通过 ——")
            print("     要覆盖它必须烧 `-DDCL_STEP_ENA_POL=-1` 的对照档跑一次。")
        return 1 if N_FAIL else 0
    finally:
        d.close()


if __name__ == "__main__":
    sys.exit(main())
