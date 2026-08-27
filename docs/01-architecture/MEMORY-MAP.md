# DTCM Memory Layout — 系统引擎 vs 运行程序

**版本**: v2.0 (明确系统引擎/运行程序分离)
**日期**: 2026-07-16
**目标**: STM32H723ZG DTCM (128KB @ 0x20000000, zero-wait)

---

## 设计原则

| 原则 | 说明 |
|------|------|
| **系统引擎只读** | 固件启动后引擎代码不修改，仅部署程序区可变 |
| **区域硬隔离** | 引擎区与程序区物理地址不重叠，MPU 可选保护 |
| **版本兼容** | 引擎区 struct 布局固定，程序区可独立演进 |
| **单一定义** | 所有地址常量在 `Inc/memory_map.h` 一处定义 |

---

## 总览

```
0x20000000 ┌──────────────────────────┐
           │    ENGINE REGION (引擎区)   │  固定，不通过部署修改
           │    System Engine           │
0x20001700 ├──────────────────────────┤
           │    PROGRAM REGION (程序区)   │  部署时整体覆写
           │    Running Program          │
0x20008800 ├──────────────────────────┤
           │    RTT / Diagnostics        │  引擎区内
0x2000D000 ├──────────────────────────┤
           │    LOG / ALARM / REC        │  引擎区内
0x2000E000 ├──────────────────────────┤
           │    SCRATCH / Stack          │  引擎区内
0x20010000 └──────────────────────────┘ DTCM 末尾 (128KB)
```

---

## 详细布局

### A. 系统引擎区 (ENGINE REGION) — 0x20000000 ~ 0x200016FF

这部分由固件源码直接管理，**部署程序时不涉及**。

#### A0. TIMING — 0x20000000 (64B)

| Offset | 名称 | 类型 | 用途 |
|--------|------|------|------|
| 0x00 | `ENGINE_MAGIC` | uint32 | 魔术字 `0xENG1N301`，标记引擎已初始化 |
| 0x04 | `ENGINE_STATE` | uint32 | 运行状态：`IDLE`/`RUNNING`/`PAUSED`/`ERROR` |
| 0x08 | `PERIOD_MIN` | uint32 | ISR 周期最小值 (DWT 周期) |
| 0x0C | `PERIOD_MAX` | uint32 | ISR 周期最大值 |
| 0x10 | `SAMPLES` | uint32 | ISR 执行计数 |
| 0x14 | `CYCLES` | uint32 | 总周期计数 |
| 0x18 | `EXEC_MIN` | uint32 | ISR 执行时间最小值 |
| 0x1C | `EXEC_MAX` | uint32 | ISR 执行时间最大值 |
| 0x20 | `FRAME_IDX` | uint32 | 当前帧索引 |
| 0x24 | `MARKER1` | uint32 | 调试标记 |
| 0x28 | `MARKER2` | uint32 | 调试标记 |
| 0x2C~0x3F | reserved | — | 保留 |

#### A1. N_ENGINE — 0x20000040 (16B)

| Offset | 名称 | 类型 | 用途 |
|--------|------|------|------|
| 0x40 | `N_ROUTES` | uint32 | **当前程序路由数**（ISR 读取） |
| 0x44 | `N_PARAMS` | uint32 | 当前程序参数数 |
| 0x48 | `N_STATES` | uint32 | 当前程序状态槽数 |
| 0x4C | `PROGRAM_MAGIC` | uint32 | 程序有效标记 `0xPR0G` |

> ⚠️ **关键变更**：N_ROUTES 从 `0x00F0` 移到 `0x0040`，**不再与 ADC_RAW 冲突**。

#### A2. ADC / Sensor — 0x20000050 ~ 0x200000FF (176B)

