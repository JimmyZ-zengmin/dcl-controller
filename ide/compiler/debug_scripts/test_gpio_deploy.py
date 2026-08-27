#!/usr/bin/env python3
"""
GPIO 输出验证测试 — 中等复杂度 PLC 程序
流程: 编译 DCL → 部署到硬件 → 验证 GPIO 输出与预期一致

测试程序 test_gpio_verify.dcl:
  - 2 个传感器 (常数: temp=25.0, enable=1.0)
  - 2 个比较运算 (temp_high, temp_ok)
  - 1 个定时器 (2秒后 system_ready=1)
  - 1 个综合逻辑 (all_ok = temp_high AND system_ready AND enable)
  - 4 个 GPIO 输出 (PE0-PE3)

预期行为:
  - t=0s: PE1=1 (temp_high), PE2=1 (temp_ok), PE0=0, PE3=0
  - t>2s: PE0=1 (all_ok), PE3=1 (system_ready)
"""
import sys, os, struct, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dcl_compiler import DCLCompiler
from dcl_hardware import Hardware, ADDRESSES, TIM1_BASE, NVIC_ISER0, TIM1_UP_IRQ_BIT

DCL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test_gpio_verify.dcl')

print("=" * 70)
print("GPIO 输出验证测试 — 中等复杂度 PLC 程序")
print("=" * 70)

# ── 1. 读取 DCL 源码 ──
with open(DCL_FILE, 'r', encoding='utf-8') as f:
    DCL_SRC = f.read()

print(f"\nDCL 源文件: {DCL_FILE}")
print(f"源码行数: {len(DCL_SRC.splitlines())}")

# ── 2. 编译 ──
c = DCLCompiler()
c.parse(DCL_SRC)
c.topological_sort()
c.validate_resources()
binary = c.generate_binary()

n_routes = struct.unpack('<I', binary[0:4])[0]
n_params = struct.unpack('<I', binary[4:8])[0]
n_active  = struct.unpack('<I', binary[8:12])[0]

route_data = binary[12 : 12 + n_routes * 16]
param_data = binary[12 + n_routes*16 : 12 + n_routes*16 + n_params * 16]

print(f"\n编译结果:")
print(f"  路由数: {n_routes}")
print(f"  参数数: {n_params}")
print(f"  激活数: {n_active}")
print(f"  二进制大小: {len(binary)} bytes")

# 打印 wire 映射
print(f"\nWire 映射:")
for name, idx in sorted(c.wire_index.items(), key=lambda x: x[1]):
    print(f"  WIRE[{idx:3d}] = {name}")

print(f"\nSensor 映射:")
for name, idx in c.sensor_source_map.items():
    print(f"  SENSOR[{idx}] = {name}")

# ── 3. 部署 ──
hw = Hardware()
if not hw.connect():
    print("\n❌ 连接失败!")
    sys.exit(1)

print("\n硬件连接成功")

# 使用 deploy() 方法: 包含握手标志 + NVIC 使能 + 启动 ISR
ok = hw.deploy(binary)
if not ok:
    print(f"❌ 部署失败: {hw.last_error}")
    sys.exit(1)

print("✅ 部署完成, TIM1 已启动 (含 DEPLOYED_MAGIC 握手)")

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

def get_w(name):
    idx = c.wire_index.get(name)
    if idx is not None: return r_float(WIRE_MAP + idx*4)
    return None

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

# ── 5. 验证测试 ──
print("\n" + "=" * 70)
print("GPIO 输出验证")
print("=" * 70)

# 等待 ISR 运行
time.sleep(0.1)

# 读取 Wire 值
print("\n--- Wire 值 ---")
w_temp = get_w('temp')
w_enable = get_w('enable')
w_temp_high = get_w('temp_high')
w_temp_ok = get_w('temp_ok')
w_system_ready = get_w('system_ready')
w_all_ok = get_w('all_ok')

print(f"  temp         = {w_temp}")
print(f"  enable       = {w_enable}")
print(f"  temp_high    = {w_temp_high}")
print(f"  temp_ok      = {w_temp_ok}")
print(f"  system_ready = {w_system_ready}")
print(f"  all_ok       = {w_all_ok}")

# 读取 GPIO
gpio = get_gpio()
print("\n--- GPIO 输出 (t=0.1s, 定时器未到期) ---")
if gpio:
    print(f"  PE0 (all_ok)       = {gpio['PE0']}")
    print(f"  PE1 (temp_high)    = {gpio['PE1']}")
    print(f"  PE2 (temp_ok)      = {gpio['PE2']}")
    print(f"  PE3 (system_ready) = {gpio['PE3']}")

