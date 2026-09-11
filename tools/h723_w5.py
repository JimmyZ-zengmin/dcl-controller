#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_w5.py — W5 外设域验收: DI · AI(ADC 16bit) · HIL

★ 设计要点:
  · **自动判别接了哪些线**: 三个实验各自先"探测", 接了才判 PASS/FAIL, 没接记 **SKIP**
    (SKIP 与 PASS 分开计, 不把"没接线"谎报成"失败"——那是判据在骗人)。
  · DI 用 **0x36 内部上/下拉自检** (零接线可证通路) + 外部钉低交叉确认。
  · AI 靠读到真实电压 (PA0=3.3V / PA1=GND) 判定; **H723 analog 模式上下拉对 ADC 无效**,
    所以 ADC 没有零接线自证手段 (实测结论, 别再试)。
  · HIL 主动写 WIRE[20] 变占空比, 看 SENSOR[2] 是否跟随 —— 跟随=跳线已接。

接线 (按需, 逐个实验):
  DI : PC0..PC3 → GND (或一根线依次碰)       AI : PA0→3.3V, PA1→GND, 共地
  HIL: PA6 → PA5 直连 (串 1kΩ+1µF 更干净)    所有实验都要与板子共地

用法:  python tools/h723_w5.py
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
CMD_ADC_SCAN     = 0x37   # [ch_start][count] → count × u16 raw (接线/映射排障)

OFF_SENSOR_MAP = 0x0040
OFF_WIRE_MAP   = 0x0240
OFF_HIL_DUTY   = 0x6E00   # u32: 固件实际写入 TIM3_CCR1 的计数值 (engine.h)
OFF_HIL_FB_RAW = 0x6E04   # u32: HIL 反馈最近一次 ADC 原始码 (排障镜像)
AI_SENSOR_BASE = 8      # SENSOR[8..10] = PA0/PA1/PA4
DI_SENSOR_BASE = 3      # SENSOR[3..6]  = PC0..PC3
HIL_FB_SENSOR  = 2      # SENSOR[2]     = PA5 反馈
HIL_U_WIRE     = 20     # WIRE[20]      = u

AI_PINS = ["PA0", "PA1", "PA4"]
DI_PINS = ["PC0", "PC1", "PC2", "PC3"]

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  [%s] %-52s %s" % ("PASS" if ok else "FAIL", name, detail))


def skip(name, detail=""):
    RESULTS.append((name, None, detail))
    print("  [SKIP] %-52s %s" % (name, detail))


