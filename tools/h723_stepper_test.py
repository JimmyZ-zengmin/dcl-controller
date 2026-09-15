#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_stepper_test.py — 步进（TB6600）分步测试向导

★ 上电顺序（务必按这个来）
   1. 先跑 `python h723_stepper_test.py safe`   → 确认"无脉冲 + PE8~PE11 全高(不导通)"
   2. **保持 PC 侧连接**，再去合 24V 电源
   3. 先 `ena` 确认 ENA 极性（手转电机轴：转得动=失能 / 锁死=使能）
   4. 再 `pulse <Hz> <ms>` 小步试转（**一定给限时**）

子命令
    safe                 查上电安全态（不接 24V 也要先过）
    ena 0|1              使能/失能（看电机轴手感判极性）
    enapol 0|1           切 ENA 有效电平（0=拉低使能 默认）—— 手感判断后用它纠正
    dir 0|1              方向
    pulse <Hz> <ms>      脉冲（ms=限时, 强烈建议给；0=不限）
    stop                 停脉冲 + 失能（安全态, sub=6）
    autotest [Hz] [ms]   两种 ENA 极性各给一次脉冲（判极性 + 看轴转不转）
    probe low|high|af     ★★ PA6 静态电平探针 —— **判接线唯一可靠的手段**（见下方说明）
    probe od 0|1          PA6 输出类型: 0=推挽 / 1=开漏（"3.3V 高电平关不断 5V 光耦"时的正解）
    state                读全部状态

★★ 为什么必须有 probe（2026-09-15 血泪）:
   电机不转时，量 PA6 和 PUL− 用的是**万用表 DC 档**。而 500Hz / 50% 方波在 DC 档上读的是
   **平均值 (~1.65V)** —— "接通的 0V 段"与"根本断路的浮空"在表上只差一点点，判不了。
   更要命的是"脉冲是不是还在跑"本身还是个变量（带限时，到点自动停）。
   ⇒ 把 PA6 从 TIM3 上摘下来、输出一个**确定的静态电平**，读数就没有任何歧义:

       先 probe high (PA6=3.3V)，量 PUL−；再 probe low (PA6=0V)，量 PUL−。

   | PA6 端 | PUL− 端 | 结论 |
   |---|---|---|
   | 3.3V / 0V 都跟得上 | 3.3V档≈2~4V, 0V档≈1.0~1.5V | ✅ 线通、端子对 |
   | 3.3V / 0V 都跟得上 | **两档都≈4.5~5V 不动** | ✗ **PA6→PUL− 这根线断 / 插错端子** |
   | 3.3V / 0V 都跟得上 | 两档都≈0V | ✗ PUL− 被短到 GND |
   | **PA6 端自己就不变**（比如恒 5V） | — | ✗ 排针脚认错了（U9 pin19 不是 PA6）/ 板载器件拉住了 |

   用完记得 `probe af` 还原到 TIM3（还原前 CC1E=0，仍然无脉冲，安全）。