# 验证 t=0.1s 时的预期
print("\n--- 验证 (t=0.1s) ---")
# temp=25.0 > 0.5 → temp_high=1
# temp=25.0, 20<25<30 → temp_ok=1
# enable=1
# system_ready=0 (定时器未到 2s)
# all_ok = 1 AND 0 AND 1 = 0

exp_pe0 = 0  # all_ok = 0
exp_pe1 = 1  # temp_high = 1
exp_pe2 = 1  # temp_ok = 1
exp_pe3 = 0  # system_ready = 0

ok_pe0 = "✅" if gpio and gpio['PE0'] == exp_pe0 else "❌"
ok_pe1 = "✅" if gpio and gpio['PE1'] == exp_pe1 else "❌"
ok_pe2 = "✅" if gpio and gpio['PE2'] == exp_pe2 else "❌"
ok_pe3 = "✅" if gpio and gpio['PE3'] == exp_pe3 else "❌"

print(f"  PE0: 实际={gpio['PE0'] if gpio else '?'} 预期={exp_pe0} {ok_pe0}")
print(f"  PE1: 实际={gpio['PE1'] if gpio else '?'} 预期={exp_pe1} {ok_pe1}")
print(f"  PE2: 实际={gpio['PE2'] if gpio else '?'} 预期={exp_pe2} {ok_pe2}")
print(f"  PE3: 实际={gpio['PE3'] if gpio else '?'} 预期={exp_pe3} {ok_pe3}")

# 等待定时器到期
print("\n--- 等待 2.5 秒 (定时器到期) ---")
time.sleep(2.5)

# 重新读取
gpio2 = get_gpio()
w_system_ready2 = get_w('system_ready')
w_all_ok2 = get_w('all_ok')

print(f"\n--- Wire 值 (t>2s) ---")
print(f"  system_ready = {w_system_ready2}")
print(f"  all_ok       = {w_all_ok2}")

print(f"\n--- GPIO 输出 (t>2s) ---")
if gpio2:
    print(f"  PE0 (all_ok)       = {gpio2['PE0']}")
    print(f"  PE1 (temp_high)    = {gpio2['PE1']}")
    print(f"  PE2 (temp_ok)      = {gpio2['PE2']}")
    print(f"  PE3 (system_ready) = {gpio2['PE3']}")

# 验证 t>2s 时的预期
print("\n--- 验证 (t>2s) ---")
# system_ready=1 (定时器到期)
# all_ok = 1 AND 1 AND 1 = 1

exp_pe0_2 = 1  # all_ok = 1
exp_pe3_2 = 1  # system_ready = 1

ok_pe0_2 = "✅" if gpio2 and gpio2['PE0'] == exp_pe0_2 else "❌"
ok_pe3_2 = "✅" if gpio2 and gpio2['PE3'] == exp_pe3_2 else "❌"

print(f"  PE0: 实际={gpio2['PE0'] if gpio2 else '?'} 预期={exp_pe0_2} {ok_pe0_2}")
print(f"  PE3: 实际={gpio2['PE3'] if gpio2 else '?'} 预期={exp_pe3_2} {ok_pe3_2}")

# ── 6. 总结 ──
print("\n" + "=" * 70)
print("测试结果总结")
print("=" * 70)

all_pass = (
    gpio and gpio['PE0'] == exp_pe0 and
    gpio and gpio['PE1'] == exp_pe1 and
    gpio and gpio['PE2'] == exp_pe2 and
    gpio and gpio['PE3'] == exp_pe3 and
    gpio2 and gpio2['PE0'] == exp_pe0_2 and
    gpio2 and gpio2['PE3'] == exp_pe3_2
)

if all_pass:
    print("\n✅ 全部通过! GPIO 输出与预期一致")
    print("   - Wire 值计算正确")
    print("   - GPIO 输出映射正确")
    print("   - 定时器功能正常")
else:
    print("\n❌ 存在失败!")
    if gpio:
        if gpio['PE0'] != exp_pe0: print(f"   PE0 错误: 实际={gpio['PE0']} 预期={exp_pe0}")
        if gpio['PE1'] != exp_pe1: print(f"   PE1 错误: 实际={gpio['PE1']} 预期={exp_pe1}")
        if gpio['PE2'] != exp_pe2: print(f"   PE2 错误: 实际={gpio['PE2']} 预期={exp_pe2}")
        if gpio['PE3'] != exp_pe3: print(f"   PE3 错误: 实际={gpio['PE3']} 预期={exp_pe3}")
    if gpio2:
        if gpio2['PE0'] != exp_pe0_2: print(f"   PE0 (t>2s) 错误: 实际={gpio2['PE0']} 预期={exp_pe0_2}")
        if gpio2['PE3'] != exp_pe3_2: print(f"   PE3 (t>2s) 错误: 实际={gpio2['PE3']} 预期={exp_pe3_2}")

print("\n完成.")
