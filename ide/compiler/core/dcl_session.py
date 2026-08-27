#!/usr/bin/env python3
"""
会话管理 - 支持多会话、状态持久化、事件广播
"""

import os
import json
import time
import threading
from typing import Dict, List, Optional, Any
from pathlib import Path

from .dcl_output import output, success, error, human, header, table
from .dcl_hardware import Hardware, get_hardware
from .dcl_compiler import compile_file, compile_source

# 会话存储目录
SESSIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sessions')
CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config')

def _ensure_dirs():
    """确保目录存在"""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)

_ensure_dirs()

class Session:
    """单个会话的状态"""
    
    def __init__(self, name: str):
        self.name = name
        self.created = time.time()
        self.last_active = time.time()
        self.current_project: Optional[str] = None
        self.last_compile: Optional[Dict] = None
        self.last_deploy: Optional[Dict] = None
        self.watched_signals: List[str] = []
        self.history: List[Dict] = []
    
    def touch(self):
        self.last_active = time.time()
    
    def to_dict(self) -> Dict:
        return {
            'name': self.name,
            'created': self.created,
            'last_active': self.last_active,
            'current_project': self.current_project,
            'last_compile': self.last_compile,
            'last_deploy': self.last_deploy,
            'watched_signals': self.watched_signals,
        }
    
    def add_history(self, action: str, data: Dict):
        self.history.append({
            'action': action,
            'timestamp': time.time(),
            'data': data,
        })
        # 只保留最近100条
        if len(self.history) > 100:
            self.history = self.history[-100:]

