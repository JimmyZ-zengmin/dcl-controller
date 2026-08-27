#!/usr/bin/env python3
"""
GPIO 输出验证测试 - 不走 IDE
编译 DCL → 部署到硬件 → 注入输入 → 读 GPIO 输出 → 对比预期
"""
import sys, time, struct

# 找到 pyocd venv
PYPATH = r"C:\Espressif\tools\python\v6.0.1\venv\Scripts\python.exe"

import subprocess, importlib.util
if importlib.util.find_spec("pyocd") is None:
    subprocess.check_call([PYPATH, "-m", "pip", "install", "pyocd"])

from pyocd.core.helpers import ConnectHelper
from pyocd.core.target import Target

# ══════════════════════════════════════════════════════
# 地址定义 (STM32H723)
# ══════════════════════════════════════════════════════
DTCM_BASE       = 0x20000000
ROUTE_TABLE     = 0x20000000
PARAM_TABLE     = 0x20002000
WIRE_MAP        = 0x20004000
SENSOR_MAP      = 0x20005000
ACTUATOR_STATUS = 0x20005100
ACTIVE_ROUTES   = 0x200000F0
SCRATCH2        = 0x200000F8

# GPIOE 寄存器
GPIOE_BASE      = 0x58021000
GPIOE_MODER     = GPIOE_BASE + 0x00
GPIOE_ODR       = GPIOE_BASE + 0x14
GPIOE_BSRR      = GPIOE_BASE + 0x18

# TIM1 寄存器
TIM1_BASE       = 0x40010000
TIM1_CR1        = TIM1_BASE + 0x00
TIM1_DIER       = TIM1_BASE + 0x0C
TIM1_SR         = TIM1_BASE + 0x10
TIM1_PSC        = TIM1_BASE + 0x28
TIM1_ARR        = TIM1_BASE + 0x2C

# NVIC
NVIC_ISER0      = 0xE000E100

# ══════════════════════════════════════════════════════
# DCL 测试程序
# ══════════════════════════════════════════════════════
TEST_DCL = """
[INPUTS]
a = SENSOR[0]
b = SENSOR[1]
c = SENSOR[2]
d = SENSOR[3]

[OUTPUTS]
out_all_high    = GPIO[0]   /* PE0 */
out_any_high    = GPIO[1]   /* PE1 */
out_not_a_and_b = GPIO[2]   /* PE2 */

[LOGIC]
wire w_and  = a AND b AND c AND d
wire w_or   = a OR b OR c OR d
wire w_not_a = NOT a
wire w_final = w_not_a AND b

out_all_high    = w_and
out_any_high    = w_or
out_not_a_and_b = w_final
"""

