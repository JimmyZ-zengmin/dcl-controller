import serial, time
ser = serial.Serial('COM11', 115200, timeout=3)
ser.reset_input_buffer()
print('Listening on COM11 for 3 seconds...')
data = ser.read(100)
print(f'Received {len(data)} bytes: {data.hex() if data else "(empty)"}')
ser.close()
