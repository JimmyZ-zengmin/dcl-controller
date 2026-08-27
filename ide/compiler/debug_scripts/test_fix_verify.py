#!/usr/bin/env python3
"""验证修复后的 deploy 功能"""
import json, urllib.request, sys

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

if not r['ok']:
    print(f"COMPILE ERROR: {r.get('err', 'unknown')}")
    sys.exit(1)

# 2. Deploy
print("\n=== DEPLOY ===")
r2 = api_post('/api/deploy', {'binary': r['binary']})
print(f"ok: {r2['ok']}, size: {r2['size']}, active_routes: {r2.get('routes', '?')}")
if not r2['ok']:
    print(f"DEPLOY ERROR: {r2.get('err', 'unknown')}")
    sys.exit(1)

# 3. Read wires
print("\n=== WIRE VALUES (16 wires) ===")
r3 = api_get('/api/wires', {'s': 0, 'c': 16})
for i, v in enumerate(r3['values']):
    print(f"  WIRE[{i}] = {v}")

# 4. Verify expected behavior
print("\n=== VERIFICATION ===")
wires = r3['values']
print(f"Expected: all sensors=0 -> all outputs should be 0")

# Wire mapping for test_logic.dcl:
# WIRE[0-3]: sensors a,b,c,d (DIRECT from ADC)
# WIRE[4]: tmp (a AND b)
# WIRE[5]: tmp (tmp AND c) 
# WIRE[6]: all_high (tmp AND d)
# WIRE[7]: tmp (a OR b)
# WIRE[8]: tmp (tmp OR c)
# WIRE[9]: any_high (tmp OR d)
# WIRE[10]: tmp (NOT a)
# WIRE[11]: not_a_and_b (tmp AND b)
# WIRE[12-14]: OUTPUT wires (out_all_high, out_any_high, out_not_a_and_b)

expected_zeros = ['WIRE[4]', 'WIRE[5]', 'WIRE[6]', 'WIRE[7]', 'WIRE[8]', 'WIRE[9]', 'WIRE[10]', 'WIRE[11]']
all_ok = True
for i in range(4, 12):
    v = wires[i] if i < len(wires) else None
    if v is not None and abs(v) < 0.01:
        print(f"  OK: WIRE[{i}] = {v} (expected ~0)")
    else:
        print(f"  FAIL: WIRE[{i}] = {v} (expected ~0)")
        all_ok = False

if all_ok:
    print("\n✓ ALL WIRE VALUES CORRECT - DEPLOY SUCCESS!")
else:
    print("\n✗ SOME WIRE VALUES INCORRECT - NEEDS DEBUGGING")
