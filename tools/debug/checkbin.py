import struct
fn = r'D:\STM\work\v3_l0\firmware\build/v3_l0.bin'
with open(fn,'rb') as f:
    b = f.read()
print(f'Bin size: {len(b)} bytes = 0x{len(b):X}')
offset = 0x640
if offset < len(b):
    print(f'bin[0x640..0x643] = {b[offset:offset+4].hex()}')
print(f'bin[0x2A4..0x2B0] = {b[0x2A4:0x2B0].hex()}')
