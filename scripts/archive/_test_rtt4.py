import subprocess, time, sys
proc = subprocess.Popen(
    ['py', '-3', '-m', 'pyocd', 'rtt', '-t', 'stm32h723xx', '-a', '0x20008000', '-s', '0x1000'],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

# Read line by line properly
start = time.time()
while time.time() - start < 3:
    l = proc.stdout.readline()
    if l and l.strip().startswith('S='):
        print(repr(l.strip()))
        break

# Read a few more
for _ in range(5):
    l = proc.stdout.readline()
    if l:
        s = l.strip()
        if s.startswith('S='):
            print(repr(s))

proc.terminate()
