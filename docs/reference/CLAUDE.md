# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**核心0 (Core 0)** — a "software-defined hardware logic chip": a dual-core ESP32-S3 industrial controller. A bare-metal 100μs ISR runs the hard real-time control loop on **物理核心1** (branded "核心0" for user mental model), while FreeRTOS on **物理核心0** handles WiFi/MQTT/SPI sensor acquisition. The control core never stops and runs independently of the service core.

## System Architecture (v2.6, 2026-06-25)

核心0 是一个**控制器的运行时系统**——不是"ESP32固件项目"。其架构分为四大域：

### 数据域 — 6种寄存器空间

| 寄存器 | 符号 | 容量 | 权限 | 生命周期 |
|--------|------|------|------|---------|
| 输入映像区 | I-Map (SENSOR_MAP) | 64ch×4B | S-Eng写, X-Eng读 | 跨周期 |
| 输出映像区 | Q-Map (ACTUATOR_STATUS) | 32ch×4B | X-Eng写, S-Eng读 | 跨周期 |
| 工作寄存器 | W-Reg (WIRE_MAP) | 1024ch×4B | X-Eng读写 | 单周期(热切换清零) |
| 状态寄存器 | S-Reg (STATE_TABLE) | 256ch×16B | X-Eng私有 | 跨周期(热切换清零) |
| 参数寄存器 | P-Reg (PARAM_TABLE) | 512ch×16B | S-Eng写, X-Eng读 | 跨周期, NVS持久化 |
| 查找表存储 | LUT-Store (LUT_DATA) | 256ch×4B | S-Eng写, X-Eng读 | 随程序 |

### 执行域 — 扫描引擎 + 指令集

- **扫描引擎 (X-Eng)**: 物理核心1, 裸机ISR, 默认100μs周期
- **服务引擎 (S-Eng)**: 物理核心0, FreeRTOS, 传感器/通讯/部署
- **程序存储器 (Prog-Store)**: ROUTE_TABLE[1024], 16B/条指令
- **程序暂存区 (Prog-Staging)**: ROUTE_STAGING[1024], 热切换用
- **38条指令**: 数据移动/算术/比较/信号调理/控制/逻辑/位运算/时序/选择

### 安全域

- **ESTOP**: GPIO4硬件+软件双重急停, S2.5段优先执行
- **WDT**: ISR喂狗, 心跳监测
- **故障模式**: BLANK(安全模式)/RUNNING/SWITCHING/ESTOP

### 工具域

- **lf_compiler**: .lf DSL编译, 目标容量从 core0_config.json 读取
- **c_compiler**: C子集→路由表, If-Conversion控制流→数据流
- **core0_config.json**: **单一配置源** — 固件和编译器共享

## Configuration Single Source of Truth

`D:\core0\firmware\core0_config.json` 是容量/性能/安全配置的唯一来源。固件 shared_mem.h 和编译器 core0_config.py 都从此读取。修改容量时只改此文件。

当前目标容量: 1024 routes, 512 params, 256 states, 1024 wires, 64 sensors, 32 actuators, 256 LUT.

## Repository State

### v2.7 — STM32H723 移植 (2026-06-28)

**核心0 ISR 引擎成功移植到 STM32H723ZGT6。**
- ISR 引擎 (26 原语 + 路由表 + WIRE_MAP 数据流) 完整运行
- 192MHz VOS0, 100μs 周期, 3 分钟 2.43M 样本
- 周期抖动: 93.8ns p-p (比 ESP32-S3 优 4.9×), 96.7% 周期零抖动
- RCC 寄存器布局确认: H723 value line 与 H743 不同
- PLL 544MHz 待解决 (Flash AC 时序兼容性)
- 编译器 -O2 优化待开启 (当前 -O0)

### v2.6 — Safety system + Architecture model (2026-06-25)

### 固件安全机制
- **BLANK安全模式**: program_state=0时ISR跳过路由遍历, 零空转, 等待prog_load
- **NVS周期安全恢复**: 拒绝恢复<50μs的危险周期值, 清除NVS中的危险值
- **ISR周期动态安全校验**: 根据exec_max×130%计算安全下限, 拒绝过短周期
- **启动诊断**: 打印程序状态/寄存器使用/ISR周期

### 编译器对齐
- **ROUTE_FMT 修复**: `<BBBBBBHHHH`(14B) → `<BHBHBBHHHH`(16B), 匹配固件v2.3+的uint16_t src_index/dst_channel
- **MAX值对齐**: 从硬编码64→从core0_config.json读取1024/512
- **AND/OR B输入端修复**: c_compiler正确传递rhs为b_var
- **容量兼容性报告**: 编译后打印寄存器使用率+校验

### 内置程序
- **安全启动程序**: 替换521路由stress test → 29路由/30参数恒温控制(25°C PID)
- 5.9%寄存器利用率, 962字节compact格式

### 编译器共享模块
- `compiler/core0_config.py`: 统一OP_MAP, STATEFUL_OPS, CRC32, 容量校验
- lf_compiler和c_compiler都import此模块

### Strategic Vision (2026-06-18)

Beyond industrial control, Core 0 is positioning as the **deterministic physical substrate for embodied intelligence**:

1. **Deterministic Communication** — WiFi/MQTT delay from floating 50-150ms → fixed known constant (~5ms), enabling AI to predict when commands execute
2. **Direct ADC Sensing** — bypass digital sensor "black boxes", connect high-speed ADC directly to analog probes, sample timing locked to ISR cycle
3. **Cloud Brain + Local Brainstem** — GPT-4 class models in cloud, Core 0 deterministic execution locally, connected via fixed-delay channel
4. **Spatial Architecture for LLM Inference** — compile AI models into deterministic routing tables, making inference time predictable

See `核心0-具身智能确定性架构方案.md` for the full architecture.

### Firmware File Structure

