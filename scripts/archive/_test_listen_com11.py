import serial, time

ser = serial.Serial('COM11', 115200, timeout=3)
ser.reset_input_buffer()
print('Listening on COM11 (DAPLink) for 5 seconds...')
data = b''
t0 = time.time()
while time.time() - t0 < 5:
    chunk = ser.read(64)
    if chunk:
        data += chunk
        print(f'[{time.time()-t0:.2f}s] {chunk}')

if data:
    print(f'\nTotal: {len(data)} bytes')
    print(f'Text: {data.decode("ascii", errors="replace")}')
else:
    print('\nNo data received on COM11')

ser.close()
