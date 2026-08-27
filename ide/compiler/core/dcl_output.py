#!/usr/bin/env python3
"""
统一输出格式 - 所有命令都通过这个模块输出
"""

import json
import sys
import time
from typing import Any, Dict, Optional

# 颜色代码
class Colors:
    RESET = '\033[0m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    DIM = '\033[2m'
    BOLD = '\033[1m'

def _supports_color():
    """检测终端是否支持颜色"""
    if sys.platform == 'win32':
        return True  # Windows 10+ 默认支持
    return hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()

USE_COLOR = _supports_color()

def _color(text, color):
    if USE_COLOR:
        return f"{color}{text}{Colors.RESET}"
    return text

# ═════════════════════════════════════════════════════════════════════════════
# 结构化输出
# ═════════════════════════════════════════════════════════════════════════════

def output(data: Dict[str, Any], exit_code: int = 0) -> int:
    """
    统一JSON输出
    
    参数:
        data: 要输出的数据字典
        exit_code: 退出码（0=成功，非0=失败）
    
    返回:
        exit_code
    """
    result = {
        'success': exit_code == 0,
        'timestamp': time.time(),
        **data
    }
    
    # 使用2空格缩进，确保可读性
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return exit_code

def success(message: str, data: Optional[Dict] = None) -> int:
    """
    成功输出
    
    参数:
        message: 成功消息
        data: 附加数据
    
    返回:
        0
    """
    result = {'message': message}
    if data:
        result.update(data)
    return output(result, exit_code=0)

def error(message: str, details: Optional[Dict] = None, suggestion: Optional[str] = None) -> int:
    """
    错误输出
    
    参数:
        message: 错误消息
        details: 详细错误信息
        suggestion: 修复建议
    
    返回:
        1
    """
    result = {'error': message}
    if details:
        result['details'] = details
    if suggestion:
        result['suggestion'] = suggestion
    return output(result, exit_code=1)

def warning(message: str, data: Optional[Dict] = None) -> int:
    """
    警告输出（不改变退出码）
    
    参数:
        message: 警告消息
        data: 附加数据
    
    返回:
        0
    """
    result = {'warning': message}
    if data:
        result.update(data)
    return output(result, exit_code=0)

def info(message: str, data: Optional[Dict] = None) -> int:
    """
    信息输出
    
    参数:
        message: 信息内容
        data: 附加数据
    
    返回:
        0
    """
    result = {'info': message}
    if data:
        result.update(data)
    return output(result, exit_code=0)

# ═════════════════════════════════════════════════════════════════════════════
# 人类可读输出（非JSON）
# ═════════════════════════════════════════════════════════════════════════════

def human(text: str, end: str = '\n'):
    """纯文本输出（不JSON格式化）"""
    print(text, end=end)

def header(text: str):
    """标题输出"""
    human("")
    human(_color(f"{'='*60}", Colors.CYAN))
    human(_color(f"  {text}", Colors.BOLD))
    human(_color(f"{'='*60}", Colors.CYAN))

def table(data: list, headers: list):
    """
    表格输出
    
    参数:
        data: 二维列表
        headers: 表头
    """
    if not data:
        human("  (无数据)")
        return
    
    # 计算列宽
    col_widths = [len(h) for h in headers]
    for row in data:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))
    
    # 输出表头
    header_str = " | ".join(h.ljust(w) for h, w in zip(headers, col_widths))
    human(_color(f"  {header_str}", Colors.BOLD))
    human(_color(f"  {'-' * len(header_str)}", Colors.DIM))
    
    # 输出数据
    for row in data:
        row_str = " | ".join(str(cell).ljust(w) for cell, w in zip(row, col_widths))
        human(f"  {row_str}")

def progress(current: int, total: int, prefix: str = ""):
    """进度条"""
    length = 40
    filled = int(length * current / total) if total > 0 else 0
    bar = '█' * filled + '░' * (length - filled)
    percent = f"{current}/{total}" if total > 0 else str(current)
    human(f"\r  {prefix} [{bar}] {percent}", end='')
    if current >= total:
        human("")
