#!/usr/bin/env python3
"""
DCL IDE CLI — 集成开发终端

与 GUI IDE 共享同一个 WebSocket 服务器，实现双向源码同步。
用法:
  python -m shell.cli [options]            # REPL 模式
  python -m shell.cli --compile prog.dcl   # 单次编译
  python -m shell.cli --deploy             # 部署上次编译结果
  python -m shell.cli --run prog.dcl       # 编译+部署+启动
  python -m shell.cli --monitor            # RTT 实时监控
  python -m shell.cli --wires [n]          # 读取 WIRE 值
  python -m shell.cli --status             # 引擎状态
"""

import asyncio
import json
import sys
import os
import base64
import argparse
from pathlib import Path
from typing import Optional, List, Dict, Any

import websockets

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8765
TIMEOUT = 10


# ══════════════════════════════════════════════════════════
# 传输层 — 封装所有 WebSocket 通信
# ══════════════════════════════════════════════════════════

class WSClient:
    """IDE WebSocket 客户端 — 请求/响应 + 推送监听"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.url = f"ws://{host}:{port}"
        self.ws = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._counter = 0
        self._listeners: List[callable] = []
        self._task = None

    async def connect(self):
        self.ws = await websockets.connect(self.url)
        self._last_binary = None
        self._task = asyncio.create_task(self._recv_loop())

    async def close(self):
        if self._task:
            self._task.cancel()
        if self.ws:
            await self.ws.close()

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter

    async def call(self, cmd: str, **kwargs) -> dict:
        """发送命令并等待对应响应"""
        mid = self._next_id()
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps({"cmd": cmd, "_id": mid, **kwargs}))
        try:
            return await asyncio.wait_for(fut, timeout=TIMEOUT)
        except asyncio.TimeoutError:
            return {"type": "error", "msg": "timeout"}

    def on_push(self, fn: callable):
        """注册推送消息监听器 (monitor_status, source_changed, etc.)"""
        self._listeners.append(fn)

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                mid = msg.get("_id")
                if mid and mid in self.pending:
                    self._pending.pop(mid).set_result(msg)
                else:
                    for fn in self._listeners:
                        try:
                            fn(msg)
                        except Exception:
                            pass
        except websockets.exceptions.ConnectionClosed:
            pass

    @property
    def pending(self):
        return self._pending

    # ── 高层 API ──────────────────────────────────────────

    async def get_source(self) -> str:
        r = await self.call("get_source")
        return r.get("source", "")

    async def set_source(self, source: str) -> bool:
        r = await self.call("set_source", source=source)
        return r.get("ok", False)

    async def compile(self, source: str) -> dict:
        return await self.call("compile", source=source)

    async def check(self, source: str) -> dict:
        return await self.call("check", source=source)

    async def deploy(self, binary_b64: str) -> dict:
        return await self.call("deploy", binary=binary_b64)

    async def start(self) -> dict:
        return await self.call("start")

    async def stop(self) -> dict:
        return await self.call("stop")

    async def reset(self) -> dict:
        return await self.call("reset")

    async def read_wires(self, start: int = 0, count: int = 16) -> dict:
        return await self.call("read_wires", start=start, count=count)

    async def write_wire(self, idx: int, value: float) -> dict:
        return await self.call("write_wire", idx=idx, value=value)

    async def get_symbol_table(self) -> dict:
        return await self.call("get_symbol_table")


# ══════════════════════════════════════════════════════════
# 展示层 — 所有输出格式化
# ══════════════════════════════════════════════════════════

def _clr(code, text):
    return f"\033[{code}m{text}\033[0m"

def _bold(t): return _clr("1", t)
def _red(t):  return _clr("31", t)
def _grn(t):  return _clr("32", t)
def _ylw(t):  return _clr("33", t)
def _blu(t):  return _clr("34", t)
def _mag(t):  return _clr("35", t)
def _cyn(t):  return _clr("36", t)


class Fmt:
    """CLI 输出格式化 — 所有 print 集中于此"""

    banner = f"""
{_cyn('═' * 50)}
{_bold('  DCL IDE CLI')}
{_cyn('═' * 50)}"""

    help_text = f"""
{_cyn('═' * 45)}
{_bold('  Source')}
  :e <file>        Load file → sync to GUI
  :w [file]        Save current source
  :show            Show current source
  :diff            Show changes since last load

{_bold('  Compile & Deploy')}
  :c               Compile current source
  :d               Deploy last compiled binary
  :r               Compile + Deploy + Start

{_bold('  Engine')}
  :start / :stop / :reset

{_bold('  Monitor')}
  :m               RTT status (Ctrl+C to stop)
  :wires [n]       Read WIRE[0..n]
  :force i v       Force WIRE[i] = v
  :symbols         Show symbol table

