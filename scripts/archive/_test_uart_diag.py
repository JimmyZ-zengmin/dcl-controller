import serial, struct, time

def crc16_ccitt(data):
    crc = 0xFFFF
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc

def build_frame(cmd, payload=b''):
    length = len(payload)
    frame = bytes([0xC0, cmd & 0xFF, length & 0xFF, (length >> 8) & 0xFF]) + payload
    crc = crc16_ccitt(frame[1:])
    frame += struct.pack('<H', crc)
    return frame

ser = serial.Serial('COM11', 115200, timeout=0.1)
ser.reset_input_buffer()

# Send STOP command
frame = build_frame(0x12)
print(f"Sending STOP: {frame.hex()}")
ser.write(frame)

# Collect ALL bytes for 2 seconds
all_data = b''
t0 = time.time()
while time.time() - t0 < 2.0:
    chunk = ser.read(64)
    if chunk:
        all_data += chunk
        print(f"  +{len(chunk)}B @ {time.time()-t0:.3f}s: {chunk.hex()}")

if all_data:
    print(f"\nTotal received: {len(all_data)} bytes")
    print(f"Hex: {all_data.hex()}")
    # Check for 0xC1 (proper response header)
    if 0xC1 in all_data:
        idx = all_data.index(0xC1)
        print(f"Found 0xC1 at offset {idx} - potential response!")
    else:
        print("No 0xC1 found - no valid response frame")
else:
    print("\nNo data received at all")

ser.close()
