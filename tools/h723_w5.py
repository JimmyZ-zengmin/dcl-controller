#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_w5.py — W5 外设域验收: AI/ADC(16bit) · DI · HIL  (0x36 零接线自检为主)

★ 判据设计 (每条都能失败):
  · 主力判据 = **0x36 零接线自检**: 固件用 GPIO 内部上拉/下拉把引脚拉到已知电平,
    再读 ADC/IDR。这样"读到一个数字"升级为"读数**跟随**引脚电压变化" ——
    后者才证明通路真的接上了。若上拉与下拉读数相同 ⇒ 该脚没被采到 (配置错), 判 FAIL。
  · ADC 引脚给两种模式各测一次 (method 0 = 引脚 analog, method 1 = 引脚 input):
    哪个模式能让内部上下拉生效, 是**硬件事实**, 不应靠猜 —— 测出来并记录。
  · SENSOR 值 (AI 电压 / DI 电平) 用 0x22 burst **独立读回**核对。
  · HIL (PA6→PA5 回环) 需外部跳线, 默认 **SKIP** (与 PASS/FAIL 分开计数, 本项目纪律)。

用法:
    python tools/h723_w5.py                 # AI/DI 零接线自检 + SENSOR 观测
    python tools/h723_w5.py --hil           # 追加 HIL 回环 (需 PA6-PA5 跳线)
