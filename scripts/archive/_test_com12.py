import serial, struct, time

PORT = 'COM12'
BAUD = 115200

def crc16_ccitt(data):
    crc = 0xFFFF
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            if crc & 0x8000: crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else: crc = (crc << 1) & 0xFFFF
    return crc

def build_frame(cmd, payload=b''):
    length = len(payload)
    frame = bytes([0xC0, cmd & 0xFF, length & 0xFF, (length >> 8) & 0xFF]) + payload
    crc = crc16_ccitt(frame[1:])
    frame += struct.pack('<H', crc)
    return frame

def parse_status_frame(data):
    if len(data) < 6 or data[0] != 0xC1: return None
    sts = data[1]
    length = data[2] | (data[3] << 8)
    payload = data[4:4+length]
    crc_recv = struct.unpack('<H', data[4+length:6+length])[0]
    crc_calc = crc16_ccitt(data[1:4+length])
    return {'status': sts, 'payload': payload, 'crc_ok': crc_recv == crc_calc, 'raw': data.hex()}

def send_cmd(ser, cmd, payload=b'', timeout=1.0):
    frame = build_frame(cmd, payload)
    ser.write(frame)
    time.sleep(0.1)
    resp = b''
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ser.in_waiting:
            resp += ser.read(ser.in_waiting)
            time.sleep(0.05)
        elif resp:
            break
    return resp

ser = serial.Serial(PORT, BAUD, timeout=0.5)
time.sleep(0.5)
ser.reset_input_buffer()

print('='*50)
print(f'DCL UART Test - {PORT}')
print('='*50)

# TEST 1: STOP
print('\n[1] STOP')
resp = send_cmd(ser, 0x12)
r = parse_status_frame(resp) if resp else None
if r: print(f'  OK: STS=0x{r["status"]:02X} CRC={"OK" if r["crc_ok"] else "FAIL"}')
else: print(f'  RAW: {resp.hex() if resp else "timeout"}')

# TEST 2: READ WIRE[0..3]
print('\n[2] READ WIRE[0..3]')
resp = send_cmd(ser, 0x20, struct.pack('<HH', 0, 4))
r = parse_status_frame(resp) if resp else None
if r and r['payload'] and len(r['payload']) >= 16:
    vals = struct.unpack('<4f', r['payload'][:16])
    print(f'  WIRE[0..3] = [{vals[0]:.4f}, {vals[1]:.4f}, {vals[2]:.4f}, {vals[3]:.4f}]')
elif r: print(f'  STS=0x{r["status"]:02X} payload={r["payload"].hex()}')
else: print(f'  RAW: {resp.hex() if resp else "timeout"}')

# TEST 3: WRITE WIRE[0]=2.5
print('\n[3] WRITE WIRE[0]=2.5')
resp = send_cmd(ser, 0x21, struct.pack('<Hf', 0, 2.5))
r = parse_status_frame(resp) if resp else None
if r: print(f'  OK: STS=0x{r["status"]:02X}')
else: print(f'  RAW: {resp.hex() if resp else "timeout"}')

# TEST 4: READ WIRE[0] again
print('\n[4] READ WIRE[0] (verify)')
resp = send_cmd(ser, 0x20, struct.pack('<HH', 0, 1))
r = parse_status_frame(resp) if resp else None
if r and r['payload'] and len(r['payload']) >= 4:
    v = struct.unpack('<f', r['payload'][:4])[0]
    print(f'  WIRE[0] = {v:.4f}')
    if abs(v - 2.5) < 0.01: print('  PASS!')
else: print(f'  RAW: {resp.hex() if resp else "timeout"}')

ser.close()
print('\nDone')