class SessionManager:
    """会话管理器"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self.sessions: Dict[str, Session] = {}
        self.current_session: str = 'default'
        self._listeners: Dict[str, List[callable]] = {}
        
        # 加载或创建默认会话
        self._load_session('default')
    
    def _session_file(self, name: str) -> str:
        return os.path.join(SESSIONS_DIR, f'{name}.json')
    
    def _save_session(self, name: str):
        """保存会话到文件"""
        session = self.sessions.get(name)
        if not session:
            return
        
        data = session.to_dict()
        data['history'] = session.history[-20:]  # 只保存最近20条历史
        
        with open(self._session_file(name), 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def _load_session(self, name: str) -> Session:
        """从文件加载会话"""
        session_file = self._session_file(name)
        
        if os.path.exists(session_file):
            with open(session_file, 'r') as f:
                data = json.load(f)
            
            session = Session(name)
            session.created = data.get('created', time.time())
            session.last_active = data.get('last_active', time.time())
            session.current_project = data.get('current_project')
            session.last_compile = data.get('last_compile')
            session.last_deploy = data.get('last_deploy')
            session.watched_signals = data.get('watched_signals', [])
            session.history = data.get('history', [])
        else:
            session = Session(name)
        
        self.sessions[name] = session
        return session
    
    def _emit(self, event: str, data: Dict):
        """广播事件"""
        for callback in self._listeners.get(event, []):
            try:
                callback(data)
            except:
                pass
    
    def on(self, event: str, callback: callable):
        """订阅事件"""
        if event not in self._listeners:
            self._listeners[event] = []
        self._listeners[event].append(callback)
    
    def off(self, event: str, callback: callable):
        """取消订阅"""
        if event in self._listeners:
            self._listeners[event] = [c for c in self._listeners[event] if c != callback]
    
    # ═════════════════════════════════════════════════════════════════════════
    # 会话操作
    # ═════════════════════════════════════════════════════════════════════════
    
    def create(self, name: Optional[str] = None) -> int:
        """创建新会话"""
        if name is None:
            # 自动命名
            idx = 1
            while f'session_{idx}' in self.sessions:
                idx += 1
            name = f'session_{idx}'
        
        if name in self.sessions:
            return error(f"会话已存在: {name}")
        
        session = self._load_session(name)
        self._save_session(name)
        
        return success(f"会话已创建: name={name}", {'session': session.to_dict()})
    
    def list_sessions(self) -> int:
        """列出所有会话"""
        # 扫描所有会话文件
        for f in os.listdir(SESSIONS_DIR):
            if f.endswith('.json'):
                name = f[:-5]  # 去掉.json
                if name not in self.sessions:
                    self._load_session(name)
        
        data = []
        for name, session in self.sessions.items():
            data.append({
                'name': name,
                'project': session.current_project,
                'last_active': session.last_active,
                'is_current': name == self.current_session,
            })
        
        return output({
            'sessions': data,
            'current': self.current_session,
        })
    
    def switch(self, name: str) -> int:
        """切换会话"""
        if name not in self.sessions:
            # 尝试加载
            self._load_session(name)
        
        if name not in self.sessions:
            return error(f"会话不存在: {name}")
        
        self.current_session = name
        self.sessions[name].touch()
        self._save_session(name)
        
        return success(f"已切换到会话: {name}")
    
    def info(self) -> int:
        """当前会话信息"""
        session = self.sessions.get(self.current_session)
        if not session:
            return error("当前会话不存在")
        
        return output({'session': session.to_dict()})
    
    def get_current(self) -> Session:
        """获取当前会话"""
        return self.sessions.get(self.current_session, Session('default'))
    
    # ═════════════════════════════════════════════════════════════════════════
    # 项目管理
    # ═════════════════════════════════════════════════════════════════════════
    
    def project_open(self, path: str) -> int:
        """打开项目"""
        session = self.get_current()
        
        if not os.path.exists(path):
            return error(f"路径不存在: {path}")
        
        session.current_project = os.path.abspath(path)
        session.touch()
        self._save_session(session.name)
        
        self._emit('project_opened', {
            'session': session.name,
            'project': session.current_project,
        })
        
        return success(f"项目已打开: {session.current_project}")
    
    def project_save(self) -> int:
        """保存项目"""
        session = self.get_current()
        
        if not session.current_project:
            return error("没有打开的项目")
        
        # 这里可以添加项目文件的保存逻辑
        session.touch()
        self._save_session(session.name)
        
        return success("项目已保存")
    
    def project_close(self) -> int:
        """关闭项目"""
        session = self.get_current()
        
        if not session.current_project:
            return error("没有打开的项目")
        
        project = session.current_project
        session.current_project = None
        session.touch()
        self._save_session(session.name)
        
        self._emit('project_closed', {
            'session': session.name,
            'project': project,
        })
        
        return success("项目已关闭")
    
    def project_info(self) -> int:
        """项目信息"""
        session = self.get_current()
        
        return output({
            'project': session.current_project,
            'session': session.name,
        })
    
    # ═════════════════════════════════════════════════════════════════════════
    # 编译/部署/运行
    # ═════════════════════════════════════════════════════════════════════════
    
    def compile(self, input_file: str, output_file: Optional[str] = None) -> int:
        """编译"""
        session = self.get_current()
        
        if not os.path.isabs(input_file) and session.current_project:
            input_file = os.path.join(session.current_project, input_file)
        
        if output_file and not os.path.isabs(output_file):
            # 只使用文件名部分，避免路径重复
            output_file = os.path.join(os.path.dirname(input_file), os.path.basename(output_file))
        
        ok, result = compile_file(input_file, output_file)
        
        if ok:
            session.last_compile = result
            session.touch()
            session.add_history('compile', result)
            self._save_session(session.name)
            
            self._emit('compiled', {
                'session': session.name,
                'result': result,
            })
            
            return success("编译成功", result)
        else:
            return error(result['error'], suggestion="检查DCL语法")
    
    def deploy(self, binary_file: str) -> int:
        """部署"""
        session = self.get_current()
        
        if not os.path.isabs(binary_file) and session.current_project:
            binary_file = os.path.join(session.current_project, binary_file)
        
        if not os.path.exists(binary_file):
            return error(f"文件不存在: {binary_file}")
        
        with open(binary_file, 'rb') as f:
            data = f.read()
        
        hw = get_hardware()
        
        if not hw.connect():
            return error("无法连接硬件", suggestion="检查硬件连接")
        
        if hw.deploy(data):
            result = {'binary': binary_file, 'size': len(data)}
            session.last_deploy = result
            session.touch()
            session.add_history('deploy', result)
            self._save_session(session.name)
            
            self._emit('deployed', {
                'session': session.name,
                'result': result,
            })
            
            return success("部署成功", result)
        else:
            return error(f"部署失败: {hw.last_error}")
    
    def run(self, input_file: str) -> int:
        """编译并运行"""
        session = self.get_current()
        
        # 编译
        if not os.path.isabs(input_file) and session.current_project:
            input_file = os.path.join(session.current_project, input_file)
        
        output_file = input_file.replace('.dcl', '.bin')
        
        ok, result = compile_file(input_file, output_file)
        
        if not ok:
            return error(result['error'])
        
        session.last_compile = result
        session.touch()
        
        # 部署
        with open(output_file, 'rb') as f:
            data = f.read()
        
        hw = get_hardware()
        
        if not hw.connect():
            return error("无法连接硬件")
        
        if hw.deploy(data):
            deploy_result = {'binary': output_file, 'size': len(data)}
            session.last_deploy = deploy_result
            session.add_history('run', {'compile': result, 'deploy': deploy_result})
            self._save_session(session.name)
            
            self._emit('run_complete', {
                'session': session.name,
                'compile': result,
                'deploy': deploy_result,
            })
            
            return success("运行成功", {
                'compile': result,
                'deploy': deploy_result,
            })
        else:
            return error(f"部署失败: {hw.last_error}")
    
    # ═════════════════════════════════════════════════════════════════════════
    # 读写WIRE
    # ═════════════════════════════════════════════════════════════════════════
    
    def read(self, target: str) -> int:
        """读取WIRE/SENSOR"""
        import re
        
        session = self.get_current()
        
        m = re.match(r'(wire|sensor)\[(\d+)(?::(\d+))?\]', target)
        if not m:
            return error("格式错误", suggestion="使用 wire[0:10] 或 sensor[0:4]")
        
        mem_type = m.group(1)
        start = int(m.group(2))
        end = int(m.group(3)) if m.group(3) else start + 1
        
        hw = get_hardware()
        
        if not hw.connect():
            return error("无法连接硬件")
        
        if mem_type == 'wire':
            values = hw.read_wires(start, end - start)
        else:
            values = hw.read_sensors(start, end - start)
        
        if values is None:
            return error("读取失败", details={'error': hw.last_error})
        
        return output({
            'target': target,
            'values': values,
            'count': len(values),
        })
    
    def write(self, target: str, value: str) -> int:
        """写入WIRE"""
        import re
        
        session = self.get_current()
        
        m = re.match(r'wire\[(\d+)\]', target)
        if not m:
            return error("格式错误", suggestion="使用 wire[5]")
        
        idx = int(m.group(1))
        
        hw = get_hardware()
        
        if not hw.connect():
            return error("无法连接硬件")
        
        if hw.write_wire(idx, float(value)):
            result = {
                'target': target,
                'value': float(value),
            }
            
            self._emit('wire_written', {
                'session': session.name,
                'result': result,
            })
            
            return success("写入成功", result)
        else:
            return error("写入失败", details={'error': hw.last_error})
    
    # ═════════════════════════════════════════════════════════════════════════
    # 监控
    # ═════════════════════════════════════════════════════════════════════════
    
    def monitor(self, rate: int = 100, signals: Optional[str] = None) -> int:
        """监控模式"""
        session = self.get_current()
        
        if signals:
            session.watched_signals = [s.strip() for s in signals.split(',')]
        
        human("开始监控 (Ctrl+C 停止)")
        human(f"采样率: {rate}ms")
        human(f"信号: {signals if signals else 'all'}")
        human("-" * 60)
        
        hw = get_hardware()
        
        if not hw.connect():
            return error("无法连接硬件")
        
        try:
            while True:
                values = hw.read_wires(0, 16)
                
                if values:
                    timestamp = time.strftime('%H:%M:%S')
                    human(f"[{timestamp}] {[round(v, 4) for v in values]}")
                
                time.sleep(rate / 1000.0)
        
        except KeyboardInterrupt:
            human("")
            human("监控已停止")
        
        return 0
    
    # ═════════════════════════════════════════════════════════════════════════
    # 状态
    # ═════════════════════════════════════════════════════════════════════════
    
    def status(self) -> int:
        """系统状态"""
        session = self.get_current()
        
        hw = get_hardware()
        hw_status = hw.get_status()
        
        return output({
            'hardware': hw_status,
            'session': session.to_dict(),
        })
    
    # ═════════════════════════════════════════════════════════════════════════
    # 交互式REPL
    # ═════════════════════════════════════════════════════════════════════════
    
    def shell(self) -> int:
        """交互式REPL"""
        session = self.get_current()
        
        header(f"DCL Shell - {session.name}")
        human("输入 'help' 查看命令, 'exit' 退出")
        human("")
        
        while True:
            try:
                line = input("dcl> ").strip()
                if not line:
                    continue
                if line in ['exit', 'quit']:
                    break
                if line == 'help':
                    self._shell_help()
                    continue
                
                # 解析命令
                parts = line.split()
                cmd = parts[0]
                cmd_args = parts[1:]
                
                if cmd == 'compile':
                    if len(cmd_args) < 1:
                        human("用法: compile <file.dcl> [-o output.bin]")
                        continue
                    output_file = None
                    if '-o' in cmd_args:
                        idx = cmd_args.index('-o')
                        if idx + 1 < len(cmd_args):
                            output_file = cmd_args[idx + 1]
                    self.compile(cmd_args[0], output_file)
                
                elif cmd == 'deploy':
                    if len(cmd_args) < 1:
                        human("用法: deploy <file.bin>")
                        continue
                    self.deploy(cmd_args[0])
                
                elif cmd == 'run':
                    if len(cmd_args) < 1:
                        human("用法: run <file.dcl>")
                        continue
                    self.run(cmd_args[0])
                
                elif cmd == 'read':
                    if len(cmd_args) < 1:
                        human("用法: read wire[0:10]")
                        continue
                    self.read(cmd_args[0])
                
                elif cmd == 'write':
                    if len(cmd_args) < 2:
                        human("用法: write wire[5] 3.14")
                        continue
                    self.write(cmd_args[0], cmd_args[1])
                
                elif cmd == 'status':
                    self.status()
                
                elif cmd == 'session':
                    if len(cmd_args) < 1:
                        human("用法: session list|new|switch|info")
                        continue
                    action = cmd_args[0]
                    if action == 'list':
                        self.list_sessions()
                    elif action == 'new':
                        self.create(cmd_args[1] if len(cmd_args) > 1 else None)
                    elif action == 'switch':
                        if len(cmd_args) < 2:
                            human("用法: session switch <name>")
                            continue
                        self.switch(cmd_args[1])
                    elif action == 'info':
                        self.info()
                
                elif cmd == 'monitor':
                    rate = 100
                    if '--rate' in cmd_args:
                        idx = cmd_args.index('--rate')
                        if idx + 1 < len(cmd_args):
                            rate = int(cmd_args[idx + 1])
                    self.monitor(rate)
                
                else:
                    human(f"未知命令: {cmd}")
            
            except EOFError:
                break
            except KeyboardInterrupt:
                human("")
                continue
            except Exception as e:
                human(f"错误: {e}")
        
        human("\n已退出")
        return 0
    
    def _shell_help(self):
        """Shell帮助"""
        help_text = """
命令列表:
  compile <file> [-o out.bin]  编译DCL程序
  deploy <file.bin>            部署到硬件
  run <file.dcl>               编译并运行
  read wire[0:10]              读取WIRE
  write wire[5] 3.14           写入WIRE
  monitor [--rate 100]         监控WIRE
  status                       系统状态
  session list                 列出会话
  session new [name]           新建会话
  session switch <name>         切换会话
  session info                 会话信息
  help                         显示帮助
  exit                         退出
"""
        human(help_text)

def DclSession() -> SessionManager:
    """获取会话管理器实例"""
    return SessionManager()

# 便捷函数
def create_session(name: Optional[str] = None) -> int:
    return DclSession().create(name)

def list_sessions() -> int:
    return DclSession().list_sessions()

def switch_session(name: str) -> int:
    return DclSession().switch(name)

def session_info() -> int:
    return DclSession().info()