| Offset | 名称 | 类型 | 用途 |
|--------|------|------|------|
| 0x50~0x5F | reserved | — | 保留 |
| 0x60~0x6F | `ADC_RAW[4]` | uint32×4 | ADC DMA 目标 (12-bit × 4ch) |
| 0x70~0x7F | `ADC_DMA_STATUS` | uint32 | DMA 状态/错误 |
| 0x80~0xFF | reserved | — | 保留 |

> ⚠️ ADC_RAW 从 `0x00F0` 移到 `0x0060`，避开 N_ENGINE。

#### A3. SHADOW_GPIO — 0x200000E0 (4B)

| Offset | 名称 | 类型 | 用途 |
|--------|------|------|------|
| 0xE0 | `SHADOW_GPIO` | uint32 | GPIOE 输出影子寄存器 (DMA 搬运源) |

#### A4. ACTUATOR_STATUS — 0x20000200 (256B)

| Offset | 名称 | 类型 | 用途 |
|--------|------|------|------|
| 0x0200 | `ACTUATOR_STATUS[64]` | float32×64 | 执行器输出值，扫描用于 PWM/GPIO 输出 |

#### A5. SENSOR_MAP — 0x20000100 (256B)

| Offset | 名称 | 类型 | 用途 |
|--------|------|------|------|
| 0x0100 | `SENSOR_MAP[64]` | float32×64 | ADC 转换后的传感器值 |

#### A6. WIRE_MAP — 0x20000300 (4KB)

| Offset | 名称 | 类型 | 用途 |
|--------|------|------|------|
| 0x0300 | `WIRE_MAP[1024]` | float32×1024 | 内部信号线 (引擎↔程序共享) |

> ⚠️ WIRE_MAP 属于引擎区，但程序路由表中的 src/wire 索引引用它。

#### A7. LUT_DATA — 0x20001300 (1KB)

| Offset | 名称 | 类型 | 用途 |
|--------|------|------|------|
| 0x1300 | `LUT_DATA[256]` | float32×256 | 引擎内置查找表 |

---

### B. 运行程序区 (PROGRAM REGION) — 0x20001700 ~ 0x200087FF

这部分**完全由部署覆写**，引擎只读取不修改。

#### B0. Program Header — 0x20001700 (16B)

| Offset | 名称 | 类型 | 用途 |
|--------|------|------|------|
| 0x00 | `PROGRAM_MAGIC` | uint32 | `0xPR0G` 标记有效程序 |
| 0x04 | `FORMAT_VERSION` | uint32 | 格式版本号 |
| 0x08 | `N_ROUTES` | uint32 | 路由条目数 (与 N_ENGINE 同步) |
| 0x0C | reserved | uint32 | 保留 |

#### B1. Route Table — 0x20001710 (16KB)

| Offset | 名称 | 类型 | 用途 |
|--------|------|------|------|
| 0x1710 | `ROUTE_TABLE[N]` | RouteEntry_t×N | 路由条目 (N ≤ 1024, 16B/entry) |

**RouteEntry_t 布局 (16 bytes, packed):**
```
[0]   src_type     uint8   // 0=SENSOR, 1=WIRE, 2=CONST
[1]   src_index    uint8
[2]   dst_type     uint8   // 3=WIRE
[3]   dst_channel  uint8
[4]   op           uint8   // opcode
[5]   flags        uint8   // bit0=enabled
[6-7] param_idx    uint16  // LE
[8-9] state_offset uint16  // LE
[10-11] actuator_idx uint16 // 0=wire-only, 1-4=PWM, 32-63=GPIO
[12-13] wire2_idx  uint16  // LE
[14-15] padding    uint16  // 对齐
```

#### B2. Param Table — 0x20005710 (8KB)

| Offset | 名称 | 类型 | 用途 |
|--------|------|------|------|
| 0x5710 | `PARAM_TABLE[N]` | ParamEntry_t×N | 参数条目 (N ≤ 512, 16B/entry) |

**ParamEntry_t 布局 (16 bytes):**
```
[0-3]   value_a  float32
[4-7]   value_b  float32
[8-11]  value_c  float32
[12-15] value_d  float32
```

