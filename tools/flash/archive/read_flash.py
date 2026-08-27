#!/usr/bin/env python3
"""Read flash words around 0x0800052A."""
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx") as session:
    target = session.target

    # SystemInit at 0x08000450, 0x0800052A is 0xDA bytes in (thumb: 0xDA/2 = 6D instructions)
    for base in [0x08000450, 0x08000480, 0x080004B0, 0x080004E0, 0x08000510, 0x08000540]:
        vals = []
        for i in range(8):
            addr = base + i * 4
            b = target.read32(addr)
            vals.append(b)
            w1 = b & 0xFFFF
            w2 = (b >> 16) & 0xFFFF
            print(f"  0x{addr:08X}: 0x{w1:04X} 0x{w2:04X}")
        print()

    # Also dump the binary file for disassembly
    print("\n=== Dumping SystemInit ELF section ===")
    # read 0x200 bytes from 0x08000450
    data = bytearray()
    for offset in range(0, 0x200, 4):
        w = target.read32(0x08000450 + offset)
        data.extend(w.to_bytes(4, 'little'))
    # Write to file for objdump analysis
    with open('d:/STM/work/dcl-controller/tools/flash/sysinit.bin', 'wb') as f:
        f.write(data)
    print("Wrote 0x800 bytes to sysinit.bin")
