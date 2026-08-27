#!/usr/bin/env python3
"""
GPIO 输出链路诊断
遍历: SENSOR → WIRE → ACTUATOR → SHADOW_GPIO → GPIOE_ODR
找出哪一段断裂
"""
import sys, os, struct, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dcl_compiler import DCLCompiler
from dcl_hardware import Hardware, ADDRESSES, TIM1_BASE

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

GPIOE_ODR = 0x58021014
# DMA2 寄存器 (DMA2_BASE=0x40020400)
DMAMUX1_BASE = 0x40020800
DMA2_S5CR    = 0x40020478  # Stream 5 Control Register
DMA2_S5NDTR  = 0x4002047C
DMA2_S5PAR   = 0x40020480
DMA2_S5M0AR  = 0x40020484
DMA2_S5FCR   = 0x4002048C
DMAMUX1_S5CR = 0x40020888  # Channel 5 sync config (0x88 + 5*4)

def main():
    print("═══ GPIO 输出链路诊断 ═══\n")

    # 1. 编译
    c = DCLCompiler()
    c.parse(TEST_DCL)
    c.topological_sort()
    c.validate_resources()
    binary = c.generate_binary()

    n_routes = struct.unpack('<I', binary[0:4])[0]
    print(f"编译: {n_routes} routes")
    print(f"  wire_index: all_high={c.wire_index['all_high']}, any_high={c.wire_index['any_high']}, not_a_and_b={c.wire_index['not_a_and_b']}")

    # 2. 部署
    hw = Hardware()
    if not hw.connect():
        print("连接失败!"); return
    ok = hw.deploy(binary)
    if not ok:
        print(f"部署失败: {hw.last_error}")
        return

    print("\n部署完成\n")

    # 3. 读取 ROUTE_TABLE[12-14] 原始字节 (OUTPUT 路由)
    print("── ROUTE_TABLE[12-14] (OUTPUT 路由原始字节) ──")
    for idx in [12, 13, 14, 15]:
        addr = ADDRESSES['ROUTE_TABLE'] + idx * 16
        raw = b''
        for j in range(0, 16, 4):
            val = hw.read32(addr + j, 1)[0]
            raw += struct.pack('<I', val)
        # 解码
        src_t, src_i, dst_t, dst_ch = raw[0], raw[1], raw[2], raw[3]
        op, flags = raw[4], raw[5]
        pi = struct.unpack('<H', raw[6:8])[0]
        so = struct.unpack('<H', raw[8:10])[0]
        ai = struct.unpack('<H', raw[10:12])[0]
        w2 = struct.unpack('<H', raw[12:14])[0]
        print(f"  R[{idx:2d}]: src({src_t},{src_i}) dst({dst_t},{dst_ch}) op={op} flags={flags} "
              f"pi={pi} so={so} ai=**{ai}** w2={w2}  raw={raw[:16].hex()}")

    # 4. 固定输入 a=1, b=1, c=1, d=1
    print("\n── 注入 sensor=(1,1,1,1) ──")
    SENSOR_MAP = ADDRESSES['SENSOR_MAP']
    WIRE_MAP   = ADDRESSES['WIRE_MAP']
    ACT_STATUS = ADDRESSES['ACTUATOR_STATUS']

    def f2b(f): return struct.unpack('I', struct.pack('f', f))[0]
    def b2f(b): return struct.unpack('f', struct.pack('I', b & 0xFFFFFFFF))[0]

    hw.write32(SENSOR_MAP + 0,  f2b(1.0))
    hw.write32(SENSOR_MAP + 4,  f2b(1.0))
    hw.write32(SENSOR_MAP + 8,  f2b(1.0))
    hw.write32(SENSOR_MAP + 12, f2b(1.0))
    hw.write32(ACTIVE_ROUTES := ADDRESSES['ACTIVE_ROUTES'], n_routes)

    # 等 5ms (50 个 ISR 周期)
    time.sleep(0.005)

    # 读关键 wire 值
    def r_float(addr):
        r = hw.read32(addr, 1)
        return b2f(r[0]) if r else None

    w_all  = r_float(WIRE_MAP + c.wire_index['all_high'] * 4)
    w_any  = r_float(WIRE_MAP + c.wire_index['any_high'] * 4)
    w_nab  = r_float(WIRE_MAP + c.wire_index['not_a_and_b'] * 4)
    print(f"  wire[all_high={c.wire_index['all_high']}]    = {w_all:.4f}  (预期 1.0)")
    print(f"  wire[any_high={c.wire_index['any_high']}]    = {w_any:.4f}  (预期 1.0)")
    print(f"  wire[not_a_and_b={c.wire_index['not_a_and_b']}] = {w_nab:.4f}  (预期 0.0, NOT 1 AND 1 = 0)")

    # 5. 读 ACTUATOR_STATUS[32-34]
    print("\n── ACTUATOR_STATUS[32-34] (GPIO 输出映射) ──")
    for ai in [32, 33, 34]:
        val = r_float(ACT_STATUS + ai * 4)
        print(f"  ACT[{ai}] = {val:.4f}  (预期: 32→{w_all}, 33→{w_any}, 34→{w_nab})")

    # 读 0-7 作对比
    print("  ── ACT[0-7] ──")
    for ai in range(8):
        val = r_float(ACT_STATUS + ai * 4)
        print(f"  ACT[{ai}] = {val:.4f}")

    # 6. 读 DMA2 Stream 5 状态
    print("\n── DMA2 Stream 5 寄存器 ──")
    s5cr   = hw.read32(DMA2_S5CR, 1)[0]
    s5ndtr = hw.read32(DMA2_S5NDTR, 1)[0]
    s5par  = hw.read32(DMA2_S5PAR, 1)[0]
    s5m0ar = hw.read32(DMA2_S5M0AR, 1)[0]
    s5fcr  = hw.read32(DMA2_S5FCR, 1)[0]
    mux5cr = hw.read32(DMAMUX1_S5CR, 1)[0]
    en   = s5cr & 1
    dir_ = (s5cr >> 6) & 3
    circ = (s5cr >> 8) & 1
    pinc = (s5cr >> 9) & 1
    minc = (s5cr >> 10) & 1
    psize = (s5cr >> 11) & 3
    msize = (s5cr >> 13) & 3
    pl   = (s5cr >> 16) & 3
    print(f"  S5CR   = 0x{s5cr:08X}  EN={en} DIR={dir_} CIRC={circ} PSIZE={psize} MSIZE={msize} PL={pl}")
    print(f"  S5NDTR = 0x{s5ndtr:08X}  (预期 1)")
    print(f"  S5PAR  = 0x{s5par:08X}  (预期 GPIOE_ODR=0x58021014)")
    print(f"  S5M0AR = 0x{s5m0ar:08X}  (预期 &SHADOW_GPIO 在 DTCM)")
    print(f"  S5FCR  = 0x{s5fcr:08X}  (FIFO/direct)")
    print(f"  MUX5CR = 0x{mux5cr:08X}  (DMAMUX sync, 预期 REQ_ID=TIM1_UP=15)")

    # 找 SHADOW_GPIO 地址 (通过搜索 main map 或直接猜测)
    # 通常在 DTCM 某个地址, 读一下 0x2000xxxx 范围
    print("\n── 搜索 SHADOW_GPIO ──")
    # 从 map 文件或读链接脚本找符号, 这里直接尝试已知地址
    candidate_addrs = [0x200000E0, 0x200000E4, 0x200000E8, 0x200000EC, 0x200000F4]
    for addr in candidate_addrs:
        val = hw.read32(addr, 1)[0]
        print(f"  [0x{addr:08X}] = 0x{val:08X}")

    # 7. 读 GPIOE_ODR 最终值
    print("\n── GPIOE_ODR ──")
    odr = hw.read32(GPIOE_ODR, 1)[0]
    print(f"  ODR = 0x{odr:08X}")
    print(f"  PE0 = {(odr>>0)&1} (预期 1, all_high=1)")
    print(f"  PE1 = {(odr>>1)&1} (预期 1, any_high=1)")
    print(f"  PE2 = {(odr>>2)&1} (预期 0, not_a_and_b=0)")

    # 8. 读 GPIOE_MODER
    print("\n── GPIOE_MODER ──")
    moder = hw.read32(0x58021000, 1)[0]
    print(f"  MODER = 0x{moder:08X}")
    for pin in range(3):
        m = (moder >> (pin*2)) & 3
        mode_str = {0: "IN", 1: "OUT", 2: "AF", 3: "AN"}[m]
        print(f"  PE{pin}: {mode_str} (预期 OUT=01)")

    print("\n═══ 诊断完成 ═══")
    hw.disconnect()

if __name__ == "__main__":
    main()
