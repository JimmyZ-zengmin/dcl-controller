#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""限时字段的**纯软件时基**探针 —— 零运动、零驱动器参与。

背景
----
`H723-MOTION-QUALITY-AUDIT.md` §D2 实测：『限时 ms』慢 ~26%，**且随主机轮询密度变化 21%**。
原报告把 1.26× 归因到"疑似扫描节拍或分频器按错时钟算的"。
**那个猜测是错的** —— 真因就在 `src/step.c: step_tick()` 里一句**每次调用都取整**：

    uint32_t dm = dt / 10u;      /* dt 单位 = 拍(100µs); 10 拍 = 1ms */

`dt` 是"本次调用距上次调用的拍差" ≈ **主循环一圈**。
`floor(dt/10)` 每次都把小数丢掉 ⇒ 累计消耗**永远 ≤ 真实**，
而丢多少取决于"主循环一圈有多长" ⇒ 所以它会**随主机轮询密度变化**。

设计（为什么本探针比原报告的 `timebase` 更硬）
--------------------------------------------
`g_step_deadline_tick` 的递减在 `step_tick()` 里，**与 `CC1E` 无关** ——
所以可以**不开脉冲、不使能、不碰电机**地量它。

    ⇒ 任何偏差都只能来自"拍记账"本身，**脉冲通路 / 光耦 / 驱动器全被排除在外**。

判据
----
| # | 判据 | 说明 |
|---|---|---|
| ① | 消耗速率应 ≈ **1000 / 真实秒** | **若 <1000 ⇒ 慢**。★ 取整只会慢，**永远不会快** |
| ② | 慢的幅度**随主机轮询密度变化** | ★ 恒定倍率的时钟错误**做不到这一点**；只有"每次调用取整"能 |
| ③ | **轴完全不动**（raw 全程不变） | 证明与运动无关 |
| ④ | `dt_max`（主循环见过的最大拍差） | 若 >1000 ⇒ 说明撞上了 `step_tick` 的**钳位** `if (dt>1000) dt=1000`，那是**第二个**丢时间的口子 |

推断
----
若 dt 近似恒定 = 10k+r 拍（k=floor(dt/10)、0≤r<10），则每次记 k、真实 K=10k+r，于是

    每秒消耗 = k × 10000/(10k+r) = 1000 × k/(k + r/10)

实测 649.4/秒 ⇒ k=1, r/10≈0.540 ⇒ dt ≈ **15.4 拍 = 1.54 ms**（主循环一圈）
实测 780.1/秒 ⇒ k=1, r/10≈0.282 ⇒ dt ≈ **12.8 拍 = 1.28 ms**

★ 两档都落在 k=1 上，正是"一次调用记 1 ms、实际过了 1.3~1.5 ms"的指纹。

修法（两行，且比现在更稳）
------------------------
把"限时"从 **ms** 改成 **目标拍号**，用一次减法判到期，**彻底不做除法**：

    /* 武装 */  s_dl_armed = 1u;  s_dl_tick = tick_now + ms * 10u;
    /* 判据 */  if (s_dl_armed && (int32_t)(tick_now - s_dl_tick) >= 0) { 到期 }

⇒ 无除法、无累计误差、无钳位需求、与主循环快慢**完全无关**。

用法
----
    python h723_limit_tick_probe.py [每档秒数, 默认 10]
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from h723_motion_probe import Dut                                    # noqa: E402

ARM_MS = 20000          # 武装的限时("ms"), 远大于观测窗 ⇒ 窗口内不会到期


def _raw(d):
    """读 96 字节原始应答 (st() 只给了部分字段, 这里要 dt_max)"""
    s, p = d.op19(0)
    if s != 0 or not p or len(p) < 96:
        return None
    return struct.unpack("<24I", p[:96])


def one(d, secs, poll, tag):
    d.ena(0)          # ★ 不使能
    d.stop()          # ★ 无脉冲
    r0 = _raw(d)
    if r0 is None:
        print("  %-10s 通信失败" % tag)
        return None
    base = r0[8]
    assert (r0[4] & 1) == 0, "CC1E 应为 0 (无脉冲)"
    d.limit(ARM_MS)
    t0 = time.time()
    frames = 0
    raws = [base]
    while True:
        if time.time() - t0 >= secs:
            break
        if poll:
            r = _raw(d)
            frames += 1
            if r:
                raws.append(r[8])
        else:
            time.sleep(0.02)
    t1 = time.time()
    r1 = _raw(d)
    d.limit(0)                       # 清掉, 不留给下一个人
    if r1 is None:
        print("  %-10s 收尾读失败" % tag)
        return None
    el = t1 - t0
    consumed = ARM_MS - r1[3]
    rate = consumed / el
    move = max(abs((x - base + 2048) % 4096 - 2048) for x in raws)
    ccer = r1[4] & 1
    print("  %-12s %5.2fs  消耗 %6d / 应 %6d  ⇒ **%.1f / 真实秒** (应 1000, %+.1f%%)"
          "  raw漂移 %d  CC1E=%d  dt_max=%d"
          % (tag, el, consumed, int(el * 1000), rate, (rate / 1000.0 - 1) * 100,
             move, ccer, r1[16]))
    if poll:
        print("               轮询 %d 帧 ⇒ %.1f 帧/真实秒" % (frames, frames / el))
    return rate


