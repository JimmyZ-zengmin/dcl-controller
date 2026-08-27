# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**DCL确定性控制主机** ("核心0") — a software-defined hardware logic chip on STM32H723ZG. A bare-metal 100μs ISR runs a deterministic control loop executing a route table of 35 primitive operations (PID, LPF, CMP, TIMER, COUNTER, etc.) over register spaces in zero-wait DTCM. Originally prototyped on ESP32-S3, now migrated to STM32H723.

**核心设计哲学：时间切片 + 空间换时间。** 硬件定时器产生确定的周期锚点 → ISR 内顺序扫描路由表 → 每条路由在 DTCM 零等待内完成读-算-写 → 计算结果直接写 GPIO/PWM 输出寄存器。无 OS、无缓存不确定度、无动态分支。

## Build & Flash

### 直接编译 (arm-none-eabi-gcc, 推荐)

```bash
cd firmware/h723-core0

ST="/c/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924/tools/bin"
GCC="$ST/arm-none-eabi-gcc.exe"

MCU="-mcpu=cortex-m7 -mfpu=fpv5-d16 -mfloat-abi=hard -mthumb"
CFLAGS="$MCU -std=gnu11 -g3 -O2 -ffunction-sections -fdata-sections -Wall --specs=nano.specs -DSTM32 -DSTM32H723ZGTx -DDEBUG -IInc"

# 编译
"$GCC" $MCU -x assembler-with-cpp -c Startup/startup_stm32h723zgtx.s -o build/Startup/startup.o
"$GCC" $CFLAGS -c Src/main.c -o build/Src/main.c

# 链接
"$GCC" $MCU -T STM32H723ZGTX_FLASH.ld --specs=nano.specs -Wl,--gc-sections \
  build/Src/main.o build/Startup/startup.o -o build/core0_h723.elf
```

### STM32CubeIDE
Import `firmware/h723-core0` and build. 这是最可靠的IDE方法。

### Flash via pyOCD
```bash
# 注意: 必须使用 stm32h723xx target, 不能用 default cortex_m
pyocd flash -t stm32h723xx firmware/h723-core0/build/core0_h723.elf

# Live register monitor (no halt)
python tools/debug/monitor.py
# Default CMSIS-DAP serial: 000000805059ed5520a4400013dd0702a5a5a5a59796990e
```

### DCL Compiler
```bash
cd ide/compiler
python dcl_compiler.py program.dcl -o program.bin    # compile to binary
python dcl_compiler.py program.dcl --json             # JSON debug dump
python dcl_compiler.py program.dcl --c                # generate C source
```

### UART Test / Jitter Analysis
```bash
python firmware/tests/test_uart.py          # UART protocol test (default COM11, 115200bps)
python tools/analysis/analyze_jitter.py     # parse DTCM timing vars from pyocd read32 output
```

### 验证脚本 (test/deploy)
```bash
# GPIO输出通道验证: 烧录 → 等待 → halt → dump 历史日志环
py tools/flash/verify_log.py

# DMA最小验证 (手动配置 Stream5 隔离硬件问题 — 已废弃, 保留参考)
py tools/flash/test_dma_manual.py
py tools/flash/verify_dma_minimal.py
```

## Architecture

### Core Concept

The MCU does **not** execute programs — it executes a **route table**. Each route entry (16 bytes) reads a source (SENSOR/WIRE/CONST), applies a primitive operation, and writes to a destination (WIRE, and optionally ACTUATOR_STATUS). The ISR iterates the route table sequentially every 100μs. A DCL compiler on the PC converts declarative source code into this route table binary, delivered via UART.

### GPIO Output: DMA (SHADOW → GPIOE_ODR)

**最终方案: GPIO 输出使用 DMA Stream5 搬运。** 选择理由:
- 输出翻转时刻严格锁定在 TIM1_CC4 match (97.5μs), 与 CPU 计算时刻无关 → 周期末尾确定性触发
- CPU 仅需 1 周期写 SHADOW, 后续由 DMA 硬件自动搬运, 零 CPU 开销
- 链路: CPU 写 SHADOW → TIM1_CC4 硬件触发 → DMAMUX → DMA Stream5 → GPIOE_ODR (4 周期, 零抖动)

