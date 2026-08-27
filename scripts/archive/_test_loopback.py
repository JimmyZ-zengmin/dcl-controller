import serial, time

ser = serial.Serial('COM11', 115200, timeout=1)
ser.reset_input_buffer()

# Loopback test: send bytes, should get them back
test_data = bytes([0xC0, 0x12, 0x00, 0x00, 0x9F, 0xE1])
print(f"Sending: {test_data.hex()}")
ser.write(test_data)

time.sleep(0.2)
resp = ser.read(64)
print(f"Received: {resp.hex() if resp else '(nothing)'}")

if resp == test_data:
    print("LOOPBACK OK - DAPLink UART works!")
else:
    print("MISMATCH - data corrupted or no echo")

ser.close()
