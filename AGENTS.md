# AGENTS.md — DCL Deterministic Controller

Bare-metal control engine on STM32H723ZG. A 100μs timer ISR scans a **route table** (not a program) of 35 primitive ops over DTCM register spaces. No OS, no cache, no dynamic branching.

---

## 架构概览：系统引擎 vs 运行程序

| 层级 | 内容 | 位置 | 谁改 |
|------|------|------|------|
| **系统引擎** | ISR、UART部署协议、RTT、内存布局 | Flash + DTCM 引擎区 (0x0000~0x16FF) | 固件工程师 |
| **运行程序** | 用户编写的 DCL 控制逻辑 (路由表) | DTCM 程序区 (0x1700~0x87FF) | IDE 部署 |

空机器只部署系统引擎一次，之后通过 IDE 反复下载/覆盖运行程序。

---

## Build & Flash

Build compiles exactly two files: `main.c` + `startup_stm32h723zgtx.s`.

```bash
cd firmware/h723-core0
build.bat
```

Flash + reset:
```bash
py -3 -m pyocd flash -t stm32h723xx firmware/h723-core0/build/core0_h723.elf && py -3 -m pyocd reset -t stm32h723xx
```

Or: `cd firmware/h723-core0 && flash.bat`

---

## Critical Gotchas

- **IWDG fires automatically** (~520ms timeout, hardware watchdog). `main()` first line must write `IWDG_KR = 0xAAAA`. Feed again in `while(1)`. Do not modify PR/RLR.
- **pyOCD target must be `stm32h723xx`**, not the default `cortex_m`. Wrong target returns 0xFF/0x00 on H7 peripheral registers. On Windows, use `py -3 -m pyocd` (bare `pyocd` not in PATH).
- **`pyocd halt` disturbs peripherals** — reading GPIOE_ODR after halt is unreliable. Use the DTCM log ring buffer at `0x2000D000` (128 entries) for ground-truth state.
- **TIM1 registers are 16-bit** — must use `*(volatile uint16_t *)`, not 32-bit writes.
- **H723 ≠ H743**: `RCC_AHB1ENR` is at offset 0xD8 (not 0xD0). GPIO is on AHB4 (no bit-band).
- **ADC destination**: DMA writes to `ADC_RAW` (DTCM+0x0060), ISR converts `ADC_RAW → SENSOR_MAP[0]`.
- **Halt-state reads lie**: after `pyocd halt`, ODR and DMA registers may read as 0. Trust the ring buffer.

---

## DTCM Register Map (base 0x20000000, 128KB zero-wait)

### 系统引擎区 (0x20000000 ~ 0x200016FF)

| Offset | 名称 | 大小 | 用途 |
|--------|------|------|------|
| 0x0000 | TIMING | 64B | SAMPLES, PERIOD_MIN/MAX, EXEC_MIN/MAX, FLAGS 等 |
| 0x0040 | N_ENGINE | 16B | N_ROUTES, N_PARAMS, N_STATES, PROGRAM_MAGIC |
| 0x0060 | ADC_RAW | 64B | ADC DMA 目标 (避开 N_ENGINE) |
| 0x00E0 | SHADOW_GPIO | 4B | GPIO 输出影子 |
| 0x0100 | SENSOR_MAP | 256B | 64×float32 传感器值 |
| 0x0200 | ACTUATOR_STATUS | 256B | 64×float32 执行器值 |
| 0x0300 | WIRE_MAP | 4KB | 1024×float32 内部信号线 |
| 0x1300 | LUT_DATA | 1KB | 256×float32 引擎内置查找表 |

### 运行程序区 (0x20001700 ~ 0x200087FF)

| Offset | 名称 | 大小 | 用途 |
|--------|------|------|------|
| 0x1700 | PROG_HEADER | 16B | magic "PR0G", version, counts |
| 0x1710 | ROUTE_TABLE | 16KB | 1024×RouteEntry_t (部署对象) |
| 0x5710 | PARAM_TABLE | 8KB | 512×ParamEntry_t |
| 0x7710 | STATE_TABLE | 4KB | 256×StateEntry_t |

### 诊断区 (0x20008800 ~ 0x2000DFFF)

| Offset | 名称 | 用途 |
|--------|------|------|
| 0x8800 | RTT Block | SEGGER RTT 控制块 + 缓冲 |
| 0xD000 | LOG_RING | 128 条目环形日志 |
| 0xD800 | ALARM_BUF | 告警环形缓冲 |
| 0xE000 | REC_BUF | 抖动测量缓冲 |

---

## RouteTable Entry (RouteEntry_t, 16 bytes packed)

```c
typedef struct __attribute__((packed, aligned(4))) {
    uint8_t src_type;     // 0=SENSOR, 1=WIRE, 2=CONST
    uint8_t src_index;
    uint8_t dst_type;     // 3=WIRE
    uint8_t dst_channel;
    uint8_t op;           // opcode 0x00–0x23 (0x0D reserved)
    uint8_t flags;        // bit0=enabled
    uint16_t param_idx;   // PARAM_TABLE index (LE)
    uint16_t state_offset;// STATE_TABLE index (LE)
    uint16_t actuator_idx;// 0=wire-only, 1-4=PWM, 32-63=GPIOE
    uint16_t wire2_idx;   // secondary wire for multi-input ops
} RouteEntry_t;
```

