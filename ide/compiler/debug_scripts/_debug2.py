import importlib.util
spec = importlib.util.spec_from_file_location(
    "m", r"D:\STM\work\dcl-controller\ide\compiler\dcl_monitor.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print("PID_FILE:", mod.PID_FILE)
print("exists:", mod.PID_FILE.exists())
print("get_pid:", mod._get_pid())