# ══════════════════════════════════════════════════════
# DCL 编译器 (简化版)
# ══════════════════════════════════════════════════════
class DCLCompiler:
    def __init__(self):
        self.routes = []
        self.params = []
        self.sensor_map = {}
        self.gpio_map = {}
        self.wire_counter = 0
        self.wire_map = {}
        self.output_wires = {}
    
    def parse(self, text):
        section = None
        for line in text.strip().split('\n'):
            line = line.strip()
            if not line or line.startswith('/*') or line.startswith('//'):
                continue
            if line.startswith('[') and line.endswith(']'):
                section = line[1:-1]
                continue
            if section == 'INPUTS':
                self._parse_io(line, self.sensor_map, 'SENSOR')
            elif section == 'OUTPUTS':
                self._parse_io(line, self.gpio_map, 'GPIO')
            elif section == 'LOGIC':
                self._parse_logic(line)
    
    def _parse_io(self, line, registry, prefix):
        lhs, rhs = line.split('=', 1)
        name = lhs.strip()
        idx = int(rhs.split('[')[1].split(']')[0])
        registry[name] = idx
    
    def _parse_logic(self, line):
        lhs, rhs = line.split('=', 1)
        lhs = lhs.strip()
        rhs = rhs.strip()
        
        if lhs.startswith('wire '):
            wname = lhs[5:].strip()
            self.wire_map[wname] = self.wire_counter
            self._compile_expr(rhs, is_wire=True, wire_name=wname)
            self.wire_counter += 1
        elif lhs.startswith('out_'):
            self.output_wires[lhs] = rhs
    
    def _compile_expr(self, expr, is_wire=False, wire_name=None):
        expr = expr.strip()
        
        # NOT unary
        if expr.startswith('NOT '):
            inner = expr[4:].strip()
            wire_idx = self._resolve(inner)
            self.routes.append({
                'opcode': 4,  # NOT
                'src': wire_idx,
                'param': 0,
                'dst': self.wire_counter if is_wire else 0
            })
            return
        
        # AND/OR multi-operands
        if ' AND ' in expr:
            parts = [p.strip() for p in expr.split(' AND ')]
            self._compile_multi(parts, is_and=True, is_wire=is_wire, wire_name=wire_name)
            return
        if ' OR ' in expr:
            parts = [p.strip() for p in expr.split(' OR ')]
            self._compile_multi(parts, is_and=False, is_wire=is_wire, wire_name=wire_name)
            return
        
        # Single wire/value — copy
        src_idx = self._resolve(expr)
        if is_wire:
            # Treat as passthrough (AND with 1)
            self.routes.append({
                'opcode': 3,  # SUB (we'll use as copy via wire+0)
                'src': src_idx,
                'param': self._alloc_param(0.0),
                'dst': self.wire_counter
            })
    
    def _compile_multi(self, parts, is_and=True, is_wire=False, wire_name=None):
        # Build tree: accumulate left-to-right
        if len(parts) == 1:
            self._compile_expr(parts[0], is_wire=is_wire, wire_name=wire_name)
            return
        
        # Two-operand case: left op right
        if len(parts) == 2:
            l_idx = self._resolve(parts[0])
            r_idx = self._resolve(parts[1])
            opcode = 1 if is_and else 2
            dst = self.wire_counter if is_wire else 0
            self.routes.append({
                'opcode': opcode,
                'src': l_idx,
                'param': r_idx,  # for AND/OR, param = right operand wire
                'dst': dst
            })
            return
        
        # Chain: left-fold
        # (a AND b AND c AND d) = ((a AND b) AND c) AND d
        # First pair
        l_idx = self._resolve(parts[0])
        r_idx = self._resolve(parts[1])
        opcode = 1 if is_and else 2
        self.routes.append({
            'opcode': opcode,
            'src': l_idx,
            'param': r_idx,
            'dst': self.wire_counter
        })
        acc_wire = self.wire_counter
        self.wire_counter += 1
        
        for i in range(2, len(parts)):
            p_idx = self._resolve(parts[i])
            if i == len(parts) - 1:
                # Last one → destination
                self.routes.append({
                    'opcode': opcode,
                    'src': acc_wire,
                    'param': p_idx,
                    'dst': self.wire_counter if is_wire else 0
                })
            else:
                self.routes.append({
                    'opcode': opcode,
                    'src': acc_wire,
                    'param': p_idx,
                    'dst': self.wire_counter
                })
                acc_wire = self.wire_counter
                self.wire_counter += 1
    
    def _resolve(self, name):
        if name.startswith('wire_') or name in self.wire_map:
            return self.wire_map.get(name, -1)
        if name in self.sensor_map:
            return 256 + self.sensor_map[name]  # sensor base = 256
        if name in self.output_wires:
            return 512 + self.wire_map.get(self.output_wires[name], 0)
        raise ValueError(f"Unknown: {name}")
    
    def _alloc_param(self, val):
        self.params.append(val)
        return len(self.params) - 1
    
    def generate_binary(self):
        route_data = b''
        for r in self.routes:
            route_data += struct.pack('<BBH', r['opcode'], r['src'], r['param'] << 8 | r['dst'])
        
        param_data = b''
        for p in self.params:
            param_data += struct.pack('<f', p)
        
        # OUTPUT routes (actuator_idx = 32 + gpio_idx)
        n_logic = len(self.routes)
        for oname, wname in self.output_wires.items():
            gpio_idx = self.gpio_map[oname]
            wire_idx = self.wire_map[wname]
            actuator_idx = 32 + gpio_idx
            route_data += struct.pack('<BBH', 5, wire_idx, actuator_idx << 8 | 0)
        
        # Pad to 16-byte alignment
        while len(route_data) % 16 != 0:
            route_data += b'\x00'
        while len(param_data) % 16 != 0:
            param_data += b'\x00'
        
        active_routes = n_logic + len(self.output_wires)
        
        return route_data, param_data, active_routes


