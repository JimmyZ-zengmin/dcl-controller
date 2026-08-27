#!/usr/bin/env python3
"""
DCL编译器 v1.0 — IEC 61131-3语法 → H723路由表二进制
目标: 核心0 H723 v1.7 固件 (100us ISR, 28种原语)

用法:
  python dcl_compiler_v1.py program.dcl -o program.bin
  python dcl_compiler_v1.py program.dcl --json   # 输出JSON调试

语法:
  SENSOR  temp      FROM ADC1_CH0    SCALE 1.0 0.0
  FILTER  temp_f    FROM temp        LOWPASS a=0.1
  PID     heater    FROM temp_f      SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
  ALARM   overheat  FROM temp_f > 80
  LOGIC   fault     = overheat OR undertemp
  LOGIC   inv       = NOT signal
  OUTPUT  heat_pwm  TO TIM1_CH1      FROM heater
  OUTPUT  fault_led TO GPIO_PE5      FROM fault

  TIMER   t1: IN=btn, PT=3s → Q=motor_on
  COUNTER c1: CU=sensor, PV=100 → Q=full, CV=count
  LATCH   sr1: S1=set, R=reset → Q1=latch_out

  CONST   pi = 3.14159
  ARITH   flow = dp MUL k
  ARITH   sum = a ADD b
  HYST    heater_on FROM temp HIGH 80 LOW 75
  EDGE    pulse FROM btn RISING
  MUX     output = mode SELECT manual ELSE auto
  RLATCH  safe: S=start, R1=estop → Q1=active
  BIT     masked = flags BITAND mask
  BIT     inv = BITNOT flags
  LUT     curve FROM x TABLE 0.0 0.5 1.0 0.8 0.3
"""

import re, struct, sys, os, json
from collections import OrderedDict, defaultdict, deque

# ═══════════════════════════════════════════
# H723 v1.7 硬件配置
# ═══════════════════════════════════════════
H723_CONFIG = {
    'max_routes': 1024,
    'max_params': 512,
    'max_states': 256,
    'max_wires': 1024,
    'max_sensors': 64,
    'max_actuators': 32,

    # DTCM地址
    'dtcm_base': 0x20000000,
    'sensor_map': 0x20000100,
    'actuator_status': 0x20000200,
    'shadow_gpio': 0x20000280,
    'wire_map': 0x20000300,
    'route_table': 0x20001700,
    'param_table': 0x20005700,
    'state_table': 0x20007700,

    # 数据结构
    'route_entry_size': 16,
    'param_entry_size': 16,
    'state_entry_size': 16,
}

MAX_SENSORS = 32
MAX_ACTUATORS = 32

# ═══════════════════════════════════════════
# IEC 61131-3 → H723 原语映射 (v1.7固件)
# ═══════════════════════════════════════════
OP_MAP = {
    'DIRECT':    0x00, 'CMP':       0x01, 'HYST':    0x02, 'CLAMP': 0x03,
    'LPF':       0x04, 'PID':       0x05, 'RATE':    0x06, 'DEADBAND': 0x07,
    'MUX':       0x08, 'EDGE':      0x09, 'LUT':     0x0A, 'CNT':    0x0B,
    'TIMER':     0x0C, 'SCALE':     0x0E, 'AND':     0x0F, 'OR':     0x10,
    'NOT':       0x11, 'REG':       0x12, 'ADD':     0x13, 'SUB':    0x14,
    'MUL':       0x15, 'DIV':       0x16, 'BITAND':  0x17, 'BITOR':  0x18,
    'BITXOR':    0x19, 'BITNOT':    0x1A, 'SR':      0x1B, 'RS':     0x1C,
    'COUNTER':   0x1D, 'LIMIT':     0x1E, 'MAX':     0x1F, 'MIN':    0x20,
    'ABS':       0x21, 'EQ':        0x22, 'NE':      0x23,
}

SRC_SENSOR = 0
SRC_WIRE = 1
SRC_CONST = 2
DST_WIRE = 3

# RouteEntry_t 结构: 16字节 packed (固件结构含2字节尾部对齐填充)
ROUTE_FMT = '<BBBBBBHHHHxx'  # 6×B(6) + 4×H(8) + 2×pad(2) = 16 bytes
# [0]src_type [1]src_index [2]dst_type [3]dst_channel
# [4]op [5]flags [6-7]param_idx [8-9]state_offset [10-11]actuator_idx [12-13]wire2_idx [14-15]padding

PARAM_FMT = '<ffff'  # value_a, value_b, value_c, value_d

STATEFUL_OPS = {'LPF', 'PID', 'RATE', 'EDGE', 'CNT', 'TIMER', 'COUNTER',
                'DEADBAND', 'HYST', 'REG', 'SR', 'RS'}

# ═══════════════════════════════════════════
# 硬件映射表
# ═══════════════════════════════════════════
HARDWARE_ACTUATOR_MAP = {
    'TIM1_CH1': 1, 'TIM1_CH2': 2, 'TIM1_CH3': 3, 'TIM1_CH4': 4,
    'GPIO_PE0': 32, 'GPIO_PE1': 33, 'GPIO_PE2': 34, 'GPIO_PE3': 35,
    'GPIO_PE4': 36, 'GPIO_PE5': 37, 'GPIO_PE6': 38, 'GPIO_PE7': 39,
    'GPIO_PE8': 40, 'GPIO_PE9': 41, 'GPIO_PE10': 42, 'GPIO_PE11': 43,
    'GPIO_PE12': 44, 'GPIO_PE13': 45, 'GPIO_PE14': 46, 'GPIO_PE15': 47,
}

