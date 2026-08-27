# 核心0 — 修正汇总

> 版本：2.4.0 | 日期：2026-06-18
> **本文档是权威修正。当与其他文档冲突时，以本文档为准。**

---

## 修正总览

经过三轮评估（初次可行性评估 → 14条解决方案 → 本逻辑推演），共发现和修正了以下问题。原始文档（`技术架构.md`、`实现方案.md`）的部分内容已被这些修正覆盖。

---

## 🔴 架构级修正（5项）

### 修正1：输出抽象从统一偏移量改为类型分发

**原错误**：
```c
REG_WRITE(GPIO_BASE + ROUTE_TABLE[i].dst_reg, *(uint32_t*)&out);
// 一个偏移量试图覆盖 GPIO / LEDC / MCPWM 三种完全不同的寄存器空间
```

**修正**：RouteEntry_t 的 `dst_reg`(uint16_t) 拆为 `dst_type`(uint8_t) + `dst_channel`(uint8_t)。ISR 中按类型分发到不同外设寄存器。

**影响**：`shared_mem.h`、ISR 输出代码、编译器路由表生成。结构体总大小不变（16字节）。

---

### 修正2：传感器从 DHT22+ADC 改为 PT100+MAX31865+SPI

**原错误**：
```
DHT22 → ADC → DMA → SENSOR_MAP
```
三重错误：DHT22 是数字单总线传感器，不能接 ADC；DMA 在 ISR 中不可控；SENSOR_MAP 权限标反。

**修正**：
```
PT100(四线) → MAX31865 → SPI2 → 物理核心0 FreeRTOS任务(100ms) → 写入 SENSOR_MAP → 物理核心1 ISR 读取
```
- SENSOR_MAP 权限改为：物理核心0 写，物理核心1 读
- SPI2 由物理核心0 独占，ISR 不碰 SPI
- 转换时间 ~21ms，CPU 占用 ~21%（100ms 间隔）

---

### 修正3：急停从"芯片内置比较器"改为外部 LM393 双重冗余

**原矛盾**：文档同时说"硬件比较器直驱"和"ISR判断GPIO"。

**修正**：
```
ESTOP_IN → LM393 → 74HC08与门 → 安全继电器线圈
              ↑         ↑
          TL431(1.25V) GPIO_OUT(正常控制)
```
- 硬件路径：LM393(1.3μs) + 74HC08(10ns) ≈ 1.5μs，**独立于ESP32**
- 软件备份：ISR检测 ESP_ESTOP_GPIO → 强制所有安全输出拉低
- 符合 IEC 61508 SIL2（独立硬件安全路径）
- 安全继电器线圈**必须**加续流二极管(1N4148)

---

### 修正4：CRC32 从 ISR 移至下载端

**原错误**：ISR 热切换路径中调用 `compute_crc32()`：
- 纯软件 CRC32 1KB 约需 50-80μs，超出 5μs 预算 10 倍以上
- 下载时已经校验过，重复计算无意义

**修正**：
- ISR：删除 CRC32，只做 memcpy(~4μs)
- 物理核心0 下载时：`esp_crc32_be()` 硬件 CRC 加速器（~300μs）校验
- 启动加载：NVS 读完后也做一次 CRC32，失败则回退到前一个 slot 或空白安全态

---

### 修正5：裸机架构方案B（交换物理核心角色）

**原方案**：物理核心0 裸机 ISR + 物理核心1 FreeRTOS → 需要 hack ESP-IDF 启动流程。

**修正方案B（推荐）**：
- `CONFIG_FREERTOS_UNICORE=y`：FreeRTOS 只在 PRO_CPU（物理核心0）
- APP_CPU（物理核心1）跑裸机 ISR，通过 `magic` 标记等待共享内存就绪
- 命名不变："核心0"仍是控制核心的品牌名

**理由**：ESP-IDF 官方支持 unicore 模式，不需要 override 内部函数，实现更简单可靠。

---

## 🔴 功能性 Bug（3项）

### 修正6：PID 微分项符号错误 ⚠️ 最严重

**原代码**：
```c
float d_term = kd * (STATE_TABLE[si].state_b - err);
// state_b = 上次误差
// 实际计算：Kd × (err_prev - err_curr)  ← 符号反了！
```

