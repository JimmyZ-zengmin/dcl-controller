# 核心0 — 完整技术手册

> 版本：3.0 | 日期：2026-06-18 | 基于实际代码审计（非历史文档）
> 
> 本文档是核心0项目的**唯一权威技术参考**。所有内容均基于 `D:\core0\firmware\` 和 `compiler\` 的实际源代码验证。当与其他文档冲突时，以本文档为准。

---

## 目录

1. [产品定义](#一产品定义)
2. [功能全景](#二功能全景)
3. [技术架构](#三技术架构)
4. [实现细节](#四实现细节)
5. [开发工具链](#五开发工具链)
6. [已验证性能](#六已验证性能)
7. [未来路线图](#七未来路线图)
8. [附录](#八附录)

---

## 一、产品定义

### 1.1 核心0 是什么

**核心0 是一个"软件定义硬件逻辑芯片"**——运行在 ESP32-S3 上的双核工业控制引擎。它不是单片机应用，不是 PLC，不是 RTOS 项目。它是一个确定性、永不停机的裸机控制循环，支持不停机在线重编程。

```
核心0 = 硬件逻辑门（确定性）+ 软件灵活性（可编程）
      = PLC 的可靠性 × 现代软件工程的生产力
```

### 1.2 核心设计原则

| 原则 | 含义 |
|------|------|
| **确定性** | 100μs 固定周期，抖动 <100ns（实测 12~87ns） |
| **隔离性** | 双核物理隔离，控制核崩溃不影响服务核，反之亦然 |
| **不停机** | 参数修改<100μs生效，程序热切换<4μs，固件OTA秒级 |
| **可定义** | .lf 数据流语言 → 编译为路由表 → ISR 直接执行 |
| **可观测** | CCOUNT 软件示波器（4.17ns精度），MQTT 500ms状态上报 |

### 1.3 适用场景

- 工业温度/压力/流量 PID 控制
- 多回路协调控制（单芯片 64 条控制路由）
- 需要在线调参 + 不停机改逻辑的连续生产场景
- 对成本敏感但拒绝牺牲可靠性的嵌入式控制

---

## 二、功能全景

### 2.1 控制原语（20条操作码，17条可用 + 1条保留 + 2条预留）

以下内容直接来源于 `shared_mem.h`（操作码定义）和 `core1_isr.c`（ISR 实现）：

#### 信号采集与标定

| 操作码 | 原语 | 函数 | 说明 |
|--------|------|------|------|
| 0x00 | DIRECT | `out = src` | 直通，无处理 |
| 0x0E | SCALE | `out = pa×src + pb` | 线性标定 y=kx+b（k=value_a, b=value_b） |

#### 信号调理

| 操作码 | 原语 | 函数 | 状态 |
|--------|------|------|------|
| 0x04 | LPF | `out = state_a×(1-α) + src×α` | 有状态 |
| 0x06 | RATE | `out = (src − state_a) / dt` | 有状态 |
| 0x07 | DEADBAND | `|src − state_a| > band` 才更新 | 有状态 |

#### 控制算法

| 操作码 | 原语 | 函数 | 状态 | 关键细节 |
|--------|------|------|------|---------|
| 0x05 | PID | `err=sp−src; P=Kp×err; I=Ki×∫err; D=Kd×(err−err_prev)` | 有状态 | **D 项使用标准形式 Kd×(err_curr−err_prev)**，输出限幅 [0,100] |

#### 逻辑判断

| 操作码 | 原语 | 函数 | 状态 |
|--------|------|------|------|
| 0x01 | CMP | `out = (src > pa) ? 1 : 0` | 无状态 |
| 0x02 | HYST | `src > on→1; src < off→0` | 有状态 |
| 0x0F | AND | `out = (src>0.5 AND WIRE_MAP[pb]>0.5) ? 1 : 0` | 无状态 |
| 0x10 | OR | `out = (src>0.5 OR WIRE_MAP[pb]>0.5) ? 1 : 0` | 无状态 |
| 0x11 | NOT | `out = (src > 0.5) ? 0 : 1` | 无状态 |

#### 输出处理

| 操作码 | 原语 | 函数 |
|--------|------|------|
| 0x03 | CLAMP | `out = clamp(src, lo, hi)` |
| 0x08 | MUX | `out = WIRE_MAP[pa & 63]` |

#### 事件与计数

| 操作码 | 原语 | 函数 | 状态 |
|--------|------|------|------|
| 0x09 | EDGE | 上升沿/下降沿/双沿 → 1.0 脉冲 | 有状态 |
| 0x0B | CNT | 边沿触发累加计数器 | 有状态 |
| 0x0C | TIMER | src>0.5 启动计时 → 到期输出 1.0 | 有状态 |

#### 高级功能

| 操作码 | 原语 | 函数 |
|--------|------|------|
| 0x0A | LUT | 线性插值查找表 `LUT_DATA[i0] + (LUT_DATA[i0+1]−LUT_DATA[i0])×frac` |

#### 保留

| 操作码 | 说明 |
|--------|------|
| 0x0D | 保留（原 OP_ESTOP，现改为 ISR 流程级实现 + 硬件路径） |

**有状态原语（分配 STATE_TABLE 槽）**：HYST, LPF, PID, RATE, DEADBAND, EDGE, CNT, TIMER（8 条）
**无状态原语（state_offset=0）**：其余 9 条

### 2.2 WIRE_MAP 数据流引擎

WIRE_MAP[64] 是核心0 区别于传统 PLC 的关键架构创新。它不是"传感器进→硬件出"的单向管道，而是原语间的全连接数据流网络：

```
SENSOR[0]──→ SCALE ──→ WIRE[0]──→ LPF ──→ WIRE[1]──→ PID ──→ WIRE[2]──→ CLAMP ──→ LEDC_CH0
                                      │
                                      ├──→ CMP ──→ WIRE[10]──┐
                                      │                          ├──→ AND ──→ WIRE[12]
                                      └──→ LPF ──→ WIRE[3]     │
                                                └──→ RATE ──→ WIRE[4]──→ CMP ──→ WIRE[11]──┘
