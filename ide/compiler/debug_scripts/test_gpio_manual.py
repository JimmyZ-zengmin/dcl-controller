#!/usr/bin/env python3
"""
GPIO 输出验证测试 — 手动注入传感器值
流程: 编译 DCL → 部署 → 手动写 SENSOR_MAP → 验证 GPIO 输出

测试程序 test_logic.dcl:
  SENSOR a FROM ADC1_CH0 SCALE 1.0 0.0
  SENSOR b FROM ADC1_CH1 SCALE 1.0 0.0
  LOGIC all_high = a AND b AND ... (与 standalone_logic_test.py 类似)
"""
import sys, os, struct, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dcl_compiler import DCLCompiler
from dcl_hardware import Hardware, ADDRESSES, TIM1_BASE, NVIC_ISER0, TIM1_UP_IRQ_BIT

DCL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test_logic.dcl')

print("=" * 70)
print("GPIO 输出验证测试 — 手动注入传感器值")
print("=" * 70)

# ── 1. 读取 DCL 源码 ──
with open(DCL_FILE, 'r', encoding='utf-8') as f:
    DCL_SRC = f.read()

print(f"\nDCL 源文件: {DCL_FILE}")
print(f"源码:\n{DCL_SRC}")

# ── 2. 编译 ──
c = DCLCompiler()
c.parse(DCL_SRC)
c.topological_sort()
c.validate_resources()
binary = c.generate_binary()

n_routes = struct.unpack('<I', binary[0:4])[0]
n_params = struct.unpack('<I', binary[4:8])[0]
n_active  = struct.unpack('<I', binary[8:12])[0]

print(f"\n编译结果: {n_routes} routes, {n_params} params, bin={len(binary)}B")

# Wire 映射
print(f"\nWire 映射:")
for name, idx in sorted(c.wire_index.items(), key=lambda x: x[1]):
    print(f"  WIRE[{idx:3d}] = {name}")

print(f"\nSensor 映射:")
for name, idx in c.sensor_source_map.items():
    print(f"  SENSOR[{idx}] = {name}")

# 关键索引
print(f"\nSensor 源顺序: {list(c.sensor_source_map.keys())}")

# ── 3. 部署 ──
hw = Hardware()
if not hw.connect():
    print("\n❌ 连接失败!"); sys.exit(1)

print("\n✅ 硬件连接成功")

ok = hw.deploy(binary)
if not ok:
    print(f"❌ 部署失败: {hw.last_error}")
    sys.exit(1)

print("✅ 部署完成")
time.sleep(0.1)

# ── 4. 工具函数 ──
SENSOR_MAP = ADDRESSES['SENSOR_MAP']
WIRE_MAP   = ADDRESSES['WIRE_MAP']
GPIOE_ODR  = 0x58021014

def f2b(f): return struct.unpack('I', struct.pack('f', f))[0]
def b2f(b): return struct.unpack('f', struct.pack('I', b & 0xFFFFFFFF))[0]

def w_float(addr, val): hw.write32(addr, f2b(val))
def r_float(addr):
    r = hw.read32(addr, 1)
    return b2f(r[0]) if r else None

def set_sensor(idx, val):
    """写入传感器值"""
    w_float(SENSOR_MAP + idx * 4, val)

def get_wire(idx):
    """读取 Wire 值"""
    return r_float(WIRE_MAP + idx * 4)

def get_gpio():
    raw = hw.read32(GPIOE_ODR, 1)
    if raw:
        v = raw[0]
        return {
            'PE0': (v >> 0) & 1,
            'PE1': (v >> 1) & 1,
            'PE2': (v >> 2) & 1,
            'PE3': (v >> 3) & 1,
        }
    return None

# ── 5. 测试用例 ──
print("\n" + "=" * 70)
print("注入测试")
print("=" * 70)

# 获取 wire 索引
ix_a = c.wire_index.get('a', -1)
ix_b = c.wire_index.get('b', -1)
ix_c = c.wire_index.get('c', -1)
ix_d = c.wire_index.get('d', -1)

ix_all_high = c.wire_index.get('all_high', -1)
ix_any_high = c.wire_index.get('any_high', -1)
ix_not_a_and_b = c.wire_index.get('not_a_and_b', -1)

