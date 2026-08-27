#!/usr/bin/env python3
"""DCL IDE UART 通信测试"""
import serial
import struct
import time

PORT = 'COM11'
BAUD = 115200

def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc

def build_frame(cmd: int, payload: bytes = b'') -> bytes:
    """构建命令帧: [0xC0][CMD][LEN:2B][PAYLOAD][CRC16:2B]"""
    length = len(payload)
    frame = bytes([0xC0, cmd & 0xFF, length & 0xFF, (length >> 8) & 0xFF]) + payload
    crc = crc16_ccitt(frame[1:])  # CRC covers CMD+LEN+PAYLOAD
    frame += struct.pack('<H', crc)
    return frame

def parse_status_frame(data: bytes):
    """解析状态帧: [0xC1][STS][LEN:2B][PAYLOAD][CRC16:2B]"""
    if len(data) < 6:
        return None
    if data[0] != 0xC1:
        return None
    sts = data[1]
    length = data[2] | (data[3] << 8)
    payload = data[4:4+length]
    crc_recv = struct.unpack('<H', data[4+length:6+length])[0]
    crc_calc = crc16_ccitt(data[1:4+length])
    ok = crc_recv == crc_calc
    return {'status': sts, 'payload': payload, 'crc_ok': ok, 'raw': data.hex()}

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
print("DCL IDE UART 通信测试")
print(f"端口: {PORT} @ {BAUD}bps")
print("=" * 60)

# ── 测试1: STOP (确保引擎静止) ──
print("\n[TEST 1] STOP 命令")
resp = send_cmd(ser, 0x12)
r = parse_status_frame(resp) if resp else None
if r:
    print(f"  响应: STS=0x{r['status']:02X} CRC={'OK' if r['crc_ok'] else 'FAIL'}")
    print(f"  RAW: {r['raw']}")
else:
    print(f"  无响应: {resp.hex() if resp else 'timeout'}")

# ── 测试2: RESET ──
print("\n[TEST 2] RESET 命令")
resp = send_cmd(ser, 0x13)
r = parse_status_frame(resp) if resp else None
if r:
    print(f"  响应: STS=0x{r['status']:02X} CRC={'OK' if r['crc_ok'] else 'FAIL'}")
else:
    print(f"  无响应: {resp.hex() if resp else 'timeout'}")

# ── 测试3: READ WIRE[0..3] (应为0, 因为RESET了) ──
print("\n[TEST 3] READ WIRE[0..3] (after RESET)")
resp = send_cmd(ser, 0x20, struct.pack('<HH', 0, 4))
r = parse_status_frame(resp) if resp else None
if r:
    print(f"  响应: STS=0x{r['status']:02X} CRC={'OK' if r['crc_ok'] else 'FAIL'}")
    if r['payload'] and len(r['payload']) >= 16:
        vals = struct.unpack('<4f', r['payload'][:16])
        print(f"  WIRE[0..3] = [{vals[0]:.4f}, {vals[1]:.4f}, {vals[2]:.4f}, {vals[3]:.4f}]")
    else:
        print(f"  payload: {r['payload'].hex() if r['payload'] else 'empty'}")
else:
    print(f"  无响应: {resp.hex() if resp else 'timeout'}")

# ── 测试4: WRITE WIRE[0]=1.0 ──
print("\n[TEST 4] WRITE WIRE[0]=1.0")
resp = send_cmd(ser, 0x21, struct.pack('<Hf', 0, 1.0))
r = parse_status_frame(resp) if resp else None
if r:
    print(f"  响应: STS=0x{r['status']:02X} CRC={'OK' if r['crc_ok'] else 'FAIL'}")
else:
    print(f"  无响应: {resp.hex() if resp else 'timeout'}")

# ── 测试5: READ WIRE[0..3] (WIRE[0]应该=1.0) ──
print("\n[TEST 5] READ WIRE[0..3] (after WRITE)")
resp = send_cmd(ser, 0x20, struct.pack('<HH', 0, 4))
r = parse_status_frame(resp) if resp else None
if r:
    print(f"  响应: STS=0x{r['status']:02X} CRC={'OK' if r['crc_ok'] else 'FAIL'}")
    if r['payload'] and len(r['payload']) >= 16:
        vals = struct.unpack('<4f', r['payload'][:16])
        print(f"  WIRE[0..3] = [{vals[0]:.4f}, {vals[1]:.4f}, {vals[2]:.4f}, {vals[3]:.4f}]")
        if abs(vals[0] - 1.0) < 0.01:
            print("  ✅ WIRE[0]=1.0 验证通过!")
else:
    print(f"  无响应: {resp.hex() if resp else 'timeout'}")

# ── 测试6: START ──
print("\n[TEST 6] START 命令")
resp = send_cmd(ser, 0x11)
r = parse_status_frame(resp) if resp else None
if r:
    print(f"  响应: STS=0x{r['status']:02X} CRC={'OK' if r['crc_ok'] else 'FAIL'}")
else:
    print(f"  无响应: {resp.hex() if resp else 'timeout'}")

# ── 测试7: READ WIRE[0..3]多次(监控变化) ──
print("\n[TEST 7] 监控WIRE值 (READ 5次, 间隔200ms)")
for i in range(5):
    resp = send_cmd(ser, 0x20, struct.pack('<HH', 0, 4), timeout=0.3)
    r = parse_status_frame(resp) if resp else None
    if r and r['payload'] and len(r['payload']) >= 16:
        vals = struct.unpack('<4f', r['payload'][:16])
        print(f"  [{i}] WIRE[0..3] = [{vals[0]:.4f}, {vals[1]:.4f}, {vals[2]:.4f}, {vals[3]:.4f}]")
    time.sleep(0.2)

# ── 测试8: STOP ──
print("\n[TEST 8] STOP 命令")
resp = send_cmd(ser, 0x12)
r = parse_status_frame(resp) if resp else None
if r:
    print(f"  响应: STS=0x{r['status']:02X} CRC={'OK' if r['crc_ok'] else 'FAIL'}")
else:
    print(f"  无响应: {resp.hex() if resp else 'timeout'}")

ser.close()
print("\n" + "=" * 60)
print("测试完成")
