#!/usr/bin/env python3
"""校验 flash 内容与 .elf 是否对齐 — PC 在 0x08001F78,读该处 64 字节"""
import sys
sys.path.insert(0, r"C:\Users\min\AppData\Local\Programs\Python\Python311\Scripts")

PROBE = "000000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    t.halt()
    pc = t.read_core_register("pc")
    print(f"PC = 0x{pc:08X}")

    # Read 64 bytes from PC location
    blk = t.read_memory_block8(pc, 16)
    print(f"Flash @ 0x{pc:08X}:")
    for i in range(0, 16, 4):
        w = (blk[i] | (blk[i+1]<<8) | (blk[i+2]<<16) | (blk[i+3]<<24))
        print(f"  0x{pc+i:08X}: {blk[i]:02X} {blk[i+1]:02X} {blk[i+2]:02X} {blk[i+3]:02X}")

    # Also: is this still in old firmware at same offset (from doflash)
    # read PWR_D3CR again in runtime
    d3 = t.read32(0x58024818)
    print(f"PWR_D3CR @ runtime = 0x{d3:08X} (VOS[17:16]={(d3>>16)&3} RDY={(d3>>13)&1})")

    t.resume()
