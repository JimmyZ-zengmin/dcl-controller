"""
非侵入式连续监控
核心理念: halt会打断火车, 我们只看不停
每100ms采样一次, 动态观察DTCM是否在变化
"""
import time
from pyocd.core.helpers import ConnectHelper

# 关键观察点 (来自CLAUDE.md中的DTCM布局)
DTCM_TIMING = 0x20000000   # 抖动统计
DTCM_SENSOR = 0x20000100   # SENSOR_MAP[0]
DTCM_WIRE7  = 0x20000300   # WIRE_MAP[7] (PID输出, 如果代码运行)
DTCM_LOG    = 0x2000D000   # 日志缓冲

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    print("="*60)
    print("非侵入式监控 (只读, 零halt)")
    print("="*60)
    print(f"{'时间':>6} | {'SENSOR[0]':>12} | {'WIRE[7]':>12} | {'变化':>6}")
    print("-"*60)

    prev_sensor = None
    prev_wire7 = None

    for i in range(50):  # 采样5秒
        sensor = target.read32(DTCM_SENSOR)
        wire7  = target.read32(DTCM_WIRE7)

        # 解码float
        import struct
        sensor_f = struct.unpack('>f', struct.pack('>I', sensor))[0]
        wire7_f  = struct.unpack('>f', struct.pack('>I', wire7))[0]

        changed = (sensor != prev_sensor) or (wire7 != prev_wire7)

        if changed or i < 3:
            print(f"{i*0.1:5.1f}s | {sensor_f:12.4f} | {wire7_f:12.4f} | {'✓' if changed else '·'}")

        prev_sensor = sensor
        prev_wire7  = wire7

        time.sleep(0.1)

    # 总结
    print("="*60)
    print("结论:")
    if prev_sensor is not None and prev_sensor != 0x3D21BD4F:
        print("  ✓ SENSOR[0]在变化 → 代码运行中!")
    else:
        print("  ✗ SENSOR[0]固定 → 代码未运行或传感器是静态值")
