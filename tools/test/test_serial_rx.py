#!/usr/bin/env python3
"""
test_serial_rx.py — 非侵入式串口接收验证

验证目标：确认板子能主动往串口发送数据
方法：只读串口，不做任何写入操作（零侵入）

用法：
  python test_serial_rx.py COM3
  python test_serial_rx.py /dev/ttyUSB0
"""

import sys
import struct
import serial
import time

# 帧协议定义（与固件一致）
STS_STREAM_DATA = 0x50
FRAME_STS = 0xC1

def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT 校验"""
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def parse_frame(data: bytes):
    """解析状态帧，返回 (status_code, payload) 或 None"""
    if len(data) < 6:
        return None

    marker = data[0]
    if marker != FRAME_STS:
        return None

    status_code = data[1]
    payload_len = struct.unpack_from('<H', data, 2)[0]
    expected_len = 1 + 1 + 2 + payload_len + 2

    if len(data) < expected_len:
        return None

    payload = data[4:4 + payload_len]
    received_crc = struct.unpack_from('<H', data, 4 + payload_len)[0]

    # 验证CRC
    crc_data = struct.pack('<BH', status_code, payload_len) + payload
    computed_crc = crc16_ccitt(crc_data)

    if received_crc != computed_crc:
        return None

    return (status_code, payload)


def main():
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <串口>")
        print(f"示例: {sys.argv[0]} COM3")
        sys.exit(1)

    port = sys.argv[1]
    baudrate = 115200

    print(f"[*] 连接 {port} @ {baudrate}bps ...")
    try:
        ser = serial.Serial(port, baudrate, timeout=0.1)
    except serial.SerialException as e:
        print(f"[!] 打开串口失败: {e}")
        sys.exit(1)

    print("[*] 等待数据（只读模式，零侵入）...")
    print("[*] 按 Ctrl+C 停止\n")

    buffer = bytearray()
    frame_count = 0
    error_count = 0
    start_time = time.time()

    try:
        while True:
            # 只读，不写！
            raw = ser.read(64)
            if raw:
                buffer.extend(raw)

                # 尝试解析帧
                while True:
                    # 查找帧头
                    try:
                        idx = buffer.index(FRAME_STS)
                    except ValueError:
                        break

                    if idx > 0:
                        del buffer[:idx]

                    if len(buffer) < 4:
                        break

                    payload_len = struct.unpack_from('<H', buffer, 2)[0]
                    frame_len = 1 + 1 + 2 + payload_len + 2

                    if len(buffer) < frame_len:
                        break

                    frame_data = bytes(buffer[:frame_len])
                    del buffer[:frame_len]

                    result = parse_frame(frame_data)
                    if result:
                        status_code, payload = result
                        frame_count += 1

                        if status_code == STS_STREAM_DATA and len(payload) >= 5:
                            wire_idx = payload[0]
                            value = struct.unpack_from('<f', payload, 1)[0]
                            elapsed = time.time() - start_time
                            print(f"[{elapsed:7.2f}s] WIRE[{wire_idx:3d}] = {value:10.4f}  (帧#{frame_count})")
                        else:
                            print(f"[{elapsed:7.2f}s] 状态=0x{status_code:02X} 负载={len(payload)}B")
                    else:
                        error_count += 1
                        print(f"[!] CRC错误 (累计{error_count}个)")

            time.sleep(0.001)  # 1ms轮询

    except KeyboardInterrupt:
        print(f"\n[*] 停止")
        print(f"[*] 总计: {frame_count} 帧, {error_count} 错误, {time.time()-start_time:.1f}秒")
        if frame_count > 0:
            print(f"[*] 平均速率: {frame_count / (time.time()-start_time):.1f} 帧/秒")

    ser.close()


if __name__ == '__main__':
    main()