```

**关键规则**：
- 每条 WIRE 只能有一个生产者（编译期验证）
- ISR 按拓扑排序顺序执行路由（生产者先于消费者）
- DST_WIRE 输出绕过硬件驱动，直接写 WIRE_MAP
- 所有 WIRE 在单个 100μs 周期内更新

### 2.3 数据源三态模型

每个路由条目的输入端可以从三种来源获取数据（`src_type` 字段）：

| src_type | 枚举值 | 数据来源 | 索引含义 |
|----------|--------|---------|---------|
| SRC_SENSOR | 0 | SENSOR_MAP[src_index] | 物理传感器端口 |
| SRC_WIRE | 1 | WIRE_MAP[src_index] | 上游原语输出 |
| SRC_CONST | 2 | PARAM_TABLE[param_idx].value_a | 常数/参数值 |

### 2.4 输出目标四态模型

| dst_type | 枚举值 | 输出目标 | 说明 |
|----------|--------|---------|------|
| DST_GPIO | 0 | GPIO 引脚 | 位操作 W1TS/W1TC 寄存器 |
| DST_LEDC | 1 | LEDC PWM 通道 | 1kHz, 13-bit (0~8191) |
| DST_MCPWM | 2 | MCPWM 输出 | Phase 2 实现 |
| DST_WIRE | 3 | WIRE_MAP[ch] | 给下游原语使用 |
| DST_DAC | 4 | DAC 输出 | 预留 |

### 2.5 急停系统（ESTOP）

#### 软件急停（已实现）

- **触发**：GPIO4 物理按钮（内部下拉，按下=HIGH）或 MQTT `trigger_estop`
- **ISR 响应**（S2.5 段，路由处理之前）：
  1. 读 GPIO4 → 若 HIGH，置 `estop_latched = true`
  2. 单指令清零所有安全 GPIO：`REG_WRITE(GPIO_OUT_W1TC_REG, estop_gpio_mask)`
  3. LEDC 占空比 → 0
  4. ACTUATOR_STATUS 全部归零
  5. 跳过路由处理，仍喂 WDT + 心跳
  6. 响应时间 ≤100μs（ISR 周期内）
- **安全 GPIO 掩码**：从 ROUTE_TABLE 自动扫描 `dst_type==DST_GPIO` 构建，程序热切换时自动重建
- **锁定模式**（`ESTOP_LATCHING=1`）：按下后锁定，需 MQTT `reset_estop` 复位
- **跨核安全**：`estop_latched` 声明为 `volatile bool`，复位写后执行 `MEMW` 屏障

#### 硬件急停（Phase 2 规划）

```
ESTOP 按钮 → LM393 比较器 → 74HC08 与门 → 安全继电器线圈
                  ↑                ↑
            TL431(1.25V)    GPIO_OUT（正常控制）

