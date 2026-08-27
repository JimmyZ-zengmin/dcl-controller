import serial, time

ser = serial.Serial('COM12', 115200, timeout=1)
ser.reset_input_buffer()

test = bytes([0xC0, 0x12, 0x00, 0x00, 0x9F, 0xE1])
print(f"Sending: {test.hex()}")
ser.write(test)
time.sleep(0.3)
resp = ser.read(64)
print(f"Received: {resp.hex() if resp else '(nothing)'}")

if resp == test:
    print("LOOPBACK OK")
elif resp:
    print(f"Got {len(resp)} bytes (not loopback)")
else:
    print("NO DATA - check wiring")

ser.close()