当前路径:
```
算路结束 → ACT[32+i] > 0.5 → CPU 写 SHADOW @ DTCM+0x00E0
TIM1_CC4 match @ 97.5μs → DMAMUX → DMA Stream5 → GPIOE_ODR (硬件自动)
```

**注意: 此路径依赖内核不被 halt。** pyocd halt 会冻结 TIM1 → 无 CC4 事件 → DMA 不搬 → GPIO 停摆。需用非侵入方式监测 (见下方)。

### ADC Input: DMA (still used)

**ADC 采集是唯一保留的 DMA 用法:**
- **DMA2 Stream 1** (原名 Stream0, 后改为 Stream1): ADC1_DR → DTCM ADC_RAW @ 0x200000F0
  - TIM1_TRGO 硬件触发, CPU 零参与
  - ISR 仅读 ADC_RAW → 换算 → SENSOR_MAP[0]
- SHADOW → GPIOE_ODR 的 DMA Stream5 已废弃删除

### Firmware Is Monolithic — Not Modular

Despite the `Src/adc/`, `Src/uart/`, `Src/clock/`, etc. subdirectories, the production firmware lives entirely in a single file: **`firmware/h723-core0/Src/main.c`** (~1950 lines, 持续在增长). All register defines, clock init, UART protocol, CANopen, ADC/DMA setup, the route engine, and every primitive are inlined there. The subdirectories contain header stubs they'll never include (main.c does `#include <stdint.h>` only) and alternative C source files that are **not compiled by build.sh**. `build.sh` compiles exactly two files: `main.c` and `startup_stm32h723zgtx.s`. To modify firmware behavior, edit main.c — do not waste time wiring in the modular headers; paste changes directly into main.c.

### Six Register Spaces (DTCM @ 0x20000000, 128KB zero-wait)

| Register | DTCM Offset | Capacity | Description |
|----------|-------------|----------|-------------|
| SENSOR_MAP | 0x0100 | 64×float32 | ADC inputs (written by DMA→ADC_RAW, ISR converts→SENSOR_MAP[0]) |
| ACTUATOR_STATUS | 0x0200 | 32×float32 | 执行器数值区 — 路由计算结果 + 阈值判断 → GPIO/PWM |
| WIRE_MAP | 0x0300 | 1024×float32 | Internal signal bus — the key dataflow mechanism |
| LUT_DATA | 0x1300 | 256×float32 | Lookup table storage |
| ROUTE_TABLE | 0x1700 | 1024×16B | Route entries (compiled program) |
| PARAM_TABLE | 0x5700 | 512×16B | Per-route parameters (Kp/Ki/Kd/SP/etc.) |
| STATE_TABLE | 0x7700 | 256×16B | Stateful primitive state (integral, last value, etc.) |

### RouteEntry_t (16 bytes, packed)
```
[0]  src_type      — SRC_SENSOR(0), SRC_WIRE(1), SRC_CONST(2)
[1]  src_index     — index into source space
[2]  dst_type      — DST_WIRE(3)
[3]  dst_channel   — WIRE_MAP index for output
[4]  op            — primitive opcode
[5]  flags         — bit0=enabled
[6-7] param_idx    — index into PARAM_TABLE (uint16 LE)
[8-9] state_offset — index into STATE_TABLE (uint16 LE)
[10-11] actuator_idx — 路由内执行器索引; >0 表示该路由输出有效, 见下
[12-13] wire2_idx  — secondary wire (COUNTER reset/load input)
[14-15] reserved
```

### Actuator Index Mapping

```text
actuator_idx:  0       = WIRE-only (无输出)
               1~4     = TIM1_CH1~CH4 (PWM输出)
               5~31    保留 (未来DAC等)
               32~63   = GPIOE[0]~GPIOE[31] (数字输出)
```

