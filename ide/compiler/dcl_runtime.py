#!/usr/bin/env python3
"""
DCL Runtime - 完整的PLC编程服务
提供编译、部署、监控API
"""

import os
import sys
import json
import time
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HTTP_PORT = 8765
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dcl_runtime.pid')
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dcl_runtime.log')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, BASE_DIR)
from core.dcl_hardware import get_hardware

class Logger:
    def __init__(self, f): self.f = f
    def log(self, lvl, msg):
        line = f"[{time.strftime('%H:%M:%S')}] [{lvl}] {msg}"
        print(line, flush=True)
        try:
            with open(self.f, 'a') as fp: fp.write(line + '\n')
        except: pass
    def info(self, m): self.log('INFO', m)
    def error(self, m): self.log('ERROR', m)

log = Logger(LOG_FILE)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    
    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)
    
    def _body(self):
        n = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(n)) if n else {}
    
    def _resolve_path(self, f):
        """将相对路径转换为绝对路径"""
        if not os.path.isabs(f):
            f = os.path.join(BASE_DIR, f)
        return f
    
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path.rstrip('/')
        q = parse_qs(u.query)
        
        try:
            if p == '/api/status':
                hw = get_hardware()
                self._json({'ok': True, 'hw_connected': hw.connected, 'routes': hw.get_active_routes()})
            elif p == '/api/wires':
                s, c = int(q.get('s', [0])[0]), int(q.get('c', [16])[0])
                hw = get_hardware()
                if not hw.connect():
                    return self._json({'ok': False, 'err': 'no hardware'}, 503)
                v = hw.read_wires(s, c)
                self._json({'ok': v is not None, 'values': v if v else []})
            elif p == '/api/introspect':
                self._json({'ok': True, 'apis': ['status', 'compile', 'deploy', 'execute', 'wires']})
            else:
                self._json({'ok': False, 'err': 'not found'}, 404)
        except Exception as e:
            log.error(f'GET {p}: {e}')
            self._json({'ok': False, 'err': str(e)}, 500)
    
    def do_POST(self):
        u = urlparse(self.path)
        p = u.path.rstrip('/')
        body = self._body()
        
        try:
            if p == '/api/compile':
                return self._handle_compile(body)
            elif p == '/api/deploy':
                return self._handle_deploy(body)
            elif p == '/api/execute':
                return self._handle_execute(body)
            else:
                self._json({'ok': False, 'err': 'not found'}, 404)
        except Exception as e:
            log.error(f'POST {p}: {e}')
            self._json({'ok': False, 'err': f'{type(e).__name__}: {e}'}, 500)
    
    def _handle_compile(self, body):
        """独立编译：只编译不部署"""
        from core.dcl_compiler import compile_file
        f = body.get('file')
        out = body.get('output')
        if not f:
            return self._json({'ok': False, 'err': 'no file'}, 400)
        
        f = self._resolve_path(f)
        if not out:
            out = f.replace('.dcl', '.bin')
        else:
            out = self._resolve_path(out)
        
        ok, res = compile_file(f, out)
        if ok:
            self._json({
                'ok': True,
                'routes': res['stats']['routes'],
                'params': res['stats']['params'],
                'wires': res['stats']['wires'],
                'binary': out,
                'size': res['stats']['binary_size']
            })
        else:
            self._json({
                'ok': False,
                'err': res.get('error', 'compile failed'),
                'line': res.get('line'),
                'col': res.get('col')
            }, 500)
    
    def _handle_deploy(self, body):
        """独立部署：只部署已编译的二进制"""
        f = body.get('binary')
        if not f or not os.path.exists(f):
            return self._json({'ok': False, 'err': f'file not found: {f}'}, 400)
        
        with open(f, 'rb') as fp:
            data = fp.read()
        
        hw = get_hardware()
        if not hw.connect():
            return self._json({'ok': False, 'err': 'no hardware'}, 503)
        
        if hw.deploy(data):
            self._json({'ok': True, 'size': len(data), 'routes': hw.get_active_routes()})
        else:
            self._json({'ok': False, 'err': hw.last_error}, 500)
    
    def _handle_execute(self, body):
        """一键执行：编译+部署"""
        from core.dcl_compiler import compile_file
        f = body.get('file')
        if not f:
            return self._json({'ok': False, 'err': 'no file'}, 400)
        
        f = self._resolve_path(f)
        out = f.replace('.dcl', '.bin')
        
        ok, res = compile_file(f, out)
        if not ok:
            return self._json({
                'ok': False,
                'err': res.get('error', 'compile failed'),
                'line': res.get('line'),
                'col': res.get('col')
            }, 500)
        
        with open(out, 'rb') as fp:
            data = fp.read()
        
        hw = get_hardware()
        if not hw.connect():
            return self._json({'ok': False, 'err': 'no hardware'}, 503)
        
        if hw.deploy(data):
            self._json({
                'ok': True,
                'routes': res['stats']['routes'],
                'params': res['stats']['params'],
                'wires': res['stats']['wires'],
                'binary': out,
                'size': len(data)
            })
        else:
            self._json({'ok': False, 'err': hw.last_error}, 500)

class Runtime:
    def __init__(self):
        self.running = False
    
    def start(self):
        log.info('Runtime starting...')
        
        hw = get_hardware()
        if hw.connect():
            log.info('Hardware connected')
        else:
            log.info('No hardware')
        
        self.httpd = ThreadingHTTPServer(('0.0.0.0', HTTP_PORT), Handler)
        self.httpd.daemon_threads = True
        
        self.running = True
        log.info(f'HTTP ready :{HTTP_PORT}')
        log.info(f'Base dir: {BASE_DIR}')
        
        with open(PID_FILE, 'w') as f:
            f.write(str(os.getpid()))
        
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        
        self.stop()
    
    def stop(self):
        log.info('Runtime stopping...')
        self.running = False
        if hasattr(self, 'httpd'):
            try: self.httpd.shutdown()
            except: pass
        get_hardware().disconnect()
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)

def is_run():
    if not os.path.exists(PID_FILE): return False
    try:
        pid = int(open(PID_FILE).read().strip())
        os.kill(pid, 0)
        return True
    except:
        try: os.remove(PID_FILE)
        except: pass
        return False

def stop_old():
    if not os.path.exists(PID_FILE): return
    try:
        pid = int(open(PID_FILE).read().strip())
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
    except: pass
    if os.path.exists(PID_FILE): os.remove(PID_FILE)

if __name__ == '__main__':
    import argparse
    a = argparse.ArgumentParser()
    a.add_argument('cmd', choices=['start', 'stop', 'status'])
    args = a.parse_args()
    
    if args.cmd == 'start':
        if is_run(): print('Already running'); sys.exit(1)
        Runtime().start()
    elif args.cmd == 'stop':
        if not is_run(): print('Not running'); sys.exit(1)
        stop_old(); print('Stopped')
    elif args.cmd == 'status':
        print('Running' if is_run() else 'Not running')
