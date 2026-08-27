#!/usr/bin/env python3
"""
DCL IDE CLI - 面向PLC编程的完整工具
工作流：新建 → 编译 → 烧录 → 监控
面向AI设计：结构化输出、简洁格式、包含建议

DCL语法（大写关键字）：
  SENSOR  name FROM source [SCALE k b]    - 传感器输入
  ARITH   name = a OP b                     - 算术运算（ADD/SUB/MUL/DIV/GT/LT/GTE/LTE）
  LIMIT   name FROM src RANGE lo hi         - 限幅
  MAX     name = src MAX value              - 取大
  MIN     name = src MIN value              - 取小
  ABS     name FROM src                     - 绝对值
  EQ      name FROM src == value            - 等于
  NE      name FROM src != value            - 不等于
  OUTPUT  name TO port                      - 输出到端口

示例：
  SENSOR temp FROM ADC1_CH0 SCALE 0.1 0.0
  LIMIT clamped FROM temp RANGE -10 10
  OUTPUT heat TO TIM1_CH1
"""

import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
from pathlib import Path

RUNTIME_URL = "http://localhost:8765"
TIMEOUT = 30

DCL_TEMPLATE = """# DCL Program - {filename}
# Created: {date}
# Description: [Add your description here]
#
# DCL语法:
#   SENSOR  name FROM source [SCALE k b]  - 传感器输入
#   ARITH   name = a OP b                 - 运算(ADD/SUB/MUL/DIV/GT/LT/GTE/LTE)
#   LIMIT   name FROM src RANGE lo hi     - 限幅
#   MAX     name = src MAX value          - 取大
#   MIN     name = src MIN value          - 取小
#   ABS     name FROM src                 - 绝对值
#   EQ      name FROM src == value        - 等于
#   NE      name FROM src != value        - 不等于
#   OUTPUT  name TO port                  - 输出到端口

# ================================================================
# Sensor Inputs
# ================================================================

SENSOR temp     FROM ADC1_CH0    SCALE 0.1 0.0
SENSOR pressure FROM ADC1_CH1    SCALE 0.01 0.0

# ================================================================
# Control Logic
# ================================================================

# Temperature high alarm (> 80.0)
ARITH temp_high = temp GT 80.0

# Pressure limit check (max 5.0)
MIN pressure_safe FROM pressure MIN 5.0

# Absolute temperature
ABS abs_temp FROM temp

# Pressure equals 3.0?
EQ pressure_is_3 FROM pressure == 3.0

# Temperature not equal to 25.0
NE temp_not_25 FROM temp != 25.0

# ================================================================
# Output
# ================================================================

OUTPUT temp_high    TO GPIO_PE0
OUTPUT pressure     TO GPIO_PE1
"""


class RuntimeClient:
    def __init__(self, url=RUNTIME_URL):
        self.url = url
    
    def get(self, path, params=None, timeout=None):
        url = f"{self.url}{path}"
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            return {"ok": False, "err": str(e)}
    
    def post(self, path, data=None, timeout=None):
        url = f"{self.url}{path}"
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            return {"ok": False, "err": str(e)}
    
    def status(self):
        return self.get("/api/status")
    
    def compile(self, f):
        return self.post("/api/compile", {"file": f})
    
    def deploy(self, binary):
        return self.post("/api/deploy", {"binary": binary})
    
    def execute(self, f):
        return self.post("/api/execute", {"file": f})
    
    def wires(self, start=0, count=16):
        return self.get("/api/wires", {"s": start, "c": count})
    
    def introspect(self):
        return self.get("/api/introspect")


