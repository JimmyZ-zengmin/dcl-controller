import serial, time

ser = serial.Serial('COM12', 115200, timeout=3)
ser.reset_input_buffer()
print('Listening on COM12 (CH340) for 5 seconds...')
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
    print('\nNo data received on COM12')

ser.close()