```
D:\core0\firmware\
├── CMakeLists.txt              # Top-level: include($ENV{IDF_PATH}/tools/cmake/project.cmake)
├── sdkconfig / sdkconfig.defaults  # WiFi SSID/Password, MQTT Broker, 16MB Flash, Octal PSRAM
├── partitions.csv              # factory(2MB) + ota_0(2MB) + ota_1(2MB) + nvs_prog(1MB)
├── main/
│   ├── CMakeLists.txt          # SRCS: main.c dht22.c wifi_mqtt.c ota_handler.c
│   ├── Kconfig.projbuild       # menuconfig: WiFi & MQTT settings
│   ├── main.c                  # app_main(): shared mem → NVS → core1 → DHT22 → WiFi+MQTT
│   ├── dht22.c/h               # DHT22 digital sensor (GPIO11, 2100ms)
│   ├── wifi_mqtt.c/h           # WiFi STA + MQTT + 10 JSON commands + OTA task spawn
│   ├── ota_handler.c/h         # HTTP download → esp_ota_* API → set boot partition
│   └── nvs_store.h             # NVS 4-slot program storage (static inline, no .c file)
├── components/
│   ├── core0/
│   │   ├── shared_mem.h        # Shared memory structs + _Static_assert + offset macros
│   │   └── core1_isr.c         # 100μs bare-metal ISR (20 primitives + WIRE_MAP dataflow)
│   └── cjson/                  # JSON parser (idf-extra-components)
└── build/
    ├── core0_controller.bin    # Compiled firmware (~934KB)
    └── flasher_args.json       # esptool flash parameters
```

Key documents:
- `修正汇总.md` — **authoritative corrections (18 items)** — supersedes conflicting parts of older docs. Read this first.
- `核心0-C编程指南.md` — **C programming guide** (execution model, syntax, patterns, limitations) — read before writing control programs
- `开发执行计划.md` — **granular execution plan** (21 steps, all complete). Use this for day-to-day work.
- `开发验证报告.md` — current implementation status, test results, OTA verification data
- `核心0技术白皮书.md` — **public-facing technical white paper** (performance, architecture, competitive comparison)
- `核心0-具身智能确定性架构方案.md` — **strategic vision**: cloud-brain + local-brainstem + deterministic communication + direct ADC sensing
- `未来应用方向与技术路线图.md` — near/mid/long-term roadmap, embodied intelligence direction
- `确定性硬件架构需求分析.md` — root cause analysis of ESP32-S3 jitter, TCM chip recommendations
- `核心0路由器-确定性WiFi架构白皮书.md` — deterministic WiFi architecture for embodied intelligence
- `核心0-确定性通用计算方案.md` — **hardware platform strategy**: first-principles hardware selection, DRAM vs SRAM/TCM analysis, ESP32-S3 → STM32H745 migration plan
- `技术架构.md` — original architecture (pre-correction, for reference)
- `决策框架.md` — all confirmed decisions (still valid)
- `实现方案.md` — original implementation spec (contains PID sign bug, wrong memory sizes, wrong sensor)
- `开发规划.md` — original 4-phase roadmap (superseded by 开发执行计划.md for sequencing)

## H723 Migration Status (2026-06-28)

**STM32H723ZGT6 核心0 ISR 引擎已完整移植并验证。**

- **项目位置**: `D:\STM\work\core0_h723\`
- **IDE**: STM32CubeIDE 1.5.1, arm-none-eabi-gcc, **-O0** (未优化)
- **调试器**: CMSIS-DAP (Luxiaoban Flash Pro), pyOCD flash/commander
- **时钟**: PLL 192MHz VOS0, 全 /1 分频, Flash 2WS
- **ISR**: 100μs TIM1, DWT_CYCCNT 测量 @192MHz (5.2ns 分辨率)
- **内存**: DTCM 128KB 零等待, 全部寄存器空间 (34KB) 位于 DTCM

### 已验证结果

| 指标 | ESP32-S3 (原) | STM32H723 (当前) | 改善 |
|------|--------------|-------------------|------|
| ISR 周期 | 100μs | 100μs | — |
| 周期抖动 p-p | 462ns | **93.8ns** | **4.9×** |
| 零抖动命中 | 0% | **96.7%** | 质的飞跃 |
| 20 路由执行 | ~20μs | 27.4μs @**-O0** | -O2 预计 5-7μs |

### 待解决

- **544MHz PLL**: VOS0 + 544MHz 切时钟崩毁, 192MHz 稳定. 根因疑似 Flash AC 时序.
  已验证: RCC_D1CFGR (0x18), RCC_D2CFGR (0x1C), VOS0, DSB/ISB 均安全.
- **-O2 优化**: 预计 4-5× 性能提升, 为 1μs 周期铺路
- **ITCM 迁移**: ISR 从 Flash 迁 ITCM (零等待取指)

### 关键文档

- `D:\STM\work\core0_h723\H723上机测试总结.md` — 完整调试记录, RCC 寄存器, 命令速查
- `D:\STM\work\core0_h723\H723性能测试报告.md` — 抖动直方图, ESP32 对比
- `D:\STM\work\core0_h723\Src\main.c` — 当前固件 (26 原语, 20 路由测试程序)

---

## Build System & Toolchain (ESP32-S3, 原平台)

- **Chip**: ESP32-S3 N16R8 (Xtensa LX7 dual-core, 240MHz, 16MB Flash, 8MB Octal PSRAM)
- **IDE**: VS Code + ESP-IDF plugin
- **Toolchain**: ESP-IDF v6.0.1 @ `C:\esp\v6.0.1\esp-idf`, GCC 15.2.0, `idf.py build/flash/monitor`
- **Python venv**: `C:\Espressif\tools\python\v6.0.1\venv`
- **FreeRTOS config**: `CONFIG_FREERTOS_NUMBER_OF_CORES=2` (SMP, but tick only on CPU0)
- **PSRAM**: Octal SPI 80MHz, `CONFIG_SPIRAM_MODE_OCT=y`, `CONFIG_SPIRAM_USE_MALLOC=y`
- **Control core (物理核心1/APP_CPU)**: Pure C, no FreeRTOS, ISR in IRAM, zero dynamic allocation
- **Service core (物理核心0/PRO_CPU)**: C + FreeRTOS + lwIP + DHT22 (temporary, replacing with MAX31865)
- **PC Compiler**: Python CLI (`lf-compile`), eventually an IDE

### Build (ESP-IDF 标准流程)

ESP-IDF v6.0.1 **拒绝在 MSYS2/Git Bash 下运行**。必须用 PowerShell 或 cmd.exe。正确环境变量见 `build_cmd.bat`。

```powershell
# === PowerShell (推荐) ===
# 桌面快捷方式: IDF_v6.0.1_Powershell.lnk (自动加载 ESP-IDF 环境)
# 或手动: 运行 build_cmd.bat
cd D:\core0\firmware
idf.py build