def probe_d3(d):
    """D3 机制 —— 忠实复刻 `cmd_units` 的**顺序**, 且保证零运动。

    原报告 §D3 只测到"units ≤10 完全不生效(跑到下一条命令才停)", **没给机制**。

    本函数复刻那一对命令的顺序 (这正是 `cmd_units` 做的):

        d.limit(units)   →   d.rate(500)        ← 两条**独立**的协议帧, 中间隔着主循环

    而 `step_tick()` 在主循环里跑, **与有没有脉冲无关** ⇒
    小限时会在 `rate()` 那一帧到达之前就被吃光；
    吃光时走的是 `step_stop_safe()`, 它把 `tleft` **置 0** —— 而 0 的语义是『不限时』(C4)。
    ⇒ **紧随其后的 `rate(hz)` 就是无限跑**, 直到下一条命令。

    ★ 零运动是怎么保证的: 全程 `ENA=0`。这块 TB6600 是 **『光耦导通 = 失能』**,
      所以 `ena=0` 时驱动器是**失能**的 ⇒ 脉冲再多电机也不动。
      (这也顺带解释了为什么"轴在动"必须用 ENA 参与 —— 见 D1。)
    """
    d.ena(0)
    d.stop()
    time.sleep(0.05)
    r0 = _raw(d)
    n0 = r0[9]
    print("  ① 武装前: tleft=%d  stop_n=%d  CC1E=%d  (ENA=0 ⇒ 驱动器失能, 轴保证不动)"
          % (r0[3], n0, r0[4] & 1))
    # ── 忠实复刻: limit 小值 → rate (两条独立帧) ──
    d.limit(2)
    d.rate(20)                # ★ 极低速率只为把"万一"压到最小; 机制与速率无关
    time.sleep(0.35)
    r1 = _raw(d)
    d.stop()
    d.limit(0)
    print("  ② `limit(2)` 后紧跟 `rate(20)`, 等 0.35s: CC1E=%d  tleft=%d  stop_n=%d(%+d)"
          % (r1[4] & 1, r1[3], r1[9], r1[9] - n0))
    if (r1[4] & 1) == 1 and r1[3] == 0 and r1[9] > n0:
        print("     ⇒ ★★★ 定案: **通道还开着(CC1E=1) 而限时已经是 0(不限时)**")
        print("        · `step_stop_safe()` 在 `rate()` 那一帧到达**之前**就跑过了 (stop_n +1)")
        print("        · 它把 tleft 置 0 = 『不限时』(C4) ⇒ 脉冲**不会自己停**")
        print("        · ⇒ 这就是 D3『限时小值不生效』: **不是时基问题, 是「顺序 × 一个值两个语义」**")
        print("        · 修 D3 只需两步: ① 拆开 `0=不限时` 与 `0=已到期`(C4);")
        print("                        ② 限时值加下界, 或把 limit/rate 合成一条原子命令")
    else:
        print("     ⇒ 未复现 (CC1E=%d tleft=%d stop_n%+d) ⇒ 机制判断需重做"
              % (r1[4] & 1, r1[3], r1[9] - n0))


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    d = Dut()
    print("=" * 100)
    print("限时字段纯软件时基探针 —— **零运动**(不使能 / 无脉冲), 只量 `step_tick` 的拍记账")
    print("=" * 100)
    print("  ★ 判据①: 速率应 ≈1000/秒; 取整只会让它**慢**, 永远不会快")
    print("  ★ 判据②: 若不同主机负载下速率不同 ⇒ 排除'固定倍率的时钟错误'")
    print()
    try:
        r_idle = one(d, secs, False, "静默(不轮询)")
        print()
        r_poll = one(d, secs, True, "密轮询")
        print()
        print("-" * 100)
        if r_idle and r_poll:
            print("  静默 %.1f/秒   密轮询 %.1f/秒   差异 %.1f%%"
                  % (r_idle, r_poll, (r_poll / r_idle - 1) * 100))
            print()
            for nm, r in (("静默", r_idle), ("密轮询", r_poll)):
                if r < 1000.0:
                    k = int(1000.0 / r)          # k = floor(dt/10); 解 k/(k+f) = rate/1000
                    f = k * 1000.0 / r - k
                    if 0 <= f < 1.0:
                        # dt(拍) = 10k + r10 = 10(k + f); 1 拍 = 100µs ⇒ 一圈 ms = dt/10
                        dt_ticks = 10.0 * (k + f)
                        print("  %-6s 反推: 每次 step_tick 记 %d ms, 而真实过了 %.2f ms"
                              " ⇒ 主循环一圈 = %.2f 拍 = **%.2f ms**, 每次丢 %.1f%%"
                              % (nm, k, dt_ticks / 10.0, dt_ticks, dt_ticks / 10.0,
                                 (1 - k / (k + f)) * 100))
                    else:
                        print("  %-6s 反推失败 (k=%d f=%.3f) ⇒ 不是单一恒定周期"
                              " ⇒ 更可能是 dt **分布**上取整, 或撞了 dt>1000 钳位" % (nm, k, f))
            if abs(r_poll - r_idle) / r_idle > 0.02:
                print()
                print("  ⇒ ✅ 判据②成立: 同一份固件、同一个请求, **速率随主机负载变化**")
                print("     ⇒ **时钟没有错**(那会给恒定倍率) ⇒ 真因是 `step_tick` 的每次调用取整")
            else:
                print()
                print("  ⇒ ⚠ 判据②不成立 ⇒ 本档不支持'取整'解释, 需重新归因"
                      " (此时才应考虑固定倍率的时基错误)")
            print()
            print("-" * 100)
            print("  D3 机制 (同样零运动):")
            print()
            probe_d3(d)
        print()
        print("  修法: 见本文件头部 —— 把限时存成**目标拍号**, 用一次减法判到期, 不做除法。")
    finally:
        try:
            d.stop()
            d.ena(0)
            d.limit(0)
            d.close()
        except Exception:
            pass
        print("\n  收尾: 已停脉冲 / 失能 / 限时清零")


if __name__ == "__main__":
    main()
