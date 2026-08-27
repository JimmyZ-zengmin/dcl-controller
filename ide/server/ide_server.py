"""
DCL IDE main server - clean router that delegates to sub-modules.

Ports:
  8080: HTTP static files (aiohttp)
  8765: WebSocket (websockets) with path-based dispatch
    /  or /usb  -> USB commands + monitor data push
    /lsp        -> LSP (future)
"""

import asyncio
import json
import logging
from pathlib import Path

import aiohttp
from aiohttp import web
import websockets

from server.usb_server import USBServer
from server.compiler_wrapper import CompilerWrapper
from server.ai_agent import AIAgent
from server.monitor_engine import MonitorEngine

logger = logging.getLogger('dcl-ide')

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = PROJECT_ROOT / 'web'


class IDEServer:
    """DCL IDE main server - HTTP static files + WebSocket routing."""

    def __init__(self, http_port=8080, ws_port=8765):
        self.http_port = http_port
        self.ws_port = ws_port
        self.usb = USBServer()
        self.compiler = CompilerWrapper()
        self.ai = AIAgent()
        self.ws_clients: set = set()
        self._last_symbol_table = []
        self.current_source = ''
        self._cmd_id = None
        self._current_ws = None

        # 监测引擎（IDE 组件）
        self.monitor = MonitorEngine(
            on_status=self._on_monitor_status,
            on_alert=self._on_monitor_alert,
        )

        # Wire up USB status callback
        self.usb.on_status = self._on_usb_status

    # ── lifecycle ──────────────────────────────────────────────

    async def start(self):
        """Start HTTP + WebSocket servers."""
        self._ws_loop = asyncio.get_event_loop()
        await self.start_http()
        await self.start_ws()
        self.monitor.start()
        logger.info("DCL IDE server started")

    async def stop(self):
        """Stop everything."""
        self.monitor.stop()
        if hasattr(self, '_ws_server'):
            self._ws_server.close()
            await self._ws_server.wait_closed()
        if hasattr(self, '_http_runner'):
            await self._http_runner.cleanup()
        await self.usb.close()
        logger.info("DCL IDE server stopped")

    # ── HTTP ───────────────────────────────────────────────────

    async def start_http(self):
        """Serve static files from web/ directory."""
        app = web.Application()
        app.router.add_route('GET', '/', self._index_handler)
        app.router.add_route('GET', '/api/monitor/latest', self._monitor_latest)
        app.router.add_route('GET', '/api/monitor/history', self._monitor_history)
        app.router.add_route('GET', '/api/monitor/alerts', self._monitor_alerts)
        app.router.add_static('/', str(WEB_ROOT), show_index=False)
        self._http_runner = web.AppRunner(app)
        await self._http_runner.setup()
        site = web.TCPSite(self._http_runner, 'localhost', self.http_port)
        await site.start()
        logger.info(f"HTTP server started: http://localhost:{self.http_port}")

    async def _monitor_latest(self, request):
        return web.json_response(self.monitor.get_latest())

    async def _monitor_history(self, request):
        seconds = float(request.query.get("seconds", "60"))
        return web.json_response(self.monitor.get_history(seconds))

    async def _monitor_alerts(self, request):
        return web.json_response(self.monitor.get_alerts())

    async def _index_handler(self, request):
        return web.FileResponse(WEB_ROOT / 'index.html')

    # ── WebSocket ──────────────────────────────────────────────

    async def start_ws(self):
        """Start WebSocket server with path-based routing."""
        self._ws_server = await websockets.serve(
            self._ws_handler, 'localhost', self.ws_port
        )
        logger.info(f"WebSocket server started: ws://localhost:{self.ws_port}")

    async def _ws_handler(self, websocket, path='/'):
        """Dispatch based on WebSocket path."""
        if path in ('/', '/usb'):
            await self._handle_usb_ws(websocket)
        elif path == '/lsp':
            logger.info("LSP WebSocket connected (not yet implemented)")
            try:
                async for _ in websocket:
                    pass
            except websockets.exceptions.ConnectionClosed:
                pass
        else:
            logger.warning(f"Unknown WS path: {path}")
            await websocket.close(4004, f"Unknown path: {path}")

    # ── Monitor callbacks ───────────────────────────────────────

    def _on_monitor_status(self, status: dict):
        """MonitorEngine 推送最新状态 (从后台线程调用)"""
        status.pop("_ts", None)
        msg = json.dumps({"type": "monitor_status", **status})
        self._threadsafe_broadcast(msg)

    def _on_monitor_alert(self, alert: dict):
        """MonitorEngine 推送告警 (从后台线程调用)"""
        msg = json.dumps({"type": "monitor_alert", **alert})
        self._threadsafe_broadcast(msg)

    def _threadsafe_broadcast(self, message: str):
        """从任意线程安全地广播到所有 WS 客户端"""
        loop = self._ws_loop if hasattr(self, '_ws_loop') else None
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(message), loop)

    # ── WebSocket ──────────────────────────────────────────────

    async def _handle_usb_ws(self, websocket):
        """Handle /usb WebSocket: register client, process commands."""
        self.ws_clients.add(websocket)
        logger.info(f"WS client connected: {websocket.remote_address}")
        try:
            async for message in websocket:
                if isinstance(message, str):
                    try:
                        cmd_dict = json.loads(message)
                    except json.JSONDecodeError:
                        await websocket.send(json.dumps({'type': 'error', 'msg': 'Invalid JSON'}))
                        continue
                    await self._handle_command(websocket, cmd_dict)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.ws_clients.discard(websocket)
            logger.info("WS client disconnected")

    # ── command dispatch ───────────────────────────────────────

    async def _reply(self, msg: dict):
        """发送响应，自动附带 _id"""
        if self._cmd_id is not None:
            msg['_id'] = self._cmd_id
        await self._current_ws.send(json.dumps(msg))

    async def _handle_command(self, ws, cmd_dict):
        """JSON command dispatch to USB / Compiler."""
        import base64
        action = cmd_dict.get('cmd', '')
        self._cmd_id = cmd_dict.get('_id')
        self._current_ws = ws
        try:
            if action == 'get_source':
                await self._reply({'type': 'get_source_result', 'source': self.current_source})

            elif action == 'set_source':
                self.current_source = cmd_dict.get('source', '')
                await self._threadsafe_broadcast(json.dumps({'type': 'source_changed', 'source': self.current_source}))
                await self._reply({'type': 'source_set', 'ok': True})

            elif action == 'scan':
                ports = await self.usb.scan_ports()
                await self._reply({'type': 'ports', 'ports': ports})

            elif action == 'connect':
                port_name = cmd_dict.get('port', '')
                ok = await self.usb.connect(port_name)
                if ok:
                    await self._reply({'type': 'connected', 'port': port_name})
                else:
                    await self._reply({'type': 'error', 'msg': f'Connect failed: {port_name}'})

            elif action == 'disconnect':
                await self.usb.disconnect()
                await self._reply({'type': 'disconnected'})

            elif action == 'compile':
                source = cmd_dict.get('source', '')
                result = self.compiler.compile(source)
                success = len(result.errors) == 0
                if success:
                    self._last_symbol_table = result.symbol_table
                response = {
                    'type': 'compile_result',
                    'success': success,
                    'binary': base64.b64encode(result.binary).decode('ascii') if result.binary else None,
                    'stats': result.stats,
                    'errors': result.errors,
                    'symbol_table': result.symbol_table,
                }
                await self._reply(response)

            elif action == 'check':
                source = cmd_dict.get('source', '')
                errors = self.compiler.check_sync(source)
                await self._reply({
                    'type': 'check_result',
                    'errors': errors,
                })

            elif action == 'deploy':
                binary_b64 = cmd_dict.get('binary', '')
                if not binary_b64:
                    await self._reply({'type': 'error', 'msg': 'No binary data'})
                    return
                binary = base64.b64decode(binary_b64)
                if self.usb.is_connected:
                    await self.usb.deploy(binary)
                else:
                    self.monitor.stop()
                    await self._swd_deploy(binary)
                    self.monitor.start()
                await self._reply({'type': 'deploy_sent', 'size': len(binary)})

            elif action == 'start':
                if self.usb.is_connected:
                    await self.usb.start_engine()
                else:
                    self.monitor.stop()
                    await self._swd_control('START')
                    self.monitor.start()
                await self._reply({'type': 'command_sent', 'cmd': 'START'})

            elif action == 'stop':
                if self.usb.is_connected:
                    await self.usb.stop_engine()
                else:
                    self.monitor.stop()
                    await self._swd_control('STOP')
                    self.monitor.start()
                await self._reply({'type': 'command_sent', 'cmd': 'STOP'})

            elif action == 'reset':
                if self.usb.is_connected:
                    await self.usb.reset_engine()
                else:
                    self.monitor.stop()
                    await self._swd_control('RESET')
                    self.monitor.start()
                await self._reply({'type': 'command_sent', 'cmd': 'RESET'})

            elif action == 'read_wires':
                start = cmd_dict.get('start', 0)
                count = cmd_dict.get('count', 64)
                await self.usb.read_wires(start, count)

            elif action == 'write_wire':
                idx = cmd_dict.get('idx', 0)
                value = cmd_dict.get('value', 0.0)
                await self.usb.write_wire(idx, value)
                await self._reply({'type': 'wire_written', 'idx': idx, 'value': value})

            elif action == 'get_symbol_table':
                await self._reply({
                    'type': 'symbol_table',
                    'symbols': self._last_symbol_table,
                })

            elif action == 'ai_chat':
                message = cmd_dict.get('message', '')
                source = cmd_dict.get('source', '')
                result = await self.ai.chat(message, source)
                await self._reply({
                    'type': 'ai_response',
                    'success': result.get('success', False),
                    'response': result.get('response', ''),
                })

            elif action == 'ai_set_key':
                key = cmd_dict.get('key', '')
                self.ai.set_api_key(key)
                await self._reply({
                    'type': 'ai_key_set',
                    'available': self.ai.is_available,
                })

            elif action == 'ai_clear':
                self.ai.clear_history()
                await self._reply({'type': 'ai_cleared'})

            else:
                await self._reply({'type': 'error', 'msg': f'Unknown command: {action}'})

        except Exception as e:
            logger.error(f"Command '{action}' failed: {e}")
            await self._reply({'type': 'error', 'msg': str(e)})

    # ── USB status callback ────────────────────────────────────

    async def _on_usb_status(self, status_code, payload):
        """Callback from USBServer, translate status frames to JSON and push to WS clients."""
        import struct
        msg = {}
        if status_code == 0x20:  # WIRE_DATA
            count = len(payload) // 4
            values = list(struct.unpack(f'<{count}f', payload[:count * 4]))
            msg = {'type': 'wires', 'values': values}
        elif status_code == 0x30:  # HEARTBEAT
            samples = struct.unpack('<I', payload[:4])[0] if len(payload) >= 4 else 0
            running = payload[4] if len(payload) >= 5 else 0
            msg = {'type': 'heartbeat', 'samples': samples, 'running': bool(running)}
        elif status_code == 0x40:  # ERROR
            msg = {'type': 'error', 'msg': payload.decode('ascii', errors='replace')}
        else:
            msg = {'type': 'status', 'code': status_code, 'len': len(payload)}

        if msg:
            await self._broadcast(json.dumps(msg))

    # ── SWD 部署 (USB 未连接时 fallback) ─────────────────────

    async def _swd_deploy(self, binary: bytes):
        """通过 pyOCD SWD 直接写 DTCM 部署路由表"""
        import struct
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._swd_deploy_sync, binary)

    def _swd_deploy_sync(self, binary: bytes):
        """
        SWD 部署 v2.0 格式:
        [Header:16B][RouteTable][ParamTable][StateTable]
        Header: [magic:4][format:4][n_routes:2][n_params:2][n_states:2][reserved:2]
        """
        import struct
        from pyocd.core.helpers import ConnectHelper

        # Parse header (16 bytes)
        magic, fmt, n_routes, n_params, n_states, _ = struct.unpack_from('<IIHHHH', binary, 0)
        if magic != 0x50523047:
            raise ValueError(f"Bad magic: 0x{magic:08X}")

        off = 16
        rb = binary[off:off + n_routes * 16]; off += n_routes * 16
        pb = binary[off:off + n_params * 16]; off += n_params * 16
        sb = binary[off:off + n_states * 16]

        # DTCM addresses (from memory_map.h)
        ADDR_N_ROUTES   = 0x20000040
        ADDR_N_PARAMS   = 0x20000044
        ADDR_N_STATES   = 0x20000048
        ADDR_PROG_MAGIC = 0x2000004C
        ADDR_ROUTE_TBL  = 0x20001710
        ADDR_PARAM_TBL  = 0x20005710
        ADDR_STATE_TBL  = 0x20007710

        with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
            t = session.target

            # 1. Pause engine
            t.write32(ADDR_N_ROUTES, 0)
            import time
            time.sleep(0.001)

            # 2. Write new program (clear + write)
            t.write_memory_block8(ADDR_ROUTE_TBL, bytes(rb))
            t.write_memory_block8(ADDR_PARAM_TBL, bytes(pb))
            t.write_memory_block8(ADDR_STATE_TBL, bytes(sb))

            # 3. Update engine state
            t.write32(ADDR_N_ROUTES, len(rb) // 16)
            t.write32(ADDR_N_PARAMS, len(pb) // 16)
            t.write32(ADDR_N_STATES, len(sb) // 16)
            t.write32(ADDR_PROG_MAGIC, 0x50523047)

    async def _swd_control(self, cmd: str):
        """通过 SWD 控制引擎 (启动/停止/复位)"""
        import struct
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._swd_control_sync, cmd)

    def _swd_control_sync(self, cmd: str):
        from pyocd.core.helpers import ConnectHelper
        with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
            t = session.target
            if cmd == 'START':
                t.write32(0x20000040, t.read32(0x20000040))
            elif cmd == 'STOP':
                t.write32(0x20000040, 0)
            elif cmd == 'RESET':
                t.write_memory_block8(0x20001710, bytes(1024 * 16))
                t.write_memory_block8(0x20005710, bytes(512 * 16))
                t.write_memory_block8(0x20007710, bytes(256 * 16))
                t.write32(0x20000040, 0)
                t.write32(0x2000004C, 0)
        logger.info("SWD control: %s", cmd)

    # ── broadcast ──────────────────────────────────────────────

    async def _broadcast(self, message):
        """Send to all WS clients."""
        if not self.ws_clients:
            return
        disconnected = set()
        for ws in self.ws_clients:
            try:
                await ws.send(message)
            except websockets.exceptions.ConnectionClosed:
                disconnected.add(ws)
        self.ws_clients -= disconnected