# === CMD ===
build_cmd.bat
```

### 从 MSYS2/Git Bash 增量编译 (ninja 直调)

**完整编译必须在原生 PowerShell/cmd.exe 中进行**（见上节）。以下 `env -i` + PowerShell 方案仅供参考，**已实测不可靠**（`MSYSTEM` 穿透 `env -i`、PowerShell `&` 操作符报 `CantActivateDocumentInPipeline`、`Start-Process` 静默失败返回 0 但不产生输出）。

仅当 CMake 缓存已存在（即之前成功跑过 `idf.py build`）时，可从 Git Bash 增量编译：

```bash
cd D:/core0/firmware/build && C:/Espressif/tools/ninja/1.12.1/ninja.exe
```

`ninja` 不检查 `MSYSTEM`，直接调用编译器。但无法处理 CMake 配置变更（如新增文件、修改 Kconfig）。

### Flash (COM7)

```powershell
# 方式1: 用 flash_args (idf.py build 自动生成)
cd D:\core0\firmware\build
python -m esptool --chip esp32s3 -p COM7 -b 460800 --before default-reset --after hard-reset write-flash "@flash_args"

# 方式2: 显式指定
python -m esptool --chip esp32s3 -p COM7 -b 460800 --before default-reset --after hard-reset write-flash --flash-mode dio --flash-size 16MB --flash-freq 80m 0x0 build\bootloader\bootloader.bin 0x8000 build\partition_table\partition-table.bin 0x19000 build\ota_data_initial.bin 0x20000 build\core0_controller.bin
```

### Monitor

```powershell
idf.py monitor -p COM7
```

### MQTT 测试 (Python + paho-mqtt)

ESP-IDF Python venv 已预装 `paho-mqtt 2.1.0`, 无需额外安装。

```python
# 快速状态查询
/c/Espressif/tools/python/v6.0.1/venv/Scripts/python.exe -c "
import paho.mqtt.client as mqtt, json, time
def on_msg(c, u, m): print(f'[{m.topic}] {m.payload.decode()[:150]}')
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.on_message = on_msg
c.connect('182.61.18.136', 1883, 60)
c.subscribe('core0/status'); c.subscribe('core0/resp')
c.loop_start(); time.sleep(1)
c.publish('core0/cmd', json.dumps({'cmd':'get_status'}))
time.sleep(3); c.loop_stop()
"
```

### .lf Compiler

```bash
python compiler/lf_compiler.py program.lf -o program.bin    # compile
python compiler/lf_compiler.py program.lf --json             # JSON debug dump
python compiler/lf_compiler.py program.lf --send <IP>        # send to ESP32 (TODO)
```

## Architecture: Dual-Core Design

### Startup Sequence
```
Power-on → ROM boot → 2nd stage bootloader → FreeRTOS init on 物理核心0
  ↓
app_main() on 物理核心0:
  1. nvs_flash_init()
  2. Load program from NVS → write ROUTE_TABLE, PARAM_TABLE to shared mem
  3. Verify program CRC32 → on failure, fall back to previous slot or blank (safe)
  4. Set SharedCtrl.magic = 0xC0C0C0C0  ← tells 物理核心1 "memory is ready"
  5. Start 物理核心1 → spins until magic valid → configures TIMG0 → starts ISR → WFI
  6. Continue on 物理核心0: WiFi init, MQTT init, SPI sensor task, main loop