**推演验证**：
```
场景：目标26°C，温度从25掉到24
  err_prev = 26-25 = 1, err_curr = 26-24 = 2
  误差增大(更偏离)→微分项应为正(加大输出)
  正确: Kd×(2-1) = +Kd ✓
  原bug: Kd×(1-2) = −Kd  ← 反而减小输出，主动放大超调
```

**修正**：
```c
float d_term = kd * (err - STATE_TABLE[si].state_b);
// 标准形式: Kd × (err_curr − err_prev)
// 适用于 derivative-on-error 和 derivative-on-measurement 两种形式
```

---

### 修正7：NVS 加载缺少程序级 CRC 校验

**原遗漏**：`nvs_load_program()` 只依赖 NVS 层的 blob 校验，不验证程序语义 CRC。如果 NVS 数据位翻转（低概率但可能），核心0 会盲目执行损坏的路由表。

**修正**：
```c
esp_err_t nvs_load_program(uint8_t slot, void* route_dst, void* param_dst) {
    // 读 blob
    nvs_get_blob(h, key, buffer, &sz);
    // 程序级 CRC32 校验
    uint32_t computed = esp_crc32_be(0, buffer, sz - 4);
    uint32_t stored   = *(uint32_t*)(buffer + sz - 4);
    if (computed != stored) {
        return ESP_ERR_INVALID_CRC;  // 触发 slot 回退
    }
    // 校验通过，加载
}
```

回退链：program_N CRC失败 → program_(N-1) → ... → program_0 → 空白路由表（安全态）。

---

### 修正8：ACTUATOR_STATUS 索引

**原代码**：
```c
ACTUATOR_STATUS[ROUTE_TABLE[i].dst_reg & 0x1F] = out;
```
在 `dst_reg` 拆分为 `dst_type` + `dst_channel` 后，这个 `& 0x1F` 不再有语义。

**修正**：`ACTUATOR_STATUS` 的索引应统一为路由表项的序号 `i`，或者定义一个新的 actuator_index 字段。最简单方案：用 actuator_index（uint8_t），由编译器在生成路由表时分配。

---

## 🟡 数据/参数修正（4项）

### 修正9：内存布局尺寸

| 区域 | 原文档 | 实际 | 修正 |
|------|--------|------|------|
| ROUTE_TABLE[64] | 4KB | 1KB (16B×64) | 偏移改为 0x0500 |
| ROUTE_STAGING[64] | 4KB | 1KB | 偏移改为 0x0900 |
| memcpy 时间 | ~13μs | ~4μs (2KB) | 性能指标更新 |

新增区域：
| 0x1680 | 1KB | LUT_DATA[256] | OP_LUT 查找表数据 |

---

### 修正10：SharedCtrl 结构体新增字段

```c
float    isr_period_s;        // ISR 周期(秒)，替代 RATE 硬编码 dt=0.0001f
uint32_t sensor_fault_flags;  // 传感器故障位掩码，物理核心0 监控用
```

---

### 修正11：PID 积分限幅改为可配置

**原硬编码**：积分项 ±50 限幅。

**修正**：在 `ParamEntry_t` 新增 `value_e` 字段（通用第五参数，20字节）或复用相邻参数槽。具体方案在 Phase 2 编码时选择。

---

### 修正12：RATE dt 从控制块读取

```c
// 原硬编码
float dt = 0.0001f;
// 修正
float dt = SHARED_CTRL->isr_period_s;
```

---

## 🟢 细节补充（3项）

### 补充1：LUT/CNT/TIMER 三条原语完整实现

见本文档末尾附录A。

### 补充2：传感器故障安全值

ISR 读取 SENSOR_MAP 后立即钳位：
```c
if (src < TEMP_MIN_VALID || src > TEMP_MAX_VALID) {
    src = DEFAULT_SAFE_VALUE;  // 可配置，如 25°C
    SHARED_CTRL->sensor_fault_flags |= (1 << port);
}
```

### 补充3：LM393 传播延迟纠正

LM393 大信号传播延迟典型值是 **1.3μs**（不是 <100ns）。硬件急停总延迟约 **1.5μs**（LM393 1.3μs + 74HC08 10ns），仍远优于 <10μs 的设计目标。

---

## 📊 对开发计划的影响