"""
import argparse
import struct
import sys
import time

try:
    import serial
except ImportError:
    print("!! 需要 pyserial: pip install pyserial")
    sys.exit(2)

_HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
sys.path.insert(0, _HERE)
from h723_modbus import Link, find_port

CMD_GET_VERSION = 0x01
CMD_READ_BURST  = 0x22
CMD_WRITE       = 0x21
CMD_ENGINE_STATUS = 0x38
CMD_RESET       = 0x13
CMD_PIN_SELFTEST = 0x36

OFF_SENSOR_MAP = 0x0040
OFF_WIRE_MAP   = 0x0240
AI_SENSOR_BASE = 8
DI_SENSOR_BASE = 3
HIL_FB_SENSOR  = 2
HIL_U_WIRE     = 20

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  [%s] %-56s %s" % ("PASS" if ok else "FAIL", name, detail))


def skip(name, detail=""):
    RESULTS.append((name, None, detail))
    print("  [SKIP] %-56s %s" % (name, detail))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--hil", action="store_true", help="追加 HIL 回环 (需 PA6→PA5 跳线)")
    ap.add_argument("--ext", action="store_true", help="外部 AI 验证 (需 PA0→3.3V, PA1→GND)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    port = find_port(a.port)
    if not port:
        print("!! 找不到串口"); return 2
    print("端口: %s @ 115200\n" % port)

    with serial.Serial(port, 115200, timeout=0.05) as ser:
        L = Link(ser, a.verbose)
        time.sleep(0.3)

        def rd_burst(addr, nwords, timeout=0.6):
            sts, p = L.xact(CMD_READ_BURST, struct.pack("<IH", addr, nwords), timeout=timeout)
            return p if sts == 0 else None

        def rd_f32(addr):
            p = rd_burst(addr, 1)
            return struct.unpack("<f", p)[0] if p and len(p) >= 4 else None

        print("── T0 前置 ──")
        sts, p = L.xact(CMD_GET_VERSION)
        record("T0 链路活性 (GET_VERSION→ACK)", sts == 0 and len(p) >= 4, "sts=%s" % sts)
        if sts != 0:
            return 2
        cap = struct.unpack("<H", p[2:4])[0]
        record("T0b cap 含 AI(0x0400) 与 MACRO(0x0800) (宣称=实现)",
               (cap & 0x0400) and (cap & 0x0800), "cap=0x%04X" % cap)
        s_bad, _ = L.xact(0x7F)
        record("T0c 工具自检: 未实现命令(0x7F)→NAK", s_bad == 0xFF, "sts=%s" % s_bad)
        if s_bad != 0xFF:
            return 2

        sts, st = L.xact(CMD_ENGINE_STATUS)
        if sts != 0 or len(st) < 27:
            print("  !! 读不到 SHM 基址"); return 2
        shm = struct.unpack("<I", st[23:27])[0]
        record("T0d 读 SHM 基址", 0x20000000 <= shm < 0x20040000, "shm=0x%08X" % shm)

        L.xact(CMD_RESET); time.sleep(0.2)

        # ══════════ 0x36 零接线自检 ══════════
        #  · DI : 内部上/下拉能直接改 IDR ⇒ 零接线即可自证 ✅
        #  · ADC: **H723 上 analog 模式下 GPIO 上下拉对 ADC 无效**, 而 input 模式下
        #    ADC 又接不到引脚 (实测: 两种模式读数都≈常量, 不随上/下拉变) ⇒ ADC 通路
        #    **无法零接线自证**, 必须外部给 3.3V/GND (见 --ext)。此处只作诊断输出。
        #    (这条硬件事实是实测出来的, 写在此处避免以后有人再试一遍内部上下拉。)
        print("\n── 0x36 零接线自检 (内部上/下拉驱动引脚) ──")
        di_ok_any = False
        for method in (0, 1):
            sts, p = L.xact(CMD_PIN_SELFTEST, bytes([method]))
            if sts != 0 or p is None or len(p) < 20:
                print("   method=%d  (无响应 sts=%s)" % (method, sts)); continue
            ai = [struct.unpack_from("<H", p, i * 2)[0] for i in range(6)]   # ch×[pu,pd]
            di = list(p[12:20])
            print("   method=%d  AI pu/pd = %s | DI pu/pd = %s"
                  % (method, [(ai[i*2], ai[i*2+1]) for i in range(3)],
                     [(di[i*2], di[i*2+1]) for i in range(4)]))
            if all(di[i*2] == 1 and di[i*2+1] == 0 for i in range(4)):
                di_ok_any = True

        record("S1 ★DI 自检: 4 路读数随内部上/下拉变化 (输入通路已接上)",
               di_ok_any, "上拉=1 / 下拉=0, 见上方 dump")
        skip("S2 ADC 零接线自检",
             "H723 analog 模式上下拉对 ADC 无效 → 需外部 3.3V/GND (--ext)")

        # ══════════ SENSOR 观测 (独立读回) ══════════
        print("\n── SENSOR_MAP 观测 (0x22 独立读回) ──")
        time.sleep(0.2)   # 等一轮 ai_tick/di_tick (10ms)
        p = rd_burst(shm + OFF_SENSOR_MAP + AI_SENSOR_BASE * 4, 3)
        if p and len(p) >= 12:
            vol = list(struct.unpack("<3f", p[:12]))
            record("A1 AI → SENSOR[8..10] 为电压且落在 [0, 3.4]V",
                   all(-0.001 <= v <= 3.4 for v in vol),
                   "V = [%.3f, %.3f, %.3f]" % tuple(vol))
        else:
            record("A1 AI → SENSOR[8..10]", False, "burst 读失败")

        p = rd_burst(shm + OFF_SENSOR_MAP + DI_SENSOR_BASE * 4, 4)
        if p and len(p) >= 16:
            d = list(struct.unpack("<4f", p[:16]))
            # 上电默认 1.0 (内上拉, 悬空=1); 也接受 0.0 (若引脚被拉低)
            record("D1 DI → SENSOR[3..6] 为 {0.0, 1.0} 之一",
                   all(x == 0.0 or x == 1.0 for x in d),
                   "[%s]" % ", ".join("%.0f" % x for x in d))
        else:
            record("D1 DI → SENSOR[3..6]", False, "burst 读失败")

        # ══════════ 外部 AI 验证 (需 PA0→3.3V, PA1→GND) ══════════
        print("\n── 外部 AI 验证 (PA0→3.3V / PA1→GND) ──")
        if not a.ext:
            skip("E1 AI: PA0=3.3V→SENSOR[8]≈3.3V 且 PA1=GND→SENSOR[9]≈0V", "未加 --ext")
            skip("E2 AI: SENSOR[8]-SENSOR[9] > 3.0V (16bit 满量程可用)", "未加 --ext")
        else:
            time.sleep(0.2)
            p = rd_burst(shm + OFF_SENSOR_MAP + AI_SENSOR_BASE * 4, 3)
            vol = list(struct.unpack("<3f", p[:12])) if p and len(p) >= 12 else None
            record("E1 AI: PA0=3.3V → SENSOR[8]≈3.3V 且 PA1=GND → SENSOR[9]≤0.2V",
                   vol is not None and 3.0 <= vol[0] <= 3.4 and vol[1] <= 0.2,
                   "V = %s" % (["%.3f" % v for v in vol] if vol else None))
            record("E2 AI: SENSOR[8]-SENSOR[9] > 3.0V (满量程可用)",
                   vol is not None and (vol[0] - vol[1]) > 3.0,
                   ("差 = %.3fV" % (vol[0] - vol[1])) if vol else "")

        # ══════════ HIL (可选, 需跳线) ══════════
        print("\n── HIL 回环 (PA6 PWM → PA5 ADC) ──")
        if not a.hil:
            skip("H1 HIL 回环 (需 PA6→PA5 跳线)", "未加 --hil; 接线后重跑")
        else:
            # 设 WIRE[20] = 512 (50% 占空比) → 期望 SENSOR[2] ≈ 1.65V
            u = 512.0
            sts, _ = L.xact(CMD_WRITE, struct.pack("<II", shm + OFF_WIRE_MAP + HIL_U_WIRE * 4,
                                                   struct.unpack("<I", struct.pack("<f", u))[0]))
            time.sleep(0.3)
            v50 = rd_f32(shm + OFF_SENSOR_MAP + HIL_FB_SENSOR * 4)
            # 设 WIRE[20] = 0 (0%) → 期望 ≈ 0V
            L.xact(CMD_WRITE, struct.pack("<II", shm + OFF_WIRE_MAP + HIL_U_WIRE * 4, 0))
            time.sleep(0.3)
            v0 = rd_f32(shm + OFF_SENSOR_MAP + HIL_FB_SENSOR * 4)
            record("H1 HIL: 50%% 占空 → SENSOR[2]≈1.65V, 0%% → ≈0V",
                   v50 is not None and v0 is not None and 1.2 <= v50 <= 2.1 and v0 <= 0.5,
                   "50%%=%.3fV  0%%=%.3fV (期望 1.65 / 0.00)" % (v50 or -1, v0 or -1))

        L.xact(CMD_RESET); time.sleep(0.1)

    print("\n" + "=" * 74)
    npass = sum(1 for _, ok, _ in RESULTS if ok is True)
    nfail = sum(1 for _, ok, _ in RESULTS if ok is False)
    nskip = sum(1 for _, ok, _ in RESULTS if ok is None)
    print("W5 外设域结果: %d PASS / %d FAIL / %d SKIP (SKIP 与 PASS 分开计)" % (npass, nfail, nskip))
    print("=" * 74)
    return 0 if (nfail == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