```

**Critical**: 物理核心1 spin-waits for `magic == 0xC0C0C0C0` before reading any shared memory. This prevents reading uninitialized memory.

### 物理核心1 (branded "核心0") — The Control Core
- Bare-metal ISR at 100μs period (TIMG0)
- TCM-locked code (IRAM_ATTR), MPU-disabled caching on shared memory region
- Reads SENSOR_MAP (written by 物理核心0 via SPI), PARAM_TABLE (writable by 物理核心0)
- Executes route table → apply_output() → GPIO/LEDC/MCPWM
- Writes ACTUATOR_STATUS for 物理核心0 to report
- Checks reload_flag each cycle for program hot-switch
- Feeds hardware WDT each cycle

### 物理核心0 (branded "核心1") — The Service Core
- FreeRTOS tasks: MQTT publish (500ms), SPI sensor acquisition (100ms), command dispatch
- **Writes** SENSOR_MAP (PT100 via MAX31865 on SPI2, ~21ms conversion, 物理核心0独占SPI2)
- **Writes** PARAM_TABLE directly — next ISR cycle picks up new values
- **Reads** ACTUATOR_STATUS, SharedCtrl.heartbeat for MQTT reporting
- Receives programs → CRC32 verify (hardware CRC, ~300μs) → save NVS → write staging → set reload_flag
- Cannot: modify ROUTE_TABLE directly (must go through staging+reload), touch GPIO outputs, stop control core

### Shared Memory Layout (64KB, dynamically allocated)

The shared memory is allocated at runtime via `heap_caps_malloc(MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL, 64KB)`. The heap manager guarantees exclusive ownership — no `.data`/`.bss`/other `malloc` calls can overwrite it. This replaces the original hardcoded `0x3FCA0000` which was corrupted by ESP-IDF heap allocations.

The base pointer `g_shared_mem` (declared in `shared_mem.h`, defined in `main.c`) is set in `app_main()` before the ISR starts. It always lands in internal DRAM (0x3FC88000~0x3FD00000 range).

RouteEntry_t is 16 bytes × 64 = **1KB** (not 4KB as originally written).

```
Offset   Size   Name                物理核心1       物理核心0
0x0000   68B    SharedCtrl          RW (heartbeat)  R (magic, flags)
0x0044   ...    (reserved)
0x0100   1KB    ROUTE_TABLE[64]     R (active, ISR)  — (no access)
0x0500   1KB    ROUTE_STAGING[64]   R                W (program download)
0x0900   1KB    PARAM_TABLE[64]     R (ISR reads)    W (tuning writes)
0x0D00   1KB    PARAM_STAGING[64]   R                W (program download)
0x1100   1KB    STATE_TABLE[64]     RW (private)     — (no access)
0x1500   256B   SENSOR_MAP[64]      R (ISR reads)    W (SPI writes)
0x1600   128B   ACTUATOR_STATUS[32] W (ISR writes)   R (MQTT reports)
0x1680   1KB    LUT_DATA[256]       R (OP_LUT reads) — (loaded at program init)
0x1A80   256B   WIRE_MAP[64]        RW (ISR writes)  R (compile verified)
```

Key design rules:
- **Dynamic allocation**: `heap_caps_malloc(DMA | INTERNAL)` — heap-guaranteed exclusive, no fixed-address conflicts
- **Non-cacheable**: Internal DRAM (D-bus) on ESP32-S3 inherently bypasses D-Cache; no MPU config needed
- **Cross-core sync**: Xtensa `MEMW` barrier (not ROM cache APIs) ensures store-buffer drain between cores
- Float writes are 32-bit, aligned, atomic on Xtensa LX7 — no tearing across ISR/FreeRTOS boundary (前提: volatile + 4字节对齐)
- Active and staging areas are separate; ISR reads active only; service core writes staging only
- Program hot-switch: service core writes staging → sets reload_flag → ISR does memcpy(~4μs for 2KB) → clears flag. No downtime.
- PARAM_TABLE: service core writes directly to active area. Worst case: one cycle of mixed old/new floats (acceptable).

### Three-Layer Permission Model (PLC-inspired)
1. **Program logic** (ROUTE_TABLE structure) — immutable at runtime; change via staging+reload (hot) or NVS slot switch
2. **Parameters** (setpoints, PID gains, thresholds in PARAM_TABLE) — service core writes anytime; effective next ISR cycle
3. **Read-only** (sensor values, actuator states, heartbeat) — service core reads for MQTT reporting

## Data Structures (packed, 4-byte aligned)

```c
// Data source type — which namespace does src_index index into?
typedef enum { SRC_SENSOR=0, SRC_WIRE=1, SRC_CONST=2 } SourceType_t;

// Output type enum — replaces the old unified dst_reg offset
typedef enum { DST_GPIO=0, DST_LEDC=1, DST_MCPWM=2, DST_WIRE=3, DST_DAC=4 } OutputType_t;

// Route entry: 16 bytes
typedef struct {
    uint8_t  src_type;       // SourceType_t: SENSOR/WIRE/CONST
    uint8_t  src_index;      // index into SENSOR_MAP, WIRE_MAP, or PARAM_TABLE (CONST)
    uint8_t  dst_type;       // OutputType_t — which peripheral or DST_WIRE
    uint8_t  dst_channel;    // channel within peripheral (GPIO pin#, LEDC ch#, WIRE index)
    uint8_t  op;             // primitive opcode
    uint8_t  flags;          // bit0=enabled
    uint16_t param_idx;      // index into PARAM_TABLE
    uint16_t state_offset;   // index into STATE_TABLE
    uint16_t actuator_idx;   // index into ACTUATOR_STATUS (for MQTT reporting)
    uint16_t reserved;
} RouteEntry_t;              // 16 bytes

// Param entry: 16 bytes (may expand to 20 bytes for value_e)
typedef struct {
    float value_a;  // PID Kp / CMP threshold / LPF alpha / ...
    float value_b;  // PID Ki / CMP low threshold / ...
    float value_c;  // PID Kd / ...
    float value_d;  // PID setpoint / output upper limit / ...
} ParamEntry_t;

// State entry: 16 bytes (private to control core)
typedef struct {
    float state_a;  // LPF last output / PID integral / HYST state / CNT count
    float state_b;  // PID last error / RATE last value / EDGE last level
    float state_c;
    float state_d;
} StateEntry_t;

