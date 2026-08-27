#!/usr/bin/env python3
"""
DCL 在线监视器 — 通过 UART (非侵入式) 监视引擎运行
==========================================================
设计文档: docs/monitor.md

用法:
  python tools/flash/monitor.py <subcmd> [args]

子命令:
  read   <addr>              立即读一个 WIRE/ACT 值 (零侵入)
  watch  <w0> [w1] [...]     持续监测,每 100 ms 刷新
  record <w0> [w1] [...]     录 CSV (离线分析)
  log                       拉引擎环形日志环,导出 CSV
  status                    一次 SNAPSHOT + OBSERVE (引擎状态)
  scope_arm --pin <n>       抖动测试模式 (GPIO 翻折)
  repl                      进入交互式 RET比分PL

其中 addr 支持别名:
  wire[N]  → DTCM + 0x0300 + N*4
  act[N]   → DTCM + 0x2000 + N*4
  hex 地址  → 直接作为 uint32

示例:
  python tools/flash/monitor.py read 0x0300
  python tools/flash/monitor.py watch wire[2] act[1]
  python tools/flash/monitor.py record act[32] act[33] --out run.csv
  python tools/flash/monitor.py log
  python tools/flash/monitor.py status
  python tools/flash/monitor.py scope_arm --pin 3
  python tools/flash/monitor.py repl
"""
import serial
import struct
import time
import sys
import os
import csv
from datetime import datetime

# ══════════════════════════════════════════════════════════════
# 配置(与 MCU 端同步)
# ══════════════════════════════════════════════════════════════
PORT = os.environ.get('DCL_PORT', 'COM11')          # ST-Link VCP
BAUD = int(os.environ.get('DCL_BAUD', '1000000'))   # 1 Mbps (OVER8)
CMSIS_SERIAL = '000000805059ed5520a4400013dd0702a5a5a5a59796990e'

# 帧码
FRAME_CMD = 0xC0
FRAME_STS = 0xC1

# CMD 码
CMD_DEPLOY   = 0x10
CMD_START    = 0x11
CMD_STOP     = 0x12
CMD_RESET    = 0x13
CMD_READ     = 0x20
CMD_WRITE    = 0x21
CMD_OBSERVE  = 0x30   # 批量读(新)
CMD_SNAPSHOT = 0x31   # 同刻冻结(新)
CMD_LOGR     = 0x32   # 拉日志环(新)

# STS 码
STS_ACK         = 0x01
STS_ERROR       = 0x02
STS_WIRE_DATA   = 0x20
STS_SNAPSHOT    = 0x30
STS_LOG_DATA    = 0x32

# DTCM 布局 (与 main.c 同步, 详见 CLAUDE.md 和 core0/Src/main.c)
DTCM_BASE        = 0x20000000
TIMING_OFF       = 0x0000
ACT_BASE_OFF     = 0x0200
SENSOR_BASE_OFF  = 0x0100
WIRE_BASE_OFF    = 0x0300
SHADOW_OFF       = 0x00E0
ADC_RAW_OFF      = 0x00F0
N_ROUTES_OFF     = 0x00F0 + 0x10  # 0x00F0 后 → 实际应由 main.c 确认
LOGR_BASE_OFF    = 0xD000

# 时序变量偏移
OFF_EXEC_MIN    = TIMING_OFF + 0x00
OFF_EXEC_MAX    = TIMING_OFF + 0x04
OFF_PERIOD_MIN  = TIMING_OFF + 0x08
OFF_PERIOD_MAX  = TIMING_OFF + 0x0C
OFF_SAMPLES     = TIMING_OFF + 0x10
OFF_LAST_ENTRY  = TIMING_OFF + 0x14
OFF_HEARTBEAT   = TIMING_OFF + 0x18
OFF_CLOCK_HZ    = TIMING_OFF + 0x1C
OFF_TIMER_HZ    = TIMING_OFF + 0x20


