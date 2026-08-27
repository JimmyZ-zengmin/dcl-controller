import asyncio, json, websockets, os, sys

async def test():
    sys.stdout = open(os.devnull, 'w')  # suppress noisy logs
    ws = await websockets.connect('ws://localhost:8765')
    sys.stdout = sys.__stdout__

    dcl_path = os.path.join(os.path.dirname(__file__), '..', 'compiler', 'reactor_control.dcl')
    src = open(dcl_path, encoding='utf-8').read()
    print(f'Loaded {len(src)} chars')

    # Compile
    await ws.send(json.dumps({'cmd': 'compile', '_id': 1, 'source': src}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get('_id') == 1:
            print('Compile:', msg.get('success'), msg.get('stats', {}))
            binary = msg.get('binary')
            break

    # Deploy
    print('Deploying via SWD...')
    await ws.send(json.dumps({'cmd': 'deploy', '_id': 2, 'binary': binary}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get('_id') == 2:
            print('Deploy:', msg)
            break

    # Start
    await ws.send(json.dumps({'cmd': 'start', '_id': 3}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get('_id') == 3:
            print('Start:', msg)
            break

    # Monitor
    print('Monitoring 5s...')
    try:
        async with asyncio.timeout(5):
            while True:
                msg = json.loads(await ws.recv())
                if msg.get('type') == 'monitor_status':
                    r = msg.get('routes', 0)
                    s = msg.get('samples', 0)
                    e = msg.get('engine_running', 0)
                    jit = msg.get('period_max', 0) - msg.get('period_min', 0)
                    print(f'  R={r} S={s} E={e} jit={jit}')
    except asyncio.TimeoutError:
        pass

    await ws.close()
    print('Done')

asyncio.run(test())
