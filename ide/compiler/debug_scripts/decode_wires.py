import struct

vals = [0x3f9d336c, 0x3dfb857a, 0x3f9d3365, 0x3dfb8579, 0x00000000, 0x4338383a, 0x43e4c599, 0x42c80000, 0x432ed0d7]
names = ['temp', 'clamped', 'max_val', 'min_val', 'abs_val', 'is_five', 'not_five', 'wire7', 'wire8']

for i, v in enumerate(vals):
    f = struct.unpack('f', struct.pack('I', v))[0]
    print(f"WIRE[{i}] ({names[i]:10s}) = {f:12.4f}  (0x{v:08x})")