class Fmt:
    @staticmethod
    def status(data):
        if not data.get("ok"):
            return f"STATUS: ERROR\n{data.get('err')}"
        lines = ["STATUS: OK"]
        lines.append(f"hardware: {'connected' if data.get('hw_connected') else 'disconnected'}")
        lines.append(f"active_routes: {data.get('routes', 0)}")
        lines.append("SUGGEST: compile <file> | execute <file>")
        return "\n".join(lines)
    
    @staticmethod
    def compile(data):
        if not data.get("ok"):
            lines = ["STATUS: ERROR"]
            lines.append(f"error: {data.get('err')}")
            if data.get('line'):
                lines.append(f"line: {data['line']}")
            return "\n".join(lines)
        lines = ["STATUS: OK"]
        lines.append(f"routes: {data.get('routes')}")
        lines.append(f"params: {data.get('params')}")
        lines.append(f"wires: {data.get('wires')}")
        lines.append(f"binary: {data.get('binary')}")
        lines.append(f"size: {data.get('size')} bytes")
        lines.append("SUGGEST: deploy <binary>")
        return "\n".join(lines)
    
    @staticmethod
    def deploy(data):
        if not data.get("ok"):
            return f"STATUS: ERROR\n{data.get('err')}"
        lines = ["STATUS: OK"]
        lines.append(f"size: {data.get('size')} bytes")
        lines.append(f"routes: {data.get('routes')}")
        lines.append("SUGGEST: wires s=0 c=9 | monitor")
        return "\n".join(lines)
    
    @staticmethod
    def execute(data):
        if not data.get("ok"):
            lines = ["STATUS: ERROR"]
            lines.append(f"error: {data.get('err')}")
            if data.get('line'):
                lines.append(f"line: {data['line']}")
            return "\n".join(lines)
        lines = ["STATUS: OK"]
        lines.append(f"routes: {data.get('routes')}")
        lines.append(f"wires: {data.get('wires')}")
        lines.append(f"size: {data.get('size')} bytes")
        lines.append("SUGGEST: wires s=0 c=9 | monitor")
        return "\n".join(lines)
    
    @staticmethod
    def wires(data):
        if not data.get("ok"):
            return f"STATUS: ERROR\n{data.get('err')}"
        values = data.get("values", [])
        lines = ["STATUS: OK"]
        for i, v in enumerate(values):
            lines.append(f"  [{i}] {v}")
        return "\n".join(lines)
    
    @staticmethod
    def monitor(prev, cur):
        if not cur.get("ok"):
            return f"STATUS: ERROR\n{cur.get('err')}"
        p_vals = prev.get("values", []) if prev else []
        c_vals = cur.get("values", [])
        lines = ["--- WIRE Monitor ---"]
        for i, (p, c) in enumerate(zip(p_vals, c_vals)):
            if p != c:
                lines.append(f"  [{i}] {p} -> {c}")
        return "\n".join(lines) if len(lines) > 1 else "  (no changes)"


def cmd_new(args):
    f = Path(args.name)
    if not f.suffix:
        f = f.with_suffix('.dcl')
    if f.exists() and not args.force:
        print(f"FILE EXISTS: {f}")
        print("Use --force to overwrite")
        return 1
    template = DCL_TEMPLATE.format(
        filename=f.name,
        date=time.strftime('%Y-%m-%d %H:%M:%S')
    )
    f.write_text(template, encoding='utf-8')
    print(f"CREATED: {f}")
    print(f"Size: {f.stat().st_size} bytes")
    print("SUGGEST: edit the file, then compile")
    return 0


def cmd_compile(args):
    c = RuntimeClient()
    data = c.compile(args.file)
    print(Fmt.compile(data))
    return 0 if data.get("ok") else 1


def cmd_deploy(args):
    c = RuntimeClient()
    data = c.deploy(args.binary)
    print(Fmt.deploy(data))
    return 0 if data.get("ok") else 1


def cmd_execute(args):
    c = RuntimeClient()
    data = c.execute(args.file)
    print(Fmt.execute(data))
    return 0 if data.get("ok") else 1


def cmd_status(args):
    c = RuntimeClient()
    print(Fmt.status(c.status()))
    return 0


def cmd_wires(args):
    c = RuntimeClient()
    data = c.wires(args.start, args.count)
    print(Fmt.wires(data))
    return 0 if data.get("ok") else 1


def cmd_monitor(args):
    c = RuntimeClient()
    rate = args.rate / 1000.0
    print(f"Monitoring WIRE (rate: {args.rate}ms, Ctrl+C to stop)")
    prev = None
    try:
        while True:
            data = c.wires(0, args.count)
            if prev is not None:
                print(Fmt.monitor(prev, data))
            prev = data
            time.sleep(rate)
    except KeyboardInterrupt:
        print("\nMonitor stopped")
    return 0