def f32(v):
    return struct.unpack("<I", struct.pack("<f", v))[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    port = find_port(a.port)
    if not port:
        print("!! 找不到串口"); return 2
    print("端口: %s @ 115200" % port)

    with serial.Serial(port, 115200, timeout=0.05) as ser:
        L = Link(ser, a.verbose)
        time.sleep(0.3)

        def rd_burst(addr, nwords, timeout=0.6):
            sts, p = L.xact(CMD_READ_BURST, struct.pack("<IH", addr, nwords), timeout=timeout)
            return p if sts == 0 else None

        def rd_f32(addr):
            p = rd_burst(addr, 1)
            return struct.unpack("<f", p)[0] if p and len(p) >= 4 else None

        def rd_floats(addr, n):
            p = rd_burst(addr, n)
            return list(struct.unpack("<%df" % n, p[:4 * n])) if p and len(p) >= 4 * n else None

        print("\n── T0 前置 ──")
        sts, p = L.xact(CMD_GET_VERSION)
        record("T0 链路活性 (GET_VERSION→ACK)", sts == 0 and len(p) >= 4, "sts=%s" % sts)
        if sts != 0:
            return 2
        cap = struct.unpack("<H", p[2:4])[0]
        record("T0b cap 含 AI(0x0400)+MACRO(0x0800) (宣称=实现)", (cap & 0x0C00) == 0x0C00,
               "cap=0x%04X" % cap)
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

        # ══════════ 采样: 0x36 自检 + SENSOR 读数 ══════════
        print("\n── 采样 (0x36 内部上/下拉自检 + SENSOR 独立读回) ──")
        sts, p = L.xact(CMD_PIN_SELFTEST, bytes([0]))
        di_pp = None
        if sts == 0 and p and len(p) >= 20:
            ai_pp = [struct.unpack_from("<H", p, i * 2)[0] for i in range(6)]
            di_pp = [(p[12 + i * 2], p[13 + i * 2]) for i in range(4)]
            print("   AI(PA0/PA1/PA4) pu/pd = %s" % [(ai_pp[i*2], ai_pp[i*2+1]) for i in range(3)])
            print("   DI(%s) pu/pd = %s" % ("/".join(DI_PINS), di_pp))

        time.sleep(0.2)
        ai_v = rd_floats(shm + OFF_SENSOR_MAP + AI_SENSOR_BASE * 4, 3)
        di_v = rd_floats(shm + OFF_SENSOR_MAP + DI_SENSOR_BASE * 4, 4)
        print("   AI 电压  = %s" % (["%.3f" % v for v in ai_v] if ai_v else None))
        print("   DI 槽值  = %s" % (["%.0f" % v for v in di_v] if di_v else None))

        # ══════════ ① DI 数字输入 ══════════
        print("\n── ① DI 数字输入 (%s) ──" % "/".join(DI_PINS))
        if not di_pp:
            record("D-1 0x36 自检响应", False, "无响应")
        else:
            # 每路要么"跟随内部上下拉"(pu=1,pd=0), 要么"被外部钉低"(pu=0,pd=0)。
            # (pu=1,pd=1 = 下拉无效 = 通路异常 ⇒ 判 FAIL, 判据能失败)
            path_ok = all(di_pp[i] in ((1, 0), (0, 0)) for i in range(4))
            record("D-1 ★DI 输入通路: 内部上/下拉生效 (或外部钉低) — 4 路均可解释",
                   path_ok, "pu/pd=%s" % di_pp)

            ext = [i for i in range(4) if di_pp[i] == (0, 0)]     # 被外部钉低
            flo = [i for i in range(4) if di_pp[i] == (1, 0)]     # 悬空(内上拉)
            if ext and di_v is not None:
                record("D-2 ★DI 外部输入: 被外部钉低的通道 SENSOR=0.0 (真采到了外部电平)",
                       all(di_v[i] == 0.0 for i in ext),
                       "%s → SENSOR=%s (外部拉低压过 40kΩ 内上拉)"
                       % ([DI_PINS[i] for i in ext], [di_v[i] for i in ext]))
            else:
                skip("D-2 ★DI 外部输入 (需把 1 路 %s 接 GND)" % DI_PINS[0],
                     "本轮无外部钉低通道 → 只证了通路, 未证外部信号")
            if flo and di_v is not None:
                record("D-3 悬空通道(内上拉) SENSOR=1.0", all(di_v[i] == 1.0 for i in flo),
                       "%s → %s" % ([DI_PINS[i] for i in flo], [di_v[i] for i in flo]))
            else:
                skip("D-3 悬空通道 SENSOR=1.0", "本轮无悬空通道")

        # ══════════ ② AI 模拟量输入 (ADC1 16bit) ══════════
        print("\n── ② AI 模拟量输入 (%s, ADC1 16bit) ──" % "/".join(AI_PINS))
        if ai_v is None:
            record("A-1 SENSOR 读回", False, "burst 失败")
        else:
            record("A-0 ADC 在转换 (读数非超时哨兵)", all(v < 3.5 for v in ai_v),
                   "V=%s" % ["%.3f" % v for v in ai_v])
            # ★ ADC 通道**是否真的接到引脚**: 每路要么被**外部钉住**(pu≈pd 且 高/低),
            #   要么**内部上下拉生效**(pu 明显 > pd)。两者都证明该通道连到了 pad。
            #   (此前这条判不出来是因为漏写 PCSEL —— ADC 根本没接引脚, 详见 adc.c。)
            if ai_pp:
                def _sel_ok(pu, pd):
                    if abs(pu - pd) < 500 and (pu > 60000 or pu < 500):
                        return True                       # 被外部钉住 (高 / 低)
                    return (pu - pd) >= 2000              # 内部上下拉生效
                record("A-sel ★ADC 通道跟随引脚 (被外部钉住 或 内部上下拉生效)",
                       all(_sel_ok(ai_pp[i*2], ai_pp[i*2+1]) for i in range(3)),
                       "pu/pd = %s" % [(ai_pp[i*2], ai_pp[i*2+1]) for i in range(3)])
            if ai_v[0] >= 2.8 and ai_v[1] <= 0.3:
                record("A-1 ★AI 外部: PA0=3.3V→SENSOR[8]∈[3.0,3.4] 且 PA1=GND→SENSOR[9]≤0.2",
                       3.0 <= ai_v[0] <= 3.4, "V=%s" % ["%.3f" % v for v in ai_v])
                record("A-2 ★AI 满量程跨度 > 3.0V (16bit 通道可用)", (ai_v[0] - ai_v[1]) > 3.0,
                       "差 = %.3fV" % (ai_v[0] - ai_v[1]))
            else:
                skip("A-1 ★AI 外部 (需 PA0→3.3V, PA1→GND)",
                     "PA0=%.3fV PA1=%.3fV → 未读到外部电压" % (ai_v[0], ai_v[1]))
                skip("A-2 ★AI 满量程跨度", "同上")
                # 自动诊断: 是"线没落到 ADC1 任何脚" 还是 "我的通道号映射错"?
                # 两者在 AI 读数上**完全一样**, 只能扫全通道裁决。
                sts, sc = L.xact(CMD_ADC_SCAN, bytes([0, 20]))
                if sts == 0 and sc and len(sc) >= 40:
                    chs = [struct.unpack_from("<H", sc, i * 2)[0] for i in range(20)]
                    hot = [(i, v) for i, v in enumerate(chs) if v > 40000]
                    print("   ↳ ADC1 全通道扫描: 高电平(>2V)通道 = %s"
                          % (hot if hot else "无 ⇒ 3.3V 没落到 ADC1 的任何脚上 (接错脚/没通)"))
                    print("     全部 = %s" % " ".join("%d:%d" % (i, v) for i, v in enumerate(chs)))

        # ══════════ ③ HIL ══════════
        print("\n── ③ HIL (PWM 输出 PA6 ← WIRE[20]; 反馈 PA5 → SENSOR[2]) ──")

        def set_u(val):
            L.xact(CMD_WRITE, struct.pack("<II", shm + OFF_WIRE_MAP + HIL_U_WIRE * 4, f32(val)))
            time.sleep(0.15)
            p = rd_burst(shm + OFF_HIL_DUTY, 1)
            return struct.unpack("<I", p)[0] if p and len(p) >= 4 else None

        def duty_exp(u):                      # ARR+1 = 1000 (1MHz / 1kHz)
            return int(round(u * 1000.0 / 1024.0))

        # H-0 **零接线**: 输出臂 —— 镜像值必须等于按公式算出的计数值
        d512, dfull, d0 = set_u(512.0), set_u(1024.0), set_u(0.0)
        ok_d = (d512 is not None and dfull is not None and d0 is not None
                and abs(d512 - duty_exp(512)) <= 2 and abs(dfull - duty_exp(1024)) <= 2 and d0 == 0)
        record("H-0 ★HIL 输出臂(零接线): WIRE[20] → TIM3 占空比镜像符合公式",
               ok_d, "u=512→%s(期望500) u=1024→%s(期望1000) u=0→%s" % (d512, dfull, d0))

        # H-1 物理回环 (需 PA6→PA5 跳线)。★ 分两级判: 先"轨道"再"中间值" ——
        #   满幅/零幅对采样相位**免疫**, 能直接回答"跳线通了没"; 50% 走平均,
        #   无 RC 时还受采样相位影响(混叠)。分开放, 失败点才定位得准。
        def hold_u(val):
            L.xact(CMD_WRITE, struct.pack("<II", shm + OFF_WIRE_MAP + HIL_U_WIRE * 4, f32(val)))
            time.sleep(0.35)
            return rd_f32(shm + OFF_SENSOR_MAP + HIL_FB_SENSOR * 4)

        v100, v0, v50 = hold_u(1024.0), hold_u(0.0), hold_u(512.0)
        _rp = rd_burst(shm + OFF_HIL_FB_RAW, 1)
        fbraw = struct.unpack("<I", _rp)[0] if _rp and len(_rp) >= 4 else None
        print("   反馈原始码镜像 OFF_HIL_FB_RAW = %s (SENSOR[2]=%.3fV)" % (fbraw, v50 if v50 is not None else -1))
        if v100 is None or v100 < 3.0:
            skip("H-1 ★HIL 物理回环 (需 PA6→PA5 跳线)",
                 "满幅时反馈=%.3fV (应≥3.0V) ⇒ 跳线未通/脚不对" % (v100 or -1))
            # 自动诊断: 保持**满幅**扫全通道 —— PWM 若真到某个 ADC1 脚, 该通道应读到满幅
            L.xact(CMD_WRITE, struct.pack("<II", shm + OFF_WIRE_MAP + HIL_U_WIRE * 4, f32(1024.0)))
            time.sleep(0.2)
            sts, sc = L.xact(CMD_ADC_SCAN, bytes([0, 20]))
            L.xact(CMD_WRITE, struct.pack("<II", shm + OFF_WIRE_MAP + HIL_U_WIRE * 4, 0))
            if sts == 0 and sc and len(sc) >= 40:
                chs = [struct.unpack_from("<H", sc, i * 2)[0] for i in range(20)]
                hot = [(i, v) for i, v in enumerate(chs) if v > 40000]
                print("   ↳ 满幅占空扫全通道: 高电平通道 = %s"
                      % (hot if hot else "无 ⇒ PA6 的 PWM 没到任何 ADC1 脚"))
                print("     全部 = %s" % " ".join("%d:%d" % (i, v) for i, v in enumerate(chs)))
        else:
            record("H-1a ★HIL 物理回环(轨道): u=1024→SENSOR[2]≥3.0V 且 u=0→≤0.4V",
                   (v0 is not None and v0 <= 0.4), "满幅=%.3fV 零幅=%.3fV" % (v100, v0 or -1))
            record("H-1b ★HIL 占空比跟随: u=512 → SENSOR[2]≈1.65V (16 次跨周期平均)",
                   1.2 <= (v50 if v50 is not None else -1) <= 2.1,
                   "50%%=%.3fV (期望 1.65)" % (v50 if v50 is not None else -1))

        L.xact(CMD_RESET); time.sleep(0.1)

    print("\n" + "=" * 74)
    npass = sum(1 for _, ok, _ in RESULTS if ok is True)
    nfail = sum(1 for _, ok, _ in RESULTS if ok is False)
    nskip = sum(1 for _, ok, _ in RESULTS if ok is None)
    print("W5 外设域: %d PASS / %d FAIL / %d SKIP  (SKIP = 该实验线未接; 与 PASS 分开计)"
          % (npass, nfail, nskip))
    print("=" * 74)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
