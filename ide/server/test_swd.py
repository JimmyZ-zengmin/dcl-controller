import asyncio, json, websockets

async def test():
    ws = await websockets.connect('ws://localhost:8765')
    src = open('compiler/reactor_control.dcl', encoding='utf-8').read()

    # Compile
    await ws.send(json.dumps({'cmd': 'compile', '_id': 1, 'source': src}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get('_id') == 1:
            print('Compile:', msg.get('success'), '|', msg.get('stats', {}))
            binary = msg.get('binary')
            break

    # Deploy (SWD)
    print('Deploying via SWD...', flush=True)
    await ws.send(json.dumps({'cmd': 'deploy', '_id': 2, 'binary': binary}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get('_id') == 2:
            print('Deploy:', msg, flush=True)
            break

    # Monitor for 8s - check route count changes
    print('Monitoring 8s (looking for R to change from 49 to 40)...', flush=True)
    r_values = set()
    try:
        async with asyncio.timeout(8):
            while True:
                msg = json.loads(await ws.recv())
                if msg.get('type') == 'monitor_status':
                    r = msg.get('routes', 0)
                    r_values.add(r)
    except asyncio.TimeoutError:
        pass

    print(f'Route counts seen: {sorted(r_values)}', flush=True)
    if 40 in r_values:
        print('SUCCESS - new program loaded (R=40 observed)!', flush=True)
    else:
        print('FAIL - R stayed at 49, deploy did not take effect', flush=True)

    await ws.close()

asyncio.run(test())