"""
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from h723_client import Dcl, find_board   # noqa: E402

C = 0x39


def cmd(d, sub, arg=0):
    pl = bytes([0x13, sub]) + struct.pack("<I", arg)
    sts, p = d.send(C, pl)
    if sts != "ACK" or len(p) < 52:
        return None
    # ★ 固件侧应答由 68B 逐次扩到 **96B** (尾部追加 IDR + 实时 TIM3 寄存器)。
    #   这里按"有多少解多少"来解, 免得新旧固件各写一份解析 (那又是"同一个量两处定义")。
    nw = min(len(p) // 4, 24)
    w = list(struct.unpack("<%dI" % nw, p[:nw * 4]))
    while len(w) < 24:
        w.append(0)
    return dict(rate=w[0], dir=w[1], ena=w[2], deadline=w[3], ccer=w[4],
                arr=w[5], ccr1=w[6], mask=w[7], raw=w[8], stops=w[9],
                odr=w[10], moder=w[11], enapol=w[12],
                tim3_cnt=w[13], pa_moder=w[14], pa_afrl=w[15], dt_max=w[16],
                pa_odr=w[17], pa_otyper=w[18], pa_idr=w[19], pe_idr=w[20],
                tim3_ccmr1=w[21], tim3_ccr1_live=w[22], tim3_arr_live=w[23],
                rlen=len(p))


# ★★★★★ 接线判定的**唯一可靠手段**: PA6 静态电平探针
#   为什么必须用它: 500Hz 方波在万用表 DC 档上读的是**平均值**(50% 占空 ⇒ 约 1.65V),
#   于是"线通了"和"线断了"在表上只差一点点, 而且"脉冲还在不在跑"本身还是个变量。
#   静态电平没有任何歧义:
#       sub=7 (0V)   ⇒ 接通的 PUL− 会被光耦 LED 钳到 ~1.0~1.5V; 断路的 PUL− 停在 ~4.5~5V
#       sub=8 (3.3V) ⇒ 接通的 PUL− 抬到 ~2~4V;               断路的 PUL− 纹丝不动
PROBE = {"low": 7, "high": 8, "af": 9}


def idrwatch(d, hz=500, secs=1.5):
    """★★★ 起脉冲, 高频采样 PA6 的 **IDR**(引脚真实电平), 统计 0/1 占比。

    为什么这是最硬的判据: IDR 与"谁在驱动引脚"**无关** ——
      · 若 AF/TIM3 通路真的接到了引脚上 ⇒ 引脚在 0/3.3V 之间翻 ⇒ 随机相位采样会同时拿到 0 和 1
      · 若引脚是高阻(只有外部上拉)          ⇒ 恒为 1
    ⇒ 一次就区分"配置全绿但脚没动"和"脚真的在动"。
    ★ 采样是异步的(每帧约几 ms), 所以只看**两个值都出现没**, 不看比例。
    """
    cmd(d, 6)                                    # 先安全态
    cmd(d, 4, int(secs * 1000) + 5000)           # 限时(留余量)
    if hz:
        cmd(d, 1, hz)
    cnt = {0: 0, 1: 0}
    t0 = time.time()
    last = None
    while time.time() - t0 < secs:
        s = cmd(d, 0)                            # sub=0 = 只查询, 零副作用
        if s is None:
            break
        last = s
        cnt[(s["pa_idr"] >> 6) & 1] += 1
    cmd(d, 6)
    n = cnt[0] + cnt[1]
    print("  脉冲=%-6dHz  采样 %d 次    IDR=0: %d 次    IDR=1: %d 次"
          % (hz, n, cnt[0], cnt[1]))
    if last is not None:
        print("  CC1E=%d  TIM3_CNT=%d  MODER=%d  AFRL=%d"
              % (last["ccer"] & 1, last["tim3_cnt"],
                 (last["pa_moder"] >> 12) & 3, (last["pa_afrl"] >> 24) & 0xF))
    if cnt[0] and cnt[1]:
        print("  ⇒ ✅ **PA6 真的在翻转** —— AF/TIM3 通路接到了引脚上")
    elif cnt[1] and not cnt[0]:
        print("  ⇒ ✗✗ **PA6 恒高、从不落低** ⇒ 引脚是高阻, MCU 没在驱动它")
        print("       ⇒ AF/TIM3 通路**没有**接到 PA6(或该脚 AF 号不对), 脉冲从未输出过")
    elif cnt[0] and not cnt[1]:
        print("  ⇒ ✗ **PA6 恒低** ⇒ 同样说明没在翻转(或停在低半周期)")
    return cnt


def probe_cmd(d, name, arg=0):
    """probe low / high / af / od <0|1> —— 见 PROBE 的说明。"""
    if name in PROBE:
        return cmd(d, PROBE[name])
    if name == "od":
        return cmd(d, 10, arg)
    raise SystemExit("probe 只认 low / high / af / od <0|1>")


def show_probe(s):
    pa6_moder = (s["pa_moder"] >> 12) & 3
    pa6_af = (s["pa_afrl"] >> 24) & 0xF
    pa6_odr = (s["pa_odr"] >> 6) & 1
    pa6_od = (s["pa_otyper"] >> 6) & 1
    pa6_idr = (s["pa_idr"] >> 6) & 1
    mode = {0: "输入", 1: "输出", 2: "AF", 3: "模拟"}[pa6_moder]
    lvl = "-" if pa6_moder != 1 else ("3.3V" if pa6_odr else "0V")
    print("  PA6: MODER=%d(%s)  AFRL=%d%s  输出类型=%s  输出电平=%s"
          % (pa6_moder, mode, pa6_af, " (TIM3)" if pa6_af == 2 else "",
             "开漏" if pa6_od else "推挽", lvl))
    print("       TIM3_CNT=%d  CC1E=%d  脉冲=%dHz" % (s["tim3_cnt"], s["ccer"] & 1, s["rate"]))
    print("       TIM3 实时: CCMR1=0x%08X (OC1M=%d%s)  ARR=%d  CCR1=%d"
          % (s["tim3_ccmr1"], (s["tim3_ccmr1"] >> 4) & 7,
             "" if ((s["tim3_ccmr1"] >> 4) & 7) == 6 else " ★不是PWM1!",
             s["tim3_arr_live"], s["tim3_ccr1_live"]))
    # ★ 缓存 (g_step_*) 与实时寄存器不一致 ⇒ 有**别人**在改 TIM3 (最可能是 hil_outputs_safe)
    if s["ccr1"] != s["tim3_ccr1_live"] or s["arr"] != s["tim3_arr_live"]:
        print("       ✗✗ **缓存与实时不一致** ⇒ 有别的代码在改 TIM3！"
              " (缓存 ARR/CCR1=%d/%d, 实时=%d/%d)"
              % (s["arr"], s["ccr1"], s["tim3_arr_live"], s["tim3_ccr1_live"]))
        if s["ccer"] & 1 and s["tim3_ccr1_live"] == 0:
            print("           ★ 实时 CCR1=0 且 CC1E=1 ⇒ PWM1 输出**恒定低** ⇒ 光耦持续导通"
                  " ⇒ 驱动器收不到边沿 ⇒ **不计数**")
    print("       PE8~PE11 电平=%s (IDR) / ODR=%s"
          % ([(s["pe_idr"] >> b) & 1 for b in (8, 9, 10, 11)],
             [(s["odr"] >> b) & 1 for b in (8, 9, 10, 11)]))
    # ★★★ 核心判据: IDR 读的是引脚**真实**电平, 与"谁在驱动它"无关。
    verdict = "?"
    if pa6_moder == 1:                       # GPIO 输出: 我们自己驱动, ODR 应等于 IDR
        verdict = "✅ 推挽驱动, IDR 跟随" if pa6_odr == pa6_idr else "⚠ ODR 说 %d 但 IDR 读 %d ⇒ 外部在抢" % (pa6_odr, pa6_idr)
    elif pa6_moder == 2 and (s["ccer"] & 1) == 0:
        # ★★★ 2026-09-15 更正 (审计 §8.6 指出): 这里原来说"通道关断但 IDR=1
        #   ⇒ AF/通道没真正接到引脚上"是**假报警**。
        #   **AF + CC1E=0 时引脚本来就是高阻**, IDR 被外部上拉读到 1 是**正常现象**
        #   (与"AF 通路通不通"无关)。老判据每次 probe af 都误报。
        #   ⇒ 判"AF/TIM3 通路有没有接到引脚上", **必须在有脉冲时判** —— 见 idrwatch。
        verdict = ("✅ AF + 关通道 ⇒ 引脚高阻(正常), IDR 被外部上拉到高"
                   "  —— 想看通路通不通请用 idrwatch"
                   if pa6_idr else
                   "◻ AF + 关通道 ⇒ IDR=0 (外部没上拉, 或被拉低)")
    print("       PA6 真实电平(IDR)=%d  ⇒ %s" % (pa6_idr, verdict))


def show(tag, s):
    pe = [(s["odr"] >> b) & 1 for b in (8, 9, 10, 11)]
    print("  %-10s 脉冲=%-6dHz  方向=%d  使能=%d  限时=%dms  CC1E=%d  ARR=%d  CCR1=%d"
          % (tag, s["rate"], s["dir"], s["ena"], s["deadline"], s["ccer"] & 1, s["arr"], s["ccr1"]))
    print("             GPIO_MASK=0x%04X  ENA极性=%d   PE8~PE11 = %s   raw=%d (%.2f°)"
          % (s["mask"], s["enapol"], pe, s["raw"], s["raw"] * 360.0 / 4096.0))
    # ★ 实时寄存器 vs 缓存 —— 任何"别人改了 TIM3"都会在这里露出来
    live_bad = (s["arr"] != s["tim3_arr_live"]) or (s["ccr1"] != s["tim3_ccr1_live"])
    if live_bad or ((s["ccer"] & 1) and s["tim3_ccr1_live"] == 0):
        print("             ✗✗ 实时 TIM3: CCMR1=0x%08X ARR=%d CCR1=%d (缓存 %d/%d)"
              " ⇒ **有别的代码在改 TIM3**"
              % (s["tim3_ccmr1"], s["tim3_arr_live"], s["tim3_ccr1_live"], s["arr"], s["ccr1"]))


def read_angle(d):
    sts, p = d.send(C, bytes([0x12]))
    if sts != "ACK" or len(p) < 40:
        return None
    w = struct.unpack("<10I", p[:40])
    return dict(raw=w[0], ok=w[4], err=w[5])


def autotest(d, hz=500, ms=1500):
    """★ 对照实验: **两种 ENA 极性各给一次脉冲**, 看轴转不转、编码器跟不跟。
    一次跑完, 省掉"猜极性→重试"的来回。判据: 哪一档 raw 在变 ⇒ 那档就是"使能"。"""
    print("=" * 78)
    print("ENA 极性 × 脉冲 对照   脉冲=%dHz  限时=%dms" % (hz, ms))
    print("=" * 78)
    for en in (0, 1):
        cmd(d, 6)                                  # 先回安全态 (sub=6 = 停止)
        cmd(d, 3, en)                              # 设 ENA
        time.sleep(0.4)
        a0 = read_angle(d)
        cmd(d, 4, ms)                              # 武装限时
        cmd(d, 1, hz)                              # 起脉冲
        samples = []
        t0 = time.time()
        while time.time() - t0 < ms / 1000.0 + 0.5:
            r = read_angle(d)
            if r:
                samples.append(r["raw"])
            time.sleep(0.005)
        cmd(d, 6)                                  # 收尾安全态
        time.sleep(0.3)
        vals = [v for v in samples if v <= 4095]
        span = (max(vals) - min(vals)) if vals else 0
        # 环形跨度: 1 整圈会回绕, 用"不同取值个数"更稳
        uniq = len(set(vals))
        print("  ENA=%d (光耦%s): 起始 raw=%d  采样 %d 个  不同取值 %d  极差 %d"
              % (en, "导通" if en == 0 else "不导通", a0["raw"] if a0 else -1,
                 len(vals), uniq, span))
        print("           AS5600 ok=%d err=%d" % (samples[-1] and 0 or 0, 0) if False else
              "           AS5600 ok=%d err=%d" % (read_angle(d)["ok"], read_angle(d)["err"]))
        if uniq > 50:
            print("           ⇒ ✅ **轴在转** ⇒ 这一档 = 使能")
        else:
            print("           ⇒ 轴没动（raw 基本不变）")
        time.sleep(0.5)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    op = sys.argv[1]
    d = Dcl(find_board(), wait=0.6)
    try:
        if op == "safe":
            cmd(d, 6)                           # 显式回安全态 (sub=6)
            s = cmd(d, 0)                       # sub=0 = 只查询(无动作)
            show("安全态", s)
            # ★★★ 2026-09-15 更正 (审计 §8.6 指出): 老判据写死 "PE8~PE11 全 1",
            #   但 PE9(ENA) 的"光耦不导通"电平**随有效极性变**:
            #       enapol=0 (拉低使能) ⇒ 不导通 = 高 = 1
            #       enapol=1 (拉高使能) ⇒ 不导通 = 低 = 0
            #   ⇒ 判据必须**跟着极性走**, 否则在 enapol=1 时它是一条**永远失败**的判据。
            # ★ 并且用 **IDR(引脚实际电平)** 而不是 ODR(我们写下去的值) ——
            #   安全态是"引脚上真的是什么", 不是"我以为写了什么"。
            pe = [(s["pe_idr"] >> b) & 1 for b in (8, 9, 10, 11)]
            ena_off = 1 if s["enapol"] == 0 else 0
            # PE8(DIR)/PE10/PE11 不参与使能, 保持"光耦不导通"= 高
            pe_ok = all(pe[0:1] + pe[2:4]) and pe[1] == ena_off
            ok = (s["rate"] == 0 and (s["ccer"] & 1) == 0 and s["mask"] == 0x0F00
                  and pe_ok)
            print()
            print("  ⇒ %s" % ("✅ 可以合 24V 电源（无脉冲 + 光耦全不导通）" if ok
                              else "✗ **不要接电源**：安全态不满足"
                                   "（PE8~PE11 实际电平=%s，其中 PE9 期望=%d(按 enapol=%d)）"
                                   % (pe, ena_off, s["enapol"])))
        elif op == "ena":
            show("使能后", cmd(d, 3, int(sys.argv[2])))
        elif op == "dir":
            show("换向后", cmd(d, 2, int(sys.argv[2])))
        elif op == "pulse":
            hz = int(sys.argv[2]); ms = int(sys.argv[3]) if len(sys.argv) > 3 else 2000
            cmd(d, 4, ms)
            s = cmd(d, 1, hz)
            show("起脉冲", s)
            print("             （限时 %dms，到点会自动停）" % ms)
        elif op == "autotest":
            autotest(d, int(sys.argv[2]) if len(sys.argv) > 2 else 500,
                     int(sys.argv[3]) if len(sys.argv) > 3 else 1500)
        elif op == "enapol":
            show("极性已切", cmd(d, 5, int(sys.argv[2])))
        elif op == "stop":
            show("已停", cmd(d, 6))
        elif op == "probe":
            # ★ 用: python h723_stepper_test.py probe high   (然后拿万用表量 PUL−)
            #        python h723_stepper_test.py probe low
            #        python h723_stepper_test.py probe af      (还原 TIM3)
            name = sys.argv[2] if len(sys.argv) > 2 else "low"
            arg = int(sys.argv[3]) if len(sys.argv) > 3 else 0
            show_probe(probe_cmd(d, name, arg))
        elif op == "idrwatch":
            # ★★ 判"PA6 到底动没动" —— 不用万用表, 读引脚自己的 IDR
            idrwatch(d, int(sys.argv[2]) if len(sys.argv) > 2 else 500,
                     float(sys.argv[3]) if len(sys.argv) > 3 else 1.5)
        else:
            show("当前", cmd(d, 0))            # sub=0 = 只查询
        d.close()
    except Exception:
        d.close()
        raise


if __name__ == "__main__":
    main()
