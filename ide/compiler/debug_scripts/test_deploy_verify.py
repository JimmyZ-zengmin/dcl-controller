#!/usr/bin/env python3
"""测试编译器修复：编译 + 部署 + 验证 test_logic.dcl"""

import json
import urllib.request

URL = "http://localhost:8765"

def api_post(endpoint, data=None):
    url = f"{URL}{endpoint}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

def api_get(endpoint, params=None):
    url = f"{URL}{endpoint}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, method='GET')
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


# 1. Compile
print("=== COMPILE test_logic.dcl ===")
r = api_post('/api/compile', {'file': 'test_logic.dcl'})
print(f"ok: {r['ok']}, routes: {r['routes']}, wires: {r['wires']}, params: {r['params']}")
print(f"binary: {r['binary']}, size: {r['size']}")

# 2. Deploy
print("\n=== DEPLOY ===")
r2 = api_post('/api/deploy', {'binary': r['binary']})
print(f"ok: {r2['ok']}, size: {r2['size']}, active_routes: {r2.get('routes', '?')}")

# 3. Read wires (12 wires expected)
print("\n=== WIRE VALUES ===")
r3 = api_get('/api/wires', {'s': 0, 'c': 16})
for i, v in enumerate(r3['values']):
    print(f"  WIRE[{i}] = {v}")

# 4. Read actuators (should all be 0 for now, no real sensor input)
print("\n=== ACTUATOR VALUES ===")
r4 = api_get('/api/wires', {'s': 0, 'c': 64})
# Actually we need to read actuator_status - let's see if wires API can do that
# For now, check the expected logic behavior:
# All sensors = 0 (no input), so:
# all_high = 0 AND 0 AND 0 AND 0 = 0
# any_high = 0 OR 0 OR 0 OR 0 = 0
# not_a_and_b = (NOT 0) AND 0 = 1 AND 0 = 0
print("\nExpected behavior (all sensors=0):")
print("  all_high (WIRE[4]) = 0 (0 AND 0 AND 0 AND 0)")
print("  any_high (WIRE[5]) = 0 (0 OR 0 OR 0 OR 0)")
print("  not_a_and_b (WIRE[6]) = 0 ((NOT 0) AND 0 = 1 AND 0)")
