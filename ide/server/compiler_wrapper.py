#!/usr/bin/env python3
"""
DCL编译器封装模块 — 不修改原有编译器，提供结构化编译结果和符号表提取。
"""

import logging
from dataclasses import dataclass, field

from compiler.dcl_compiler import (
    DCLCompiler,
    OP_MAP,
    SRC_SENSOR,
    SRC_WIRE,
    SRC_CONST,
    DST_WIRE,
)

logger = logging.getLogger('dcl-ide.compiler')

# 操作码 → 名称 反向查找表
OP_NAMES = {
    0x00: 'DIRECT', 0x01: 'CMP', 0x02: 'HYST', 0x03: 'CLAMP',
    0x04: 'LPF', 0x05: 'PID', 0x06: 'RATE', 0x07: 'DEADBAND',
    0x08: 'MUX', 0x09: 'EDGE', 0x0A: 'LUT', 0x0B: 'CNT',
    0x0C: 'TIMER', 0x0E: 'SCALE', 0x0F: 'AND', 0x10: 'OR',
    0x11: 'NOT', 0x12: 'REG', 0x13: 'ADD', 0x14: 'SUB',
    0x15: 'MUL', 0x16: 'DIV', 0x17: 'BITAND', 0x18: 'BITOR',
    0x19: 'BITXOR', 0x1A: 'BITNOT', 0x1B: 'SR', 0x1C: 'RS',
    0x1D: 'COUNTER',
}


@dataclass
class Diagnostic:
    """编译诊断信息"""
    message: str
    line: int = 0
    severity: str = 'error'  # error | warning | info


@dataclass
class CompileResult:
    """编译结果数据"""
    binary: bytes = b''
    errors: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    symbol_table: list = field(default_factory=list)
    c_source: str = ''


class CompilerWrapper:
    """DCL编译器封装，提供结构化编译与符号表提取。"""

    def compile(self, source: str) -> CompileResult:
        """完整编译：解析 → 排序 → 校验 → 生成二进制 + 符号表 + 统计"""
        result = CompileResult()
        compiler = DCLCompiler()

        try:
            compiler.parse(source)
            compiler.topological_sort()
            compiler.validate_resources()
        except RuntimeError as e:
            result.errors.append(str(e))
            logger.warning('编译失败: %s', e)
            return result

        # 生成二进制
        result.binary = compiler.generate_binary()
        result.c_source = compiler.generate_c_source()

        # 提取符号表
        result.symbol_table = self._extract_symbol_table(compiler)

        # 统计信息
        result.stats = {
            'routes': len(compiler.routes),
            'params': compiler.next_param,
            'states': compiler.next_state,
            'wires': compiler.next_wire,
            'binary_size': len(result.binary),
        }

        logger.info(
            '编译成功: %d routes, %d params, %d states, %d wires, %d bytes',
            result.stats['routes'], result.stats['params'],
            result.stats['states'], result.stats['wires'],
            result.stats['binary_size'],
        )
        return result

    def check_sync(self, source: str) -> list:
        """同步检查：仅返回错误字符串列表，供IDE实时检查用。"""
        errors = []
        compiler = DCLCompiler()
        try:
            compiler.parse(source)
            compiler.topological_sort()
            compiler.validate_resources()
        except RuntimeError as e:
            errors.append(str(e))
        return errors

    def check(self, source: str) -> list:
        """仅检查语法与语义错误，不生成二进制。适用于LSP诊断。"""
        diagnostics = []
        compiler = DCLCompiler()

        try:
            compiler.parse(source)
            compiler.topological_sort()
            compiler.validate_resources()
        except RuntimeError as e:
            diagnostics.append(Diagnostic(message=str(e)))

        logger.debug('检查完成: %d 条诊断', len(diagnostics))
        return diagnostics

    def _extract_symbol_table(self, compiler: DCLCompiler) -> list:
        """
        从编译器的 wire_index 和 routes 提取符号表。

        对 wire_index 中每个信号，确定:
        - name: 信号名
        - wire_idx: 线索引
        - fb_type: 写入该线缆的功能块类型（通过 routes 反查 op）
        - direction: 'input' / 'output' / 'internal'
        """
        # 构建 wire_idx → 写入该线缆的路由列表 的映射
        wire_writers: dict[int, list] = {}
        for route in compiler.routes:
            if route['dst_type'] == DST_WIRE:
                wire_writers.setdefault(route['dst_channel'], []).append(route)

        # 判断哪些线缆由 SENSOR 写入
        sensor_wires: set[int] = set()
        for route in compiler.routes:
            if route['src_type'] == SRC_SENSOR and route['dst_type'] == DST_WIRE:
                sensor_wires.add(route['dst_channel'])

        # 识别 OUTPUT 信号：名称以 "out_" 前缀开头（编译器 _parse_output 的约定）
        output_names: set[str] = set()
        for name in compiler.wire_index:
            if name.startswith('out_'):
                output_names.add(name)

        symbol_table = []
        for name, wire_idx in compiler.wire_index.items():
            # 确定方向
            if wire_idx in sensor_wires:
                direction = 'input'
            elif name in output_names:
                direction = 'output'
            else:
                direction = 'internal'

            # 确定写入该线缆的功能块类型
            fb_type = ''
            if wire_idx in wire_writers:
                # 取第一个写入路由的 op 进行反向查找
                op_code = wire_writers[wire_idx][0]['op']
                fb_type = OP_NAMES.get(op_code, f'UNKNOWN(0x{op_code:02X})')

            symbol_table.append({
                'name': name,
                'wire_idx': wire_idx,
                'fb_type': fb_type,
                'direction': direction,
            })

        logger.debug('符号表提取: %d 个信号', len(symbol_table))
        return symbol_table