// Control block: 68 bytes (actual struct, verified by _Static_assert)
typedef struct {
    uint32_t magic;              // 0xC0C0C0C0 — service core checks this before access
    uint32_t version;
    uint32_t core0_heartbeat;    // incremented each ISR cycle
    uint8_t  reload_flag;        // service core sets → ISR copies staging→active → clears
    uint8_t  active_slot;
    uint8_t  program_state;      // 0=blank, 1=running, 2=switching
    uint8_t  reserved1;
    uint32_t route_crc;
    uint32_t param_version;
    uint32_t nvs_load_status;
    float    isr_period_s;       // ISR period in seconds (e.g., 0.0001f)
    // CCOUNT software oscilloscope (timing measurement, no external HW needed)
    uint32_t timing_entry_cc;    // CCOUNT at ISR entry
    uint32_t timing_exit_cc;     // CCOUNT at ISR exit
    uint32_t timing_exec_min;    // min execution cycles this window
    uint32_t timing_exec_max;    // max execution cycles this window
    uint32_t timing_last_entry;  // previous entry CCOUNT (for period calc)
    uint32_t timing_period_min;  // min ISR period in cycles
    uint32_t timing_period_max;  // max ISR period in cycles
    uint32_t timing_samples;     // number of samples since last reset
    uint32_t timing_reset_flag;  // set by service core to reset min/max
} SharedCtrl_t;
```

Max capacity: 64 routes, 64 params, 64 states, 64 sensors, 32 actuators, 256 LUT entries, 4 NVS program slots.

## Output Dispatching (replaces REG_WRITE with unified offset)

```c
// IRAM-resident dispatcher — called per route entry in ISR
// Full implementation in components/core0/output_driver.h
static inline void IRAM_ATTR apply_output(uint8_t type, uint8_t ch, float val) {
    switch (type) {
    case DST_GPIO:
        if (val > 0.5f) REG_WRITE(GPIO_OUT_W1TS_REG, 1U << ch);
        else            REG_WRITE(GPIO_OUT_W1TC_REG, 1U << ch);
        break;
    case DST_LEDC:
        // duty = val × (2^LEDC_DUTY_RES - 1); write duty reg + trigger update
        // ESP32-S3 LEDC: ledc_ll_set_duty_int_part + ledc_ll_ls_channel_update
        break;
    case DST_WIRE:
        // Not a hardware output — ISR writes WIRE_MAP[ch] directly before calling apply_output
        break;
    case DST_MCPWM:
        // Phase 2: duty = val × MCPWM_PERIOD; write timestamp register
        break;
    }
}
```

## WIRE_MAP Dataflow Architecture

WIRE_MAP[64] is the key innovation that enables multi-hop dataflow graphs. Each route entry can:
- **Read from** SENSOR_MAP, WIRE_MAP, or PARAM_TABLE (CONST) — selected by `src_type`
- **Write to** WIRE_MAP or a hardware output — selected by `dst_type`

This means primitives can chain arbitrarily:
```
SENSOR[0] → SCALE → WIRE[0] → LPF → WIRE[1] → PID → WIRE[2] → CLAMP → LEDC_CH0
                                WIRE[1] → CMP(>30) → WIRE[10] ─┐
                                WIRE[1] → LPF → WIRE[3] → RATE → WIRE[4] → CMP(>2) → WIRE[11] ─┐
                                                                          WIRE[10] → AND(B=WIRE[11]) → WIRE[12]
