import sys
sys.path.insert(0, r'D:\STM\work\dcl-controller\ide\compiler')
import dcl_monitor
print(f"PID_FILE = {dcl_monitor.PID_FILE}")
print(f"BASE_DIR = {dcl_monitor.BASE_DIR}")
print(f"get_pid() = {dcl_monitor._get_pid()}")
print(f"_is_running() = {dcl_monitor._is_running(dcl_monitor._get_pid())}")