def cmd_repl(args):
    c = RuntimeClient()
    print("DCL REPL - 'help' for commands, 'quit' to exit")
    prev_wires = None
    while True:
        try:
            line = input("\n> ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        
        if cmd == 'quit':
            break
        elif cmd == 'help':
            print("Commands:")
            print("  new <file>        - Create new DCL file")
            print("  compile <file>    - Compile DCL to binary")
            print("  deploy <binary>   - Deploy binary to hardware")
            print("  execute <file>    - Compile + deploy")
            print("  status            - Runtime status")
            print("  wires [s] [c]     - Read WIRE values")
            print("  monitor [rate]    - Continuous monitor")
            print("  introspect        - API list")
            print("  quit              - Exit")
        elif cmd == 'new':
            if len(parts) < 2:
                print("Usage: new <file>")
                continue
            Path(parts[1]).write_text(DCL_TEMPLATE.format(
                filename=parts[1], date=time.strftime('%Y-%m-%d %H:%M:%S')))
            print(f"Created: {parts[1]}")
        elif cmd == 'compile':
            if len(parts) < 2:
                print("Usage: compile <file>")
                continue
            print(Fmt.compile(c.compile(parts[1])))
        elif cmd == 'deploy':
            if len(parts) < 2:
                print("Usage: deploy <binary>")
                continue
            print(Fmt.deploy(c.deploy(parts[1])))
        elif cmd == 'execute':
            if len(parts) < 2:
                print("Usage: execute <file>")
                continue
            print(Fmt.execute(c.execute(parts[1])))
        elif cmd == 'status':
            print(Fmt.status(c.status()))
        elif cmd == 'wires':
            s = int(parts[1]) if len(parts) > 1 else 0
            c_count = int(parts[2]) if len(parts) > 2 else 16
            data = c.wires(s, c_count)
            print(Fmt.wires(data))
            prev_wires = data
        elif cmd == 'monitor':
            rate = int(parts[1]) if len(parts) > 1 else 200
            print(f"Monitoring (rate: {rate}ms, Ctrl+C to stop)")
            try:
                p = prev_wires
                while True:
                    d = c.wires(0, 16)
                    print(Fmt.monitor(p, d))
                    p = d
                    time.sleep(rate / 1000.0)
            except KeyboardInterrupt:
                print("\nStopped")
        elif cmd == 'introspect':
            print(json.dumps(c.introspect(), indent=2))
        elif cmd == 'runtime':
            if len(parts) < 2:
                print("Usage: runtime [start|stop|status]")
                continue
            if parts[1] == 'start':
                os.system('start python dcl_runtime.py start')
            elif parts[1] == 'stop':
                os.system('python dcl_runtime.py stop')
            elif parts[1] == 'status':
                os.system('python dcl_runtime.py status')
        else:
            print(f"Unknown: {cmd}. Type 'help' for commands.")
    print("Bye!")
    return 0


def main():
    parser = argparse.ArgumentParser(prog='dcl', description="DCL IDE - PLC Programming Tool")
    sub = parser.add_subparsers(dest="cmd")
    
    p_new = sub.add_parser("new", help="Create new DCL file with template")
    p_new.add_argument("name", help="File name")
    p_new.add_argument("-f", "--force", action="store_true", help="Overwrite existing")
    
    p_compile = sub.add_parser("compile", help="Compile DCL file")
    p_compile.add_argument("file", help="DCL source file")
    
    p_deploy = sub.add_parser("deploy", help="Deploy binary to hardware")
    p_deploy.add_argument("binary", help="Binary file")
    
    p_exec = sub.add_parser("execute", help="Compile + deploy")
    p_exec.add_argument("file", help="DCL source file")
    
    sub.add_parser("status", help="Runtime status")
    
    p_wires = sub.add_parser("wires", help="Read WIRE values")
    p_wires.add_argument("-s", "--start", type=int, default=0)
    p_wires.add_argument("-c", "--count", type=int, default=9)
    
    p_mon = sub.add_parser("monitor", help="Continuous WIRE monitor")
    p_mon.add_argument("-r", "--rate", type=int, default=200, help="Refresh rate (ms)")
    p_mon.add_argument("-c", "--count", type=int, default=9)
    
    sub.add_parser("introspect", help="API discovery")
    sub.add_parser("repl", help="Interactive mode")
    
    args = parser.parse_args()
    
    cmds = {
        'new': cmd_new,
        'compile': cmd_compile,
        'deploy': cmd_deploy,
        'execute': cmd_execute,
        'status': cmd_status,
        'wires': cmd_wires,
        'monitor': cmd_monitor,
        'repl': cmd_repl,
    }
    
    if args.cmd in cmds:
        try:
            return cmds[args.cmd](args)
        except Exception as e:
            print(f"ERROR: {e}")
            return 1
    elif args.cmd == 'introspect':
        c = RuntimeClient()
        print(json.dumps(c.introspect(), indent=2))
        return 0
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