正常: ESTOP=0V → LM393=HIGH → AND = GPIO 控制
急停: ESTOP=3.3V → LM393=LOW → AND = LOW（继电器断开，独立于 ESP32）
```

- 硬件路径延迟：LM393(1.3μs) + 74HC08(10ns) ≈ 1.5μs
- 符合 IEC 61508 SIL2（独立硬件安全路径）
- 安全继电器线圈必须有续流二极管（1N4148）

### 2.6 三层权限模型（PLC 风格）

| 层级 | 内容 | 修改方式 | 生效延迟 |
|------|------|---------|---------|
| **程序逻辑** | ROUTE_TABLE 结构 | 写暂存区 + reload_flag → ISR memcpy | <4μs |
| **参数** | PARAM_TABLE 数值 | 核心0 直接写活动区 | <100μs |
| **只读** | SENSOR_MAP, ACTUATOR_STATUS, heartbeat | 不可写（权限控制） | — |

### 2.7 网络功能

#### MQTT 主题

| 主题 | 方向 | QoS | 用途 |
|------|------|-----|------|
| `core0/status` | ESP32 → Broker | 0 | 500ms 状态上报（heartbeat, sensor, actuator, ISR timing, estop） |
| `core0/cmd` | Broker → ESP32 | 0 | 指令接收 |
| `core0/resp` | ESP32 → Broker | 0 | 指令响应 |

#### MQTT 指令集（10 条）

| 指令 | 参数 | 功能 | 实现文件 |
|------|------|------|---------|
| `set_param` | idx, field(0~3), value | 直接写 PARAM_TABLE[idx].value_a/b/c/d | wifi_mqtt.c:105 |
| `set_setpoint` | idx, value | 写 PARAM_TABLE[idx].value_d | wifi_mqtt.c:138 |
| `set_pid` | idx, kp, ki, kd | 写 PARAM_TABLE[idx].value_a/b/c | wifi_mqtt.c:167 |
| `get_status` | — | 返回完整状态 JSON | wifi_mqtt.c:202 |
| `save_params` | — | 当前参数保存到 NVS | wifi_mqtt.c:209 |
| `ota_start` | url | 触发 OTA 固件升级（独立任务） | wifi_mqtt.c:252 |
| `ota_status` | — | 查询 OTA 进度 (0~100) | wifi_mqtt.c:282 |
| `reboot` | — | 软件复位 | wifi_mqtt.c:294 |
| `trigger_estop` | — | 软件触发急停 | wifi_mqtt.c:301 |
| `reset_estop` | — | 复位急停锁定 | wifi_mqtt.c:307 |

#### OTA 固件升级

- **执行方式**：独立 FreeRTOS 任务（prio=configMAX_PRIORITIES-2, 8KB 栈），不阻塞 MQTT 事件线程
- **流程**：MQTT ota_start → HTTP GET (934KB, ~14s) → esp_ota_write() 逐块写入 → esp_ota_end() → esp_ota_set_boot_partition() → 2s 延迟落盘 → esp_restart()
- **分区**：factory(2MB) + ota_0(2MB) + ota_1(2MB)，支持自动回滚
- **验证**：OTA 期间 heartbeat 持续递增（5,231K→5,375K），MQTT keepalive 不中断

### 2.8 NVS 程序持久化

- **命名空间**：`core0_cfg`
- **存储格式**（每个 slot 3 个独立 key）：
  - `route_N` = RouteEntry_t[64]（1KB blob）
  - `param_N` = ParamEntry_t[64]（1KB blob）
  - `lut_N` = float[256]（1KB blob，可选）
  - `active` = uint8_t（当前活跃 slot）
- **LUT 加载失败不致命**，允许降级运行
- **所有 NVS 函数**均为 `static inline`，定义在 `nvs_store.h`（无 .c 文件）

---

## 三、技术架构

### 3.1 双核物理隔离

```
物理核心0 (PRO_CPU)                         物理核心1 (APP_CPU)
┌──────────────────────────┐               ┌──────────────────────────┐
│ FreeRTOS                 │   共享内存      │ 裸机 ISR 引擎            │
│ • WiFi STA + 自动重连     │◄═══════════►│ • 100μs GPTimer 中断     │
│ • MQTT 客户端 + 10 指令   │   64KB        │ • WIRE_MAP 数据流执行     │
│ • DHT22 传感器采集        │  DRAM内部      │ • 20 条原语分发          │
│ • OTA 固件升级（独立任务） │  non-cacheable │ • apply_output() 输出    │
│ • NVS 程序存储            │               │ • 软件 ESTOP 检查        │
│ • 参数在线修改             │               │ • CCOUNT 性能测量        │
│                          │               │ • 硬件 WDT 喂狗          │
│ 崩溃 → 控制核继续运行      │               │ 崩溃 → 服务核可检测重启    │
└──────────────────────────┘               └──────────────────────────┘
```

**关键配置**：
- 芯片：ESP32-S3 N16R8（Xtensa LX7, 240MHz, 16MB Flash, 8MB Octal PSRAM）
- FreeRTOS：仅物理核心0（v6.x 默认 tick 仅在 CPU0）
- 物理核心1：无 FreeRTOS，初始化后删任务，IDLE 接管
- Task WDT：关闭（`CONFIG_ESP_TASK_WDT_EN=n`），ISR 内部 heartbeat 替代

### 3.2 启动序列

```
上电 → ROM boot → 2nd stage bootloader → FreeRTOS 初始化（仅 PRO_CPU）
  ↓
