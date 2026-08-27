import subprocess, platform, os, sys

def _is_running(pid):
    print(f"Checking PID {pid}, platform: {platform.system()}")
    if pid is None:
        print("  -> PID is None, returning False")
        return False
    if platform.system() == "Windows":
        try:
            cmd = ['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH']
            print(f"  Running: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            print(f"  stdout: {result.stdout!r}")
            print(f"  pid in stdout: {str(pid) in result.stdout}")
            return str(pid) in result.stdout
        except Exception as e:
            print(f"  Exception: {e}")
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

# Test with known running PID
test_pid = int(open(r"D:\STM\work\dcl-controller\ide\compiler\dcl_monitor.pid").read())
print(f"PID from file: {test_pid}")
result = _is_running(test_pid)
print(f"Result: {result}")

# Also check with Get-Process
print(f"Get-Process check:")
r = subprocess.run(['tasklist', '/FI', f'PID eq {test_pid}', '/FO', 'CSV', '/NH'],
                   capture_output=True, text=True)
print(r.stdout)
