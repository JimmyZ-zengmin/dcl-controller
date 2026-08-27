#!/usr/bin/env python3
"""
独立 DCL 逻辑验证测试 — 不依赖 IDE 运行时
流程: 编译 DCL → 手动部署到硬件 → 注入已知输入 → 读取 WIRE 值 → 对比预期

binary 格式: header(12B) + route_data(nb*16) + param_data(np*16) + io_map(64B) + crc(4B)
"""
import sys, os, struct, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dcl_compiler import DCLCompiler
from dcl_hardware import Hardware, ADDRESSES, TIM1_BASE, NVIC_ISER1, TIM1_UP_IRQ_BIT

TEST_DCL = """
SENSOR a FROM ADC1_CH0 SCALE 1.0 0.0
SENSOR b FROM ADC1_CH1 SCALE 1.0 0.0
SENSOR c FROM ADC1_CH2 SCALE 1.0 0.0
SENSOR d FROM ADC1_CH3 SCALE 1.0 0.0
LOGIC all_high = a AND b AND c AND d
LOGIC any_high = a OR b OR c OR d
LOGIC not_a_and_b = NOT a AND b
OUTPUT all_high TO GPIO_PE0
OUTPUT any_high TO GPIO_PE1
OUTPUT not_a_and_b TO GPIO_PE2
"""

print("=" * 70)
print("独立 DCL 逻辑验证")
print("=" * 70)

# ── 1. 编译 ──
c = DCLCompiler()
c.parse(TEST_DCL)
c.topological_sort()
c.validate_resources()
binary = c.generate_binary()

n_routes = struct.unpack('<I', binary[0:4])[0]
n_params = struct.unpack('<I', binary[4:8])[0]
n_active  = struct.unpack('<I', binary[8:12])[0]

route_data = binary[12 : 12 + n_routes * 16]
param_data = binary[12 + n_routes*16 : 12 + n_routes*16 + n_params*16]

print(f"编译: {n_routes} routes, {n_params} params, bin={len(binary)}B")
print(f"  route_data: {len(route_data)}B, param_data: {len(param_data)}B")

# wire / sensor 映射
print("\nWire 映射:", {k: v for k, v in sorted(c.wire_index.items(), key=lambda x: x[1])})
print("Sensor 映射:", c.sensor_source_map)

# 关键索引
ix = {name: c.wire_index.get(name, -1) for name in ['a','b','all_high','any_high','not_a_and_b']}
print(f"关键 wire 索引: {ix}")

# ── 2. 部署 ──
hw = Hardware()
if not hw.connect():
    print("连接失败!"); sys.exit(1)

# 使用 deploy() 方法: 包含握手标志 + NVIC 使能 + 启动 ISR
ok = hw.deploy(binary)
if not ok:
    print(f"部署失败: {hw.last_error}")
    sys.exit(1)

print("\n部署完成, TIM1 已启动 (含 DEPLOYED_MAGIC 握手)")
time.sleep(0.03)

# ── 3. 工具函数 ──
SENSOR_MAP = ADDRESSES['SENSOR_MAP']
WIRE_MAP   = ADDRESSES['WIRE_MAP']
GPIOE_ODR  = 0x58021014

def f2b(f): return struct.unpack('I', struct.pack('f', f))[0]
def b2f(b): return struct.unpack('f', struct.pack('I', b & 0xFFFFFFFF))[0]

def w_float(addr, val): hw.write32(addr, f2b(val))
def r_float(addr):
    r = hw.read32(addr, 1)
    return b2f(r[0]) if r else None

SENSOR_NAME_MAP = {'a': 'ADC1_CH0', 'b': 'ADC1_CH1', 'c': 'ADC1_CH2', 'd': 'ADC1_CH3'}

def set_s(name, val):
    src = SENSOR_NAME_MAP.get(name, name)
    idx = c.sensor_source_map.get(src)
    if idx is not None:
        w_float(SENSOR_MAP + idx * 4, val)

def get_w(name):
    idx = c.wire_index.get(name)
    if idx is not None: return r_float(WIRE_MAP + idx*4)
    return None

# 读初始状态
print("\n初始 ISR 运行中 (sensor[0] 被 VREFINT 覆盖):")
sensors = hw.read_sensors(0, 4)
wires   = hw.read_wires(0, 16)
print(f"  sensors: {[f'{s:.3f}' for s in sensors]}")
print(f"  wires[0..11]: {[f'{wires[i]:.3f}' if i < len(wires) else '?' for i in range(12)]}")

# ── 4. 注入已知输入测试 ──
print("\n" + "=" * 70)
print("注入已知输入测试")
print("=" * 70)