## Actuator Index Mapping

- `0` = wire-only (no output)
- `1–4` = TIM1_CH1–CH4 (PWM output via `TIM1_CCR1–CCR4`)
- `5–31` = reserved
- `32–63` = GPIOE[0]–GPIOE[31] (digital, threshold at 0.5)

> ⚠️ DMA-GPIO link requires a running core. pyocd SWD connection freezes TIM1 → DMA stops → GPIO halts. This is normal debugger behavior, not an engine fault. Use RTT or PE2 heartbeat for monitoring.

---

## DCL Compiler & Deploy

### 编译

```bash
cd ide/compiler
python dcl_compiler.py program.dcl -o program.bin
python dcl_compiler.py program.dcl --json
python dcl_compiler.py program.dcl --c
```

输出格式 v2.0:
```
[ProgramHeader: 16B] [RouteTable: n*16B] [ParamTable: n*16B] [StateTable: n*16B]
Header: magic(4) + version(4) + n_routes(2) + n_params(2) + n_states(2) + reserved(2)
```

### IDE 部署 (CLI / GUI)

```bash
cd ide
python shell/main.py                # GUI
python shell/main.py --cli          # 终端 CLI
python shell/main.py --cli --new    # 新建实例
python shell/main.py --cli --monitor # 仅监控
```

CLI 编译 + 部署 + 启动:
```
:e program.dcl       # 加载文件
:c                   # 编译
:d                   # 部署 (SWD 直写)
:start               # 启动引擎
:m                   # 监控 RTT
```

### UART 部署 (备用)

当 PC 通过 USB-TTL 连接 STM32 USART2 (PD5/PD6, 115200bps):
```
DEPLOY 命令: [0xC0][0x10][len:2B:LE][payload][CRC16:2B:LE]
固件 handle_deploy() 接收后写入程序区 + 启动引擎
```

---

## Monitoring (Non-Intrusive)

1. **RTT (首选)**: SEGGER RTT control block @ DTCM+0x8800, 每 100ms 上报
   ```
   py -3 -m pyocd rtt -t stm32h723xx -a 0x20008000 -s 0x1000
   ```
   格式: `S=183000 P=23978..24022 R=40 E=1` (S=SAMPLES, P=PERIOD, R=routes, E=engine)

2. **PE2 heartbeat**: ISR 翻转 GPIOE bit2 — 接 LED 或逻辑分析仪

3. **IDE CLI**: `:m` 命令实时显示 RTT 状态

---

## IDE WebSocket 协议

端口 8765, 路径 `/`。消息为 JSON, 请求带 `_id`, 响应带相同 `_id`。

| cmd | 说明 | 重要字段 |
|-----|------|---------|
| `compile` | 编译源码 | `source` → `binary`, `stats`, `symbol_table` |
| `deploy` | 部署二进制到硬件 | `binary` (base64) |
| `start/stop/reset` | 引擎控制 | — |
| `read_wires` | 读 WIRE 值 | `start`, `count` → `values[]` |
| `write_wire` | 强制 WIRE 值 | `idx`, `value` |
| `get_source/set_source` | 源码同步 | `source` |
| `get_symbol_table` | 获取符号表 | — |

推送消息 (server → client, 无 `_id`):
- `monitor_status`: RTT 引擎状态
- `compile_result`: 编译完成通知
- `source_changed`: 源码变更广播

---

## 文件结构

```
firmware/h723-core0/
├── Inc/memory_map.h          ← 系统引擎区 + 运行程序区地址定义
├── Src/main.c                ← 固件主程序 (~1900行, 引擎 ISR + 部署协议)
├── Inc/registers.h           ← 外设寄存器定义

ide/
├── compiler/dcl_compiler.py  ← DCL 编译器 (IEC 61131-3 语法 → H723 binary)
├── compiler/memory_formats.py ← binary 格式参考 (如有)
├── server/
│   ├── ide_server.py         ← WS 服务器 + SWD 部署
│   ├── monitor_engine.py     ← RTT 非侵入监控线程
│   ├── usb_server.py         ← UART 串口通信 (备用)
│   └── compiler_wrapper.py   ← 编译器封装 (结构化结果)
├── web/                      ← GUI (HTML/JS + Monaco Editor)
├── shell/
│   ├── main.py               ← 入口 (GUI/CLI 模式切换)
│   └── cli.py                ← CLI 客户端 (~490行, 集成 REPL)
├── requirements.txt          ← 依赖 (aiohttp, websockets, pywebview)
└── compiler/reactor_control.dcl ← 示例程序

docs/MEMORY-MAP.md            ← 详细内存布局文档
```

---

## Outdated Files

`README.md` 引用 `make`, EtherCAT, Modbus RTU, `npm start` — 都不存在。以此文件为准。
