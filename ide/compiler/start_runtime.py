#!/usr/bin/env python3
"""
DCL Runtime 启动器 - 后台启动Runtime
"""
import os
import sys
import subprocess

if __name__ == '__main__':
    runtime_dir = os.path.dirname(os.path.abspath(__file__))
    runtime_script = os.path.join(runtime_dir, 'dcl_runtime.py')
    python_exe = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    
    # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    
    proc = subprocess.Popen(
        [python_exe, runtime_script, 'start'],
        cwd=runtime_dir,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True
    )
    
    print(f'DCL Runtime started (PID: {proc.pid})')