# ══════════════════════════════════════════════════════
# 主测试流程
# ══════════════════════════════════════════════════════
def main():
    print("═══ GPIO 输出验证测试 (pyocd) ═══\n")
    
    # 1. 编译 DCL
    print("[1/4] 编译 DCL → 二进制...")
    c = DCLCompiler()
    c.parse(TEST_DCL)
    route_data, param_data, n_active = c.generate_binary()
    n_routes = n_active
    n_params = len(c.params)
    print(f"     routes={n_routes}, params={n_params}")
    print(f"     route_data: {len(route_data)} bytes")
    print(f"     param_data: {len(param_data)} bytes")
    
    # 2. 连接硬件
    print("\n[2/4] 连接 STM32H723...")
    session = ConnectHelper.session_with_chosen_probe(
        target_override="stm32h723zgtx",
        connect_mode="under_reset"
    )
    if not session:
        print("ERROR: 无法连接目标芯片")
        return
    target = session.board.target
    print(f"     已连接: {target.part_number}")
    
    # 3. 部署路由表
    print("\n[3/4] 部署路由表...")
    
    # 停止 TIM1
    target.write32(TIM1_DIER, 0)
    target.write32(TIM1_CR1, 0)
    target.write32(TIM1_SR, 0)
    
    # 握手标志: 告诉固件 main() 已被部署,跳过硬编码路由
    target.write32(SCRATCH2, 0xDEADBEEF)
    
    # 确保 ACTIVE_ROUTES 为零
    target.write32(ACTIVE_ROUTES, 0)
    
    # 填充 WIRE_MAP 为零 (1024 个 float = 4096 bytes)
    for addr in range(WIRE_MAP, WIRE_MAP + 4096, 4):
        target.write32(addr, 0)
    
    # 填充 ACTUATOR_STATUS 为零 (64 个 float = 256 bytes)
    for addr in range(ACTUATOR_STATUS, ACTUATOR_STATUS + 256, 4):
        target.write32(addr, 0)
    
    # 写入路由表
    for offset in range(0, len(route_data), 4):
        val = struct.unpack('<I', route_data[offset:offset+4])[0]
        target.write32(ROUTE_TABLE + offset, val)
    
    # 写入参数表
    for offset in range(0, len(param_data), 4):
        val = struct.unpack('<I', param_data[offset:offset+4])[0]
        target.write32(PARAM_TABLE + offset, val)
    
    # 写入 ACTIVE_ROUTES
    target.write32(ACTIVE_ROUTES, n_active)
    
    # 确认 GPIOE  pins are output (MODER = 01 for pin 0,1,2)
    moder = target.read32(GPIOE_MODER)
    # PE0=bit0-1, PE1=bit2-3, PE2=bit4-5
    # Need 0b010101 = 0x15 for pins 0,1,2 as output
    # Don't override — firmware main() should set this
    # Just read for diagnostics
    print(f"     GPIOE_MODER = 0x{moder:08X}")
    
    # 使能 TIM1_UP 中断 (IRQ 25 = NVIC_ISER0 bit 25)
    target.write32(NVIC_ISER0, 1 << 25)
    
    # 启动 TIM1 (enable UIE + CEN)
    target.write32(TIM1_DIER, 1)  # UIE
    target.write32(TIM1_CR1, 1)   # CEN
    
    print("     部署完成 ✓")
    
    # 4. 测试用例
    print("\n[4/4] 注入输入, 验证 GPIO 输出...")
    test_cases = [
        # (a, b, c, d, expected_pe0, expected_pe1, expected_pe2)
        (0, 0, 0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1, 1, 0),
        (0, 1, 0, 1, 0, 1, 1),
        (1, 0, 1, 0, 0, 1, 0),
        (0, 0, 1, 1, 0, 1, 0),
        (1, 1, 0, 0, 0, 1, 0),
        (0, 1, 1, 1, 0, 1, 1),
        (1, 1, 1, 0, 0, 1, 0),
    ]
    
    all_pass = True
    for i, (va, vb, vc, vd, exp_pe0, exp_pe1, exp_pe2) in enumerate(test_cases):
        # 注入 sensor 值 (float)
        target.write32(SENSOR_MAP + 0, struct.unpack('<I', struct.pack('<f', float(va)))[0])
        target.write32(SENSOR_MAP + 4, struct.unpack('<I', struct.pack('<f', float(vb)))[0])
        target.write32(SENSOR_MAP + 8, struct.unpack('<I', struct.pack('<f', float(vc)))[0])
        target.write32(SENSOR_MAP + 12, struct.unpack('<I', struct.pack('<f', float(vd)))[0])
        
        # 等待 ISR 执行 (至少一个周期 100μs)
        time.sleep(0.001)  # 1ms = 10 periods
        
        # 读取 GPIOE_ODR
        odr = target.read32(GPIOE_ODR)
        pe0 = (odr >> 0) & 1
        pe1 = (odr >> 1) & 1
        pe2 = (odr >> 2) & 1
        
        ok = (pe0 == exp_pe0) and (pe1 == exp_pe1) and (pe2 == exp_pe2)
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"     Test {i}: a={va} b={vb} c={vc} d={vd} → PE0={pe1} PE1={pe1} PE2={pe2} (exp: {exp_pe0}{exp_pe1}{exp_pe2}) {status}")
        
        if not ok:
            all_pass = False
            # 诊断
            wire12 = target.read32(WIRE_MAP + 12*4)
            wire13 = target.read32(WIRE_MAP + 13*4)
            wire14 = target.read32(WIRE_MAP + 14*4)
            act32 = target.read32(ACTUATOR_STATUS + 32*4)
            act33 = target.read32(ACTUATOR_STATUS + 33*4)
            act34 = target.read32(ACTUATOR_STATUS + 34*4)
            print(f"       诊断: wire[12]={wire12:08X} wire[13]={wire13:08X} wire[14]={wire14:08X}")
            print(f"             act[32]={act32:08X} act[33]={act33:08X} act[34]={act34:08X}")
    
    print(f"\n═══ 结论: {'全部通过 ✓' if all_pass else '存在失败 ✗'} ═══")
    
    # 停止 TIM1
    target.write32(TIM1_DIER, 0)
    target.write32(TIM1_CR1, 0)
    
    session.close()

if __name__ == "__main__":
    main()