app_main() [PRO_CPU]:
  0. heap_caps_malloc(64KB) → g_shared_mem（堆管理器保证独占）
  1. nvs_flash_init()
  2. 从 NVS 加载程序 → 写 ROUTE_TABLE, PARAM_TABLE 到共享内存
  3. ota_mark_valid()（确认固件正常，取消 OTA 回滚）
  4. SHARED_CTRL->magic = 0xC0C0C0C0  ← 信号："内存就绪"
  5. xTaskCreatePinnedToCore(core1_task, ..., 核心1)
     └→ spin-wait magic == 0xC0C0C0C0
        └→ core1_init()
           ├→ 配置 GPTimer (100μs 自动重载)
           ├→ build_test_routes()（或使用 NVS 加载的路由）
           ├→ 构建 ESTOP 安全 GPIO 掩码
           ├→ 使能 FPU（wsr.cpenable）
           ├→ 配置 LEDC PWM (1kHz, 13-bit)
           └→ gptimer_start() → ISR 开始运行
  6. 启动 DHT22 采集任务
  7. 启动 WiFi + MQTT（wifi_mqtt_init）
  8. 主循环：每 5s 打印心跳 + ISR 性能统计
```

**关键**：物理核心1 spin-waits `magic == 0xC0C0C0C0` 后才读取任何共享内存，防止读取未初始化数据。

### 3.3 共享内存布局（64KB，动态分配）

共享内存通过 `heap_caps_malloc(MALLOC_CAP_DMA | MALLOC_CAP_8BIT | MALLOC_CAP_INTERNAL, 64KB)` 在运行时分配。基址指针 `g_shared_mem` 在 `app_main()` 最早阶段设置。所有区域通过宏访问：

```
偏移      大小    名称                ISR(核心1)       FreeRTOS(核心0)
0x0000    68B    SharedCtrl          RW(heartbeat)    R(magic, flags, timing)
0x0044    ...    (reserved)
0x0100    1KB    ROUTE_TABLE[64]     R(只读)          禁止访问
0x0500    1KB    ROUTE_STAGING[64]   R                W(程序下载)
0x0900    1KB    PARAM_TABLE[64]     R(每周期读取)      W(调参写入)
0x0D00    1KB    PARAM_STAGING[64]   R                W(程序下载)
0x1100    1KB    STATE_TABLE[64]     RW(原语私有)      禁止访问
0x1500    256B   SENSOR_MAP[64]      R(每周期读取)      W(SPI/传感器写入)
0x1600    128B   ACTUATOR_STATUS[32] W(每周期写入)      R(MQTT上报)
0x1680    1KB    LUT_DATA[256]       R(OP_LUT读取)     W(程序加载时)
0x1A80    256B   WIRE_MAP[64]        RW(原语间数据流)   R(编译验证)
```

**设计规则**：
- 编译期 `_Static_assert` 验证：结构体大小、4 字节对齐、区域不重叠
- 内部 DRAM（D-bus 0x3FC8xxxx）天然 non-cacheable，无需 MPU 配置
- 跨核同步使用 Xtensa `MEMW` 屏障（排空写缓冲），不使用 ROM 缓存 API
- Float 写为 32-bit 对齐 → Xtensa LX7 单条 `s32i` 指令 → 原子操作

### 3.4 ISR 执行流程

以下流程直接来源于 `core1_isr.c:core0_isr()`：

```
每个 100μs 周期:
  │
  ├─ [S0] 使能 FPU（wsr.cpenable 1; rsync）
  │
  ├─ [S0.5] 程序热切换检查
  │   if (reload_flag):
  │     memcpy(ROUTE_TABLE, ROUTE_STAGING, 1KB)   ← ~3μs
  │     memcpy(PARAM_TABLE, PARAM_STAGING, 1KB)    ← ~1μs
  │     重建 ESTOP 安全 GPIO 掩码
  │     reload_flag = 0
  │
  ├─ [S1] CCOUNT 周期统计
  │   计算 min/max ISR 周期
  │
  ├─ [S2] GPIO 调试翻转（DEBUG_GPIO=15）
  │
  ├─ [S2.5] ESTOP 软件急停检查
  │   if (gpio_get_level(GPIO4) || estop_latched):
  │     GPIO_OUT_W1TC_REG ← estop_gpio_mask    ← 单指令
  │     LEDC duty → 0
  │     ACTUATOR_STATUS → 0
  │     heartbeat++, memw
  │     return（跳过路由处理）
  │
  ├─ [S3] 路由表遍历（按拓扑排序顺序）
  │   for each enabled route:
  │     ├─ 取数据源: SENSOR_MAP / WIRE_MAP / PARAM_TABLE(CONST)
  │     ├─ 取参数: PARAM_TABLE[pi].value_a/b/c/d
  │     ├─ 取状态: STATE_TABLE[si].state_a/b
  │     ├─ switch(r->op) → 执行原语
  │     ├─ if (dst_type == DST_WIRE):
  │     │     WIRE_MAP[ch] = out
  │     │   else:
  │     │     ACTUATOR_STATUS[actuator_idx] = out
  │     │     apply_output(dst_type, dst_channel, out)
  │
  ├─ [S4] heartbeat++, MEMW 屏障
  │
  └─ [S5] CCOUNT 执行时间统计