```

The ISR iterates routes **sequentially** (route[0], route[1], ...), so the .lf compiler performs **topological sort** (Kahn's algorithm) to ensure producer routes appear before consumers. Without topological sort, a consumer reading WIRE_MAP would get the previous cycle's stale value.

DST_WIRE outputs bypass the hardware dispatcher — the ISR writes `WIRE_MAP[ch] = out` directly before calling `apply_output()`. This gives WIRE_MAP writes the same 100μs update latency as hardware outputs.

## 26 Hardware Primitives

| # | Op | Primitive | Notes |
|---|-----|-----------|-------|
| 1 | DIRECT (0x00) | Passthrough | src → output |
| 2 | CMP (0x01) | Comparator | Threshold 0/1 output |
| 3 | HYST (0x02) | Hysteresis | On/off thresholds, state-latched |
| 4 | CLAMP (0x03) | Limiter | [lo, hi] bounds |
| 5 | LPF (0x04) | 1st-order low-pass | y[n]=y[n-1]×(1-α)+x[n]×α |
| 6 | PID (0x05) | PID controller | **D-term: Kd×(err_curr−err_prev)** — note subtraction order! |
| 7 | RATE (0x06) | Rate of change | (src−last)/dt, dt from SharedCtrl.isr_period_s |
| 8 | DEADBAND (0x07) | Deadband filter | Suppresses |Δ| < band |
| 9 | MUX (0x08) | Multiplexer | Select from N inputs (reads WIRE_MAP) |
| 10 | EDGE (0x09) | Edge detector | Rising/falling/both → 1.0 pulse |
| 11 | LUT (0x0A) | Lookup table | Linear interpolation over LUT_DATA[] |
| 12 | CNT (0x0B) | Counter | Edge-triggered software counter |
| 13 | TIMER (0x0C) | Timer | Start signal → count up → expire signal |
| — | (0x0D) | *Reserved* | (was OP_ESTOP; ESTOP moved to ISR flow + hardware path) |
| 14 | SCALE (0x0E) | Linear scaling | y = k×x + b (k=value_a, b=value_b) |
| 15 | AND (0x0F) | Logical AND | (src>0.5 AND WIRE_MAP[B]>0.5) ? 1 : 0 |
| 16 | OR (0x10) | Logical OR | (src>0.5 OR WIRE_MAP[B]>0.5) ? 1 : 0 |
| 17 | NOT (0x11) | Logical NOT | (src>0.5) ? 0 : 1 |
| 18 | REG (0x12) | Register/Latch | out=*sa, *sa=src — D flip-flop for state vars |
| 19 | ADD (0x13) | Addition | y = src + WIRE[pb] |
| 20 | SUB (0x14) | Subtraction | y = src - WIRE[pb] |
| 21 | MUL (0x15) | Multiplication | y = src × WIRE[pb] |
| 22 | DIV (0x16) | Division | y = src / WIRE[pb], ÷0→0 |
| 23 | BITAND (0x17) | Bitwise AND | (int)src & (int)WIRE[pb] |
| 24 | BITOR (0x18) | Bitwise OR | (int)src \| (int)WIRE[pb] |
| 25 | BITXOR (0x19) | Bitwise XOR | (int)src ^ (int)WIRE[pb] |
| 26 | BITNOT (0x1A) | Bitwise NOT | ~(int)src |

**Stateful primitives** (allocate STATE_TABLE slots): HYST, LPF, PID, RATE, DEADBAND, EDGE, CNT, TIMER, REG (9 total).
**Stateless primitives**: DIRECT, CMP, CLAMP, MUX, LUT, SCALE, AND, OR, NOT, ADD, SUB, MUL, DIV, BITAND, BITOR, BITXOR, BITNOT (17 total).

### ⚠️ PID Derivative Term — Bug Fixed
The original code had `d_term = kd * (err_prev - err_curr)` which REVERSES the sign. Correct formula:
```c
float d_term = kd * (err - STATE_TABLE[si].state_b);  // Kd × (err_curr − err_prev)
```
With the bug, derivative would amplify oscillations instead of damping them. This is the single most critical fix.

## ISR Execution Flow (corrected)

```c
void IRAM_ATTR core0_timer_isr(void) {
    // S0: Program hot-switch
    if (SHARED_CTRL->reload_flag) {
        SHARED_CTRL->program_state = 2;
        memcpy(ROUTE_TABLE,  ROUTE_STAGING, 1KB);  // ~3μs
        memcpy(PARAM_TABLE,  PARAM_STAGING, 1KB);  // ~1μs
        // NO CRC here — verified by service core before setting reload_flag
        SHARED_CTRL->reload_flag = 0;
        SHARED_CTRL->program_state = 1;
    }

    // S1: Sensor fault clamp (NEW — prevents blind driving on sensor failure)
    for (i = 0; i < MAX_ROUTES; i++) {
        if (!enabled) continue;
        float src = SENSOR_MAP[ROUTE_TABLE[i].src_index];
        if (src < TEMP_MIN_VALID || src > TEMP_MAX_VALID) {
            src = DEFAULT_SAFE_VALUE;
            SHARED_CTRL->sensor_fault_flags |= (1 << ROUTE_TABLE[i].src_index);
        }
        // ... execute primitive with safe_src ...
    }

    // S2: ESTOP software backup (hardware LM393 already cut safety relay)
    if (ESTOP_GPIO_IN) {
        for (int i = 0; i < MAX_SAFETY_OUTPUTS; i++)
            apply_output(DST_GPIO, safety_pins[i], 0.0f);
        goto feed_wdt;  // skip normal control, but still feed WDT
    }

    // S3: Route table traversal + primitives + apply_output

    // S4: Heartbeat + feed WDT
    SHARED_CTRL->core0_heartbeat++;
    TIMERG0.wdtfeed = 1;
}
```

## Emergency Stop (ESTOP)

### 软件急停 (已实现, v2.0)

**GPIO**: ESTOP_BUTTON = GPIO4 (输入, 内部下拉, 按下=HIGH)

**ISR 逻辑** (S2.5 段, 路由处理之前):
1. 检测 GPIO4 或 `estop_latched` 标志
2. 若激活: `GPIO_OUT_W1TC_REG ← estop_gpio_mask` (单指令清零所有安全 GPIO)
3. LEDC 占空比 → 0
4. ACTUATOR_STATUS 全部归零
5. 跳过路由处理, 仍喂 WDT + 心跳
6. 响应时间: ≤100μs (ISR 周期)

**安全 GPIO 掩码**: 从 ROUTE_TABLE 自动扫描所有 DST_GPIO 路由构建, 程序热切换时自动重建.

**锁定模式** (`ESTOP_LATCHING=1`): 按下后锁定, 需 MQTT `reset_estop` 复位.

**MQTT 接口**:
```json
{"cmd":"trigger_estop"}   // 软件触发急停
{"cmd":"reset_estop"}     // 复位急停锁定
// 状态上报自动包含: "estop":true/false
```

### 硬件急停 (Phase 2, 待实现)

```
ESTOP button → LM393 comparator → AND gate (74HC08) → safety relay coil
                    ↑                    ↑
              TL431 Vref (1.25V)    GPIO_OUT (ESP32 normal control)

Normal:  ESTOP=0V → LM393=HIGH → AND output = GPIO control
ESTOP:   ESTOP=3.3V → LM393=LOW → AND output = LOW (relay OFF, regardless of GPIO)
```

- Hardware path: LM393 (1.3μs) + 74HC08 (~10ns) ≈ **1.5μs** — independent of ESP32
- Software backup: ISR reads ESTOP_GPIO_IN → sets all safety GPIOs low (<100μs)
- Safety relay coil MUST have a flyback diode (1N4148)
- This design meets IEC 61508 SIL2 requirement for one independent hardware safety path

## Sensor Architecture (CORRECTED)

```
PT100 (4-wire) → MAX31865 → SPI2 (CS=GPIO10, MOSI=11, MISO=13, CLK=12)
                                ↓
                    物理核心0 FreeRTOS task (100ms周期)
                    读取 16-bit ADC → 转换 °C → 写入 SENSOR_MAP[0]
                                ↓
                    物理核心1 ISR (100μs周期)
                    读取 SENSOR_MAP[0] — 1000次读到同一个值，无抖动
```

- SPI2 is exclusively owned by 物理核心0; the ISR never touches SPI
- Conversion time ~21ms → 21% CPU at 100ms interval (acceptable)
- Float write to SENSOR_MAP is 32-bit aligned → atomic on Xtensa LX7
- Sensor fault: if value outside [-40, 150] °C → ISR clamps to DEFAULT_SAFE_VALUE

## NVS Program Storage & CRC

### Program Slots (actual implementation)
```
NVS namespace "core0_cfg":
  "route_0"  = RouteEntry_t[64]   (1KB blob)
  "param_0"  = ParamEntry_t[64]   (1KB blob)
  "lut_0"    = float[256]         (1KB blob, optional — fails gracefully)
  "route_1"  = RouteEntry_t[64]   (1KB blob)
  "param_1"  = ParamEntry_t[64]   (1KB blob)
  "lut_1"    = float[256]         (1KB blob)
  "active"   = uint8_t            (which slot to load)