ISR 对 actuator_idx 的处理:
- `0 < ai < MAX_ACTUATORS` → `ACTUATOR_STATUS[ai] = float result`
- ISR 末尾扫描 ACT[32]~ACT[63]: `if (ACT[i] > 0.5f) gpio_bits |= 1<<(i-32)`
- 写 `SHADOW @ DTCM+0x00E0` → TIM1_CC4 硬件触发 DMA Stream5 → GPIOE_ODR
- **整个搬运依赖内核运行, pyocd halt 会冻结此链路**

### 35 Primitives (opcodes 0x00–0x23, 0x0D reserved)

**Stateless (23):** DIRECT, CMP, CLAMP, MUX, LUT, SCALE, AND, OR, NOT, ADD, SUB, MUL, DIV, BITAND, BITOR, BITXOR, BITNOT, LIMIT, MAX, MIN, ABS, EQ, NE

**Stateful (12, allocate STATE_TABLE slots):** LPF, PID, RATE, EDGE, CNT, TIMER, COUNTER, DEADBAND, HYST, REG, SR, RS

All multi-input primitives read their second operand from `WIRE_MAP[(int)p->value_a]`.

### ISR Execution Flow (TIM1_UP_IRQHandler @ line ~1227)

1. **入口诊断**: XOR GPIOE_ODR bit2 (逻辑分析仪测 ISR 心跳)
2. **读ADC**: 读 ADC_RAW @ 0x00F0 → 换算 → SENSOR_MAP[0] (DMA 提前搬)
3. **DMA验证**: 读 DMA2_S1M0AR 确认 ADC-DMA 配置未被破坏; 否则直接读 ADC1_DR 兜底
4. **Period计算**: DWT_CYCCNT → 跟踪 MIN/MAX 抖动
5. **路由扫描**: for each enabled route → read src → execute_primitive → write WIRE_MAP[dst]; if ai > 0, also write ACTUATOR_STATUS[ai]
6. **PWM输出**: 读 ACT[1..3] → 算 CCR1~CCR3 → 写 TIM1_CCR1~CCR3
7. **GPIO输出**: 读 ACT[32..63] → 阈值判断 → 组合 gpio_bits → 写 SHADOW @ DTCM+0x00E0
8. **测量**: 执行时间, SAMPLES++
9. **日志**: 每 100 周期记入环形缓冲 (DTCM+0xD000, 128 条历史)

> GPIO 实际物理输出由 TIM1_CC4 match 触发 DMA Stream5 搬运 SHADOW → GPIOE_ODR,
> 翻转时刻锁定在周期末尾 ~97.5μs.

### UART Frame Protocol

Direction | Sync | Cmd/Status | Length | Payload | CRC16
----------|------|-----------|--------|---------|------
PC→MCU | 0xC0 | CMD (1B) | LE u16 | variable | LE u16 (CCITT, poly=0x1021)
MCU→PC | 0xC1 | STS (1B) | LE u16 | variable | LE u16

Commands: DEPLOY(0x10) [routes+params binary], START(0x11), STOP(0x12), RESET(0x13), READ(0x20), WRITE(0x21)

### CANopen (FDCAN1, 500kbps, PD0=RX/PD1=TX)

Implements: NMT state machine, Heartbeat producer (1s), SDO read (expedited, object dictionary), standard COB IDs with NODE_ID=1.

### IWDG 独立看门狗 (重要!)