```

### 3.5 程序热切换机制

```
核心0 (FreeRTOS):
  1. 接收新程序 → CRC32 校验 → 保存 NVS
  2. 写 ROUTE_STAGING, PARAM_STAGING（暂存区）
  3. MEMW 屏障
  4. SHARED_CTRL->reload_flag = 1
  5. spin-wait reload_flag → 0（超时 200μs）

ISR (核心1):
  if (reload_flag):
    memcpy ROUTE_TABLE ← ROUTE_STAGING (1KB, ~3μs)
    memcpy PARAM_TABLE ← PARAM_STAGING (1KB, ~1μs)
    重建 ESTOP 安全 GPIO 掩码
    reload_flag = 0
  → 下个周期自动执行新程序
```

- 总切换时间 <5μs，不停机
- CRC 校验由核心0 在设置 reload_flag 之前完成（ISR 内不做 CRC）
- 切换后 ESTOP 掩码自动重建

---

## 四、实现细节

### 4.1 关键数据结构

以下结构体直接来源于 `shared_mem.h`（已通过 `_Static_assert` 验证）：

```c
// 数据源类型
typedef enum { SRC_SENSOR=0, SRC_WIRE=1, SRC_CONST=2 } SourceType_t;

// 输出目标类型
typedef enum { DST_GPIO=0, DST_LEDC=1, DST_MCPWM=2, DST_WIRE=3, DST_DAC=4 } OutputType_t;

// 路由表项：16 字节（packed, 4字节对齐）
typedef struct {
    uint8_t  src_type;       // SRC_SENSOR / SRC_WIRE / SRC_CONST
    uint8_t  src_index;      // 数据源索引
    uint8_t  dst_type;       // DST_GPIO / DST_LEDC / DST_MCPWM / DST_WIRE / DST_DAC
    uint8_t  dst_channel;    // 通道号
    uint8_t  op;             // 操作码 (0x00~0x11)
    uint8_t  flags;          // bit0=启用
    uint16_t param_idx;      // PARAM_TABLE 索引
    uint16_t state_offset;   // STATE_TABLE 索引（无状态原语=0）
    uint16_t actuator_idx;   // ACTUATOR_STATUS 索引
    uint16_t reserved;
} RouteEntry_t;              // sizeof=16 ✓

// 参数表项：16 字节
typedef struct {
    float value_a;  // PID Kp / CMP threshold / SCALE k / AND/OR A阈值
    float value_b;  // PID Ki / CMP low / SCALE b / AND/OR B输入端(WIRE索引)
    float value_c;  // PID Kd
    float value_d;  // PID setpoint / output upper limit
} ParamEntry_t;              // sizeof=16 ✓

// 状态表项：16 字节（ISR 私有）
typedef struct {
    float state_a;  // LPF上次输出 / PID积分 / HYST状态 / CNT计数 / TIMER累加
    float state_b;  // PID上次误差 / RATE上次值 / EDGE上次电平 / CNT上次 / TIMER到期
    float state_c;  // 预留
    float state_d;  // 预留
} StateEntry_t;              // sizeof=16 ✓