```
Note: All NVS functions are `static inline` in `nvs_store.h` — there is no `nvs_store.c`.

### Startup Load
  1. Read "active" slot number
  2. Load route_N + param_N + lut_N blobs from NVS into shared memory
  3. LUT loading failure is non-fatal (falls through gracefully)
  4. NVS blob layer provides basic integrity; application-level CRC planned for future (see 修正汇总 §7)

### Program Download (service core)
  1. Receive binary + expected_crc (from .lf compiler output)
  2. Verify CRC32 using hardware CRC accelerator (`esp_crc32_be`, ~300μs)
  3. CRC mismatch → reject, return error
  4. Save to NVS as separate route_N/param_N/lut_N keys
  5. Write to staging area (ROUTE_STAGING + PARAM_STAGING)
  6. Set reload_flag
  7. Spin-wait for reload_flag → 0 (timeout 200μs)

### OTA (Firmware Upgrade)
Flash partition layout:
```
0x020000  factory     (2MB)   ←出厂固件 (esptool write_flash 0x20000)
0x220000  ota_0       (2MB)   ←OTA 目标
0x420000  ota_1       (2MB)   ←OTA 备用
0x19000   otadata     (8KB)   ←启动标记
```

OTA flow:
1. MQTT `ota_start` → spawn `ota_task` (独立任务，不阻塞 MQTT)
2. HTTP GET → chunked `esp_ota_write()` → `esp_ota_end()` → `esp_ota_set_boot_partition()`
3. Publish `ota_done` → delay 2s → `esp_restart()`
4. Bootloader reads otadata → boots ota_0 → `ota_mark_valid()` confirms firmware
5. If new firmware boot-loops, bootloader auto-rolls back to factory

HTTP server for OTA binary: `python3 -m http.server 8080` on cloud server (`/var/www/html/`).

## Performance (CCOUNT 实测, 240MHz, 10路由)

| Metric | Target | Measured | Notes |
|--------|--------|----------|-------|
| ISR scan period | 100μs | 100.00μs | 24,000 CCOUNT cycles |
| ISR execution (10 routes) | <10μs | **8.3~9.8μs** | 1994~2344 cycles, 9.8% budget |
| ISR execution (64 routes) | <10μs | ~12μs (est.) | 13% budget at full load |
| ISR period jitter | <1μs | 18~38μs | WiFi+SPI contention on other core |
| ESTOP response (software) | <100μs | ≤100μs | ISR周期内, 单指令 GPIO 清零 |
| Emergency stop (hardware) | <1.5μs | TBD | LM393 + 74HC08, Phase 2 |
| Program hot-switch (memcpy) | <5μs | ~4μs | 2KB SRAM→SRAM copy |
| Parameter change effective | <100μs | <100μs | next ISR cycle |
| OTA download (934KB) | — | ~14s | HTTP, ESP32 HTTP client |
| Power-up → ISR running | <10ms | — | NVS load + init |
| ISR period dynamic switch | — | <1 ISR cycle | MQTT `set_period`, zero downtime |
| Service core crash impact | None | ✅ Verified | control core runs independently |

## .lf Language (v1) & Compiler (v2.0)

Dataflow DSL. Keywords: `sensor`, `actuator`, `param`, `connect`. 20 primitives (SCALE/AND/OR/NOT added in v2.0).
```c
sensor temp = adc(0);
actuator heater = pwm(0);
param setpoint = 25.0;
connect temp → lpf(alpha=0.1) → pid(kp=2,ki=0.1,kd=0.05,sp=setpoint) → heater;
```

Compiler at `compiler/lf_compiler.py` (~550 lines, Python). Usage:
```bash
python lf_compiler.py program.lf -o program.bin    # compile
python lf_compiler.py program.lf --json             # JSON debug dump
python lf_compiler.py program.lf --send <IP>        # send to ESP32 (TODO)
```

Output format: `route_table.bin` (64×16B) + `param_table.bin` (64×16B) + CRC32 (4B) = 2052 bytes.

**v2.0 improvements** (2026-06-18):
- **Topological sort**: Kahn's algorithm on dataflow DAG — ensures producer routes run before consumers in ISR sequential execution. Cycle detection with clear error messages.
- **Param value propagation**: Named params (`pid(kp=pid_kp, ...)`) correctly pass their values into primitive ParamEntries. Previously returned defaults.
- **State slot optimization**: Only 8 stateful primitives (HYST, LPF, PID, RATE, DEADBAND, EDGE, CNT, TIMER) allocate STATE_TABLE slots. Stateless primitives use state_offset=0.
- **Wire reference resolution**: AND/OR `b=wire_name` references resolved after all wires are known (handles forward references).
- **CRC32-BE**: Output includes CRC32 matching ESP32 `esp_crc32_be()` (poly 0x04C11DB7).
- **Validation**: Capacity checks (MAX_ROUTES=64, MAX_PARAMS=64, MAX_WIRES=64, MAX_STATES=64), undefined wire/sensor detection, duplicate wire producer detection.

**Test coverage**: `compiler/test_program.lf` (恒温控制+告警, 9 routes, 16 params). Complex test (双通道PID+AND/OR, 12 routes) verified. 54 primitive unit/fuzz tests passing.

## C Compiler (v1.0) — `compiler/c_compiler.py`

C 子集 → 路由表编译器。约 1800 行 Python。将 C 语法控制程序编译为 ISR 可执行的路由表。

```bash
python c_compiler.py program.c -o program.bin
python c_compiler.py program.c --json     # JSON IR 调试输出
```

**支持特性:** float/int 类型, state float 跨周期状态, if/else (含嵌套), switch/case/default, for/while 编译期展开, ?: 三元, enum 常量, 数组 float arr[N], 位运算 & | ^ ~, 十六进制 0x/二进制 0b 字面量, 内置函数 (pid/clamp/lpf/scale/cmp), 关键字参数 (sp=...).

**编译管线:** C源码 → Tokenizer → Parser(AST) → If-Conversion(控制流→数据流) → IR(三地址码) → Resource Allocation → Topological Sort → Route Table Binary + CRC32.

**核心变换:** 控制流→数据流 (if-conversion)。所有分支都计算，MUX 选择结果。状态变量延迟写机制（body 结束时统一 flush，避免多次写冲突）。

**限制:** 无指针/malloc/递归/函数调用/while(1)/变长循环。数组下标必须常量。最大 64 路由/64 参数/64 状态。

**编程指南:** `核心0-C编程指南.md`

## MQTT Commands

### Request/Response Topics
| Topic | Direction | Purpose |
|-------|-----------|---------|
| `core0/cmd` | Broker → ESP32 | Command subscription (QoS 0) |
| `core0/resp` | ESP32 → Broker | Command response (JSON) |
| `core0/status` | ESP32 → Broker | Status report (500ms, JSON) |

### Command Set (13 commands)
```json
{"cmd": "set_param",     "idx": 3, "field": 3, "value": 28.0}
{"cmd": "set_setpoint",  "idx": 3, "value": 28.0}
{"cmd": "set_pid",       "idx": 3, "kp": 3.0, "ki": 0.15, "kd": 0.08}
{"cmd": "get_status"}
{"cmd": "save_params"}
{"cmd": "ota_start",     "url": "http://182.61.18.136:8080/core0_controller.bin"}
{"cmd": "ota_status"}
{"cmd": "reboot"}
{"cmd": "trigger_estop"}  // 软件触发急停 (v2.0)
{"cmd": "reset_estop"}    // 复位急停锁定 (v2.0)
{"cmd": "set_period",    "us": 50}    // 动态切换 ISR 周期 (v2.0, 10~100000μs, 零停机)
{"cmd": "get_period"}                  // 查询当前 ISR 周期 + 频率
{"cmd": "prog_load",    "data": "<hex>"}  // 程序热下载 (v2.1, hex→CRC→NVS→热切换, 不停机)
```

### ISR Period Dynamic Switching (v2.0)

ISR 周期可在运行时通过 MQTT 动态切换，无需重编译或重启：

```
核心0 MQTT 收到 set_period → 写 isr_period_s → 置 period_update_pending=1 → MEMW
    ↓
