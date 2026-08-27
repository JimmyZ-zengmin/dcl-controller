import subprocess, time, sys
proc = subprocess.Popen(
    ['py', '-3', '-m', 'pyocd', 'rtt', '-t', 'stm32h723xx', '-a', '0x20008000', '-s', '0x1000'],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
time.sleep(3)
lines = []
for _ in range(60):
    l = proc.stdout.readline()
    if l and l.strip().startswith('S='):
        lines.append(l.strip())
    if len(lines) >= 15:
        break
proc.terminate()
for l in lines:
    print(l)
