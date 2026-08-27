#!/usr/bin/env python3
"""
DMA 验证: 手动写 SHADOW,立刻读 ODR 看是否被 DMA 搬动

思路:
  引擎运行中,ISR 会不断覆盖 SHADOW (= 通常 ~0)
  我们在某个瞬间写 SHADOW = 0x00000001
  然后立即读 ODR
  - 如果 ODR bit0 = 1 → DMA 搬了 → DMA 工作
  - 如果 ODR bit0 = 0 → DMA 没搬

这个测试时间敏感 (ISR 周期 100μs),但有概率抓住 DMA 工作周期
"""
import os, sys, time

os.chdir(r'D:\STM\work\dcl-controller')
sys.path.insert(0, 'ide/compiler')
from dcl_compiler import DCLCompiler
from pyocd.core.helpers import ConnectHelper

SHADOW  = 0x200000E0
ODR     = 0x58021014
SAMPLES = 0x20000010

def main():
    # 部署任意 DCL (用 medium_test,有实际输出)
    src = open('ide/compiler/samples/medium_test.dcl', encoding='utf-8').read()
    c = DCLCompiler(); c.parse(src); c.topological_sort()
    bin_data = c.generate_binary()
    n = bin_data[0] | (bin_data[1] << 8)
    rb = bin_data[4:4+n*16]
    pb = bin_data[4+n*16:]

    with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
        t = session.target
        # 部署
        t.write_memory_block8(0x20001700, bytes(1024 * 16))
        t.write_memory_block8(0x20005700, bytes(512 * 16))
        t.write_memory_block8(0x20001700, bytes(rb))
        t.write_memory_block8(0x20005700, bytes(pb))
        t.write32(0x200000F0, n)
        time.sleep(1.0)

        # 快速多轮:写 SHADOW → 即刻读 ODR
        hit = 0
        miss = 0
        for i in range(200):
            t.write32(SHADOW, 0x00000001)  # 写 1
            odr_val = t.read32(ODR)  # 即刻读 (不停留!)
            if odr_val & 1:  # bit0 = 1
                hit += 1
            else:
                miss += 1
            # 不 sleep,连续操作

        print(f'ODR bit0=1: {hit}/200')
        print(f'ODR bit0=0: {miss}/200')

        if hit > 0:
            print(f'[OK] 至少 {hit} 次 ODR=1 → DMA 工作!')
        else:
            print(f'[!!] ODR 从未出现 1 → DMA 可能没工作')
            print(f'    或 DMA 传输频率低 (10kHz),都被 ISR 覆盖了')


if __name__ == "__main__":
    main()