# 获取传感器索引
s_a = c.sensor_source_map.get('ADC1_CH0', -1)
s_b = c.sensor_source_map.get('ADC1_CH1', -1)
s_c = c.sensor_source_map.get('ADC1_CH2', -1)
s_d = c.sensor_source_map.get('ADC1_CH3', -1)

print(f"\n传感器: a→CH0[{s_a}], b→CH1[{s_b}], c→CH2[{s_c}], d→CH3[{s_d}]")
print(f"Wires: a[{ix_a}], b[{ix_b}], c[{ix_c}], d[{ix_d}]")
print(f"       all_high[{ix_all_high}], any_high[{ix_any_high}], not_a_and_b[{ix_not_a_and_b}]")

# 停止 ISR 以便手动注入
hw.write32(TIM1_BASE, 0)

cases = [
    # a, b, c, d, exp_all, exp_any, exp_nab, desc
    (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "全零"),
    (1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0, "全真(NOT a=0)"),
    (0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, "a=0,b=1 → NOT a AND b=1"),
    (1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, "a=1,b=0,c=1,d=1"),
    (0.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0, "a=0,b=c=d=1"),
]

print(f"\n{'#':>2} {'描述':20s} | {'all_h':>6s} {'PE0':>3s} {'✓':>1s} | {'any_h':>6s} {'PE1':>3s} {'✓':>1s} | {'nab':>6s} {'PE2':>3s} {'✓':>1s}")
print("-" * 100)

all_pass = True
for i, (va, vb, vc, vd, eax, eaxh, enab, desc) in enumerate(cases):
    # 注入传感器值
    if s_a >= 0: set_sensor(s_a, va)
    if s_b >= 0: set_sensor(s_b, vb)
    if s_c >= 0: set_sensor(s_c, vc)
    if s_d >= 0: set_sensor(s_d, vd)

    # 运行 ISR
    hw.write32(TIM1_BASE + 0x10, 0x0000FFFF)
    hw.write32(TIM1_BASE, 1)
    time.sleep(0.002)  # 2ms
    hw.write32(TIM1_BASE, 0)

    # 读取 Wire 值
    ah = get_wire(ix_all_high)
    ahx = get_wire(ix_any_high)
    nab = get_wire(ix_not_a_and_b)

    def chk(got, exp):
        if got is None: return "FAIL"
        return "PASS" if abs(got - exp) < 0.01 else "FAIL"

    r1 = chk(ah, eax)
    r2 = chk(ahx, eaxh)
    r3 = chk(nab, enab)

    # 读取 GPIO
    gpio = get_gpio()
    if gpio:
        pe0 = gpio['PE0']
        pe1 = gpio['PE1']
        pe2 = gpio['PE2']
    else:
        pe0 = pe1 = pe2 = 0

    exp_pe0 = 1 if eax > 0.5 else 0
    exp_pe1 = 1 if eaxh > 0.5 else 0
    exp_pe2 = 1 if enab > 0.5 else 0

    ok_pe0 = "✓" if pe0 == exp_pe0 else "✗"
    ok_pe1 = "✓" if pe1 == exp_pe1 else "✗"
    ok_pe2 = "✓" if pe2 == exp_pe2 else "✗"

    if r1 != "PASS" or r2 != "PASS" or r3 != "PASS" or \
       pe0 != exp_pe0 or pe1 != exp_pe1 or pe2 != exp_pe2:
        all_pass = False

    ah_str = f"{ah:.4f}" if ah is not None else "?"
    ahx_str = f"{ahx:.4f}" if ahx is not None else "?"
    nab_str = f"{nab:.4f}" if nab is not None else "?"

    print(f"{i+1:2d}  {desc:20s} | {ah_str:>6s} {pe0:3d} {ok_pe0:1s} | {ahx_str:>6s} {pe1:3d} {ok_pe1:1s} | {nab_str:>6s} {pe2:3d} {ok_pe2:1s}")

print("-" * 100)
print(f"\n{'✅ 全部通过! Wire 和 GPIO 输出正常' if all_pass else '❌ 存在失败!'}")

# 打印 GPIO 状态
gpio = get_gpio()
if gpio:
    print(f"\nGPIOE 输出:")
    print(f"  PE0 (all_high)    = {gpio['PE0']}")
    print(f"  PE1 (any_high)    = {gpio['PE1']}")
    print(f"  PE2 (not_a_and_b) = {gpio['PE2']}")

print("\n完成.")