// 控制块：68 字节
typedef struct {
    uint32_t magic;              // 0xC0C0C0C0
    uint32_t version;
    uint32_t core0_heartbeat;    // 每周期递增
    uint8_t  reload_flag;        // 热切换标志
    uint8_t  active_slot;
    uint8_t  program_state;      // 0=blank, 1=running, 2=switching
    uint8_t  reserved1;
    uint32_t route_crc;
    uint32_t param_version;
    uint32_t nvs_load_status;
    float    isr_period_s;       // 0.0001f
    // CCOUNT 软件示波器
    uint32_t timing_entry_cc;
    uint32_t timing_exit_cc;
    uint32_t timing_exec_min;
    uint32_t timing_exec_max;
    uint32_t timing_last_entry;
    uint32_t timing_period_min;
    uint32_t timing_period_max;
    uint32_t timing_samples;
    uint32_t timing_reset_flag;
} SharedCtrl_t;              // sizeof=68 ✓
```

### 4.2 硬件输出驱动

`output_driver.h` 实现了 ISR 内调用的 `apply_output()`：

```c
static inline void IRAM_ATTR apply_output(uint8_t type, uint8_t ch, float val) {
    switch (type) {
    case DST_GPIO:
        // 直接写 GPIO 置位/清零寄存器，单指令完成
        if (val > 0.5f) REG_WRITE(GPIO_OUT_W1TS_REG, 1U << ch);
        else            REG_WRITE(GPIO_OUT_W1TC_REG, 1U << ch);
        break;
    case DST_LEDC:
        // 1kHz, 13-bit (0~8191)
        // 使用 ESP32-S3 LEDC LL 层 API
        uint32_t duty = (uint32_t)(val * 8191.0f);
        ledc_ll_set_duty_int_part(&LEDC, LEDC_LOW_SPEED_MODE, ch, duty);
        ledc_ll_ls_channel_update(&LEDC, LEDC_LOW_SPEED_MODE, ch);
        break;
    case DST_WIRE:
        // 不是硬件输出 —— ISR 主循环直接写 WIRE_MAP[ch]
        break;
    case DST_MCPWM:
        // Phase 2 实现
        break;
    }
}
```

### 4.3 关键陷阱与解决

#### FPU 协处理器使能

**问题**：Xtensa ISR 默认关闭 FPU，第一条浮点指令触发 `CoprocessorUnusable` panic。
**解决**：ISR 入口第一行执行 `__asm__ volatile("wsr.cpenable %0; rsync" : : "r"(1))`。

#### 共享内存分配冲突

**问题**：原始硬编码地址 `0x3FCA0000` 位于 ESP-IDF 堆中间，`malloc` 会覆盖路由表。
**解决**：改为 `heap_caps_malloc(DMA|INTERNAL, 64KB)` 动态分配，堆管理器保证独占。

#### 跨核缓存一致性

**问题**：ESP32-S3 内部 DRAM（0x3FC8xxxx）天然 non-cacheable，但写缓冲（store buffer）可能延迟写入对另一核心的可见性。
**解决**：在以下位置使用 Xtensa `MEMW` 指令排空写缓冲：
- ISR 末尾（heartbeat++ 后）
- 核心0 写 PARAM_TABLE 后
- 核心0 写 SENSOR_MAP 后
- 核心0 写暂存区 + 设置 reload_flag 前
- 核心0 写 estop_latched 后

#### 数据放置与 WDT

**问题**：`IRAM_ATTR` 用于数据会将数据放入 PSRAM → ISR 访问 PSRAM 可能阻塞 → 硬件 WDT 超时。
**解决**：仅函数使用 `IRAM_ATTR`，数据不加属性（默认内部 DRAM）。

#### PID 微分项符号

**原始 bug**：`d_term = Kd × (err_prev − err_curr)` ← 符号反转
**修正**：`d_term = Kd × (err_curr − err_prev)` ← 标准形式

### 4.4 DHT22 传感器驱动

- **位置**：`main/dht22.c`
- **接口**：单总线（GPIO11），开漏输出 + 上拉
- **协议**：MCU 拉低 1.2ms → 释放 → DHT22 回应 80μs 低 + 80μs 高 → 40bit 数据
- **时序**：需关中断（`portDISABLE_INTERRUPTS`），bit-bang 精度约 1μs
- **输出**：温度×10（如 253=25.3°C），湿度×10（如 560=56.0%RH）
- **校验**：8 位 checksum
- **采样周期**：2100ms（DHT22 最小间隔 2s）
- **注意**：此为开发阶段临时方案，工业部署需替换为 PT100 + MAX31865（SPI2）

### 4.5 .lf 编译器 v2.0

- **文件**：`compiler/lf_compiler.py`（~555 行 Python）
- **输入**：.lf 数据流描述语言
- **输出**：route_table.bin (64×16B) + param_table.bin (64×16B) + CRC32-BE (4B) = 2052 字节

**语法示例**（`test_program.lf`）：
```c
sensor temp = adc(0);
actuator heater = pwm(0);
actuator alarm = gpio(16);
param setpoint = 28.0;
param pid_kp = 2.0;

connect temp → scale(k=1, b=0) → temp_raw;
connect temp_raw → lpf(alpha=0.3) → temp_smooth;
connect temp_smooth → pid(kp=pid_kp, ki=0.1, kd=0.05, sp=setpoint) → pid_out;
connect pid_out → clamp(lo=0, hi=100) → heater;
```

**v2.0 核心算法**：

1. **解析**：正则匹配 sensor/actuator/param/connect 语句
2. **拓扑排序**（Kahn 算法）：
   - 构建 DAG：wire_producer[wire] → route_index, 消费者路由依赖生产者
   - BFS 入队零入度节点 → 排序输出
   - 检测循环依赖（`len(order) != n`）
3. **参数传递**：`_get_val()` 对 `('param', idx)` 类型查找 `self.params` 中的实际值
4. **状态槽优化**：`STATEFUL_OPS = {0x02,0x04,0x05,0x06,0x07,0x09,0x0B,0x0C}`，无状态原语 `state_offset=0`
5. **Wire 引用延迟解析**：AND/OR 的 `b=wire_name` 在所有 parse 完成后回填
6. **CRC32-BE**：多项式 0x04C11DB7，与 ESP32 `esp_crc32_be()` 一致
7. **验证**：容量检查 + 未定义 wire/sensor 检测 + 重复 wire 生产者检测

### 4.6 ESP-IDF 适配要点

| 版本 | ESP-IDF v6.0.1 |
|------|----------------|
| 编译器 | GCC 15.2.0 (xtensa-esp-elf) |
| CMake | 4.0.3 |
| 关键变化 | `driver` 组件拆分为 `esp_driver_gptimer`、`esp_hal_gpio` 等独立子组件 |
| 中文路径 | CMake 4.0.3 在中文路径下崩溃，项目必须在纯英文路径 |
| 分区表 | CSV 不支持行尾注释 (`#`) |
| Tick 中断 | v6.x 默认仅在 CPU0，无需额外配置 |

