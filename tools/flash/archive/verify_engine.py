#!/usr/bin/env python3
"""
引擎自检 — 仅用 pyocd 不停顿地读 DTMC 变量。
不停留（halt 会停 ISR, 读到的 ODR 不可靠）。

判断依据:
  - SAMPLES 是否在增长（ISR 心跳）
  - PE2 是否在翻转（GPIOE_ODR bit2 toggles every ISR）
  - EXEC_MIN/MAX 是否在合理范围（~200-500 cycles for 49 routes）
  - ACTUATOR_STATUS[32..63] 是否非零（路由在输出）
"""
import sys, time, struct
try:
    from pyocd.core.helpers import ConnectHelper
except ImportError:
    print("pip install pyocd"); sys.exit(1)

# 地址
SAMPLES    = 0x20000010   # TIMING_BASE + 0x10
EXEC_MIN   = 0x20000000   # TIMING_BASE + 0x00
EXEC_MAX   = 0x20000004
PERIOD_MIN = 0x20000008
PERIOD_MAX = 0x2000000C
N_ROUTES   = 0x200000F0
ODR        = 0x58021014   # GPIOE_ODR
ACT_BASE   = 0x20000200   # ACTUATOR_STATUS

def read_arr(t, addr, n):
    data = t.read_memory_block8(addr, n * 4)
    return struct.unpack(f'<{n}f', bytes(data)) if len(data)==n*4 else []

def read32(t, addr):
    return t.read32(addr)

def main():
    with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
        t = session.target
        # pyocd flash 后 MCU 停在 reset vector (halted)
        # 必须 reset_and_halt → resume 才能让 main() 完整跑完
        t.reset_and_halt()
        time.sleep(0.01)
        t.resume()
        time.sleep(0.05)

        # ── 1. 读初始状态 — 不停顿 ──
        s0 = read32(t, SAMPLES)
        odr_a = read32(t, ODR)
        n_routes = read32(t, N_ROUTES)
        time.sleep(0.5)   # 500ms
        s1 = read32(t, SAMPLES)
        odr_b = read32(t, ODR)

        delta_samples = s1 - s0
        isr_hz = delta_samples / 0.5
        print(f"SAMPLES     : {s0} → {s1}  (Δ={delta_samples}, ~{isr_hz:.0f} Hz)")
        print(f"N_ROUTES    : {n_routes}")
        print(f"GPIOE_ODR   : 0x{odr_a:02X} → 0x{odr_b:02X}  (PE2 toggling={(odr_a!=odr_b)})")

        if 9000 < isr_hz < 11000:
            print(f"[PASS] ISR 频率正常 (~100μs 周期)")
        elif isr_hz == 0:
            print(f"[FAIL] ISR 没在跑 (SAMPLES 没增长)")
        else:
            print(f"[WARN] ISR 频率异常: {isr_hz:.0f} Hz")

        # ── 2. 读 PERIOD 抖动 (不停顿, 多读几次) ──
        per_min = read32(t, PERIOD_MIN)
        per_max = read32(t, PERIOD_MAX)
        exec_min = read32(t, EXEC_MIN)
        exec_max = read32(t, EXEC_MAX)
        print(f"PERIOD_MIN  : {per_min} cycles  ({per_min/136:.1f} μs @136MHz)")
        print(f"PERIOD_MAX  : {per_max} cycles  ({per_max/136:.1f} μs @136MHz)")
        print(f"EXEC_MIN    : {exec_min} cycles  ({exec_min/136:.1f} μs)")
        print(f"EXEC_MAX    : {exec_max} cycles  ({exec_max/136:.1f} μs)")

        # 100μs exact = 13600 cycles
        if per_max > 0 and abs(per_max - 13600) < 500:
            print(f"[PASS] 周期抖动: {per_max - per_min} cycles ({(per_max-per_min)/136:.2f} μs)")
        elif per_min == 0xFFFFFFFF:
            print(f"[INFO] PERIOD 尚未采样够")

        # ── 3. 读 ACTUATOR_STATUS[32..63] 验证路由输出 ──
        if n_routes > 49:
            # 若有 deploy_test_routes 部署的 32 条测试路由 ACT[32+i] = 1.0
            act_arr = read_arr(t, ACT_BASE + 32*4, 32)
            if act_arr:
                nonzero = [(i, v) for i, v in enumerate(act_arr) if abs(v) > 0.01]
                print(f"\nACT[32..63] 非零: {len(nonzero)}/32")
                if len(nonzero) >= 30:
                    print(f"[PASS] 路由输出写入 ACTUATOR_STATUS — 全部 ~1.0")
                    # ACT[32+i]>0.5 → gpio_bits bit i=32 应在 GPIOE_ODR 里
                    gpio_bits = 0
                    for i, v in enumerate(act_arr):
                        if v > 0.5:
                            gpio_bits |= (1 << i)
                    print(f"  预期 GPIOE_ODR = 0x{gpio_bits:08X}")
                    print(f"  实测 GPIOE_ODR = 0x{read32(t, ODR):08X}  (注:halt读回可能不准)")
                else:
                    for i, v in nonzero[:8]:
                        print(f"    ACT[{32+i}] = {v:.3f}")

if __name__ == "__main__":
    main()
