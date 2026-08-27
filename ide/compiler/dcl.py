#!/usr/bin/env python3
"""
DCL - 统一入口
用法:
  <subcommand> [args...]
  
子命令:
  daemon   守护进程管理
  session  会话管理
  project  项目管理
  compile  编译程序
  deploy   部署程序
  run      运行程序
  read     读取WIRE
  write    写入WIRE
  monitor  监控WIRE
  status   查看状态
  shell    交互式REPL
"""

import sys
import os

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.dcl_daemon import DclDaemon
from core.dcl_session import DclSession
from core.dcl_output import output, success, error

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    command = sys.argv[1]
    args = sys.argv[2:]

    # ═════════════════════════════════════════════════════════════════════════
    # 子命令路由
    # ═════════════════════════════════════════════════════════════════════════
    
    if command == 'daemon':
        return cmd_daemon(args)
    elif command == 'session':
        return cmd_session(args)
    elif command == 'project':
        return cmd_project(args)
    elif command == 'compile':
        return cmd_compile(args)
    elif command == 'deploy':
        return cmd_deploy(args)
    elif command == 'run':
        return cmd_run(args)
    elif command == 'read':
        return cmd_read(args)
    elif command == 'write':
        return cmd_write(args)
    elif command == 'monitor':
        return cmd_monitor(args)
    elif command == 'status':
        return cmd_status(args)
    elif command == 'shell':
        return cmd_shell(args)
    else:
        print(f"未知命令: {command}")
        print(__doc__)
        return 1

# ═════════════════════════════════════════════════════════════════════════════
# 守护进程管理
# ═════════════════════════════════════════════════════════════════════════════

def cmd_daemon(args):
    """
    守护进程管理
    dcl daemon start|stop|restart|status
    """
    if len(args) < 1:
        print("用法: dcl daemon start|stop|restart|status")
        return 1

    action = args[0]
    daemon = DclDaemon()

    if action == 'start':
        return daemon.start()
    elif action == 'stop':
        return daemon.stop()
    elif action == 'restart':
        return daemon.restart()
    elif action == 'status':
        return daemon.status()
    else:
        print(f"未知操作: {action}")
        return 1

# ═════════════════════════════════════════════════════════════════════════════
# 会话管理
# ═════════════════════════════════════════════════════════════════════════════

def cmd_session(args):
    """
    会话管理
    dcl session new [<name>]    — 创建新会话
    dcl session list            — 列出所有会话
    dcl session switch <name>   — 切换会话
    dcl session info            — 当前会话信息
    """
    if len(args) < 1:
        print("用法: dcl session new|list|switch|info")
        return 1

    action = args[0]
    session = DclSession()

    if action == 'new':
        name = args[1] if len(args) > 1 else None
        return session.create(name)
    elif action == 'list':
        return session.list_sessions()
    elif action == 'switch':
        if len(args) < 2:
            print("用法: dcl session switch <name>")
            return 1
        return session.switch(args[1])
    elif action == 'info':
        return session.info()
    else:
        print(f"未知操作: {action}")
        return 1

# ═════════════════════════════════════════════════════════════════════════════
# 项目管理
# ═════════════════════════════════════════════════════════════════════════════

def cmd_project(args):
    """
    项目管理
    dcl project open <path> — 打开项目
    dcl project save       — 保存项目
    dcl project close      — 关闭项目
    dcl project info       — 项目信息
    """
    if len(args) < 1:
        print("用法: dcl project open|save|close|info")
        return 1

    action = args[0]
    session = DclSession()

    if action == 'open':
        if len(args) < 2:
            print("用法: dcl project open <path>")
            return 1
        return session.project_open(args[1])
    elif action == 'save':
        return session.project_save()
    elif action == 'close':
        return session.project_close()
    elif action == 'info':
        return session.project_info()
    else:
        print(f"未知操作: {action}")
        return 1

# ═════════════════════════════════════════════════════════════════════════════
# 编译/部署/运行
# ═════════════════════════════════════════════════════════════════════════════

def cmd_compile(args):
    """
    编译DCL程序
    dcl compile <file.dcl> [-o output.bin] [--session <name>]
    """
    if len(args) < 1:
        print("用法: dcl compile <file.dcl> [-o output.bin]")
        return 1

    input_file = args[0]
    output_file = None
    
    # 简单解析参数
    if '-o' in args:
        idx = args.index('-o')
        if idx + 1 < len(args):
            output_file = args[idx + 1]

    session = DclSession()
    return session.compile(input_file, output_file)

def cmd_deploy(args):
    """
    部署程序到硬件
    dcl deploy <file.bin>
    """
    if len(args) < 1:
        print("用法: dcl deploy <file.bin>")
        return 1

    binary_file = args[0]
    session = DclSession()
    return session.deploy(binary_file)

def cmd_run(args):
    """
    编译并运行
    dcl run <file.dcl>
    """
    if len(args) < 1:
        print("用法: dcl run <file.dcl>")
        return 1

    input_file = args[0]
    session = DclSession()
    return session.run(input_file)

# ═════════════════════════════════════════════════════════════════════════════
# 读写WIRE
# ═════════════════════════════════════════════════════════════════════════════

def cmd_read(args):
    """
    读取WIRE/SENSOR
    dcl read wire[0:10]
    """
    if len(args) < 1:
        print("用法: dcl read wire[0:10]")
        return 1

    target = args[0]
    session = DclSession()
    return session.read(target)

def cmd_write(args):
    """
    写入WIRE
    dcl write wire[5] 3.14
    """
    if len(args) < 2:
        print("用法: dcl write wire[5] 3.14")
        return 1

    target = args[0]
    value = args[1]
    session = DclSession()
    return session.write(target, value)

# ═════════════════════════════════════════════════════════════════════════════
# 监控
# ═════════════════════════════════════════════════════════════════════════════

def cmd_monitor(args):
    """
    监控模式
    dcl monitor [--rate 100] [--signals temp,pid_out]
    """
    rate = 100
    signals = None
    
    if '--rate' in args:
        idx = args.index('--rate')
        if idx + 1 < len(args):
            rate = int(args[idx + 1])
    
    if '--signals' in args:
        idx = args.index('--signals')
        if idx + 1 < len(args):
            signals = args[idx + 1]

    session = DclSession()
    return session.monitor(rate, signals)

# ═════════════════════════════════════════════════════════════════════════════
# 状态
# ═════════════════════════════════════════════════════════════════════════════

def cmd_status(args):
    """
    查看系统状态
    dcl status
    """
    session = DclSession()
    return session.status()

# ═════════════════════════════════════════════════════════════════════════════
# 交互式REPL
# ═════════════════════════════════════════════════════════════════════════════

def cmd_shell(args):
    """
    进入交互式REPL
    dcl shell
    """
    session = DclSession()
    return session.shell()

if __name__ == '__main__':
    sys.exit(main())
