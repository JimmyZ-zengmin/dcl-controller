#!/usr/bin/env python3
"""
硬件通信 - 封装pyocd操作
使用 pyocd 原生命令 (loadmem/fill) 替代分号分隔多命令，确保可靠性
"""

import struct
import subprocess
import json
import time
import os
import tempfile
from typing import List, Optional, Dict, Any

# pyocd路径
PYOCD = r'C:\Espressif\tools\python\v6.0.1\venv\Scripts\pyocd.exe'

# DTCM地址映射
ADDRESSES = {
    'ROUTE_TABLE': 0x20001700,
    'PARAM_TABLE': 0x20005700,
    'ACTIVE_ROUTES': 0x200000F0,
    'WIRE_MAP': 0x20000300,
    'SENSOR_MAP': 0x20000100,      # 修正: 与固件一致
    'ACTUATOR_STATUS': 0x20000200,  # 新增: 执行器状态
    'SCRATCH2': 0x200000F8,         # DTCM 暂存器 (部署握手)
}

# TIM1 寄存器 (用于停止/启动 ISR 引擎)
TIM1_BASE = 0x40010000
TIM1_CR1  = TIM1_BASE + 0x00   # 控制寄存器 (CEN位)
TIM1_DIER = TIM1_BASE + 0x0C   # DMA/中断使能 (UIE位)
TIM1_SR   = TIM1_BASE + 0x10   # 状态寄存器

# NVIC 寄存器 (TIM1_UP_IRQn = 25, 在 NVIC_ISER0 的 bit 25)
NVIC_ISER0      = 0xE000E100      # 中断使能寄存器0
NVIC_ISER1      = 0xE000E104      # 中断使能寄存器1
TIM1_UP_IRQ_BIT = 25              # TIM1_UP position in NVIC_ISER0

# Scratch register for "deployed" flag (pyocd ↔ firmware handshake)
SCRATCH2        = 0x200000F8      # DTCM 暂存器
DEPLOYED_MAGIC  = 0xDEADBEEF      # 与固件 main.c 匹配

# 临时文件目录 (用于 loadmem 命令)
TEMP_DIR = r'C:\Temp'


