import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "dcl_monitor", str(Path(r"D:\STM\work\dcl-controller\ide\compiler\dcl_monitor.py")))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

print(f"PID_FILE constant: {mod.PID_FILE}")
print(f"Calling cmd_start()...")
mod.cmd_start()
print(f"After cmd_start, PID exists: {mod.PID_FILE.exists()}")
if mod.PID_FILE.exists():
    print(f"Content: {mod.PID_FILE.read_text()}")