- H723 value line IWDG 默认 option byte = hardware watchdog
- **上电后自动启动, 软件无法禁用**, LSI=32kHz, 默认 PR=0, RLR=0xFFF → ~512ms 超时
- 必须在 main()`**第一行**就立刻 feed (`IWDG_KR = 0xAAAA`), 否则初始化未半即 reset
- **`while(1)` 中也要持续 feed**, 避免主循环阻塞时 reset
- 不要修改 PR/RLR (PVU/RVU 等待期间 counter 可能跑完)

### Clock Configuration

- VOS0 + PLL 544MHz VCO (HSI/4 × 34), HSI→PLL switch via intermediate 288MHz step
- TIM1 = 136MHz (544/2/4×2), DWT_CYCCNT @ 136MHz
- Flash wait states: FLASH_ACR = 0x324
- ITCM: 64KB @ 0x00000000 (zero-wait fetch for ISR; code marked `.itcm_code`)
- DTCM: 128KB @ 0x20000000 (zero-wait for all register spaces)
- AXI SRAM: 320KB @ 0x24000000

### Two Firmware Variants

**h723-core0/** — Production firmware (interpreter ISR, 100μs). Everything in main.c. Built with build.sh or STM32CubeIDE. Full feature set: UART protocol, CANopen, ADC via DMA, PWM output, GPIO output, DCL-compiled test programs.

**h723-core0-1us/** — Research firmware for 1μs cycle (separate flat 4-file project: main.c + 2 linker scripts + startup). Experimental "compiled ISR" that translates the route table into Thumb-2 machine code in ITCM at runtime. Proof-of-concept only — uses 8 routes (2 PID chains); prim_handler is stubbed. HardFault handler saves stacked registers to DTCM+0x040 for post-mortem via pyocd.

## Key Design Rules

- **All signals are float32 on a unified WIRE bus** — no types, no casting in the runtime
- **WIRE_MAP is single-cycle (DTCM zero-wait)**, updated sequentially in route table order
- **The DCL compiler performs topological sort** (Kahn's algorithm, `topological_sort()`) so producers run before consumers
- **PID derivative**: `Kd × (err_curr − err_prev)` — the sign matters; reversed sign amplifies oscillation
- **CANopen_poll() and uart_poll() run in the main loop (not ISR)** — they must not block the ISR
- **ITCM code coherency**: DSB + ICIALLU invalidate + DSB + ISB sequence required after writing code to ITCM
- **GPIO 输出走 DMA Stream5** (SHADOW → GPIOE_ODR, TIM1_CC4 触发) — 周期末尾确定性翻转
- **DMA 搬 ADC→DTCM 仍然保留** — 异步外设→内存是 DMA 真正有用的场景
- **DMA 链路依赖内核运行** — pyocd halt 会冻结整个 TIM1+DMAMUX+DMA 链路, GPIO 输出停摆

## Repository Structure

```
dcl-controller/
├── firmware/
│   ├── h723-core0/          # Production: single-file firmware (main.c)
│   │   ├── Src/main.c       # ← THE firmware (~1950 lines, everything here)
│   │   ├── Inc/*.h          # ← 未链接的头文件 (仅参考)
│   │   ├── Startup/         # 启动汇编
│   │   ├── *.ld             # 链接脚本 (FLASH/RAM)
│   │   └── build/           # 编译输出 (elf/map/o)
│   ├── h723-core0-1us/      # 1μs research firmware (separate project)
│   └── tests/               # Python UART + jitter test scripts
├── ide/
│   ├── compiler/            # DCL compiler (dcl_compiler.py, ~1094 lines)
│   ├── web/                 # Web IDE (React/CSS/JS)
│   └── extension/           # VS Code extension stub
├── tools/
│   ├── flash/               # build.sh, boot.py, diagnostic scripts
│   │   └── build.sh         # ← HERE 路径需改为当前项目
│   ├── debug/               # pyocd monitors, fault diagnostics
│   │   └── monitor.py       # live register dump
│   └── analysis/            # Jitter analysis (analyze_jitter.py)
└── docs/
    ├── DCL语言规范-v1.0.md   # DCL language specification
    ├── DCL-v2.0-测试提示词.md # Hardware verification test procedures
    ├── DCL闭环闭环自查报告.md # Self-audit: found 7 fatal + 5 severe issues
    ├── FUTURE-ROADMAP.md    # ← 未来升级与研究方向 (2026-07-13 起草)
    └── reference/            # Architecture docs, technical manuals
```

Note: `README.md` in the repo root is **outdated** (references `make`, EtherCAT, Modbus RTU, and `npm start` workflows that do not exist). Prefer this CLAUDE.md for accurate build and architecture information.

## Critical Gotchas

- **Monolithic firmware**: main.c contains everything; the `Src/*/` subdirs are not compiled by `build.sh`. Edit main.c directly.
- **IWDG 立刻 feed**: `main()` 第一行必须 `IWDG_KR = 0xAAAA`，否则芯片在初始化中途reset (~520ms 超时)。不要修改 PR/RLR (PVU/RVU 等待期间 counter 仍跑)。
- **Performance counters** live at DTCM+0x0000 (not in the route table). PERIOD_MIN/MAX in DWT cycles @136MHz. 13600 cycles = 100μs exact.
- **Route count** is stored at DTCM+0xF0 as a uint32 (not in a struct). The ISR reads it to bound its loop.
- **TIM1 registers are 16-bit** — must use `*(volatile uint16_t *)` writes, not 32-bit.
- **H723 value line ≠ H743**: RCC_AHB1ENR is at offset 0xD8 (not 0xD0). GPIO is on AHB4 (no bit-band, no 0x42xxxxxx addresses).
- **ADC destination**: DMA 写 `ADC_RAW` (DTCM+0x00F0), 不是直接写 SENSOR_MAP. ISR 读 ADC_RAW → SENSOR_MAP[0].
- **ADC requires calibration**: must run ADCAL after power-up, LDORDY must be waited for.
- **pyocd 必须用 stm32h723xx target**: 默认 `cortex_m` 无法正确访问 H7 所有外设寄存器, 会返回 0xFF/0x00. 脚本中应用 `target_override='stm32h723xx'` 参数.
- **pyocd halt 后读 GPIOE_ODR 不准**: halt 可能改变外设状态. 历史日志缓冲 (DTCM+0xD000) 更可靠.
- **pyocd 连接会冻结 DMA-GPIO 链路**: SWD 连接占用总线 → TIM1 停摆 → DMA 不搬 → GPIO 输出停止. 这是正常现象, 不是引擎故障.
- **ITCM store→load coherency**: Thumb-2 code written to ITCM via D-Bus store may not be visible to I-Bus fetch without full DSB+ICIALLU+DSB+ISB barrier sequence.
- **HardFault diagnosis**: h723-core0-1us has a HardFault handler that saves stacked registers to DTCM+0x040 area for post-mortem.

## pyocd 连接对 DMA-GPIO 的影响

pyocd 通过 SWD 连接芯片时会短暂 halt 内核, 甚至仅仅是连接(DMA 线路被占用)就会导致 TIM1 停摆。症状:

- SAMPLES 冻结不增长
- GPIO 输出停止
- 寄存器读回 0 或旧值

**非侵入式监测方案:**

1. **RTT (首选)**: 固件已实现 SEGGER RTT 控制块 (DTCM+0x8800), 每 100ms 上报一次引擎状态。用 `pyocd rtt` 读取, **完全不干扰引擎**
2. **DTCM 环形缓冲**: 已存在 (DTCM+0xD000, 128 条)。ISR 每 100 周期写入一条, halt 后内容保留
3. **LED/逻辑分析仪**: ISR 已翻转 PE2 (bit2), 接逻辑分析仪看心跳
4. **UART 主动上报**: 固件定期发状态帧, PC 串口助手监控 (不依赖 pyocd)

**RTT 使用方法:**
```bash
pyocd rtt -t stm32h723xx -a 0x20008000 -s 0x1000        # 实时查看
pyocd rtt -t stm32h723xx -a 0x20008000 -s 0x1000 -d log.txt  # 输出到文件
```

输出格式: `S=183000 P=23946..24062 R=49 E-1`
- S=SAMPLES, P=PERIOD_MIN..PERIOD_MAX, R=ROUTES, E=engine_running

**调试原则: 看现象用 RTT 或 PE2 心跳, 不要用 pyocd halt 去看 ISR 是否运行。**
