"""
原始串口读取测试 - 查看CH340是否有任何数据
"""
import sys
import serial
import time

port = sys.argv[1] if len(sys.argv) > 1 else "COM12"
baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200

print(f"[*] 打开 {port} @ {baud}bps ...")
print(f"[*] 按 Ctrl+C 停止\n")

try:
    ser = serial.Serial(port, baudrate=baud, timeout=0.1)
except Exception as e:
    print(f"[ERROR] 无法打开串口: {e}")
    sys.exit(1)

print(f"[*] 串口已打开: {ser.name}")
print(f"[*] 等待数据...\n")

start = time.time()
byte_count = 0
try:
    while True:
        data = ser.read(64)  # 读取最多64字节
        if data:
            elapsed = time.time() - start
            byte_count += len(data)
            # 打印十六进制和ASCII
            hex_str = ' '.join(f'{b:02X}' for b in data)
            ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)
            print(f"[{elapsed:7.3f}s] ({len(data):3d}B) {hex_str}")
            print(f"           ASCII: {ascii_str}")
except KeyboardInterrupt:
    print(f"\n[*] 停止. 总计收到 {byte_count} 字节")
    ser.close()