{_bold('  Other')}
  :help / :q
{_cyn('═' * 45)}"""

    @staticmethod
    def compile_result(r: dict):
        if r.get("success"):
            st = r.get("stats", {})
            syms = r.get("symbol_table", [])
            lines = [
                _grn("  ✓ Compile OK"),
                f"    routes={st.get('routes', 0)}  params={st.get('params', 0)}  "
                f"states={st.get('states', 0)}  wires={st.get('wires', 0)}",
                f"    binary={st.get('binary_size', 0)} bytes  symbols={len(syms)}",
            ]
            for s in syms[:15]:
                d = {"input": _blu("IN"), "output": _mag("OUT"), "internal": "   "}.get(s.get("direction", ""), "   ")
                lines.append(f"    {d} {s['name']:20} WIRE[{s['wire_idx']:3}]  {s.get('fb_type', '-')}")
            if len(syms) > 15:
                lines.append(f"    ... +{len(syms) - 15} more")
        else:
            lines = [_red("  ✗ Compile FAILED")]
            for e in r.get("errors", []):
                lines.append(f"    {_red(str(e))}")
        print("\n".join(lines))

    @staticmethod
    def deploy_result(r: dict):
        if r.get("deploy_sent"):
            print(_grn(f"  ✓ Deployed {r['size']} bytes"))
        elif r.get("ok"):
            print(_grn("  ✓ Deploy OK"))
        else:
            print(_red(f"  ✗ Deploy failed: {r.get('msg', '?')}"))

    @staticmethod
    def wires(values: list):
        if not values:
            print(_ylw("  (no data)"))
            return
        for i, v in enumerate(values):
            print(f"  WIRE[{i:3}] = {v:12.4f}")

    @staticmethod
    def symbols(syms: list):
        if not syms:
            print(_ylw("  (no symbols — run :c first)"))
            return
        for s in syms:
            d = {"input": _blu("IN"), "output": _mag("OUT"), "internal": "   "}.get(s.get("direction", ""), "   ")
            print(f"  {d} {s['name']:20} WIRE[{s['wire_idx']:3}]  {s.get('fb_type', '-')}")

    @staticmethod
    def source(source: str):
        if not source:
            print(_ylw("  (empty)"))
            return
        for i, ln in enumerate(source.split("\n"), 1):
            print(f"  {_cyn(f'{i:3}')} {ln}")

    @staticmethod
    def monitor_line(s: dict):
        pmin = s.get("period_min", 0)
        pmax = s.get("period_max", 0)
        jitter = pmax - pmin if pmax >= pmin else 0
        engine = _grn("RUN") if s.get("engine_running") else _red("STOP")
        return (f"\r  S={s.get('samples', 0):>10}  "
                f"P={pmin}..{pmax} ({jitter:>4})  "
                f"R={s.get('routes', 0):>3}  {engine}   ")

    @staticmethod
    def monitor_alert(a: dict):
        print(_red(f"\n  [ALERT] {a.get('code', '?')}"))

    @staticmethod
    def source_changed():
        print(_ylw("\n  [Source updated from GUI]"))


# ══════════════════════════════════════════════════════════
# 命令层 — REPL 命令处理
# ══════════════════════════════════════════════════════════

async def cmd_repl(client: WSClient):
    """交互式 REPL"""
    print(Fmt.banner)
    print(f"  Connected to {client.url}")
    print(f"  Type :help for commands, :q to quit\n")

    client.on_push(lambda msg: _handle_push(msg))

    while True:
        try:
            line = input(_cyn("> ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        try:
            should_quit = await _exec(client, line)
            if should_quit:
                break
        except SystemExit:
            break
        except Exception as e:
            print(_red(f"  Error: {e}"))

    print(_cyn("\nBye."))


def _handle_push(msg: dict):
    t = msg.get("type")
    if t == "monitor_status":
        print(Fmt.monitor_line(msg), end="", flush=True)
    elif t == "source_changed":
        Fmt.source_changed()
    elif t == "monitor_alert":
        Fmt.monitor_alert(msg)


async def _exec(client: WSClient, line: str) -> bool:
    """执行一条 REPL 命令，返回 True 表示退出"""
    if not line.startswith(":"):
        print(_ylw("  Commands start with :"))
        return False

    parts = line[1:].split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("q", "quit"):
        return True

    if cmd == "help":
        print(Fmt.help_text)

    elif cmd == "e":
        if not arg:
            print(_ylw("  Usage: :e <file>")); return False
        path = Path(arg)
        if not path.exists():
            print(_red(f"  File not found: {path}")); return False
        source = path.read_text(encoding="utf-8")
        ok = await client.set_source(source)
        print(_grn(f"  Loaded {path} ({len(source)} chars) → synced to GUI") if ok else _red("  Failed"))

    elif cmd == "w":
        source = await client.get_source()
        out = Path(arg) if arg else Path("program.dcl")
        out.write_text(source, encoding="utf-8")
        print(_grn(f"  Saved to {out}"))

    elif cmd == "show":
        Fmt.source(await client.get_source())

    elif cmd == "c":
        print("  Compiling...")
        cr = await client.compile(await client.get_source())
        client._last_binary = cr.get("binary")
        cr.pop("binary", None)
        Fmt.compile_result(cr)

    elif cmd == "d":
        if not client._last_binary:
            print(_ylw("  No binary. Run :c first.")); return False
        r = await client.call("deploy", binary=client._last_binary)
        Fmt.deploy_result(r)

    elif cmd == "r":
        print("  Compile + Deploy + Start...")
        cr = await client.compile(await client.get_source())
        client._last_binary = cr.get("binary")
        cr.pop("binary", None)
        Fmt.compile_result(cr)
        if not cr.get("success"):
            return False
        dr = await client.call("deploy", binary=client._last_binary)
        Fmt.deploy_result(dr)
        if dr.get("deploy_sent") or dr.get("ok"):
            await client.start()
            print(_grn("  Engine started"))

    elif cmd == "start":
        await client.start(); print(_grn("  Engine started"))
    elif cmd == "stop":
        await client.stop(); print(_ylw("  Engine stopped"))
    elif cmd == "reset":
        await client.reset(); print(_ylw("  Engine reset"))

    elif cmd == "m":
        print(_cyn("  RTT Monitor (Ctrl+C stop)...\n"))
        try:
            while True:
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        print()

    elif cmd == "wires":
        n = int(arg) if arg else 16
        Fmt.wires((await client.read_wires(0, n)).get("values", []))

    elif cmd == "force":
        sp = arg.split()
        if len(sp) < 2:
            print(_ylw("  Usage: :force <idx> <value>")); return False
        await client.write_wire(int(sp[0]), float(sp[1]))
        print(_grn(f"  WIRE[{sp[0]}] = {sp[1]}"))

    elif cmd == "symbols":
        Fmt.symbols((await client.get_symbol_table()).get("symbols", []))

    else:
        print(_ylw(f"  Unknown: :{cmd}"))

    return False


# ══════════════════════════════════════════════════════════
# 入口 — argparse + 模式分发
# ══════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(
        prog="dcl-ide",
        description="DCL IDE — CLI / GUI 集成开发环境",
    )
    p.add_argument("--host", default=DEFAULT_HOST, help="服务器主机")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="WebSocket 端口")
    p.add_argument("--compile", metavar="FILE", help="编译 DCL 文件")
    p.add_argument("--deploy", action="store_true", help="部署上次编译结果")
    p.add_argument("--run", metavar="FILE", help="编译 + 部署 + 启动")
    p.add_argument("--monitor", action="store_true", help="RTT 实时监控")
    p.add_argument("--wires", type=int, nargs="?", const=16, metavar="N", help="读取 WIRE[0..N]")
    p.add_argument("--status", action="store_true", help="引擎状态")
    args = p.parse_args()

    asyncio.run(_main_async(args))


async def _main_async(args):
    client = WSClient(args.host, args.port)
    try:
        await client.connect()
    except Exception as e:
        print(_red(f"  Connect failed: {e}"))
        print(_ylw("  Start GUI first: python shell/main.py"))
        sys.exit(1)

    try:
        if args.compile:
            await _cmd_compile(client, args.compile)
        elif args.deploy:
            await _cmd_deploy(client)
        elif args.run:
            await _cmd_run(client, args.run)
        elif args.monitor:
            await _cmd_monitor(client)
        elif args.wires is not None:
            await _cmd_wires(client, args.wires)
        elif args.status:
            await _cmd_status(client)
        else:
            await cmd_repl(client)
    finally:
        await client.close()


async def _cmd_compile(client: WSClient, path: str):
    src = Path(path).read_text(encoding="utf-8")
    await client.set_source(src)
    Fmt.compile_result(await client.compile(src))


async def _cmd_deploy(client: WSClient):
    # 尝试获取最近编译的二进制 (从 REPL 上下文或服务器)
    # 在非 REPL 模式下, 先检查本地是否有上次编译的结果
    r = await client.call("get_last_binary")
    binary = r.get("binary", "")
    if not binary:
        print(_ylw("  No binary available. Use --compile first."))
        return
    Fmt.deploy_result(await client.call("deploy", binary=binary))


async def _cmd_run(client: WSClient, path: str):
    src = Path(path).read_text(encoding="utf-8")
    await client.set_source(src)
    r = await client.compile(src)
    Fmt.compile_result(r)
    if not r.get("success"):
        return
    binary = r.get("binary", "")
    if not binary:
        print(_ylw("  No binary from compile"))
        return
    dr = await client.call("deploy", binary=binary)
    Fmt.deploy_result(dr)
    if dr.get("deploy_sent") or dr.get("ok"):
        await client.start()
        print(_grn("  Engine started"))


async def _cmd_monitor(client: WSClient):
    print(_cyn("  RTT Monitor (Ctrl+C stop)...\n"))
    client.on_push(_handle_push)
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass


async def _cmd_wires(client: WSClient, n: int):
    Fmt.wires((await client.read_wires(0, n)).get("values", []))


async def _cmd_status(client: WSClient):
    r = await client.read_wires(0, 4)
    s = await client.get_source()
    syms = (await client.get_symbol_table()).get("symbols", [])
    print(f"  Source: {len(s)} chars, {len(s.splitlines())} lines")
    print(f"  Symbols: {len(syms)}")
    print(f"  WIRE values: {len(r.get('values', []))} returned")


if __name__ == "__main__":
    main()