cases = [
#    a    b    c    d       exp_all  exp_any  exp_nab   描述
  (0.0, 0.0, 0.0, 0.0,     0.0,     0.0,     0.0,   "全零"),
  (1.0, 1.0, 1.0, 1.0,     1.0,     1.0,     0.0,   "全真(NOT a=0)"),
  (1.0, 1.0, 0.0, 0.0,     0.0,     1.0,     0.0,   "a=1,b=1,c=0,d=0"),
  (0.0, 1.0, 0.0, 0.0,     0.0,     1.0,     1.0,   "a=0,b=1,NOT a AND b=1"),
  (0.0, 0.0, 1.0, 1.0,     0.0,     1.0,     0.0,   "a=0,b=0,c=1,d=1"),
  (1.0, 0.0, 1.0, 1.0,     0.0,     1.0,     0.0,   "a=1,b=0,c=1,d=1"),
  (0.0, 1.0, 1.0, 1.0,     0.0,     1.0,     1.0,   "a=0,b=c=d=1 → all=0,any=1,nab=1"),
]

print(f"\n{'#':>2} {'描述':22s} | {'wire_all':>8s} {'PE0':>3s} {'✓':>1s} | "
      f"{'wire_any':>8s} {'PE1':>3s} {'✓':>1s} | "
      f"{'wire_nab':>8s} {'PE2':>3s} {'✓':>1s}")
print("-" * 110)

all_pass = True
for i, (va, vb, vc, vd, eax, eaxh, enab, desc) in enumerate(cases):
    # 停止 ISR (部署后 ISR 已启动, 这里暂停以注入测试值)
    hw.write32(TIM1_BASE, 0)

    # 注入 sensor 值
    set_s('a', va); set_s('b', vb); set_s('c', vc); set_s('d', vd)

    # 运行 ~10 个 ISR 周期 (1ms)
    hw.write32(TIM1_BASE + 0x10, 0x0000FFFF)
    hw.write32(TIM1_BASE, 1)
    time.sleep(0.002)  # 2ms, 确保 ISR 处理完
    hw.write32(TIM1_BASE, 0)

    # 读 wires
    ah = get_w('all_high')
    ahx = get_w('any_high')
    nab = get_w('not_a_and_b')

    def chk(got, exp):
        if got is None: return "FAIL"
        return "PASS" if abs(got - exp) < 0.01 else "FAIL"

    r1 = chk(ah, eax)
    r2 = chk(ahx, eaxh)
    r3 = chk(nab, enab)

    # 读 GPIO 引脚 (PE0/PE1/PE2)
    raw = hw.read32(GPIOE_ODR, 1)
    if raw:
        odr = raw[0]
        pe0 = (odr >> 0) & 1
        pe1 = (odr >> 1) & 1
        pe2 = (odr >> 2) & 1
    else:
        pe0 = pe1 = pe2 = 0

    # GPIO 预期: wire > 0.5 → 高电平
    exp_pe0 = 1 if eax > 0.5 else 0
    exp_pe1 = 1 if eaxh > 0.5 else 0
    exp_pe2 = 1 if enab > 0.5 else 0

    ok_pe0 = "✓" if pe0 == exp_pe0 else "✗"
    ok_pe1 = "✓" if pe1 == exp_pe1 else "✗"
    ok_pe2 = "✓" if pe2 == exp_pe2 else "✗"

    if r1 != "PASS" or r2 != "PASS" or r3 != "PASS" or \
       pe0 != exp_pe0 or pe1 != exp_pe1 or pe2 != exp_pe2:
        all_pass = False

    print(f"{i+1:2d}  {desc:22s} | "
          f"{ah:8.4f} {pe0:3d} {ok_pe0:1s} | "
          f"{ahx:8.4f} {pe1:3d} {ok_pe1:1s} | "
          f"{nab:8.4f} {pe2:3d} {ok_pe2:1s}")

print("-" * 110)
print(f"\n{'✅ 全部通过! GPIO 输出正常' if all_pass else '❌ 存在失败! Wire 或 GPIO 输出不正确'}")

# ── 5. GPIO 引脚状态 ──
print("\n" + "-" * 70)
print("GPIOE 输出 (最后一次测试后的 ODR)")
print("-" * 70)
raw = hw.read32(GPIOE_ODR, 1)
if raw:
    v = raw[0]
    print(f"  ODR = 0x{v:08X}")
    print(f"  PE0 (all_high)    = {(v>>0)&1}")
    print(f"  PE1 (any_high)    = {(v>>1)&1}")
    print(f"  PE2 (not_a_and_b) = {(v>>2)&1}")

print("\n完成.")