| 修正项 | 影响范围 | 额外工期 |
|--------|---------|---------|
| 输出抽象重构 | shared_mem.h, ISR, 编译器 | +0.5天 |
| 传感器方案 | 硬件BOM, SPI驱动, SENSOR_MAP权限 | +1天 |
| 急停电路 | 底板原理图 | 原理图预算内 |
| CRC移出ISR | 删ISR代码, nvs_loader加校验 | +0.5天 |
| 内存布局修正 | 宏定义 | +0.1天 |
| 裸机架构方案B | 启动流程重写 | 可能还快1天 |
| PID符号+其他bug | ISR代码 | +0.5天 |
| **合计** | | **约+2.5天** |

---

## ⚠️ 仍需验证的事项（Phase 1 必须做）

| # | 事项 | 验证方法 |
|---|------|---------|
| 1 | 共享内存基址 `0x3FCA0000` 可用性 | 对照 ESP32-S3 TRM，逻辑分析仪实测 |
| 2 | LEDC/MCPWM 寄存器偏移公式 | 对照 TRM + 单元测试 |
| 3 | Float 32位写的原子性 | 汇编审查（确认 `s32i` 指令） |
| 4 | ESP-IDF unicore 模式核心1启动 | 示波器测启动延迟 |
| 5 | SRAM Bank 分配（避免双核竞争） | 链路文件审查 |
| 6 | `.lf` 编译器寄存器分配策略 | 中端设计文档（尚未开始） |

---

## 附录A：LUT/CNT/TIMER 完整实现

```c
// OP_LUT: 线性插值查找表
case OP_LUT: {
    float idx  = src;
    int   i0   = (int)idx;
    float frac = idx - (float)i0;
    uint16_t base = (uint16_t)PARAM_TABLE[pi].value_a;  // LUT_DATA 基索引
    uint16_t len  = (uint16_t)PARAM_TABLE[pi].value_b;  // 表格长度
    if (i0 < 0) { out = LUT_DATA[base]; break; }
    if (i0 >= len - 1) { out = LUT_DATA[base + len - 1]; break; }
    float v0 = LUT_DATA[base + i0];
    float v1 = LUT_DATA[base + i0 + 1];
    out = v0 + (v1 - v0) * frac;
    break;
}

// OP_CNT: 边沿触发计数器
case OP_CNT: {
    uint8_t edge_type = (uint8_t)PARAM_TABLE[pi].value_a;  // 0=上升沿, 1=下降沿
    float reset_val   = PARAM_TABLE[pi].value_b;            // 复位值
    uint8_t rising  = (src > 0.5f && STATE_TABLE[si].state_b <= 0.5f);
    uint8_t falling = (src < 0.5f && STATE_TABLE[si].state_b >= 0.5f);
    if ((edge_type == 0 && rising) || (edge_type == 1 && falling)) {
        STATE_TABLE[si].state_a += 1.0f;
    }
    // 复位：src > reset_val 时清零（可选特性）
    if (reset_val > 0.0f && src > reset_val) {
        STATE_TABLE[si].state_a = 0.0f;
    }
    STATE_TABLE[si].state_b = src;
    out = STATE_TABLE[si].state_a;
    break;
}

// OP_TIMER: 可复位定时器
case OP_TIMER: {
    float duration = PARAM_TABLE[pi].value_a;  // 定时时长（秒）
    float dt       = SHARED_CTRL->isr_period_s;
    if (src > 0.5f) {
        // 启动信号有效：累加计时
        STATE_TABLE[si].state_a += dt;
        if (STATE_TABLE[si].state_a >= duration) {
            STATE_TABLE[si].state_b = 1.0f;   // 定时到期
        } else {
            STATE_TABLE[si].state_b = 0.0f;   // 尚未到期
        }
    } else {
        // 启动信号撤销：全部复位
        STATE_TABLE[si].state_a = 0.0f;
        STATE_TABLE[si].state_b = 0.0f;
    }
    out = STATE_TABLE[si].state_b;
    break;
}
```

---

## 🟢 实现阶段修正（2026-06-17）

### 修正13：共享内存分配方式 — 从硬编码地址改为堆分配

**问题**：原始设计使用硬编码 `SHARED_MEM_BASE = 0x3FCA0000`。该地址落在 ESP-IDF 内部 DRAM 堆范围（0x3FC88000~0x3FD00000）内。GPTimer 驱动初始化时 `malloc()` 返回的内存地址与共享内存重叠，覆盖 ROUTE_TABLE/PARAM_TABLE 数据。

**症状**：
- ISR 读取 ROUTE_TABLE 得到垃圾值（flags=184 而非 0x01）
- WIRE_MAP 全部为 0.000
- PID 输出天文数字