---

## 五、开发工具链

### 5.1 固件编译与烧录

```bash
# 编译（从 D:\core0\firmware，必须在 cmd.exe 或 PowerShell 中）
idf.py build

# 使用构建脚本
build_cmd.bat        # CMD 编译（自动设置 ESP-IDF 环境变量）
build_flash.ps1      # PowerShell 编译 + 烧录

# 直接烧录（绕过 idf.py）
cd build
python -m esptool --chip esp32s3 -p COM7 -b 460800 --before default-reset --after hard-reset \
  write-flash --flash-mode dio --flash-size 16MB --flash-freq 80m \
  0x0 bootloader/bootloader.bin \
  0x8000 partition_table/partition-table.bin \
  0x19000 ota_data_initial.bin \
  0x20000 core0_controller.bin

# 串口监视
idf.py monitor -p COM7

# 增量编译（Git Bash 环境）
export PATH="/c/Espressif/tools/python/v6.0.1/venv/Scripts:/c/Espressif/tools/xtensa-esp-elf/esp-15.2.0_20251204/xtensa-esp-elf/bin:$PATH"
cd D:/core0/firmware/build && ninja
```

### 5.2 .lf 编译器使用

```bash
# 编译 .lf → 路由表二进制
python compiler/lf_compiler.py program.lf -o program.bin

# 调试输出（JSON 格式）
python compiler/lf_compiler.py program.lf --json

# 发送到 ESP32（网络发送功能待实现）
python compiler/lf_compiler.py program.lf --send 192.168.1.100
```

### 5.3 MQTT 测试

```bash
# 使用 mosquitto_pub 发送指令
mosquitto_pub -h <broker_ip> -t "core0/cmd" -m '{"cmd":"get_status"}'
mosquitto_pub -h <broker_ip> -t "core0/cmd" -m '{"cmd":"set_setpoint","idx":2,"value":32.0}'
mosquitto_pub -h <broker_ip> -t "core0/cmd" -m '{"cmd":"trigger_estop"}'

# 订阅状态
mosquitto_sub -h <broker_ip> -t "core0/status"
mosquitto_sub -h <broker_ip> -t "core0/resp"

# OTA 固件服务
cd /var/www/html && python3 -m http.server 8080
```

---

## 六、已验证性能

以下数据来自 CCOUNT 软件示波器实测（240MHz, 10~64 路由）：

| 指标 | 实测值 | 目标 | 余量 |
|------|--------|------|------|
| ISR 扫描周期 | 100.00μs (24,000 cycles) | 100μs | ✅ |
| 周期抖动（64路由满载） | 12~87ns | <1,000ns | **11~83x** |
| ISR 执行（10路由） | 8.3~9.8μs (1994~2344 cyc) | <100μs | **10x** |
| ISR 执行（64路由满载） | 11.7~13.1μs | <100μs | **7.6x** |
| 软件 ESTOP 响应 | ≤100μs | <100μs | ✅ |
| 硬件 ESTOP 响应 | 1.5μs（目标） | <10μs | Phase 2 |
| 程序热切换（memcpy 2KB） | ~4μs | <5μs | ✅ |
| 参数修改生效 | <100μs | <100μs | ✅ |
| OTA 下载 934KB | ~14s (HTTP) | <60s | ✅ |
| 核心0崩溃对核心1 | 零影响 | 零影响 | ✅ |
| 双核竞争影响 | 0ns | 无 | ✅ |

**抖动 12ns 的含义**：12ns ÷ 100,000ns = 0.012%。相当于要求 1mm 精度做到 0.12μm 偏差。比工业 PLC 典型抖动（10~100μs）好 800~8000 倍。

---

## 七、未来路线图

### 7.1 高优先级

| 事项 | 说明 | 涉及文件 |
|------|------|---------|
| **PT100 + MAX31865** | 工业级温度传感器替换 DHT22，SPI2 接口 | 新增 SPI 驱动，修改 main.c 传感器任务 |
| **MCPWM 输出** | 支持高精度 PWM（电机/伺服控制） | output_driver.h |
| **PID 自动整定** | 阶跃响应法 / Ziegler-Nichols | core1_isr.c（新增诊断模式） |
| **ESTOP 硬件电路** | LM393 + 74HC08 面包板验证 | 底板原理图 |

### 7.2 中优先级