下个 ISR 周期检测到标志 → gptimer_set_alarm_action(timer, new_config) → 清除标志
    ↓
下次中断即用新周期。零停机。响应延迟 <当前ISR周期。
```

**压榨实测** (10路由混合负载, 240MHz):

| 周期 | CPU占用 | 确定性 | 备注 |
|------|--------|--------|------|
| 100μs | 9.9% | ✅ 基准 | 原始默认值 |
| 50μs | 19.5% | ✅ 安全 | 5x 余量 |
| 30μs | 77.0% | ✅ 可用 | 安全地板, ≤50路由混合负载 |
| 20μs | 123% | ❌ 超周期 | ISR 执行不完 |
| 15μs | 164% | ❌ | 确定性丢失 |
| 12μs | — | ❌ 死机 | WDT 复位, 需断电重启 |

安全范围: 10~100000μs (代码硬限制)。`get_period` 返回当前周期、秒值和频率。

### OTA Flow
```
MQTT ota_start → ESP32 publishes "ota_started" → HTTP GET (934KB, ~14s)
→ esp_ota_write() chunked → esp_ota_end() → set boot partition
→ publishes "ota_done" → delay 2s (flash cache flush) → esp_restart()
→ bootloader reads otadata → boots from ota_0 → WiFi reconnect <3s
→ MQTT reconnect <5s → status reports resume
```

OTA runs in an **independent FreeRTOS task** (`ota_task`, prio configMAX_PRIORITIES-2, 8KB stack).
The MQTT event thread returns immediately after spawning the task — keepalive pings
and status reports continue uninterrupted during the entire OTA process.

Error codes: `0x7002` = HTTP connect failed (firewall/server down), `0x7005` = connection closed mid-transfer.
If OTA fails, the ESP32 stays on the current firmware and reports the error via MQTT.

## Key Design Decisions

- **Dual-core role swap (方案B)**: Use `CONFIG_FREERTOS_UNICORE=y` — FreeRTOS on PRO_CPU, bare-metal ISR on APP_CPU. Simpler, officially supported.
- **Shared memory allocation**: Dynamic via `heap_caps_malloc(DMA | INTERNAL)` — prevents heap/.data/.bss conflicts. Replaces hardcoded `0x3FCA0000` which was silently overwritten by ESP-IDF malloc. See `缓存一致性问题.md` §7.
- **Cache coherency**: ESP32-S3 internal DRAM (D-bus 0x3FC8xxxx) is inherently non-cacheable — D-Cache only handles PSRAM. ROM `Cache_WriteBack_Addr`/`Invalidate` are no-ops here. Use Xtensa `MEMW` barriers instead.
- **Output abstraction**: `dst_type` + `dst_channel` dispatcher, not unified register offset
- **Sensor**: DHT22 on GPIO11 (dev phase); target is PT100 + MAX31865 on SPI2
- **ESTOP**: External LM393 + AND gate for hardware safety path; ISR GPIO write as software backup
- **CRC**: Verified by service core before setting reload_flag; removed from ISR entirely
- **PID derivative**: `Kd × (err_curr − err_prev)` — verified correct sign
- **RATE dt**: From SharedCtrl.isr_period_s, not hardcoded
- **Sensor faults**: ISR clamps out-of-range values to safe default; Core 1 detects via sensor_fault_flags
- **NVS recovery**: Program-level CRC on load; fallback to previous slot on failure; blank (safe) as last resort
- **Float atomicity**: 32-bit aligned volatile writes are atomic on Xtensa LX7 (单条 s32i 指令)
- **Parameter writes**: Direct to active PARAM_TABLE; worst case one cycle mixed values (acceptable)
