import struct, sys
# Read DTCM area containing WIRE_MAP and timing
PYOCD = r"C:\Espressif\tools\python\v6.0.1\venv\Scripts\pyocd.exe"
import subprocess
result = subprocess.run([PYOCD, "commander", "-t", "stm32h723xx", "-M", "attach",
                         "-c", "halt",
                         "-c", "read32 0x20000008",
                         "-c", "read32 0x2000000C",
                         "-c", "read32 0x20000010",
                         "-c", "read32 0x20000018",
                         "-c", "read32 0x200000F0",
                         "-c", "read32 0x20000400",
                         "-c", "read32 0x20000404",
                         "-c", "read32 0x20000408",
                         "-c", "read32 0x20000410",
                         "-c", "read32 0x20000414",
                         "-c", "read32 0x20000200",
                         "-c", "read32 0x2000A000",
                         "-c", "read32 0x2000A004",
                         "-c", "read32 0x2000A008",
                         "-c", "read32 0x2000A00C",
                         "-c", "read32 0x2000A010",
                         "-c", "read32 0x2000A014",
                         "-c", "resume",
                         "-c", "exit"],
                        capture_output=True, text=True)
print("stdout:")
print(result.stdout)
print("stderr (filtered):")
for line in result.stderr.split("\n"):
    if "coresight" in line.lower() or "error" in line.lower():
        print(line)

# Parse values
lines = result.stdout.split("\n")
vals = {}
for line in lines:
    line = line.strip()
    if ":" in line and "|" in line:
        parts = line.split(":")
        addr = int(parts[0], 16)
        hexval = parts[1].split("|")[0].strip()
        vals[addr] = int(hexval, 16)

def f(addr):
    if addr in vals:
        return struct.pack('<I', vals[addr])
    return 0.0
def fu(addr):
    if addr in vals:
        return vals[addr]
    return 0

print("\n=== DTCM State ===")
print(f"PERIOD_MIN    = 0x{fu(0x20000008):08X} = {fu(0x20000008)} cy = {fu(0x20000008)/544:.1f} us")
print(f"PERIOD_MAX    = 0x{fu(0x2000000C):08X} = {fu(0x2000000C)} cy = {fu(0x2000000C)/544:.1f} us")
print(f"SAMPLES       = {fu(0x20000010)}")
print(f"HEARTBEAT     = {fu(0x20000018)}")
print(f"ACTIVE_ROUTES = {fu(0x200000F0)}")
print(f"SENSOR[0]     = {struct.unpack('<f', struct.pack('<I', vals.get(0x20000200, 0)))[0]:.6f}")
print(f"WIRE[0]       = {struct.unpack('<f', struct.pack('<I', vals.get(0x20000400, 0)))[0]:.6f}  (exp 51.0 = 25*2+1)")
print(f"WIRE[1]       = {struct.unpack('<f', struct.pack('<I', vals.get(0x20000404, 0)))[0]:.6f}  (exp 51.0 = clamp 0,100)")
print(f"WIRE[2]       = {struct.unpack('<f', struct.pack('<I', vals.get(0x20000408, 0)))[0]:.6f}  (exp 1.0 = 51>50)")
print(f"WIRE[3]       = {struct.unpack('<f', struct.pack('<I', vals.get(0x20000410, 0)))[0]:.6f}  (exp ~2.5-5.1 = LPF α=0.1)")
print(f"WIRE[4]       = {struct.unpack('<f', struct.pack('<I', vals.get(0x20000414, 0)))[0]:.6f}  (exp 51.0 = DIRECT)")
print(f"PARAM[0].a    = {struct.unpack('<f', struct.pack('<I', vals.get(0x2000A000, 0)))[0]:.6f}  (exp 2.0)")
print(f"PARAM[0].b    = {struct.unpack('<f', struct.pack('<I', vals.get(0x2000A004, 0)))[0]:.6f}  (exp 1.0)")
print(f"PARAM[1].a    = {struct.unpack('<f', struct.pack('<I', vals.get(0x2000A008, 0)))[0]:.6f}  (exp 0.0)")
