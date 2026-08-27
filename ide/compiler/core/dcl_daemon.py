#!/usr/bin/env python3
"""
守护进程 - 常驻后台，保持硬件连接，处理多客户端
"""

import os
import sys
import json
import time
import threading
import socket
import signal
from typing import Dict, List, Optional
from pathlib import Path

from .dcl_output import output, success, error, human, header
from .dcl_hardware import Hardware, get_hardware
from .dcl_session import DclSession

# IPC配置
PIPE_DIR = r'\\.\pipe'
PIPE_NAME = 'dcl_daemon'
PORT = 8766  # TCP端口作为备选

# PID文件
PID_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'dcl_daemon.pid')

class DclDaemon:
    """DCL守护进程"""
    
    def __init__(self):
        self.running = False
        self.hw = get_hardware()
        self.session_mgr = DclSession()
        self._server = None
        self._thread = None
        self._clients: List[socket.socket] = []
    
    def start(self) -> int:
        """启动守护进程"""
        # 检查是否已经运行
        if self._is_running():
            return error("守护进程已在运行")
        
        human("正在启动DCL守护进程...")
        
        # 创建PID文件
        with open(PID_FILE, 'w') as f:
            f.write(str(os.getpid()))
        
        # 连接硬件
        if not self.hw.connect():
            human("警告: 硬件未连接，将继续等待...")
        
        self.running = True
        
        # 启动TCP服务器
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(('localhost', PORT))
        self._server.listen(5)
        self._server.settimeout(1.0)
        
        self._thread = threading.Thread(target=self._server_loop, daemon=True)
        self._thread.start()
        
        human(f"守护进程已启动 (PID: {os.getpid()}, PORT: {PORT})")
        human("等待客户端连接...")
        
        # 主循环
        try:
            while self.running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        
        self._cleanup()
        return 0
    
    def stop(self) -> int:
        """停止守护进程"""
        if not self._is_running():
            return error("守护进程未运行")
        
        # 读取PID
        with open(PID_FILE, 'r') as f:
            pid = int(f.read().strip())
        
        human(f"正在停止守护进程 (PID: {pid})...")
        
        try:
            # 发送停止信号
            if sys.platform == 'win32':
                import ctypes
                ctypes.windll.kernel32.GenerateConsoleCtrlEvent(0, pid)
            else:
                os.kill(pid, signal.SIGTERM)
        except:
            pass
        
        # 等待进程结束
        time.sleep(2)
        
        # 清理PID文件
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        
        return success("守护进程已停止")
    
    def restart(self) -> int:
        """重启守护进程"""
        self.stop()
        time.sleep(1)
        return self.start()
    
    def status(self) -> int:
        """守护进程状态"""
        running = self._is_running()
        
        data = {
            'running': running,
            'hardware_connected': self.hw.connected,
            'sessions': len(self.session_mgr.sessions),
        }
        
        if running:
            with open(PID_FILE, 'r') as f:
                data['pid'] = int(f.read().strip())
        
        return output(data)
    
    def _is_running(self) -> bool:
        """检查守护进程是否运行"""
        if not os.path.exists(PID_FILE):
            return False
        
        with open(PID_FILE, 'r') as f:
            pid = int(f.read().strip())
        
        # 检查进程是否存在
        try:
            if sys.platform == 'win32':
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    return True
            else:
                os.kill(pid, 0)
                return True
        except:
            pass
        
        # PID文件存在但进程不存在，清理
        os.remove(PID_FILE)
        return False
    
    def _server_loop(self):
        """服务器主循环"""
        while self.running:
            try:
                client, addr = self._server.accept()
                human(f"客户端已连接: {addr}")
                self._clients.append(client)
                
                # 为客户端创建处理线程
                thread = threading.Thread(
                    target=self._client_handler,
                    args=(client, addr),
                    daemon=True
                )
                thread.start()
            except socket.timeout:
                continue
            except:
                if self.running:
                    human("服务器错误")
    
    def _client_handler(self, client: socket.socket, addr):
        """处理客户端连接"""
        try:
            while self.running:
                # 接收命令
                data = client.recv(4096)
                if not data:
                    break
                
                try:
                    request = json.loads(data.decode('utf-8'))
                    response = self._handle_command(request)
                except Exception as e:
                    response = {'success': False, 'error': str(e)}
                
                # 发送响应
                client.send(json.dumps(response).encode('utf-8'))
        except:
            pass
        finally:
            human(f"客户端已断开: {addr}")
            self._clients.remove(client)
            client.close()
    
    def _handle_command(self, request: Dict) -> Dict:
        """处理客户端命令"""
        cmd = request.get('command')
        args = request.get('args', {})
        
        # 命令路由
        if cmd == 'compile':
            ok, result = compile_file(args.get('input'), args.get('output'))
            return {'success': ok, **result}
        
        elif cmd == 'deploy':
            with open(args.get('binary'), 'rb') as f:
                data = f.read()
            ok = self.hw.deploy(data)
            return {'success': ok, 'error': self.hw.last_error if not ok else None}
        
        elif cmd == 'read':
            values = self.hw.read_wires(args.get('start', 0), args.get('count', 16))
            return {'success': values is not None, 'values': values}
        
        elif cmd == 'write':
            ok = self.hw.write_wire(args.get('index'), args.get('value'))
            return {'success': ok, 'error': self.hw.last_error if not ok else None}
        
        elif cmd == 'status':
            return {'success': True, 'hardware': self.hw.get_status()}
        
        elif cmd == 'broadcast':
            # 向所有客户端广播事件
            self._broadcast(args)
            return {'success': True}
        
        else:
            return {'success': False, 'error': f'未知命令: {cmd}'}
    
    def _broadcast(self, event: Dict):
        """向所有客户端广播事件"""
        data = json.dumps(event).encode('utf-8')
        for client in self._clients[:]:
            try:
                client.send(data)
            except:
                # 客户端已断开
                self._clients.remove(client)
    
    def _cleanup(self):
        """清理资源"""
        human("\n正在清理...")
        
        if self._server:
            self._server.close()
        
        for client in self._clients:
            try:
                client.close()
            except:
                pass
        
        self._clients.clear()
        
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        
        self.hw.disconnect()
        human("守护进程已退出")

def daemon_main():
    """守护进程入口点"""
    daemon = DclDaemon()
    
    # 信号处理
    def signal_handler(sig, frame):
        daemon.running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    sys.exit(daemon.start())

if __name__ == '__main__':
    daemon_main()
