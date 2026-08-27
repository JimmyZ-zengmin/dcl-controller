#!/usr/bin/env python3
"""Decode DMA2 S5CR bit fields and HISR flags."""
import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    t.reset_and_halt()
    t.resume()
    time.sleep(1.0)
    t.halt()

    s5cr = t.read32(0x40020488)
    hisr = t.read32(0x40020404)
    lisr = t.read32(0x40020400)
    s5ndtr = t.read32(0x4002048C)
    s5m0ar = t.read32(0x40020494)
    s5par  = t.read32(0x40020490)
    pc = t.read_core_register("pc")

    print(f'S5CR  = 0x{s5cr:08X}')
    print(f'  EN     = {s5cr & 1}')
    print(f'  DBM    = {(s5cr >> 19) & 1}')
    print(f'  PRIO   = {(s5cr >> 16) & 3}  (0=低 1=中 2=高 3=最高)')
    print(f'  MSIZE  = {(s5cr >> 13) & 3}  (0=8 1=16 2=32)')
    print(f'  PSIZE  = {(s5cr >> 11) & 3}  (0=8 1=16 2=32)')
    print(f'  MINC   = {(s5cr >> 10) & 1}  (memory increment)')
    print(f'  PINC   = {(s5cr >> 9) & 1}  (peripheral increment)')
    print(f'  CIRC   = {(s5cr >> 8) & 1}')
    print(f'  DIR    = {(s5cr >> 6) & 3}  (0=外设→内存 1=内存→外设 2=内存→内存)')
    print(f'  PFCTRL = {(s5cr >> 5) & 1}  (peripheral is flow controller)')
    print(f'  TCIE   = {(s5cr >> 4) & 1}')
    print(f'  HTIE   = {(s5cr >> 3) & 1}')
    print(f'  TEIE   = {(s5cr >> 2) & 1}')
    print(f'  DMEIE  = {(s5cr >> 1) & 1}')

    print(f'\nHISR  = 0x{hisr:08X}')
    print(f'  TCIF5  = {(hisr>>9) & 1}  (transfer complete)')
    print(f'  HTIF5  = {(hisr>>10) & 1}  (half transfer)')
    print(f'  TEIF5  = {(hisr>>11) & 1}  (transfer error)')
    print(f'  DMEIF5 = {(hisr>>2) & 1}  (direct mode error)')
    print(f'  FEIF5  = {(hisr>>6) & 1}  (FIFO error)')

    print(f'\nLISR  = 0x{lisr:08X}  (Stream1-4 flags)')

    print(f'\nS5NDTR = 0x{s5ndtr:08X}  (counter, decrements on each transfer)')
    print(f'S5M0AR = 0x{s5m0ar:08X}  (SHADOW_GPIO expect 0x200000E0)')
    print(f'S5PAR  = 0x{s5par:08X}  (GPIOE_ODR expect 0x58021014)')
    print(f'PC     = 0x{pc:08X}')