#### B3. State Table — 0x20007710 (4KB)

| Offset | 名称 | 类型 | 用途 |
|--------|------|------|------|
| 0x7710 | `STATE_TABLE[N]` | StateEntry_t×N | 状态条目 (N ≤ 256, 16B/entry) |

**StateEntry_t 布局 (16 bytes):**
```
[0-3]   state_a  float32  // 积分器/上次值
[4-7]   state_b  float32  // 定时器累计/计数器
[8-11]  state_c  float32  // 限幅状态
[12-15] state_d  float32  // 保留
```

---

### C. 诊断/通信区 — 0x20008800 ~ 0x2000DFFF

#### C0. RTT Block — 0x20008800 (2KB)

| Offset | 名称 | 用途 |
|--------|------|------|
| 0x8800 | RTT Control Block | SEGGER RTT CB (16B 头 + channel 描述) |
| 0x8900 | RTT UP0 Buffer | 目标→主机 (1024B) |
| 0x8A00 | RTT DOWN0 Buffer | 主机→目标 (16B) |

#### C1. LOG_RING — 0x2000D000 (4KB)

| Offset | 名称 | 用途 |
|--------|------|------|
| 0xD000 | LOG_RING[128] | 128 条目环形日志 (每条 32B) |

#### C2. ALARM_BUF — 0x2000D800 (2KB)

| Offset | 名称 | 用途 |
|--------|------|------|
| 0xD800 | ALARM_BUF[128] | 告警环形缓冲 (每条 8B) |

#### C3. REC_BUF — 0x2000E000 (8KB)

| Offset | 名称 | 用途 |
|--------|------|------|
| 0xE000 | REC_BUF | 抖动测量环形缓冲 |

---

## 部署协议

### 通过 SWD (pyOCD) 部署

```
1. IDE 发送 compile → 编译器输出 binary
2. IDE 发送 deploy:
   a. 写 N_ENGINE.N_ROUTES = 0  (暂停引擎扫描)
   b. 等待 ≥100μs (确保 ISR 看到 0)
   c. 写 PROGRAM 区 (Header + RouteTable + ParamTable + StateTable)
   d. 写 N_ENGINE.N_ROUTES = N  (恢复引擎扫描)
   e. 写 N_ENGINE.PROGRAM_MAGIC = 0xPR0G
3. 引擎 ISR 下一轮自动使用新路由表
```

### 通过 UART 部署 (handle_deploy)

```
1. PC 发送 CMD_DEPLOY 帧 (0xC0 0x10 len payload crc)
2. 固件 handle_deploy():
   a. N_ENGINE.N_ROUTES = 0
   b. 解析 payload → RouteTable/ParamTable/StateTable
   c. N_ENGINE.N_ROUTES = n_routes
   d. PROGRAM_MAGIC = 0xPR0G
   e. 发送 ACK
```

---

## 关键变更 vs 当前固件

| 项目 | 当前 (v1.8) | 目标 (v2.0) |
|------|-------------|-------------|
| N_ROUTES 地址 | 0x200000F0 (与 ADC_RAW 冲突) | 0x20000040 (独立) |
| ADC_RAW 地址 | 0x200000F0 | 0x20000060 |
| 路由表地址 | 0x20001700 | 0x20001710 (加 16B header) |
| 程序区起始 | 无 header | 0x20001700 (Program Header) |
| 引擎/程序分离 | 混在一起 | 明确分离 |
| 部署时引擎状态 | 不停止 | N_ROUTES=0 暂停 |

---

## 兼容性说明

- **引擎区 struct 布局固定**：一旦发布，字段偏移不变
- **程序区格式版本化**：`FORMAT_VERSION` 允许未来扩展
- **WIRE_MAP 共享**：引擎区包含 WIRE_MAP，程序通过 wire_index 引用
- **SENSOR_MAP 共享**：引擎区包含 SENSOR_MAP，程序通过 sensor_index 引用