| 事项 | 说明 |
|------|------|
| **HTTPS OTA** | 替换 HTTP 明文传输，增加 TLS 证书校验 |
| **MQTT 认证** | Broker 用户名/密码（sdkconfig 中已预留配置项） |
| **运行时 WiFi 配网** | 支持运行时修改 SSID/密码（当前编译时写入） |
| **LUT_DATA .lf 语法** | 编译器支持 LUT 表数据定义（当前需手动填充） |
| **PC IDE** | lf-compile 图形界面 |

### 7.3 低优先级

| 事项 | 说明 |
|------|------|
| **I2S 音频告警** | MAX98357 喇叭输出（硬件已就绪） |
| **以太网支持** | ESP32-S3 通过 SPI 以太网 PHY |
| **多芯片级联** | CAN/RS-485 多核心0 协同 |
| **Modbus TCP/RTU** | 工业协议兼容 |

---

## 八、附录

### 附录A：项目文件清单

```
D:\core0\firmware\                     # ESP32-S3 固件
├── CMakeLists.txt
├── sdkconfig / sdkconfig.defaults
├── partitions.csv
├── build_cmd.bat / build_flash.ps1
├── main/
│   ├── CMakeLists.txt
│   ├── main.c                         # 入口：共享内存分配 → NVS → core1 → DHT22 → WiFi+MQTT
│   ├── dht22.c                        # DHT22 单总线驱动（GPIO11）
│   ├── wifi_mqtt.c                    # WiFi STA + MQTT 客户端 + 10 条指令处理
│   ├── wifi_mqtt.h
│   ├── ota_handler.c                  # HTTP 下载 → OTA 分区写入
│   ├── ota_handler.h
│   ├── nvs_store.h                    # NVS 程序存储（static inline, 无 .c 文件）
│   └── Kconfig.projbuild
├── components/
│   ├── core0/
│   │   ├── shared_mem.h               # 共享内存结构体 + _Static_assert + 偏移宏
│   │   ├── core1_isr.c                # 100μs 裸机 ISR 引擎
│   │   └── output_driver.h            # GPIO/LEDC 硬件输出驱动
│   └── cjson/                         # JSON 解析（idf-extra-components）
└── build/
    └── core0_controller.bin           # 编译产物 (~934KB)

D:\001项目\开发文件V2.0版本\核心0高精度控制方案\
├── 核心0-完整技术手册.md               # ← 本文档（权威）
├── CLAUDE.md                          # AI 上下文参考
├── 核心0技术白皮书.md                  # 产品级概述
├── 修正汇总.md                        # 18 项修正记录
├── 开发验证报告.md                     # 实测数据与验证结果
├── 决策框架.md                         # 架构决策记录
├── 开发执行计划.md                     # 21 步执行计划
├── compiler/
│   ├── lf_compiler.py                 # .lf 编译器 v2.0
│   └── test_program.lf                # 测试程序（9 路由，16 参数）
├── tests/                             # 原语测试框架（Python）
└── 技术架构.md / 实现方案.md / 开发规划.md  # 原始文档（部分过时）
```

### 附录B：参考对照表

| 旧称（已废弃） | 新称 | 说明 |
|--------------|------|------|
| `src_port` (uint16_t) | `src_type` + `src_index` | 支持 SENSOR/WIRE/CONST 三态 |
| `dst_reg` (unified offset) | `dst_type` + `dst_channel` | 类型分发替代统一偏移 |
| `SHARED_MEM_BASE = 0x3FCA0000` | `g_shared_mem` (heap_caps_malloc) | 动态分配替代硬编码 |
| `Cache_WriteBack_Addr` / `Invalidate` | Xtensa `MEMW` | 内部 DRAM non-cacheable |
| `CONFIG_FREERTOS_UNICORE=y` | `CONFIG_FREERTOS_NUMBER_OF_CORES=2` | SMP 双核（实际只用单核 FreeRTOS） |
| 17 原语 | 20 操作码（17 可用 + 1 保留 + 2 预留） | SCALE/AND/OR/NOT 新增 |
| NVS `program_N` blob | NVS `route_N` + `param_N` + `lut_N` | 分离存储 |
| OTA 在 MQTT 线程执行 | OTA 独立 FreeRTOS 任务 | 不阻塞 MQTT keepalive |

### 附录C：编译器已知限制

1. `--send` 网络发送功能未实现
2. LUT_DATA[256] 未从 .lf 源生成（需语法扩展）
3. 命名参数被原语引用时，两者各自占用 PARAM_TABLE 槽（命名参数槽可能冗余）
4. MUX 的 select 参数仅支持字面量，不支持 wire 名引用
5. 不支持 `connect` 语句的跨文件引用

---

> **核心0 不是一个"控制器产品"。它是一个"控制器的定义方式"。**
>
> 不用写 C 代码实现 PID——写 .lf 描述数据流，编译器将其编译为硬件逻辑。
> 不用下线设备改程序——改完文本下发，ISR 在 4 微秒内完成切换。
> 不需要示波器验证实时性——CCOUNT 在每台设备上运行，精度 4 纳秒。
>
> **这就是软件定义硬件。**
