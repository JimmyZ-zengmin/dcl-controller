"""
快速检查H723是否在运行：读取SAMPLES计数器，看是否在递增
"""
import pyocd
from pyocd.core.helpers import ConnectHelper

DTCM_BASE = 0x20000000
SAMPLES_ADDR = DTCM_BASE + 0x0010  # TIMING_BASE + 0x10

print("[*] 连接 H723 (stm32h723xx)...")

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target
    
    # 连续读取3次SAMPLES，间隔0.5秒
    prev = target.read32(SAMPLES_ADDR)
    import time
    
    for i in range(3):
        time.sleep(0.5)
        curr = target.read32(SAMPLES_ADDR)
        delta = curr - prev
        freq = delta / 0.5  # Hz
        
        status = "✅ 运行中" if delta > 0 else "❌ 停止"
        print(f"  读取{i+1}: SAMPLES = {curr:>10d}  (Δ={delta:>6d}, {freq:.0f}Hz) {status}")
        prev = curr
    
    # 读取心跳标记
    heartbeat = target.read32(DTCM_BASE + 0x18)  # HEARTBEAT
    print(f"\n[*] HEARTBEAT标记: 0x{heartbeat:08X}")
    
    # 读取执行周期统计
    period_min = target.read32(DTCM_BASE + 0x08)  # PERIOD_MIN
    period_max = target.read32(DTCM_BASE + 0x0C)  # PERIOD_MAX
    exec_min = target.read32(DTCM_BASE + 0x00)    # EXEC_MIN
    exec_max = target.read32(DTCM_BASE + 0x04)    # EXEC_MAX
    
    print(f"[*] 执行周期: {exec_min}~{exec_max} DWT cycles")
    print(f"[*] ISR周期:  {period_min}~{period_max} DWT cycles")
    print(f"  (136MHz, 100μs = 13600 cycles)")
    
    if delta > 0:
        print("\n✅ 硬件正常运行！可以断开DAPLink，接CH340")
    else:
        print("\n❌ 硬件未运行，请检查固件")
