#!/usr/bin/env python3
"""
最小串口探测脚本 — 不改动固件,只测试物理链路能不能通
结果:
  - 物理回环 TX↔RX 能收到 echo → PC 端串口 + DAP-Link 桥正常,问题在固件
  - 无 echo → 换波特率 / COM 口 / DAP-Link 没有 VCP
"""
import serial
import serial.tools.list_ports
import struct
import time
import sys

print("=" * 60)
print("Step 1: 列出所有串口,找 'Flash Pro' 对应的 COM 口")
print("=" * 60)
ports = serial.tools.list_ports.comports()
flash_pro_com = None
for p in ports:
    print(f"  device={p.device}  desc={p.description}  hwid={p.hwid}")
    if 'flash pro' in p.description.lower() or 'flash pro' in p.device.lower():
        flash_pro_com = p.device
        print(f"  >>> 匹配到 Flash Pro COM 口: {flash_pro_com}")

if flash_pro_com is None:
    print("\n没有找到 'Flash Pro' 串口。可能的 COM 口列表:")
    for p in ports:
        print(f"  {p.device}: {p.description}")
    # 猜测一个
    if ports:
        flash_pro_com = ports[0].device
        print(f"\n猜测使用第一个 COM 口: {flash_pro_com}")
    else:
        print("\n没有找到任何串口!")
        sys.exit(1)

print(f"\n使用 COM 口: {flash_pro_com}")

# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("Step 2: 物理回环测试 (板子上 TX 和 RX 先短接)")
print("=" * 60)
print("(假设 TX/RX 已物理短接;如果不短接,Step 2 回不来属正常)")
time.sleep(0.5)

for baud in [115200, 9600, 1000000, 460800]:
    print(f"\n--- 测试 {baud} baud ---")
    try:
        ser = serial.Serial(flash_pro_com, baud, timeout=0.3)
        time.sleep(0.2)
        ser.reset_input_buffer()

        # 发 5 字节
        test_data = b'\xC0\x12\x00\x00\x55'
        ser.write(test_data)
        time.sleep(0.2)

        resp = ser.read(10)
        if resp:
            print(f"  收到 {len(resp)} 字节: {resp.hex()} → 物理层通了!")
        else:
            print(f"  无响应 (可能是 MCU 在跑固件没环回,或波特率不对)")
        ser.close()
    except Exception as e:
        print(f"  串口打开失败: {e}")

# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("Step 3: 尝试发送 CMD_READ (按固件当前支持的方式)")
print("=" * 60)

def crc16_ccitt(data, init=0xFFFF):
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc

def build_frame(cmd, payload=b''):
    length = len(payload)
    frame = bytes([0xC0, cmd & 0xFF, length & 0xFF, (length >> 8) & 0xFF]) + payload
    crc = crc16_ccitt(frame[1:])
    return frame + struct.pack('<H', crc)

# 恢复 115200 baud (固件当前的值)
baud = 115200
print(f"\n使用 {baud} baud, 发 CMD_READ WIRE[0..0]...")
try:
    ser = serial.Serial(flash_pro_com, baud, timeout=1.0)
    time.sleep(0.3)
    ser.reset_input_buffer()

    # CMD_READ: payload = [start:2B LE][count:2B LE]
    cmd = build_frame(0x20, struct.pack('<HH', 0, 1))
    print(f"  发送帧: {cmd.hex()}")
    ser.write(cmd)

    deadline = time.time() + 1.0
    buf = b''
    while time.time() < deadline:
        if ser.in_waiting:
            buf += ser.read(ser.in_waiting)
            if buf and buf[0] == 0xC1:
                sts = buf[1]
                length = buf[2] | (buf[3] << 8)
                total = 4 + length + 2
                if len(buf) >= total:
                    payload = buf[4:4 + length]
                    crc_r = struct.unpack('<H', buf[4 + length:total])[0]
                    crc_c = crc16_ccitt(buf[1:4 + length])
                    print(f"  收到响应! sts=0x{sts:02X} len={length} crc_ok={crc_r == crc_c}")
                    print(f"  响应原始数据: {buf[:total].hex()}")
                    if crc_r == crc_c and sts == 0x20:
                        start = payload[0] | (payload[1] << 8)
                        count = payload[2] | (payload[3] << 8)
                        off = 4
                        for i in range(count):
                            raw = payload[off:off + 4]
                            if len(raw) == 4:
                                val_f = struct.unpack('<f', raw)[0]
                                val_u = struct.unpack('<I', raw)[0]
                                print(f"  wire[{start + i}] = {val_f:.6f}  (raw 0x{val_u:08X})")
                            off += 4
                    break
            time.sleep(0.05)
    else:
        if buf:
            print(f"  收到乱码: {buf.hex()}")
        else:
            print(f"  无响应。MCU 未回 → 链路不通或 MCU 当前固件/引脚不对")
    ser.close()
except Exception as e:
    print(f"  串口错误: {e}")

print("\n" + "=" * 60)
print("结论:")
print("  如果 Step 2 物理回环有 echo → PC 串口链路 OK,问题在固件侧")
print("  如果 Step 2 完全无 echo → DAP-Link VCP 没接 / COM 不对 / 驱动没装")
print("=" * 60)
