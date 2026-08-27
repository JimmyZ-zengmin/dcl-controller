import subprocess, time, sys
proc = subprocess.Popen(
    ['py', '-3', '-m', 'pyocd', 'rtt', '-t', 'stm32h723xx', '-a', '0x20008000', '-s', '0x1000'],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

time.sleep(2)

lines = []
for _ in range(100):
    l = proc.stdout.readline()
    if l:
        s = l.strip()
        if s.startswith('S='):
            lines.append(s)
        # Also print any non-S= lines to see what's happening
    if len(lines) >= 20:
        break

proc.terminate()

print(f"Got {len(lines)} status lines")
for l in lines[-15:]:
    print(l)

# Check if J= appears
has_j = any('J=' in l for l in lines)
print(f"\nJ= field present: {has_j}")