# ══════════════════════════════════════════════════════════════
# 底层帧协议
# ══════════════════════════════════════════════════════════════
def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-CCITT (poly=0x1021), 与 MCU 端一致。"""
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc


def build_frame(cmd: int, payload: bytes = b'') -> bytes:
    """构建 CMD 帧: [0xC0][CMD][LEN:2LE][PAYLOAD][CRC16:2LE]"""
    length = len(payload)
    frame = bytes([FRAME_CMD, cmd & 0xFF, length & 0xFF, (length >> 8) & 0xFF]) + payload
    crc = crc16_ccitt(frame[1:])          # CRC 覆盖 CMD+LEN+PAYLOAD
    return frame + struct.pack('<H', crc)


def parse_status_frame(data: bytes):
    """解析 STS 帧:
       返回 dict {'sts': int, 'payload': bytes, 'crc_ok': bool}
       若数据不够返回 None。"""
    if len(data) < 6 or data[0] != FRAME_STS:
        return None
    sts = data[1]
    length = data[2] | (data[3] << 8)
    total = 4 + length + 2
    if len(data) < total:
        return None
    payload = data[4:4 + length]
    crc_recv = struct.unpack('<H', data[4 + length:total])[0]
    crc_calc = crc16_ccitt(data[1:4 + length])
    return {'sts': sts, 'payload': payload, 'crc_ok': crc_ok,
            'raw': data[:total], 'remainder': data[total:]}


class DCLMonitor:
    """通过 UART 与核心0通信的上下文管理器。"""

    def __init__(self, port: str = PORT, baud: int = BAUD, timeout: float = 1.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None

    def open(self):
        self.ser = serial.Serial(self.port, self.baud,
                                 timeout=self.timeout,
                                 inter_byte_timeout=0.05)
        time.sleep(0.3)
        self.ser.reset_input_buffer()

    def close(self):
        if self.ser:
            self.ser.close()
            self.ser = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *a):
        self.close()

    def _send_cmd(self, cmd: int, payload: bytes = b'') -> dict | None:
        """发一个 CMD, 等 STS 返回。"""
        self.ser.write(build_frame(cmd, payload))
        deadline = time.time() + self.timeout
        buf = b''
        while time.time() < deadline:
            if self.ser.in_waiting:
                buf += self.ser.read(self.ser.in_waiting)
                # 在累积的 buffer 中找第一个合法 STS 帧
                while True:
                    idx = buf.find(bytes([FRAME_STS]))
                    if idx > 0:
                        buf = buf[idx:]          # 把前面的杂音丢掉
                    if idx == 0 and len(buf) >= 6:
                        r = parse_status_frame(buf)
                        if r is not None:
                            self._last_sts = r
                            self._last_remainder = r.pop('remainder')
                            return r
                        break
                    break
            time.sleep(0.01)
        return None

    # ── READ / OBSERVE ─────────────────────────────────────────
    def read_one(self, addr: int) -> float | int | None:
        """读一个 32-bit 值 (READ)。
           addr 为 DTCM 字节偏移。
           返回 int 或 float 由调用者按需解读。"""
        payload = struct.pack('<H', addr) + struct.pack('<H', 1)
        r = self._send_cmd(CMD_READ, payload)
        if r is None or r['sts'] != STS_WIRE_DATA or len(r['payload']) < 4:
            return None
        return struct.unpack('<f', r['payload'][:4])[0]

    def observe(self, addrs: list[int]) -> list[float | int | None]:
        """批量读 (OBSERVE)。
           addrs 为字节偏移列表。
           返回相同长度的 list, 失败位为 None。"""
        if len(addrs) > 64:
            raise ValueError("OBSERVE 最多一次 64 个地址")
        payload = struct.pack('<H', len(addrs))
        for a in addrs:
            payload += struct.pack('<H', a)
        r = self._send_cmd(CMD_OBSERVE, payload)
        if r is None or r['sts'] != STS_WIRE_DATA:
            return [None] * len(addrs)
        n = struct.unpack('<H', r['payload'][:2])[0]
        vals = []
        off = 2
        for i in range(min(n, len(addrs))):
            raw = r['payload'][off:off + 4]
            if len(raw) == 4:
                vals.append(struct.unpack('<f', raw)[0])
            else:
                vals.append(None)
            off += 4
        return vals

    # ── SNAPSHOT ────────────────────────────────────────────────
    def snapshot(self) -> dict | None:
        """发 SNAPSHOT, 立即用 OBSERVE 把 snapshot entry 拉出来。"""
        if self._send_cmd(CMD_SNAPSHOT) is None:
            return None
        # 固定 snap entry 在 SNAP_BASE (DTCM + 0xD500, 64B)
        # 简化: 直接 OBSERVE SHADOW 和 时序 变量构成一次快照
        addrs = [OFF_SAMPLES, OFF_PERIOD_MIN, OFF_PERIOD_MAX,
                 OFF_EXEC_MIN, OFF_EXEC_MAX, OFF_HEARTBEAT,
                 SHADOW_OFF, ADC_RAW_OFF]
        vals = self.observe(addrs)
        return {
            'SAMPLES':     vals[0],
            'PERIOD_MIN':  vals[1],
            'PERIOD_MAX':  vals[2],
            'EXEC_MIN':    vals[3],
            'EXEC_MAX':    vals[4],
            'HEARTBEAT':   vals[5],
            'SHADOW':      vals[6],
            'ADC_RAW':     vals[7],
        }

    # ── LOGR ───────────────────────────────────────────────────
    def log_dump(self) -> list[dict] | None:
        """拉取 LOG_RING 当前条目 (简化版: 通过 OBSERVE 读 LOG_DRAM 变量)。"""
        addrs = [
            DTCM_BASE + 0xD000 + 0x2000,  # LOG_COUNT
            DTCM_BASE + 0xD000 + 0x000,   # 最近 entry0 的 SAMPLES
            DTCM_BASE + 0xD000 + 0x010,   # N_ROUTES
            DTCM_BASE + 0xD000 + 0x018,   # ACT[32]
            DTCM_BASE + 0xD000 + 0x01C,   # ACT[63]
            DTCM_BASE + 0xD000 + 0x008,   # SHADOW
            DTCM_BASE + 0xD000 + 0x00C,   # ODR
        ]
        vals = self.observe(addrs)
        return {
            'log_count':    vals[0],
            'SAMPLES':      vals[1],
            'N_ROUTES':     vals[2],
            'act_32':       vals[3],
            'act_63':       vals[4],
            'SHADOW':       vals[5],
            'ODR':          vals[6],
        }

    # ── SCOPE 模式 ─────────────────────────────────────────────
    def scope_arm(self, pin: int = 3) -> bool:
        """发 scope_arm 命令:
           让 MCU 在 ISR 入口翻转 GPIOE[pin](诊断标记)。
           MCU 暂未实现 scope_arm CMD, 这里用 WRITE 一个触发变量
           来提示后续配套改动。"""
        print(f'[scope_arm] 目标: PE{pin} 翻折作 scope 触发参考示波器。')
        print('[scope_arm] 提示: MCU 端需在 CMD 0x33 (SCOPE_ARM) 中实现。')
        print('[scope_arm] 目前请直接用 PE14 (OC4REF = DMA 触发源) 作为参考信号。')
        return True


# ══════════════════════════════════════════════════════════════
# 别名解析
# ══════════════════════════════════════════════════════════════
def resolve_addr(s: str) -> int:
    """把 'wire[2]', 'act[32]', '0x0308' 解析为字节偏移。"""
    s = s.strip()
    if s.lower().startswith('wire'):
        n = int(s[4:].strip('[] '))
        return WIRE_BASE_OFF + n * 4
    if s.lower().startswith('act'):
        n = int(s[3:].strip('[] '))
        return ACT_BASE_OFF + n * 4
    return int(s, 0)


def nice(x):
    """人类可读的 float 展示。"""
    if x is None:
        return 'None'
    if isinstance(x, float):
        return f'{x:.6g}'
    return str(x)


# ══════════════════════════════════════════════════════════════
# 子命令
# ══════════════════════════════════════════════════════════════
def cmd_read(args, mon: DCLMonitor):
    addr = resolve_addr(args[0])
    v = mon.read_one(addr)
    if v is None:
        print('Error: 无响应或帧错误。')
        return
    f = struct.unpack('<f', struct.pack('<I', int(v) & 0xFFFFFFFF))[0] \
        if isinstance(v, int) else v
    print(f'[read] 0x{addr:08X} → {nice(f)} (raw 0x{int(v) & 0xFFFFFFFF:08X})')


def cmd_watch(args, mon: DCLMonitor):
    addrs = [resolve_addr(a) for a in args]
    labels = [f'0x{a:08X}' for a in addrs]
    print(f'[watch] 监视 {len(args)} 个地址, 每 100 ms 刷新。Ctrl-C 退出。')
    print('  '.join(f'{l:>24s}' for l in labels))
    try:
        while True:
            vals = mon.observe(addrs)
            print('  '.join(f'{nice(v):>24s}' for v in vals), end='\r',
                  flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        print('\n[watch] 退出。')


def cmd_record(args, mon: DCLMonitor):
    if '--out' in args:
        out_idx = args.index('--out')
        out_path = args[out_idx + 1]
        addr_args = args[:out_idx] + args[out_idx + 2:]
    else:
        out_path = 'monitor_{}.csv'.format(
            datetime.now().strftime('%Y%m%d_%H%M%S'))
        addr_args = args
    addrs = [resolve_addr(a) for a in addr_args]
    print(f'[record] 写到 {out_path}, 每 100 ms 一行。Ctrl-C 退出。')
    with open(out_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['time'] +
                       [f'0x{a:08X}' for a in addrs])
        try:
            while True:
                t = time.strftime('%H:%M:%S')
                vals = mon.observe(addrs)
                writer.writerow([t] + [nice(v) for v in vals])
                time.sleep(0.1)
        except KeyboardInterrupt:
            print(f'\n[record] 已保存: {out_path}')


def cmd_status(args, mon: DCLMonitor):
    snap = mon.snapshot()
    if snap is None:
        print('Error: SNAPSHOT 失败。')
        return
    print('[status] 引擎快照:')
    for k, v in snap.items():
        print(f'  {k:>14s}: {nice(v)}')


def cmd_log(args, mon: DCLMonitor):
    d = mon.log_dump()
    if d is None:
        print('Error: LOGR 失败。')
        return
    print('[log] 日志环当前条目:')
    for k, v in d.items():
        print(f'  {k:>14s}: {nice(v)}')


def cmd_scope(args, mon: DCLMonitor):
    pin = 3
    if '--pin' in args:
        pin = int(args[args.index('--pin') + 1])
    mon.scope_arm(pin)


# ══════════════════════════════════════════════════════════════
# REPL
# ══════════════════════════════════════════════════════════════
def cmd_repl(args, mon: DCLMonitor):
    print('[repl] DCL 监视器 REPL。输入 help 看帮助, quit 退出。')
    while True:
        try:
            line = input('dcl-monitor> ').strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break
        if not line:
            continue
        parts = line.split()
        head = parts[0].lower()
        try:
            if head in ('quit', 'exit'):
                break
            elif head == 'help':
                print('read <addr>  | watch <a> <b>...  | record <a> <b>... --out f.csv')
                print('status       | log            | scope_arm --pin N | repl')
                print(f'当前端口: {mon.port} @ {mon.baud} baud')
            elif head == 'read':
                cmd_read(parts[1:], mon)
            elif head == 'watch':
                cmd_watch(parts[1:], mon)
            elif head == 'record':
                cmd_record(parts[1:], mon)
            elif head == 'status':
                cmd_status(parts[1:], mon)
            elif head == 'log':
                cmd_log(parts[1:], mon)
            elif head in ('scope_arm', 'scope'):
                cmd_scope(parts[1:], mon)
            else:
                print(f'未知命令: {head}')
        except Exception as e:
            print(f'Error: {e}')


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════
SUB_CMDS = {
    'read':   cmd_read,
    'watch':  cmd_watch,
    'record': cmd_record,
    'status': cmd_status,
    'log':    cmd_log,
    'scope_arm': cmd_scope,
    'repl':   cmd_repl,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in SUB_CMDS:
        print(__doc__)
        sys.exit(1)
    sub = sys.argv[1]
    extra = sys.argv[2:]
    with DCLMonitor() as mon:
        SUB_CMDS[sub](extra, mon)


if __name__ == '__main__':
    main()