**修正**：用 `heap_caps_malloc(MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL, 64KB)` 动态分配共享内存。基址指针 `g_shared_mem` 在 `app_main()` 开头设置。堆管理器保证该块独占，不会被 `.data`/`.bss`/其他 `malloc` 覆盖。

**影响文件**：`shared_mem.h`, `main.c`

---

### 修正14：跨核同步机制 — MEMW 替代 ROM 缓存 API

**问题**：ESP32-S3 内部 DRAM (D-bus, 0x3FC88000~0x3FD00000) 本身 non-cacheable——D-Cache 仅处理 PSRAM（0x3C000000~0x3E000000）。代码中使用的 `Cache_WriteBack_Addr()`/`Cache_Invalidate_Addr()` 对内部 DRAM 地址是 NO-OP。此外，ESP32-S3 ROM 的 `Cache_WriteBack_Addr` 有已知 bug（`CONFIG_ESP_ROM_HAS_CACHE_WRITEBACK_BUG=y`）。

`main.c:176` 的 `Cache_Invalidate_Addr(SHARED_MEM_BASE, 64KB)` 尤其危险：Invalidate 直接丢弃脏缓存行不做 WriteBack，可能导致数据丢失。

**修正**：移除所有 ROM 缓存 API 调用（`#include "rom/cache.h"`），改用 Xtensa `MEMW` 指令排空写缓冲。在以下位置添加 `__asm__ volatile("memw")`：
- `core1_init()` 的 `build_test_routes()` 之后
- ISR 末尾（心跳++后）
- DHT22 任务写 SENSOR_MAP 后
- param_tuner_task 写 PARAM_TABLE 后
- hot_switch_task 写暂存区后

**影响文件**：`core1_isr.c`, `main.c`

---

### 修正15：PID 参数 PARAM[2].value_c 值

**原设计**：`PARAM_TABLE[2].value_c = 0.05f`（Kd）

**实际代码**：`build_test_routes()` 中写入 `PARAM_TABLE[2].value_c = 0.1f`（与 value_b 相同）。差异不影响当前功能（PID D-term = 0 × value_c），但需在后续 PID 整定时统一。

---

## 附录B：验收检查清单

在 Phase 1 结束前，以下条件必须全部满足：

- [x] 物理核心0 写 SENSOR_MAP → 物理核心1 ISR 读到正确值（CCOUNT验证 ✓）
- [x] PARAM_TABLE 在线修改 → 下一 ISR 周期生效（串口验证 ✓）
- [x] 程序热切换 → PWM 输出无毛刺（串口验证 ✓）
- [ ] ESTOP 按钮按下 → 安全继电器在 <2μs 内断开（逻辑分析仪）
- [x] 物理核心0 重启 → 物理核心1 PWM 输出不变（验证通过）
- [x] PID 阶跃响应方向正确（设定值↑ → 输出↑）
- [x] NVS 空芯片上电 → 所有 GPIO 输出 = 安全值
- [ ] LEDC/MCPWM 寄存器公式验证通过（单元测试）
- [x] WiFi STA 连接 + MQTT 状态上报（500ms 持续上报 ✓）
- [x] MQTT 指令接收与响应（10条指令全部可用 ✓）
- [x] OTA 固件升级端到端（HTTP下载→写入→重启→恢复 ✓）
- [x] OTA 期间 ISR 持续运行不中断（heartbeat 连续 ✓）
- [x] .lf 编译器 v2.0（拓扑排序+参数修正+CRC32+状态槽优化 ✓）
- [x] 软件急停（GPIO4+ISR锁定+MQTT trigger/reset ✓）
- [x] CCOUNT 性能验证（10路由 8.3~9.9μs, 9.9%预算 ✓）

已完成：10/12

---

## 🟢 实现阶段修正 — 续（2026-06-18）

### 修正16：OTA 从 MQTT 事件线程阻塞调用改为独立任务执行

**问题**：`handle_ota_start()` 在 MQTT 事件回调线程中直接调用阻塞的 `ota_start()`：
```c
// wifi_mqtt.c:238 — 原始代码
esp_err_t err = ota_start(url);  // HTTP下载+Flash写入，阻塞10~60秒
if (err == ESP_OK) { esp_restart(); }
```