SENSOR_HARDWARE_MAP = {
    'ADC1_CH0': (0, 0), 'ADC1_CH1': (0, 1), 'ADC1_CH2': (0, 2), 'ADC1_CH3': (0, 3),
    'ADC1_CH4': (0, 4), 'ADC1_CH5': (0, 5), 'ADC1_CH6': (0, 6), 'ADC1_CH7': (0, 7),
    'GPIO_PE0': (1, 0), 'GPIO_PE1': (1, 1), 'GPIO_PE2': (1, 2), 'GPIO_PE3': (1, 3),
    'GPIO_PE4': (1, 4), 'GPIO_PE5': (1, 5), 'GPIO_PE6': (1, 6), 'GPIO_PE7': (1, 7),
}


def crc32_be(data: bytes) -> int:
    """CRC32 big-endian (匹配H723固件)"""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= (byte << 24)
        for _ in range(8):
            if crc & 0x80000000:
                crc = (crc << 1) ^ 0x04C11DB7
            else:
                crc <<= 1
            crc &= 0xFFFFFFFF
    return crc


class DCLCompiler:
    def __init__(self):
        self.routes = []          # RouteEntry列表
        self.params = []          # (a,b,c,d) tuple列表
        self.state_slots = {}     # instance_name → state_offset
        self.wire_index = {}      # signal_name → wire_index
        self.next_wire = 0
        self.next_param = 0
        self.next_state = 0
        self.sensors = {}
        self.actuators = {}
        # SENSOR src_index mapping
        self.sensor_source_map = {}   # source_name → sensor_index (0, 1, 2, ...)
        self.next_sensor_idx = 0
        self.sensor_source_list = []  # ordered list of source names
        # OUTPUT actuator mapping
        self.actuator_map = {}        # target_name → actuator_index
        self.next_actuator_idx = 1    # 0=unused, 1=TIM1_CH1, 2=TIM1_CH2, etc.
        # LUT storage
        self.next_lut = 0
        self.lut_entries = []

    def alloc_wire(self, name: str) -> int:
        if name not in self.wire_index:
            self.wire_index[name] = self.next_wire
            self.next_wire += 1
        return self.wire_index[name]

    def alloc_param(self, a=0.0, b=0.0, c=0.0, d=0.0) -> int:
        idx = self.next_param
        self.params.append((a, b, c, d))
        self.next_param += 1
        return idx

    def alloc_state(self, name: str) -> int:
        if name not in self.state_slots:
            self.state_slots[name] = self.next_state
            self.next_state += 1
        return self.state_slots[name]

    # ═══════════════════════════════════════════
    # 解析器 (带行号追踪)
    # ═══════════════════════════════════════════
    def parse(self, source: str):
        source = re.sub(r'//.*', '', source)
        source = re.sub(r'/\*.*?\*/', '', source, flags=re.DOTALL)

        for lineno, line in enumerate(source.split('\n'), 1):
            line = line.strip()
            if not line or line.startswith('#'): continue

            line = re.sub(r'\s+', ' ', line)

            try:
                if line.startswith('SENSOR '):
                    self._parse_sensor(line)
                elif line.startswith('OUTPUT '):
                    self._parse_output(line)
                elif line.startswith('TIMER '):
                    self._parse_timer(line)
                elif line.startswith('COUNTER '):
                    self._parse_counter(line)
                elif line.startswith('RATE '):
                    self._parse_unary(line, 'RATE', OP_MAP['RATE'])
                elif line.startswith('DEADBAND '):
                    self._parse_unary(line, 'DEADBAND', OP_MAP['DEADBAND'], True)
                elif line.startswith('SCALE '):
                    self._parse_scale(line)
                elif line.startswith('LATCH '):
                    self._parse_latch(line)
                elif line.startswith('FILTER '):
                    self._parse_filter(line)
                elif line.startswith('PID '):
                    self._parse_pid(line)
                elif line.startswith('ALARM '):
                    self._parse_alarm(line)
                elif line.startswith('LOGIC '):
                    self._parse_logic(line)
                elif line.startswith('ARITH '):
                    self._parse_arith(line)
                elif line.startswith('CONST '):
                    self._parse_const(line)
                elif line.startswith('HYST '):
                    self._parse_hyst(line)
                elif line.startswith('EDGE '):
                    self._parse_edge(line)
                elif line.startswith('MUX '):
                    self._parse_mux(line)
                elif line.startswith('RLATCH '):
                    self._parse_rlatch(line)
                elif line.startswith('BIT '):
                    self._parse_bit(line)
                elif line.startswith('LUT '):
                    self._parse_lut(line)
                elif line.startswith('LIMIT '):
                    self._parse_limit(line)
                elif line.startswith('MAX '):
                    self._parse_max(line)
                elif line.startswith('MIN '):
                    self._parse_min(line)
                elif line.startswith('ABS '):
                    self._parse_abs(line)
                elif line.startswith('EQ '):
                    self._parse_eq(line)
                elif line.startswith('NE '):
                    self._parse_ne(line)
                else:
                    raise RuntimeError(f"第{lineno}: 无法识别的语句: {line}")
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError(f"第{lineno}: 解析错误: {e}")

    def _parse_sensor(self, line):
        # SENSOR name FROM source [SCALE k b] [RANGE lo hi]
        m = re.match(r'SENSOR\s+(\w+)\s+FROM\s+(\w+)', line)
        if m:
            name, source = m.group(1), m.group(2)
            self.sensors[name] = source
            # Map source name to sensor index
            if source not in self.sensor_source_map:
                self.sensor_source_map[source] = self.next_sensor_idx
                self.next_sensor_idx += 1
                self.sensor_source_list.append(source)
            si = self.sensor_source_map[source]
            w = self.alloc_wire(name)
            scale = re.search(r'SCALE\s+([\d.]+)\s+([\d.]+)', line)
            rng = re.search(r'RANGE\s+([\d.]+)\s+([\d.]+)', line)
            if scale:
                k, b = float(scale.group(1)), float(scale.group(2))
            elif rng:
                k, b = float(rng.group(1)), float(rng.group(2))
            else:
                k, b = 1.0, 0.0
            if k != 1.0 or b != 0.0:
                pi = self.alloc_param(k, b, 0, 0)
                self.routes.append({
                    'src_type': SRC_SENSOR, 'src_index': si,
                    'dst_type': DST_WIRE, 'dst_channel': w,
                    'op': OP_MAP['SCALE'], 'flags': 1,
                    'param_idx': pi, 'state_offset': 0,
                    'actuator_idx': 0, 'wire2_idx': 0,
                })
            else:
                self.routes.append({
                    'src_type': SRC_SENSOR, 'src_index': si,
                    'dst_type': DST_WIRE, 'dst_channel': w,
                    'op': OP_MAP['DIRECT'], 'flags': 1,
                    'param_idx': 0, 'state_offset': 0,
                    'actuator_idx': 0, 'wire2_idx': 0,
                })

    def _parse_output(self, line):
        # OUTPUT name TO target [FROM signal]
        # 如果省略 FROM signal, 默认 signal = name
        m = re.match(r'OUTPUT\s+(\w+)\s+TO\s+(\w+)(?:\s+FROM\s+(\w+))?', line)
        if m:
            name = m.group(1)
            target = m.group(2)
            signal = m.group(3) if m.group(3) else name
            src_w = self.alloc_wire(signal)
            actuator_idx = HARDWARE_ACTUATOR_MAP.get(target, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': self.alloc_wire(f'out_{name}'),
                'op': OP_MAP['DIRECT'], 'flags': 1,
                'param_idx': 0, 'state_offset': 0,
                'actuator_idx': actuator_idx, 'wire2_idx': 0,
            })

    def _parse_timer(self, line):
        # TIMER name: IN=signal, PT=3s [, mode=TON|TOF|TP] → Q=signal [, ET=signal]
        m = re.match(r'TIMER\s+(\w+)\s*:\s*IN\s*=\s*(\w+)\s*,\s*PT\s*=\s*(\d+)\s*(ms|s)\s*(?:,\s*mode\s*=\s*(TON|TOF|TP))?\s*→\s*Q\s*=\s*(\w+)(?:\s*,\s*ET\s*=\s*(\w+))?', line)
        if m:
            name, in_sig, pt_val, pt_unit = m.group(1), m.group(2), m.group(3), m.group(4)
            mode_str = m.group(5) or 'TON'
            q_sig, et_sig = m.group(6), m.group(7)
            mode_map = {'TON': 0, 'TOF': 1, 'TP': 2}
            mode = mode_map.get(mode_str, 0)
            pt_ms = float(pt_val) * (1.0 if pt_unit == 'ms' else 1000.0)
            src_w = self.alloc_wire(in_sig)
            q_w = self.alloc_wire(q_sig)
            state = self.alloc_state(name)
            pi_q = self.alloc_param(float(mode), pt_ms, 0.0, 0.0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': q_w,
                'op': OP_MAP['TIMER'], 'flags': 1,
                'param_idx': pi_q, 'state_offset': state,
                'actuator_idx': 0, 'wire2_idx': 0,
            })
            if et_sig:
                et_w = self.alloc_wire(et_sig)
                pi_et = self.alloc_param(float(mode), pt_ms, 1.0, 0.0)
                self.routes.append({
                    'src_type': SRC_WIRE, 'src_index': src_w,
                    'dst_type': DST_WIRE, 'dst_channel': et_w,
                    'op': OP_MAP['TIMER'], 'flags': 1,
                    'param_idx': pi_et, 'state_offset': state,
                    'actuator_idx': 0, 'wire2_idx': 0,
                })

    def _parse_counter(self, line):
        # COUNTER name: counting_signal [, mode=CTU|CTD|CTUD] [, PV=100] [, R=reset_sig] → Q=signal, CV=signal
        # 支持3种格式:
        #   COUNTER name: CU=signal, PV=100 → Q=q, CV=cv    (CTU, 默认)
        #   COUNTER name: CD=signal, PV=100 → Q=q, CV=cv    (CTD)
        #   COUNTER name: CU=signal, CD=signal → Q=q, CV=cv (CTUD)

        # Try CTUD first: CU=xxx, CD=xxx
        m = re.match(r'COUNTER\s+(\w+)\s*:\s*CU\s*=\s*(\w+)\s*,\s*CD\s*=\s*(\w+)\s*(?:,\s*PV\s*=\s*(\d+))?\s*→\s*Q\s*=\s*(\w+)\s*,\s*CV\s*=\s*(\w+)', line)
        if m:
            name, cu, cd = m.group(1), m.group(2), m.group(3)
            pv = float(m.group(4)) if m.group(4) else 100.0
            qu, cv = m.group(5), m.group(6)
            self._add_counter_routes(name, cu_src=cu, cd_src=cd, pv=pv, qu=qu, cv=cv, mode=2)
            return

        # Try CTD: CD=xxx
        m = re.match(r'COUNTER\s+(\w+)\s*:\s*CD\s*=\s*(\w+)\s*,\s*PV\s*=\s*(\d+)\s*→\s*Q\s*=\s*(\w+)\s*,\s*CV\s*=\s*(\w+)', line)
        if m:
            name, cd, pv, qu, cv = m.group(1), m.group(2), float(m.group(3)), m.group(4), m.group(5)
            self._add_counter_routes(name, cd_src=cd, pv=pv, qu=qu, cv=cv, mode=1)
            return

        # CTU (default): CU=xxx
        m = re.match(r'COUNTER\s+(\w+)\s*:\s*CU\s*=\s*(\w+)\s*,\s*PV\s*=\s*(\d+)\s*→\s*Q\s*=\s*(\w+)\s*,\s*CV\s*=\s*(\w+)', line)
        if m:
            name, cu, pv, qu, cv = m.group(1), m.group(2), float(m.group(3)), m.group(4), m.group(5)
            self._add_counter_routes(name, cu_src=cu, pv=pv, qu=qu, cv=cv, mode=0)

    def _add_counter_routes(self, name, cu_src=None, cd_src=None, pv=100, qu=None, cv=None, mode=0):
        """Helper to add counter routes (shared by CTU/CTD/CTUD)"""
        src_w = self.alloc_wire(cu_src or cd_src)
        cv_w = self.alloc_wire(cv)
        qu_w = self.alloc_wire(qu)
        state = self.alloc_state(name)

        # CV route
        if mode == 2:  # CTUD
            cd_w = self.alloc_wire(cd_src)
            pi_cv = self.alloc_param(2.0, pv, 0.0, float(cd_w))
        elif mode == 1:  # CTD
            pi_cv = self.alloc_param(1.0, pv, 0.0, 200.0)
        else:  # CTU
            pi_cv = self.alloc_param(0.0, pv, 0.0, 200.0)
        self.routes.append({
            'src_type': SRC_WIRE, 'src_index': src_w,
            'dst_type': DST_WIRE, 'dst_channel': cv_w,
            'op': OP_MAP['COUNTER'], 'flags': 1,
            'param_idx': pi_cv, 'state_offset': state,
            'actuator_idx': 0, 'wire2_idx': 0,
        })
        # QU route
        pi_qu = self.alloc_param(float(mode), pv, 1.0, 200.0)
        self.routes.append({
            'src_type': SRC_WIRE, 'src_index': src_w,
            'dst_type': DST_WIRE, 'dst_channel': qu_w,
            'op': OP_MAP['COUNTER'], 'flags': 1,
            'param_idx': pi_qu, 'state_offset': state,
            'actuator_idx': 0, 'wire2_idx': 0,
        })

    def _parse_latch(self, line):
        # LATCH name: S1=signal, R=signal → Q1=signal
        m = re.match(r'LATCH\s+(\w+):\s*S1=(\w+),\s*R=(\w+)\s*→\s*Q1=(\w+)', line)
        if m:
            name, s1, r, q1 = m.group(1), m.group(2), m.group(3), m.group(4)
            src_w = self.alloc_wire(s1)
            r_w = self.alloc_wire(r)
            q1_w = self.alloc_wire(q1)
            state = self.alloc_state(name)
            pi = self.alloc_param(float(r_w), 0, 0, 0)  # p->value_a = R wire index
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': q1_w,
                'op': OP_MAP['SR'], 'flags': 1,
                'param_idx': pi, 'state_offset': state,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_scale(self, line):
        """SCALE name FROM signal RANGE lo hi — 线性标定 y=kx+b"""
        m = re.match(r'SCALE\s+(\w+)\s+FROM\s+(\w+)\s+RANGE\s+([\d.]+)\s+([\d.]+)', line)
        if m:
            name, src, lo, hi = m.group(1), m.group(2), float(m.group(3)), float(m.group(4))
            src_w = self.alloc_wire(src)
            dst_w = self.alloc_wire(name)
            pi = self.alloc_param(hi - lo, lo, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['SCALE'], 'flags': 1,
                'param_idx': pi, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_unary(self, line, keyword, opcode, parse_param=False):
        """通用一元操作: KEYWORD name FROM signal [param]"""
        m = re.match(rf'{keyword}\s+(\w+)\s+FROM\s+(\w+)(?:\s*,\s*([\d.]+))?', line)
        if m:
            name, src = m.group(1), m.group(2)
            src_w = self.alloc_wire(src)
            dst_w = self.alloc_wire(name)
            state = self.alloc_state(name) if opcode in [op for op, v in OP_MAP.items() if op in STATEFUL_OPS] else 0
            pi = self.alloc_param(float(m.group(3)) if parse_param and m.group(3) else 0.0, 0, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': opcode, 'flags': 1,
                'param_idx': pi, 'state_offset': state,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_filter(self, line):
        # FILTER name FROM signal LOWPASS a=0.1
        m = re.match(r'FILTER\s+(\w+)\s+FROM\s+(\w+)\s+LOWPASS\s+a=([\d.]+)', line)
        if m:
            name, src, alpha = m.group(1), m.group(2), float(m.group(3))
            src_w = self.alloc_wire(src)
            dst_w = self.alloc_wire(name)
            state = self.alloc_state(name)
            pi = self.alloc_param(alpha, 0, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['LPF'], 'flags': 1,
                'param_idx': pi, 'state_offset': state,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_pid(self, line):
        # PID name FROM signal SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
        # 支持: KD=0.02 或 KD  0.02 (多空格/无等号)
        m = re.match(r'PID\s+(\w+)\s+FROM\s+(\w+)\s+SP\s*=\s*([\d.]+)\s+KP\s*=\s*([\d.]+)\s+KI\s*=\s*([\d.]+)\s+KD\s*=?\s*([\d.]+)(?:\s+LIMIT\s+([\d.]+)\s+([\d.]+))?', line)
        if m:
            name, src, sp, kp, ki, kd = m.group(1), m.group(2), float(m.group(3)), float(m.group(4)), float(m.group(5)), float(m.group(6))
            lo, hi = (float(m.group(7)), float(m.group(8))) if m.group(7) else (0.0, 100.0)
            src_w = self.alloc_wire(src)
            pid_w = self.alloc_wire(f'{name}_pid')
            clamp_w = self.alloc_wire(name)
            state = self.alloc_state(name)
            # PID route
            pi_pid = self.alloc_param(kp, ki, kd, sp)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': pid_w,
                'op': OP_MAP['PID'], 'flags': 1,
                'param_idx': pi_pid, 'state_offset': state,
                'actuator_idx': 0, 'wire2_idx': 0,
            })
            # CLAMP route — 共享 PID 的 state slot, 写入 i_limit 到 state_c
            pi_clamp = self.alloc_param(lo, hi, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': pid_w,
                'dst_type': DST_WIRE, 'dst_channel': clamp_w,
                'op': OP_MAP['CLAMP'], 'flags': 1,
                'param_idx': pi_clamp, 'state_offset': state,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_alarm(self, line):
        # ALARM name FROM signal > threshold
        m = re.match(r'ALARM\s+(\w+)\s+FROM\s+(\w+)\s+([><]=?)\s+([\d.]+)', line)
        if m:
            name, src, op, th = m.group(1), m.group(2), m.group(3), float(m.group(4))
            cmp_mode = {'>': 0, '>=': 1, '<': 2, '<=': 3}[op]
            src_w = self.alloc_wire(src)
            dst_w = self.alloc_wire(name)
            pi = self.alloc_param(th, float(cmp_mode), 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['CMP'], 'flags': 1,
                'param_idx': pi, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_logic(self, line):
        # LOGIC name = expression
        # Supports: NOT sig, sig1 AND/OR sig2, chained AND/OR, NOT in expressions
        m = re.match(r'LOGIC\s+(\w+)\s*=\s+(.+)', line)
        if not m:
            return
        name = m.group(1)
        expr = m.group(2).strip()
        
        # Tokenize: split by AND/OR while preserving operators
        # NOT must be a separate token so parser can recognize it before operand
        tokens = re.findall(r'(NOT|\w+|\S+)', expr)
        if not tokens:
            return
        
        # Parse into operands and operators
        # tokens like: ['A', 'AND', 'B', 'AND', 'NOT', 'C', 'OR', 'D']
        # Need to handle: NOT prefix, AND/OR operators
        
        # First pass: build list of (operand, is_not) and operators
        operands = []
        operators = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in ('AND', 'OR'):
                operators.append(token)
                i += 1
            elif token == 'NOT':
                # Next token is the operand
                if i + 1 < len(tokens):
                    operands.append((tokens[i + 1], True))
                    i += 2
                else:
                    i += 1
            else:
                operands.append((token, False))
                i += 1
        
        if not operands:
            return
        
        # If only one operand with NOT
        if len(operands) == 1 and operators == []:
            sig_name, is_not = operands[0]
            if is_not:
                src_w = self.alloc_wire(sig_name)
                dst_w = self.alloc_wire(name)
                self.routes.append({
                    'src_type': SRC_WIRE, 'src_index': src_w,
                    'dst_type': DST_WIRE, 'dst_channel': dst_w,
                    'op': OP_MAP['NOT'], 'flags': 1,
                    'param_idx': 0, 'state_offset': 0,
                    'actuator_idx': 0, 'wire2_idx': 0,
                })
            return
        
        # Generate intermediate wires for chained operations
        # Process left to right: A AND B AND C → tmp1=A AND B, result=tmp1 AND C
        current_wire = None
        current_is_not = False
        
        for idx, (op_name, is_not) in enumerate(operands):
            if idx == 0:
                # First operand
                if is_not:
                    # Generate NOT route
                    tmp_w = self.alloc_wire(f'_logic_tmp_{name}_{idx}')
                    src_w = self.alloc_wire(op_name)
                    self.routes.append({
                        'src_type': SRC_WIRE, 'src_index': src_w,
                        'dst_type': DST_WIRE, 'dst_channel': tmp_w,
                        'op': OP_MAP['NOT'], 'flags': 1,
                        'param_idx': 0, 'state_offset': 0,
                        'actuator_idx': 0, 'wire2_idx': 0,
                    })
                    current_wire = tmp_w
                else:
                    current_wire = self.alloc_wire(op_name)
                continue
            
            # Get operator for this operand
            op_type = operators[idx - 1] if idx - 1 < len(operators) else None
            if op_type is None:
                break
            
            # Get operand wire
            if is_not:
                # Generate NOT route first
                not_w = self.alloc_wire(f'_logic_tmp_{name}_{idx}')
                src_w = self.alloc_wire(op_name)
                self.routes.append({
                    'src_type': SRC_WIRE, 'src_index': src_w,
                    'dst_type': DST_WIRE, 'dst_channel': not_w,
                    'op': OP_MAP['NOT'], 'flags': 1,
                    'param_idx': 0, 'state_offset': 0,
                    'actuator_idx': 0, 'wire2_idx': 0,
                })
                src_wire = not_w
            else:
                src_wire = self.alloc_wire(op_name)
            
            # Generate binary operation route
            if idx == len(operands) - 1:
                dst_w = self.alloc_wire(name)  # Final result
            else:
                dst_w = self.alloc_wire(f'_logic_tmp_{name}_{idx}')
            
            opcode = OP_MAP[op_type]
            pi = self.alloc_param(float(src_wire), 0, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': current_wire,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': opcode, 'flags': 1,
                'param_idx': pi, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })
            current_wire = dst_w

    # ═══════════════════════════════════════════
    # 新增语法解析
    # ═══════════════════════════════════════════
    def _parse_arith(self, line):
        # ARITH name = sig1 OP sig2  (ADD|SUB|MUL|DIV)
        m = re.match(r'ARITH\s+(\w+)\s*=\s*(\w+)\s+(ADD|SUB|MUL|DIV)\s+(\w+)', line)
        if m:
            name, s1, op, s2 = m.group(1), m.group(2), m.group(3), m.group(4)
            src_w = self.alloc_wire(s1)
            dst_w = self.alloc_wire(name)
            s2_w = self.alloc_wire(s2)
            opcode = OP_MAP[op]
            pi = self.alloc_param(float(s2_w), 0, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': opcode, 'flags': 1,
                'param_idx': pi, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_const(self, line):
        # CONST name = value
        m = re.match(r'CONST\s+(\w+)\s*=\s*([\d.]+)', line)
        if m:
            name, val = m.group(1), float(m.group(2))
            dst_w = self.alloc_wire(name)
            pi = self.alloc_param(0, 0, 0, val)
            self.routes.append({
                'src_type': SRC_CONST, 'src_index': 0,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['DIRECT'], 'flags': 1,
                'param_idx': pi, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_hyst(self, line):
        # HYST name FROM signal HIGH h LOW l
        m = re.match(r'HYST\s+(\w+)\s+FROM\s+(\w+)\s+HIGH\s+([\d.]+)\s+LOW\s+([\d.]+)', line)
        if m:
            name, src, hi, lo = m.group(1), m.group(2), float(m.group(3)), float(m.group(4))
            src_w = self.alloc_wire(src)
            dst_w = self.alloc_wire(name)
            state = self.alloc_state(name)
            pi = self.alloc_param(hi, lo, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['HYST'], 'flags': 1,
                'param_idx': pi, 'state_offset': state,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_edge(self, line):
        # EDGE name FROM signal RISING|FALLING|BOTH
        m = re.match(r'EDGE\s+(\w+)\s+FROM\s+(\w+)\s+(RISING|FALLING|BOTH)', line)
        if m:
            name, src, mode_str = m.group(1), m.group(2), m.group(3)
            mode_map = {'RISING': 0, 'FALLING': 1, 'BOTH': 2}
            mode = mode_map.get(mode_str, 0)
            src_w = self.alloc_wire(src)
            dst_w = self.alloc_wire(name)
            state = self.alloc_state(name)
            pi = self.alloc_param(float(mode), 0, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['EDGE'], 'flags': 1,
                'param_idx': pi, 'state_offset': state,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_mux(self, line):
        # MUX name = sel SELECT a ELSE b
        m = re.match(r'MUX\s+(\w+)\s*=\s*(\w+)\s+SELECT\s+(\w+)\s+ELSE\s+(\w+)', line)
        if m:
            name, sel, if_true, if_false = m.group(1), m.group(2), m.group(3), m.group(4)
            sel_w = self.alloc_wire(sel)
            dst_w = self.alloc_wire(name)
            true_w = self.alloc_wire(if_true)
            false_w = self.alloc_wire(if_false)
            pi = self.alloc_param(float(false_w), float(true_w), 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': sel_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['MUX'], 'flags': 1,
                'param_idx': pi, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_rlatch(self, line):
        # RLATCH name: S=sig, R1=sig → Q1=out  (RS flip-flop, reset-dominant)
        m = re.match(r'RLATCH\s+(\w+):\s*S=(\w+),\s*R1=(\w+)\s*→\s*Q1=(\w+)', line)
        if m:
            name, s, r1, q1 = m.group(1), m.group(2), m.group(3), m.group(4)
            src_w = self.alloc_wire(s)
            r1_w = self.alloc_wire(r1)
            q1_w = self.alloc_wire(q1)
            state = self.alloc_state(name)
            pi = self.alloc_param(float(r1_w), 0, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': q1_w,
                'op': OP_MAP['RS'], 'flags': 1,
                'param_idx': pi, 'state_offset': state,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_bit(self, line):
        # Try binary first: BIT name = sig1 OP sig2
        m = re.match(r'BIT\s+(\w+)\s*=\s*(\w+)\s+(BITAND|BITOR|BITXOR)\s+(\w+)', line)
        if m:
            name, s1, op, s2 = m.group(1), m.group(2), m.group(3), m.group(4)
            src_w = self.alloc_wire(s1)
            dst_w = self.alloc_wire(name)
            s2_w = self.alloc_wire(s2)
            opcode = OP_MAP[op]
            pi = self.alloc_param(float(s2_w), 0, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': opcode, 'flags': 1,
                'param_idx': pi, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })
            return
        # Try unary: BIT name = BITNOT sig
        m = re.match(r'BIT\s+(\w+)\s*=\s*BITNOT\s+(\w+)', line)
        if m:
            name, s1 = m.group(1), m.group(2)
            src_w = self.alloc_wire(s1)
            dst_w = self.alloc_wire(name)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['BITNOT'], 'flags': 1,
                'param_idx': 0, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_lut(self, line):
        # LUT name FROM signal TABLE v0 v1 v2 ... vN
        m = re.match(r'LUT\s+(\w+)\s+FROM\s+(\w+)\s+TABLE\s+(.*)', line)
        if m:
            name, src = m.group(1), m.group(2)
            table_vals = [float(v) for v in m.group(3).split()]
            src_w = self.alloc_wire(src)
            dst_w = self.alloc_wire(name)
            lut_start = self.next_lut
            for i, v in enumerate(table_vals):
                self.lut_entries.append((lut_start + i, v))
            self.next_lut += len(table_vals)
            pi = self.alloc_param(float(lut_start), float(len(table_vals)), 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['LUT'], 'flags': 1,
                'param_idx': pi, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_limit(self, line):
        # LIMIT name FROM signal RANGE lo hi
        m = re.match(r'LIMIT\s+(\w+)\s+FROM\s+(\w+)\s+RANGE\s+([-\d.]+)\s+([-\d.]+)', line)
        if m:
            name, src, lo, hi = m.group(1), m.group(2), float(m.group(3)), float(m.group(4))
            src_w = self.alloc_wire(src)
            dst_w = self.alloc_wire(name)
            pi = self.alloc_param(lo, hi, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['LIMIT'], 'flags': 1,
                'param_idx': pi, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_max(self, line):
        # MAX name = signal1 MAX signal2
        m = re.match(r'MAX\s+(\w+)\s*=\s*(\w+)\s+MAX\s+([-\d.]+|\w+)', line)
        if m:
            name, s1, s2 = m.group(1), m.group(2), m.group(3)
            src_w = self.alloc_wire(s1)
            dst_w = self.alloc_wire(name)
            # s2可能是常量或信号名
            try:
                s2_val = float(s2)
                # 常量，创建临时wire
                s2_w = self.alloc_wire(f'_const_{s2}')
                # 添加一个CONST路由来设置这个wire的值
                pi_const = self.alloc_param(s2_val, 0, 0, 0)
                self.routes.append({
                    'src_type': SRC_CONST, 'src_index': 0,
                    'dst_type': DST_WIRE, 'dst_channel': s2_w,
                    'op': OP_MAP['DIRECT'], 'flags': 1,
                    'param_idx': pi_const, 'state_offset': 0,
                    'actuator_idx': 0, 'wire2_idx': 0,
                })
            except ValueError:
                # 信号名
                s2_w = self.alloc_wire(s2)
            pi = self.alloc_param(float(s2_w), 0, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['MAX'], 'flags': 1,
                'param_idx': pi, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_min(self, line):
        # MIN name = signal1 MIN signal2
        m = re.match(r'MIN\s+(\w+)\s*=\s*(\w+)\s+MIN\s+([-\d.]+|\w+)', line)
        if m:
            name, s1, s2 = m.group(1), m.group(2), m.group(3)
            src_w = self.alloc_wire(s1)
            dst_w = self.alloc_wire(name)
            try:
                s2_val = float(s2)
                s2_w = self.alloc_wire(f'_const_{s2}')
                pi_const = self.alloc_param(s2_val, 0, 0, 0)
                self.routes.append({
                    'src_type': SRC_CONST, 'src_index': 0,
                    'dst_type': DST_WIRE, 'dst_channel': s2_w,
                    'op': OP_MAP['DIRECT'], 'flags': 1,
                    'param_idx': pi_const, 'state_offset': 0,
                    'actuator_idx': 0, 'wire2_idx': 0,
                })
            except ValueError:
                s2_w = self.alloc_wire(s2)
            pi = self.alloc_param(float(s2_w), 0, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['MIN'], 'flags': 1,
                'param_idx': pi, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_abs(self, line):
        # ABS name FROM signal
        m = re.match(r'ABS\s+(\w+)\s+FROM\s+(\w+)', line)
        if m:
            name, src = m.group(1), m.group(2)
            src_w = self.alloc_wire(src)
            dst_w = self.alloc_wire(name)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['ABS'], 'flags': 1,
                'param_idx': 0, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_eq(self, line):
        # EQ name FROM signal == threshold
        m = re.match(r'EQ\s+(\w+)\s+FROM\s+(\w+)\s*==\s*([-\d.]+)', line)
        if m:
            name, src, threshold = m.group(1), m.group(2), float(m.group(3))
            src_w = self.alloc_wire(src)
            dst_w = self.alloc_wire(name)
            pi = self.alloc_param(threshold, 0, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['EQ'], 'flags': 1,
                'param_idx': pi, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    def _parse_ne(self, line):
        # NE name FROM signal != threshold
        m = re.match(r'NE\s+(\w+)\s+FROM\s+(\w+)\s*!=\s*([-\d.]+)', line)
        if m:
            name, src, threshold = m.group(1), m.group(2), float(m.group(3))
            src_w = self.alloc_wire(src)
            dst_w = self.alloc_wire(name)
            pi = self.alloc_param(threshold, 0, 0, 0)
            self.routes.append({
                'src_type': SRC_WIRE, 'src_index': src_w,
                'dst_type': DST_WIRE, 'dst_channel': dst_w,
                'op': OP_MAP['NE'], 'flags': 1,
                'param_idx': pi, 'state_offset': 0,
                'actuator_idx': 0, 'wire2_idx': 0,
            })

    # ═══════════════════════════════════════════
    # 代码生成
    # ═══════════════════════════════════════════
    def topological_sort(self):
        """按依赖关系排序路由 (src必须在dst之前计算)"""
        graph = defaultdict(list)
        indeg = defaultdict(int)
        for i in range(len(self.routes)):
            indeg[i] = 0
        # wire_dst[wire_idx] = 写入该wire的路由索引
        wire_dst = {}
        for i, r in enumerate(self.routes):
            if r['dst_type'] == DST_WIRE:
                wire_dst[r['dst_channel']] = i
        for i, r in enumerate(self.routes):
            if r['src_type'] == SRC_WIRE:
                src_w = r['src_index']
                if src_w in wire_dst:
                    j = wire_dst[src_w]
                    if j != i:
                        graph[j].append(i)
                        indeg[i] += 1
        # Kahn算法
        q = deque([i for i in range(len(self.routes)) if indeg[i] == 0])
        sorted_list = []
        while q:
            n = q.popleft()
            sorted_list.append(n)
            for m in graph[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    q.append(m)
        if len(sorted_list) != len(self.routes):
            # 找环中的路由
            remaining = [i for i in range(len(self.routes)) if indeg[i] > 0]
            ops = [self._op_name(self.routes[i]['op']) for i in remaining[:5]]
            raise RuntimeError(f"循环依赖: {len(sorted_list)}/{len(self.routes)} 可排序, 环中路由: {ops}")
        self.routes = [self.routes[i] for i in sorted_list]

    def validate_resources(self):
        """编译时资源超限检查"""
        errors = []
        if len(self.routes) > H723_CONFIG['max_routes']:
            errors.append(f"路由超限: {len(self.routes)} > {H723_CONFIG['max_routes']}")
        if self.next_param > H723_CONFIG['max_params']:
            errors.append(f"参数超限: {self.next_param} > {H723_CONFIG['max_params']}")
        if self.next_state > H723_CONFIG['max_states']:
            errors.append(f"状态超限: {self.next_state} > {H723_CONFIG['max_states']}")
        if self.next_wire > H723_CONFIG['max_wires']:
            errors.append(f"WIRE超限: {self.next_wire} > {H723_CONFIG['max_wires']}")
        # WIRE冲突检测: 同名的wire_index唯一(自动分配已保证)
        if errors:
            raise RuntimeError("资源错误:\n  " + "\n  ".join(errors))

    def generate_binary(self) -> bytes:
        """
        生成 v2.0 运行程序二进制格式

        格式:
          [ProgramHeader: 16B]
          [RouteTable: n_routes × 16B]
          [ParamTable: n_params × 16B]
          [StateTable: n_states × 16B]

        ProgramHeader (16B):
          [0-3]   magic       uint32  = 0x50523047 ("PR0G")
          [4-7]   version     uint32  = 1
          [8-11]  flags       uint32  = 0
          [12-15] reserved    uint32  = 0
        """
        route_bin = b''
        for r in self.routes:
            route_bin += struct.pack(ROUTE_FMT,
                r['src_type'], r['src_index'], r['dst_type'], r['dst_channel'],
                r['op'], r['flags'], r['param_idx'], r['state_offset'],
                r.get('actuator_idx', 0), r.get('wire2_idx', 0))

        param_bin = b''
        for p in self.params:
            param_bin += struct.pack(PARAM_FMT, p[0], p[1], p[2], p[3])

        # State table: all zeros for initial deploy
        n_states = sum(1 for r in self.routes
                       if self._op_name(r['op']).replace('OP_', '') in STATEFUL_OPS)
        state_bin = b'\x00' * (n_states * 16)

        # Program Header (16 bytes, format '<IIHHHH'):
        #   [0-3]   magic       uint32 = 0x50523047 ("PR0G")
        #   [4-7]   format      uint32 = 1
        #   [8-9]   n_routes    uint16 (LE)
        #   [10-11] n_params    uint16 (LE)
        #   [12-13] n_states    uint16 (LE)
        #   [14-15] reserved    uint16
        header = struct.pack('<IIHHHH',
            0x50523047,                         # magic "PR0G"
            1,                                  # format version
            len(self.routes),                   # n_routes
            self.next_param,                    # n_params
            n_states,                           # n_states
            0)                                  # reserved

        return header + route_bin + param_bin + state_bin

    def _fmt_float(self, v: float) -> str:
        """格式化为C浮点字面量"""
        if v == int(v): return f'{int(v)}.0f'
        return f'{v}f'

    def _op_name(self, opcode: int) -> str:
        """操作码→宏名"""
        for k, v in OP_MAP.items():
            if v == opcode: return f'OP_{k}'
        return str(opcode)

    def generate_c_source(self) -> str:
        """生成C代码 (可直接粘贴到main.c, 使用OP_宏名)"""
        lines = []
        lines.append(f'    /* DCL: {len(self.routes)} routes, {self.next_param} params, {self.next_state} states */')
        # PARAM_TABLE
        for i, (a, b, c, d) in enumerate(self.params):
            lines.append(f'    PARAM_TABLE[{i}].value_a = {self._fmt_float(a)}; '
                         f'PARAM_TABLE[{i}].value_b = {self._fmt_float(b)}; '
                         f'PARAM_TABLE[{i}].value_c = {self._fmt_float(c)}; '
                         f'PARAM_TABLE[{i}].value_d = {self._fmt_float(d)};')
        lines.append('')
        # Routes
        for r in self.routes:
            st = {0: 'SRC_SENSOR', 1: 'SRC_WIRE', 2: 'SRC_CONST'}[r['src_type']]
            op = self._op_name(r['op'])
            lines.append(
                f'    init_route(ri++, {st}, {r["src_index"]}, DST_WIRE, {r["dst_channel"]}, '
                f'{op}, {r["param_idx"]}, {r["state_offset"]});'
            )
        return '\n'.join(lines)

    def print_stats(self):
        import sys
        out = sys.stdout
        out.write(f"Routes: {len(self.routes)}/{H723_CONFIG['max_routes']}\n")
        out.write(f"Params: {self.next_param}/{H723_CONFIG['max_params']}\n")
        out.write(f"States: {self.next_state}/{H723_CONFIG['max_states']}\n")
        out.write(f"Wires:  {self.next_wire}/{H723_CONFIG['max_wires']}\n")
        for i, r in enumerate(self.routes):
            op_name = self._op_name(r['op'])
            out.write(f"  R{i}: {op_name} wire[{r['src_index']}] -> wire[{r['dst_channel']}] pi={r['param_idx']} si={r['state_offset']}\n")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('input', help='.dcl source file')
    ap.add_argument('-o', '--output', help='output binary file')
    ap.add_argument('--json', action='store_true', help='output JSON')
    ap.add_argument('--c', action='store_true', help='output C source')
    args = ap.parse_args()

    # 尝试多种编码
    for enc in ['utf-8-sig', 'utf-8', 'gbk', 'latin-1']:
        try:
            with open(args.input, 'r', encoding=enc) as f:
                source = f.read()
            break
        except UnicodeDecodeError:
            continue
    else:
        raise RuntimeError(f"无法解码文件 {args.input}, 请保存为UTF-8编码")

    compiler = DCLCompiler()
    try:
        compiler.parse(source)
        compiler.topological_sort()
        compiler.validate_resources()
    except RuntimeError as e:
        print(f"❌ 编译失败: {e}", file=sys.stderr)
        sys.exit(1)
    compiler.print_stats()

    if args.c:
        print(compiler.generate_c_source())
        return

    if args.json:
        print(json.dumps({'routes': compiler.routes, 'params': compiler.params}, indent=2))

    if args.output:
        binary = compiler.generate_binary()
        with open(args.output, 'wb') as f:
            f.write(binary)
        print(f"\n生成: {args.output} ({len(binary)} bytes)")


if __name__ == '__main__':
    main()
