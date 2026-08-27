#!/usr/bin/env python3
"""DCL IDE shell entry point — GUI (native window) or CLI (terminal)"""

import asyncio
import logging
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('dcl-ide')

HTTP_PORT = 8081
WS_PORT = 8765


def run_server(http_port, ws_port):
    """Run the asyncio IDE server in a background thread."""
    from server.ide_server import IDEServer

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _start():
        ide = IDEServer(http_port=http_port, ws_port=ws_port)
        await ide.start()
        return ide

    ide = loop.run_until_complete(_start())
    logger.info('IDE server running on http://localhost:%d', http_port)
    loop.run_forever()


def run_gui(http_port, ws_port):
    """Start server + pywebview native window."""
    server_thread = threading.Thread(target=run_server, args=(http_port, ws_port), daemon=True)
    server_thread.start()
    time.sleep(1)

    import webview
    url = f'http://localhost:{http_port}'
    logger.info('Native window opening: %s', url)
    window = webview.create_window(
        'DCL IDE',
        url,
        width=1280,
        height=800,
        min_size=(800, 600),
    )
    webview.start(debug=False)


def run_cli(http_port, ws_port, monitor_only):
    """Connect CLI to existing server (or start new if requested)."""
    if monitor_only:
        asyncio.run(_cmd_cli_monitor(http_port, ws_port))
    else:
        asyncio.run(_cmd_cli_repl(http_port, ws_port))


async def _cmd_cli_repl(_http_port, ws_port):
    from shell.cli import WSClient, cmd_repl
    client = WSClient(port=ws_port)
    try:
        await client.connect()
    except Exception as e:
        from shell.cli import _red, _ylw
        print(_red(f"  Connect failed: {e}"))
        print(_ylw("  Start GUI first: python shell/main.py"))
        sys.exit(1)
    try:
        await cmd_repl(client)
    finally:
        await client.close()


async def _cmd_cli_monitor(_http_port, ws_port):
    from shell.cli import WSClient, _handle_push
    client = WSClient(port=ws_port)
    try:
        await client.connect()
    except Exception as e:
        from shell.cli import _red
        print(_red(f"  Connect failed: {e}"))
        sys.exit(1)
    client.on_push(_handle_push)
    from shell.cli import _cyn
    print(_cyn("  RTT Monitor (Ctrl+C stop)...\n"))
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await client.close()


def main():
    import argparse
    p = argparse.ArgumentParser(prog='dcl-ide', description='DCL IDE — GUI or CLI')
    p.add_argument('--cli', action='store_true',
                   help='Terminal CLI mode (connect to running GUI)')
    p.add_argument('--new', action='store_true',
                   help='CLI mode: start a new server instance')
    p.add_argument('--monitor', action='store_true',
                   help='CLI mode: monitor-only (no REPL)')
    p.add_argument('--http-port', type=int, default=HTTP_PORT,
                       help=f'HTTP port (default {HTTP_PORT})')
    p.add_argument('--ws-port', type=int, default=WS_PORT,
                       help=f'WebSocket port (default {WS_PORT})')
    args = p.parse_args()

    if args.cli:
        if args.new:
            # Start a new server in background, then connect CLI
            server_thread = threading.Thread(
                target=run_server, args=(args.http_port, args.ws_port), daemon=True)
            server_thread.start()
            time.sleep(1)
        run_cli(args.http_port, args.ws_port, args.monitor)
    else:
        run_gui(args.http_port, args.ws_port)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