HTTP 下载 934KB 固件需 10~60 秒（取决于 WiFi 速度），期间：
- MQTT keepalive ping 无法发送 → Broker 超时断开客户端
- 若 HTTP 中途 TCP 超时，`esp_http_client_read()` 永久阻塞整个 MQTT 协议栈
- `esp_restart()` 前仅 1 秒延迟，Flash 内部缓存可能未完全写入物理 Flash
- OTA 失败时 `ota_start()` 返回错误，但 MQTT 客户端可能已被 Broker 断开，`publish_response()` 无效

**修正**：将 OTA 执行移入独立 FreeRTOS 任务：
```c
// wifi_mqtt.c — 修正后
typedef struct { char url[256]; } ota_task_params_t;

static void ota_task(void *arg) {
    ota_task_params_t *params = (ota_task_params_t *)arg;
    esp_err_t err = ota_start(params->url);
    if (err == ESP_OK) {
        publish_response("{\"ok\":\"ota_done\",\"msg\":\"rebooting...\"}");
        vTaskDelay(pdMS_TO_TICKS(2000));  // 等 Broker ACK + Flash 落盘
        esp_restart();
    } else {
        char buf[128];
        snprintf(buf, sizeof(buf), "{\"err\":\"ota_failed\",\"code\":%d}", err);
        publish_response(buf);
    }
    free(params);
    vTaskDelete(NULL);
}

static void handle_ota_start(const cJSON *cmd) {
    // ... 解析 URL ...
    publish_response_obj(resp);  // 立即返回 "ota_started"
    ota_task_params_t *params = malloc(sizeof(ota_task_params_t));
    strncpy(params->url, url, sizeof(params->url) - 1);
    xTaskCreatePinnedToCore(ota_task, "ota_task", 8192, params,
                            configMAX_PRIORITIES - 2, NULL, 0);
}
```

**关键改进**：
| 项目 | 修正前 | 修正后 |
|------|--------|--------|
| MQTT 事件线程 | 阻塞 10~60s | 立即返回 |
| MQTT keepalive | 中断 | 持续发送，Broker 稳定 |
| status 上报 | OTA 期间中断 | 不中断（heartbeat 连续 5,231K→5,375K） |
| 重启前延迟 | 1 秒 | 2 秒（确保 Flash 缓存落盘） |
| OTA 失败处理 | MQTT 可能已断开 | MQTT 保持连接，错误可上报 |

**OTA 错误码参考**（ESP-IDF 定义）：

| 错误码 | 宏 | 含义 |
|--------|-----|------|
| 0x7002 | ESP_ERR_HTTP_CONNECT | HTTP 连接失败（服务器不可达/防火墙） |
| 0x7005 | ESP_ERR_HTTP_CONNECTION_CLOSED | 下载中途连接断开 |
| ESP_ERR_INVALID_SIZE | — | 固件超出分区大小（ota_0 最大 2MB） |

**验证**（2026-06-18）：
- OTA 端到端测试通过：MQTT 指令 → HTTP 下载 934KB → 写入 ota_0 → 重启 → <5s WiFi+MQTT 恢复
- OTA 期间 heartbeat 从 5,231,510 连续增长到 5,375,130（无间断）
- 重启后 heartbeat 归零（61,647），state 从 blank→running，全部正常

**影响文件**：`wifi_mqtt.c`（新增 `#include <stdlib.h>`、`ota_task_params_t` 结构体、`ota_task()` 函数、修改 `handle_ota_start()`）

---

### 修正17：`.lf` 编译器 v2.0 — 拓扑排序 + 参数传递修复 + 状态槽优化

**发现日期**：2026-06-18

**问题**：
1. **参数传递 bug**：`pid(kp=pid_kp, ...)` 中引用的命名参数值在 `_get_val()` 返回 `default` 而非实际值，导致 PID 拿到 (1.0, 0.0, 0.0, 0.0) 而非用户声明的 (2.0, 0.1, 0.05, 28.0)
2. **无拓扑排序**：ISR 顺序执行路由表，若生产者路由在消费者之后，消费者读到上周期的旧值
3. **State 槽位浪费**：所有原语都分配 STATE_TABLE 槽（包括无状态的 DIRECT, CMP, CLAMP, SCALE, AND, OR, NOT）
4. **AND/OR wire 引用未延迟解析**：`and(b=rate_hi)` 若 `rate_hi` 尚未分配线号，pb 被设为 1.0（静默错误）
5. **无 CRC32 / 无容量校验**

**修正**：编译器从 361 行重构为 ~550 行，实现：

