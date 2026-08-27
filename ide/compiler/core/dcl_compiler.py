#!/usr/bin/env python3
"""
编译器封装
"""

import os
import sys
from typing import Optional, Dict, Any, Tuple

# 导入编译器
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dcl_compiler import DCLCompiler

def compile_file(input_file: str, output_file: Optional[str] = None) -> Tuple[bool, Dict[str, Any]]:
    """
    编译DCL文件
    
    参数:
        input_file: 输入文件路径
        output_file: 输出文件路径（可选）
    
    返回:
        (成功标志, 结果字典)
    """
    if not os.path.exists(input_file):
        return False, {'error': f'文件不存在: {input_file}'}
    
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            source = f.read()
        
        compiler = DCLCompiler()
        compiler.parse(source)
        compiler.topological_sort()
        compiler.validate_resources()
        
        # 生成二进制
        binary = compiler.generate_binary()
        
        # 写入文件
        if output_file:
            with open(output_file, 'wb') as f:
                f.write(binary)
        
        # 构建符号表
        symbol_table = {
            'wires': {name: idx for name, idx in compiler.wire_index.items()},
            'sensors': {name: idx for name, idx in compiler.sensor_source_map.items()},
            'params': compiler.next_param,
            'states': compiler.next_state,
        }
        
        return True, {
            'input': input_file,
            'output': output_file,
            'stats': {
                'routes': len(compiler.routes),
                'params': compiler.next_param,
                'states': compiler.next_state,
                'wires': compiler.next_wire,
                'binary_size': len(binary),
            },
            'symbol_table': symbol_table,
            'binary_hex': binary.hex() if not output_file else None,
        }
        
    except Exception as e:
        return False, {
            'error': f'编译失败: {str(e)}',
            'type': type(e).__name__,
        }

def compile_source(source: str) -> Tuple[bool, Dict[str, Any]]:
    """
    编译DCL源代码（字符串）
    
    参数:
        source: DCL源代码
    
    返回:
        (成功标志, 结果字典)
    """
    try:
        compiler = DCLCompiler()
        compiler.parse(source)
        compiler.topological_sort()
        compiler.validate_resources()
        
        # 生成二进制
        binary = compiler.generate_binary()
        
        # 构建符号表
        symbol_table = {
            'wires': {name: idx for name, idx in compiler.wire_index.items()},
            'sensors': {name: idx for name, idx in compiler.sensor_source_map.items()},
            'params': compiler.next_param,
            'states': compiler.next_state,
        }
        
        return True, {
            'stats': {
                'routes': len(compiler.routes),
                'params': compiler.next_param,
                'states': compiler.next_state,
                'wires': compiler.next_wire,
                'binary_size': len(binary),
            },
            'symbol_table': symbol_table,
            'binary': binary,
        }
        
    except Exception as e:
        return False, {
            'error': f'编译失败: {str(e)}',
            'type': type(e).__name__,
        }