class Hardware:
    """硬件通信类"""
    
    def __init__(self):
        self.connected = False
        self.last_error = None
    
    def connect(self) -> bool:
        """连接硬件"""
        try:
            result = subprocess.run(
                [PYOCD, 'commander', '-t', 'stm32h723xx', '-c', 'status'],
                capture_output=True, text=True, timeout=5
            )
            self.connected = result.returncode == 0
            return self.connected
        except Exception as e:
            self.last_error = str(e)
            self.connected = False
            return False
    
    def disconnect(self):
        """断开连接"""
        self.connected = False
    
    def _pyocd_cmd(self, cmd: str, timeout: int = 10) -> tuple:
        """
        执行 pyocd 命令
        
        返回: (success: bool, stdout: str, stderr: str)
        """
        try:
            result = subprocess.run(
                [PYOCD, 'commander', '-t', 'stm32h723xx', '-c', cmd],
                capture_output=True, text=True, timeout=timeout
            )
            return (result.returncode == 0, result.stdout, result.stderr)
        except Exception as e:
            return (False, '', str(e))
    
    def read32(self, addr: int, count: int = 1) -> Optional[List[int]]:
        """
        读取32位数据
        
        参数:
            addr: 起始地址
            count: 读取字数 (每个字4字节)
        
        返回:
            整数列表，失败返回None
            
        注意: pyocd read32 的 LEN 参数是字节数，必须4字节对齐
        """
        byte_count = count * 4
        ok, stdout, stderr = self._pyocd_cmd(f'read32 0x{addr:08X} {byte_count}')
        
        if not ok:
            self.last_error = stderr
            return None
        
        # 解析输出
        # 格式: 20000300:  3f9d336c 3dfb857a ...
        values = []
        for line in stdout.split('\n'):
            line = line.strip()
            if not line or line.startswith('Error') or ':' not in line:
                continue
            
            # 提取地址后的hex值
            parts = line.split(':', 1)
            if len(parts) >= 2:
                hex_part = parts[1].split('|')[0].strip()
                for p in hex_part.split():
                    if len(p) == 8:
                        try:
                            values.append(int(p, 16))
                        except:
                            pass
        
        return values if values else None
    
    def write32(self, addr: int, value: int) -> bool:
        """
        写入32位数据
        
        参数:
            addr: 地址
            value: 值
        
        返回:
            是否成功
        """
        ok, _, stderr = self._pyocd_cmd(f'write32 0x{addr:08X} 0x{value:08X}')
        if not ok:
            self.last_error = stderr
        return ok
    
    def write_block(self, addr: int, data: bytes) -> bool:
        """
        写入数据块（使用 pyocd loadmem 命令）
        
        参数:
            addr: 起始地址
            data: 字节数据
        
        返回:
            是否成功
        """
        if not data:
            return True
        
        # 确保临时目录存在
        os.makedirs(TEMP_DIR, exist_ok=True)
        
        # 写入临时文件
        tmp_path = os.path.join(TEMP_DIR, f'dcl_write_{addr:08X}_{int(time.time()*1000)%1000000:06d}.bin')
        try:
            with open(tmp_path, 'wb') as f:
                f.write(data)
            
            # 使用 loadmem 命令写入
            # 注意: pyocd 命令中的路径使用正斜杠或双反斜杠
            cmd_path = tmp_path.replace('\\', '/')
            ok, stdout, stderr = self._pyocd_cmd(f'loadmem 0x{addr:08X} {cmd_path}', timeout=30)
            
            if not ok:
                self.last_error = stderr
                return False
            
            return True
        except Exception as e:
            self.last_error = str(e)
            return False
        finally:
            # 清理临时文件
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except:
                pass
    
    def fill_block(self, addr: int, size: int, value: int = 0) -> bool:
        """
        填充/清零内存块（使用 pyocd fill 命令）
        
        参数:
            addr: 起始地址
            size: 字节数
            value: 填充值 (默认0)
        
        返回:
            是否成功
        """
        if size <= 0:
            return True
        
        # pyocd fill 命令: fill 32 ADDR LEN PATTERN
        # LEN 是字节数，ADDR+LEN 必须在有效内存范围内
        ok, stdout, stderr = self._pyocd_cmd(
            f'fill 32 0x{addr:08X} 0x{size:08X} 0x{value:08X}', timeout=30
        )
        
        if not ok:
            self.last_error = stderr
            return False
        
        return True
    
    def verify_block(self, addr: int, expected: bytes) -> bool:
        """
        验证内存块数据（读回并对比）
        
        参数:
            addr: 起始地址
            expected: 期望的字节数据
        
        返回:
            是否匹配
        """
        if not expected:
            return True
        
        # 读回数据 (count = 字节数/4)
        word_count = len(expected) // 4
        if word_count == 0:
            return True
        
        raw = self.read32(addr, word_count)
        if raw is None:
            return False
        
        # 转换为字节并对比
        actual = b''.join(struct.pack('<I', v) for v in raw)
        return actual == expected
    
    def read_wires(self, start: int = 0, count: int = 16) -> Optional[List[float]]:
        """
        读取WIRE（浮点值）
        
        参数:
            start: 起始WIRE索引
            count: 读取数量
        
        返回:
            浮点值列表
        """
        addr = ADDRESSES['WIRE_MAP'] + start * 4
        raw = self.read32(addr, count)
        
        if raw is None:
            return None
        
        # 将32位整数转换为float
        values = []
        for val in raw:
            try:
                fval = struct.unpack('f', struct.pack('I', val))[0]
                values.append(fval)
            except:
                values.append(0.0)
        
        return values
    
    def write_wire(self, index: int, value: float) -> bool:
        """
        写入WIRE（浮点值）
        
        参数:
            index: WIRE索引
            value: 浮点值
        
        返回:
            是否成功
        """
        addr = ADDRESSES['WIRE_MAP'] + index * 4
        ivalue = struct.unpack('<I', struct.pack('<f', float(value)))[0]
        return self.write32(addr, ivalue)
    
    def read_sensors(self, start: int = 0, count: int = 16) -> Optional[List[float]]:
        """读取SENSOR"""
        addr = ADDRESSES['SENSOR_MAP'] + start * 4
        raw = self.read32(addr, count)
        
        if raw is None:
            return None
        
        values = []
        for val in raw:
            try:
                fval = struct.unpack('f', struct.pack('I', val))[0]
                values.append(fval)
            except:
                values.append(0.0)
        
        return values
    
    def get_active_routes(self) -> Optional[int]:
        """获取活跃路由数量"""
        raw = self.read32(ADDRESSES['ACTIVE_ROUTES'], 1)
        return raw[0] if raw else None
    
    def deploy(self, data: bytes) -> bool:
        """
        部署二进制数据到硬件
        
        参数:
            data: 二进制数据（包含头部）
        
        返回:
            是否成功
        """
        try:
            # 解析头部
            if len(data) < 16:
                self.last_error = "数据太短"
                return False
            
            # Header: n_routes(4B) + n_params(4B) + active_routes(4B) = 12 bytes
            route_count = struct.unpack_from('<I', data, 0)[0]
            param_count = struct.unpack_from('<I', data, 4)[0]
            active_routes = struct.unpack_from('<I', data, 8)[0]
            
            offset = 12
            route_size = route_count * 16
            param_size = param_count * 16
            
            if len(data) < offset + route_size + param_size:
                self.last_error = "数据长度不匹配"
                return False
            
            route_data = data[offset:offset + route_size]
            offset += route_size
            param_data = data[offset:offset + param_size]
            
            # ── Step 1: 停止 ISR 引擎 (防止写入过程中 ISR 读取不一致数据) ──
            self.write32(TIM1_DIER, 0x00000000)   # 禁用 UIE 中断
            self.write32(TIM1_SR,  0x0000FFFF)    # 清除所有状态标志
            self.write32(TIM1_CR1, 0x00000000)    # CEN=0, 停止计数器
            
            # ── Step 2: 清零旧路由表 (使用 fill 命令，单次调用清零整个区域) ──
            # ROUTE_TABLE = 1024 entries × 16 bytes = 16384 bytes
            self.fill_block(ADDRESSES['ROUTE_TABLE'], 1024 * 16)
            # PARAM_TABLE = 512 entries × 16 bytes = 8192 bytes
            self.fill_block(ADDRESSES['PARAM_TABLE'], 512 * 16)
            # WIRE_MAP = 1024 × 4 = 4096 bytes
            self.fill_block(ADDRESSES['WIRE_MAP'], 1024 * 4)
            # ACTUATOR_STATUS = 64 × 4 = 256 bytes
            self.fill_block(ADDRESSES['ACTUATOR_STATUS'], 64 * 4)
            
            # ── Step 3: 写入 ROUTE_TABLE (使用 loadmem 命令) ──
            if route_data:
                if not self.write_block(ADDRESSES['ROUTE_TABLE'], route_data):
                    return False
            
            # ── Step 4: 写入 PARAM_TABLE (使用 loadmem 命令) ──
            if param_data:
                if not self.write_block(ADDRESSES['PARAM_TABLE'], param_data):
                    return False
            
            # ── Step 5: 验证写入的数据 ──
            if route_data and not self.verify_block(ADDRESSES['ROUTE_TABLE'], route_data):
                self.last_error = "ROUTE_TABLE 验证失败"
                return False
            if param_data and not self.verify_block(ADDRESSES['PARAM_TABLE'], param_data):
                self.last_error = "PARAM_TABLE 验证失败"
                return False
            
            # ── Step 6: 设置 ACTIVE_ROUTES ──
            if not self.write32(ADDRESSES['ACTIVE_ROUTES'], active_routes):
                return False

            # ── Step 6b: 部署握手标志 + 使能 NVIC TIM1_UP 中断 ──
            # 写入 DEPLOYED_MAGIC 让 main() 跳过硬编码路由
            if not self.write32(ADDRESSES['SCRATCH2'], DEPLOYED_MAGIC):
                return False

            # TIM1_UP_IRQn = 25, 对应 NVIC_ISER0 bit 25
            # 芯片热启动后 NVIC 寄存器被清零, 必须重新使能
            nvic_val = (1 << TIM1_UP_IRQ_BIT)
            if not self.write32(NVIC_ISER0, nvic_val):
                return False

            # ── Step 7: 启动 ISR 引擎 ──
            self.write32(TIM1_DIER, 0x00000001)   # 启用 UIE 中断
            self.write32(TIM1_CR1, 0x00000001)    # CEN=1, 启动计数器
            
            return True
            
        except Exception as e:
            self.last_error = str(e)
            return False
    
    def get_status(self) -> Dict[str, Any]:
        """获取硬件状态"""
        active_routes = self.get_active_routes()
        
        return {
            'connected': self.connected,
            'hardware': 'STM32H723ZGT6',
            'frequency': '544MHz',
            'isr_period': '100us',
            'active_routes': active_routes,
            'addresses': ADDRESSES,
        }


# 全局硬件实例
_hw = Hardware()

def get_hardware() -> Hardware:
    """获取全局硬件实例"""
    return _hw