1. **参数传递修复**：`_get_val()` 对 `('param', idx)` 类型查找 `self.params` 中的实际值
2. **拓扑排序**：Kahn 算法构建 DAG → 检测循环依赖 → 确保生产者先于消费者
   - wire producer 唯一性检查（`_writes_wire` 去重）
   - AND/OR 的 B 输入端（`_b_wire_name`）纳入依赖图
3. **状态槽优化**：`STATEFUL_OPS = {HYST, LPF, PID, RATE, DEADBAND, EDGE, CNT, TIMER}` 集合，无状态原语 `state_offset=0`
4. **Wire 引用延迟解析**：`_resolve_wire_refs()` 在所有 parse 完成后回填 AND/OR 的 `param_values.value_b`
5. **CRC32-BE**：多项式 0x04C11DB7，与 ESP32 `esp_crc32_be()` 一致
6. **验证**：路由/参数/线/状态容量检查 + 未定义引用检查 + 循环检测

**修正前后路由表对比**（`test_program.lf`）：

| 指标 | 修正前 | 修正后 |
|------|--------|--------|
| PID 参数 | (1.0, 0.0, 0.0, 0.0) | (2.0, 0.1, 0.05, 28.0) ✅ |
| State 槽位数 | 9（全部） | 4（仅 LPF×2, PID, RATE） |
| 路由顺序 | 声明的顺序（可能错误） | 拓扑排序保证生产者优先 |
| AND b= 线号 | 1.0（错误） | 6.0（rate_hi 的 wire 索引） |
| CRC32 | 无 | 0xFB4C3086 |
| 循环检测 | 无 | 3 路由循环正确报错 |

**验证**（2026-06-18）：
- `test_program.lf` 编译通过：9 路由, 16 参数, 7 线, 4 状态, CRC32=0xFB4C3086
- 复杂测试（双通道 PID + AND/OR 逻辑）：12 路由, 20 参数, 8 线, 4 状态, 编译通过
- 循环检测测试：3 路由循环正确报错
- 未定义 wire 引用正确报错
- 54 个 Python 原语单元/模糊测试全部通过

**影响文件**：`compiler/lf_compiler.py`（完整重构）

---

### 修正18：软件急停 (ESTOP) — GPIO 直读 + 锁定 + MQTT 控制

**发现日期**：2026-06-18

**背景**：原型阶段暂不搭建 LM393+74HC08 硬件急停电路，先用软件实现完整的急停逻辑。

**实现**：

1. **GPIO 配置**：ESTOP_BUTTON = GPIO4（输入，内部下拉，按下=HIGH）
2. **ISR 急停检查**（S2.5 段，路由处理之前）：
   - 读 GPIO4 → 若 HIGH 则置 `estop_latched = true`
   - 锁定状态下：`GPIO_OUT_W1TC_REG ← estop_gpio_mask`（单指令清零所有安全 GPIO）
   - LEDC 占空比 → 0，ACTUATOR_STATUS 全部归零
   - 跳过路由处理，仍喂 WDT + 心跳
   - 响应时间 ≤100μs（ISR 周期内）
3. **安全 GPIO 掩码自动构建**：扫描 ROUTE_TABLE 中所有 `dst_type == DST_GPIO` 的路由，构建位掩码。程序热切换时自动重建。
4. **锁定模式**（`ESTOP_LATCHING=1`）：按下后锁定，需显式复位。
5. **MQTT 接口**：
   - `{"cmd":"trigger_estop"}` — 软件触发（无需物理按钮）
   - `{"cmd":"reset_estop"}` — 复位锁定
   - 状态上报新增 `"estop":true/false` 字段
6. **跨核安全**：`estop_latched` 声明为 `volatile bool`，`core1_estop_reset()` 写后执行 `MEMW` 屏障。

**验证**（2026-06-18，MQTT 远程测试）：
| 测试项 | 结果 |
|--------|------|
| `trigger_estop` → `"estop":true` | ✅ |
| 急停期间输出归零 (`actuator_0=0`) | ✅ |
| 心跳持续递增（WDT 正常） | ✅ 371555→406555 |
| ISR 抖动（急停期间） | 475ns（极低，仅 GPIO 检查） |
| `reset_estop` → `"estop":false` | ✅ |
| 复位后恢复正常控制 | ✅ |

**影响文件**：`core1_isr.c`（+60行）、`shared_mem.h`（+5行）、`wifi_mqtt.c`（+14行）
