import subprocess, sys, time, os
from pathlib import Path

BASE = Path(r"D:\STM\work\dcl-controller\ide\compiler")
PID_FILE = BASE / "dcl_monitor.pid"

# Clean start
PID_FILE.unlink(missing_ok=True)

# Use pythonw
pythonw = sys.executable.replace("python.exe", "pythonw.exe")
print(f"pythonw: {pythonw}, exists: {os.path.isfile(pythonw)}")

PROC = subprocess.Popen(
    [pythonw, str(BASE / "dcl_monitor.py"), "run"],
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    stdin=subprocess.DEVNULL,
)
print(f"Popen returned PID: {PROC.pid}")
PID_FILE.write_text(str(PROC.pid))
print(f"PID file written: {PID_FILE.read_text()}")
print(f"PID file exists: {PID_FILE.exists()}")
