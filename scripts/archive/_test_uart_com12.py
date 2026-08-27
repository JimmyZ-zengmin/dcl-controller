import serial, struct, time

PORT = 'COM12'
BAUD = 115200

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

def parse_status_frame(data):
    if len(data) < 6 or data[0] != 0xC1:
        return None
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

print("=" * 60)
print(f"DCL UART Test - {PORT} @ {BAUD}bps")
print("=" * 60)

# TEST 1: STOP
print("\n[1] STOP")
resp = send_cmd(ser, 0x12)
r = parse_status_frame(resp) if resp else None
if r:
    print(f"  OK: STS=0x{r['status']:02X} CRC={'OK' if r['crc_ok'] else 'FAIL'}")
else:
    print(f"  RAW: {resp.hex() if resp else 'timeout'}")

# TEST 2: RESET
print("\n[2] RESET")
resp = send_cmd(ser, 0x13)
r = parse_status_frame(resp) if resp else None
if r:
    print(f"  OK: STS=0x{r['status']:02X} CRC={'OK' if r['crc_ok'] else 'FAIL'}")
else:
    print(f"  RAW: {resp.hex() if resp else 'timeout'}")

# TEST 3: READ WIRE[0..3]
print("\n[3] READ WIRE[0..3]")
resp = send_cmd(ser, 0x20, struct.pack('<HH', 0, 4))
r = parse_status_frame(resp) if resp else None
if r and r['payload'] and len(r['payload']) >= 16:
    vals = struct.unpack('<4f', r['payload'][:16])
    print(f"  WIRE[0..3] = [{vals[0]:.4f}, {vals[1]:.4f}, {vals[2]:.4f}, {vals[3]:.4f}]")
elif r:
    print(f"  STS=0x{r['status']:02X} payload={r['payload'].hex()}")
else:
    print(f"  RAW: {resp.hex() if resp else 'timeout'}")

# TEST 4: WRITE WIRE[0]=1.0
print("\n[4] WRITE WIRE[0]=1.0")
resp = send_cmd(ser, 0x21, struct.pack('<Hf', 0, 1.0))
r = parse_status_frame(resp) if resp else None
if r:
    print(f"  OK: STS=0x{r['status']:02X} CRC={'OK' if r['crc_ok'] else 'FAIL'}")
else:
    print(f"  RAW: {resp.hex() if resp else 'timeout'}")

# TEST 5: READ WIRE[0..3] again
print("\n[5] READ WIRE[0..3] (after WRITE)")
resp = send_cmd(ser, 0x20, struct.pack('<HH', 0, 4))
r = parse_status_frame(resp) if resp else None
if r and r['payload'] and len(r['payload']) >= 16:
    vals = struct.unpack('<4f', r['payload'][:16])
    print(f"  WIRE[0..3] = [{vals[0]:.4f}, {vals[1]:.4f}, {vals[2]:.4f}, {vals[3]:.4f}]")
    if abs(vals[0] - 1.0) < 0.01:
        print("  WIRE[0]=1.0 PASS!")
else:
    print(f"  RAW: {resp.hex() if resp else 'timeout'}")

# TEST 6: START
print("\n[6] START")
resp = send_cmd(ser, 0x11)
r = parse_status_frame(resp) if resp else None
if r:
    print(f"  OK: STS=0x{r['status']:02X} CRC={'OK' if r['crc_ok'] else 'FAIL'}")
else:
    print(f"  RAW: {resp.hex() if resp else 'timeout'}")

# TEST 7: Monitor WIRE changes
print("\n[7] Monitor WIRE[0..3] (5 reads)")
for i in range(5):
    resp = send_cmd(ser, 0x20, struct.pack('<HH', 0, 4), timeout=0.3)
    r = parse_status_frame(resp) if resp else None
    if r and r['payload'] and len(r['payload']) >= 16:
        vals = struct.unpack('<4f', r['payload'][:16])
        print(f"  [{i}] [{vals[0]:.4f}, {vals[1]:.4f}, {vals[2]:.4f}, {vals[3]:.4f}]")
    time.sleep(0.2)

# TEST 8: STOP
print("\n[8] STOP")
resp = send_cmd(ser, 0x12)
r = parse_status_frame(resp) if resp else None
if r:
    print(f"  OK: STS=0x{r['status']:02X} CRC={'OK' if r['crc_ok'] else 'FAIL'}")
else:
    print(f"  RAW: {resp.hex() if resp else 'timeout'}")

ser.close()
print("\n" + "=" * 60)
print("Done")
