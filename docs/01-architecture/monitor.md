# 在线监视器设计文档

> **版本**: v1.0 | **日期**: 2026-07-15
> **目标**: 用 UART 取代 pyocd halt,零侵入地监视引擎运行

---

## 0. 问题与原则

**为什么不用 pyocd?**
pyocd halt 会使 CPU 暂停、AHB 总线锁住,DMA 搬被推迟,下一个 ISR 间隔异常 —— halt 本身就是抖动源之一。用它观测"系统真实抖动"就像用体温计量体温时把人推进冰柜。

**为什么是 UART?**
Nucleo 板载 ST-Link 通过同一根 USB 线提供了 VCP (Virtual COM Port),PD5/PD6 已经在板上连到 ST-Link 的 USB-UART 桥,不需要加线。DMA Stream2 自动把 UART 收到的字节搬到 buffer,主循环慢速消费,ISR 全程不参与 —— 引擎不知道自己被"读了"。

**核心原则: PC 端只读缓冲区里已有的数据。引擎不因被监测而多执行一行指令。**

---

## 1. 架构

```
IDE / CLI (PC)
        │
        ▼
   UART (1 Mbps, 8N1, ST-Link VCP)
   CMD 帧: [0xC0][CMD:1B][LEN:LE u16][PAYLOAD][CRC16:LE u16]
   STS 帧: [0xC1][STS:1B][LEN:LE u16][PAYLOAD][CRC16:LE u16]
        │
        ▼
   MCU main loop: uart_poll() 收字节 → 帧解析 → dispatch → 回 STS
        │
        ▼
   ISR (100μs) ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ 完全不知 UART 被读
        │
   DTCM buffers
     WIRE_MAP[*], ACT[*]            ← CMD_OBSERVE 读
     snap_ring[] @ 0xD500 (100Hz)   ← CMD_SNAPSHOT 拉
     LOG_RING[] @ 0xD000 (10ms/条)  ← CMD_LOGR 拉
```

---

## 2. CMD 协议

### 2.1 已有 CMD (CLAUDE.md UART Frame Protocol 定义)

| CMD | 码 | 方向 | 说明 |
|-----|---|------|------|
| DEPLOY | 0x10 | PC→MCU | 部署路由表+参数 |
| START | 0x11 | PC→MCU | 启动引擎 |
| STOP | 0x12 | PC→MCU | 停止引擎 |
| RESET | 0x13 | PC→MCU | 复位引擎 |
| READ | 0x20 | PC→MCU | 读 WIRE_MAP 区段 |
| WRITE | 0x21 | PC→MCU | 写 WIRE_MAP 单值 |

### 2.2 新增 CMD (监视器专用)

#### CMD_OBSERVE (0x30) — 批量读

**PC→MCU**:
```
[0x30][count:LE u16][ addr0:LE u16 ][ addr1:LE u16 ]...[ addr_{n-1}:LE u16 ]
                                  ←─── payload ──────────
每个 addr = DTCM 字节偏移 (如 WIRE_MAP[2] → 0x0308)
```

**MCU→PC**:
```
[0x30 as STS][count:LE u16][ val0:LE f32/u32 ][ val1:LE f32/u32 ]...[ val_{n-1} ]
```
val 为 4 字节,float 为 IEEE-754 LE;addr 越界时该 val 填 0x80000000 静默跳过。

**带宽**: 20 个 float → 84 字节 ≈ 0.74 ms @ 1 Mbps ( vs 单读 20 次 = 22 ms )。

#### CMD_SNAPSHOT (0x31) — 同一时刻冻结

**PC→MCU**:
```
[0x31][wire_count:LE u16][ wire0_off:LE u16 ][ wire1_off:LE u16 ]...
```
选定要看哪几个 WIRE/ACT,MCU 在每 10 ms 的 ISR 出口把这批值同刻冻结到 snap_ring。

**MCU→PC** (PC 用 CMD_OBSERVE 读 snap_ring):
每 entry 固定 64 B:
```
[0]  SAMPLES          (u32)
[1]  PERIOD_MIN       (u32 @240MHz)
[2]  PERIOD_MAX       (u32)
[3]  EXEC_MIN         (u32)
[4]  EXEC_MAX         (u32)
[5]  N_ROUTES         (u32)
[6]  fails            (u32)  ← DMA Stream5 配置校验失败次数, 0=正常
[7]  reserved
[8..15] wire[0..7]    (float)  ← 与上述引擎状态同一次 ISR 冻结
```

**为何要 SNAPSHOT?** PC 先后读 SAMPLES 和 SHADOW,中间隔 1 ms ≈ 10 个 ISR 周期,两个值来自不同周期,不构成因果。SNAPSHOT 在同一次 ISR 出口同时写所有字段,**PC 拿到的值和 ISR 出口时刻的引擎状态有严格的因果关系**,可与示波器波形按 SAMPLES 号对齐。

#### CMD_LOGR (0x32) — 读已有环形日志

引擎已经在每 10 ms 往 `LOG_RING[] @ DTCM 0xD000` 写 24 B/entry (SAMPLES, N_ROUTES, ACT[32], ACT[63], SHADOW, GPIOE_ODR)。CMD_LOGR 把自上次拉取以来的新条目发回,期间引擎不因被读做任何额外工作。

### 2.3 STS 码

| STS | 含义 |
|-----|------|
| 0x01 ACK | 命令正常完成 |
| 0x02 ERROR | 非法命令 / 参数超限 |
| 0x20 WIRE_DATA | READ/OBSERVE 的数据 |
| 0x30 SNAPSHOT_DATA | SNAPSHOT 的数据 |
| 0x32 LOG_DATA | LOGR 的数据 |

---

## 3. MCU 端改动

| 改动 | 文件:行 | 工作量 |
|------|---------|--------|
| 波特率切 1 Mbps (OVER8=1, BRR=0x88) | `firmware/h723-core0/Src/main.c:879-889` 中 BRR 行 | 极小 |
| CMD 分发加 0x30/0x31/0x32 | `uart_handle_command()` switch | 极小 |
| `handle_observe()` | 新函数, 循环累加 payload 中每个 val | 小 |
| `handle_snapshot()` | 解析 wire_count + 偏移表, 保存到内部状态 | 小 |
| `handle_logr()` | 读写指针差, 发回新条目 | 小 |
| ISR 出口 `snap_ring` 写入 | `TIM1_UP_IRQHandler` 末尾 | 极小 |
| `fails` 变量 (DMA 校验) | 新增 u32 | 极小 |
| snap_ring @ DTCM 0xD500 | `#define SNAP_BASE (DTCM_BASE + 0xD500)` | 极小 |

**关键过程步骤(波特率切换)**:
```c
1. USART2_CR1 &= ~1;           // UE=0 禁能 USART
2. while (USART2_CR1 & 1);    // 等 UE 清
3. USART2_CR1 |= (1<<15);     // OVER8=1 (oversampling=8)
4. USART2_PRESC = 0;           // async prescaler=1
5. USART2_BRR = 0x88;          // mantissa=0x08, frac=0; f_ck/(8×8.5)=1M
6. USART2_CR1 |= 1;            // UE=1 使能
```

**验证公式** (RM0468 §53.8.5):
- OVER8=1 → Baud = 2 × f_ck / USARTDIV
- f_ck = 68 MHz (APB1), USARTDIV = BRR[15:4] + BRR[2:0]>>1
- BRR = 0x88 = 0x08<<4 | 0 → USARTDIV = 8.5
- Baud = 2 × 68M / 136 = 1,000,000 bps (零误差整数)

---

## 4. PC 端工具 (tools/flash/monitor.py)

### 4.1 命令行接口

```bash
# 直接读 (零侵入引擎时序)
python tools/flash/monitor.py read 0x0308

# 持续监测,终端每 100 ms 刷新
python tools/flash/monitor.py watch wire[2] act[1] act[32]

# 录 CSV (离线分析)
python tools/flash/monitor.py record wire[2] act[32] --out run.csv

# 读日志环, 导出 CSV
python tools/flash/monitor.py log

# 内置 REPL
python tools/flash/monitor.py repl
>> status                     # 一次 SNAPSHOT + OBSERVE
>> observe 0x0300:20          # wire[0..19] 一次性
>> logr                       # 拉最新日志
>> scope_arm --pin 2          # 抖动测试模式
>> quit
```

支持别名: `wire[N]` → `0x0300 + N*4`,`act[N]` → `0x0200 + N*4`。

### 4.2 零侵入保证

- 所有数据走 DMA Stream2 + 主循环 uart_poll,**PC 的任何读写不影响 ISR 时序**
- 主循环的 `uart_poll()` 读 ring buffer 内的字节,**被 ISR 抢占时正常等待**,下一次循环继续
- 波特率自适应:PC 端配 `BAUD = 1_000_000`,MCU 启动时配 OVER8 + BRR;烧录后未改波特率则 PC 端用 115200 重新烧回或等待用户同步。切换期间可回退 115200 救场(已烧录 115200 的固件仍可用)

---

## 5. 抖动测量的专用用法

**目标**: 验证 DMA Stream5 搬 SHADOW→GPIOE_ODR 的边沿抖动低于示波器本底。

**步骤**:
1. CLI 发 `scope_arm --pin 3`:让引擎在每 ISR 入口翻转 PE3(诊断标记)。
2. 示波器 CH1: PE3 (ISR 入口标记),CH2: PE14 (CH4 OC4REF = DMA 触发时刻)。
3. CLI 同步 `record act[32] act[33] --out scope.csv` + `log`,PC 端 CSV 带本机示波器波形 SAMPLES 号对齐。
4. DMA→GPIOE 抖动 = CH1 与 CH2 上升沿之差(带引擎运行状态)。

**为什么不直接用 SHADOW vs ODR?** SHADOW 在 DTCM,外部不可见;PE14 是 OC4REF (CCR4 match),是 DMA 的触发源 —— 这两点之差即为"从触发到 OD R 实际翻转"的纯硬件延迟,完全不含 CPU pipeline 成分,是你核心理念中"输出时刻锁定在 ~97.5μs"的最终证据。

---

## 6. 实施阶段 (每阶段自测后再进下一阶段)

| 阶段 | 工作量 | 测试标准 | 交付 |
|------|--------|---------|------|
| **S1 MCU 波特率** | 5 行改动 | 烧录后 PC 能通 READ WIRE_MAP[0] | 固件:1 Mbps 运行 |
| **S2 PC baseline: REPL + read** | 0.5d | `repl → read wire[0]` 反复 100 次无错帧;引擎 SAMPLES 在跑,PERIOD_MIN 无异常偏移 | CLI:基础 READ 在线可用 |
| **S3 OBSERVE 批量** | 0.2d | `observe wire[0..19]` 每秒读 100 次,抖动的 delta 在理论范围 | CLI:批量读 |
| **S4 SNAPSHOT 同刻** | 0.3d | `status` 中 SAMPLES 字段 10 次连续读构成单调递增序列;wire[N] 与 SAMPLES 同步变化 | CLI:SNAPSHOT |
| **S5 LOGR + log** | 0.2d | `log` 导出 CSV,图形与 ISR 日志周期吻合 | CLI:离线分析 |
| **S6 scope_arm 模式** | 0.2d | 示波器 PE3 与 PE14 时间差在 ±5 ns 内 | 抖动最终证据 |

**每个阶段的回归测试清单**:
- [PERIOD_MIN 在阶段初/末对比,确认监测手段不引入额外抖动]
- [SAMPLES +10 确认引擎在监测期间稳定跑]
- [N_ROUTES 不回落 (排除 IWDG reset 导致的观测假象)]

---

## 7. 文件清单

```
新增:
  docs/monitor.md                           ← 本文档
  tools/flash/monitor.py                    ← CLI 工具 (取代 halt-based 观测)
修改:
  firmware/h723-core0/Src/main.c            ← 波特率 + 3 个新 CMD
  CLAUDE.md                                 ← 反映"监视器是主要的非侵入式诊断通道"
删除 / 保留:
  tools/debug/monitor.py                    ← 保留作参考, 不再作为主要工具
```

---

*文档 v1.0 — 2026-07-15*

---

## 附录：迁移计划（v1.0 → v2.0 以太网）

> 当前 UART 监控方案将在 Phase 1 以太网通道就绪后迁移到 UDP/TCP 推流。

| 阶段 | 监控通道 | 控制/调参通道 | 状态 |
|------|---------|-------------|------|
| P0 | RTT (SWD) + UART | UART 0xC0帧 | ✅ **当前** |
| P1-1 | RTT + UDP双推流 | UART + TCP双通道 | 🔄 过渡（UDP优先） |
| P1-2 | UDP推流（主力） | TCP:502（主力） | 🎯 **目标** |
| P2 | UDP推流 + EtherCAT | TCP + EtherCAT | 远期 |

### 迁移目标

| 特性 | v1.0 (UART) | v2.0 (以太网) | 优势 |
|------|------------|-------------|------|
| 速率 | 1Mbps | 100Mbps | **100×** |
| 数据流 | 命令/响应（拉） | UDP推流（推） | 零等待，10kHz实时 |
| 延迟 | ~1ms (命令轮询) | <100μs (窗口内立即) | **10×** |
| 同时监控数 | 20 float/次 | 64 float/周期订阅 | 更丰富 |
| 烧录 | USART 115200 (~30s) | TCP:502 (~3ms) | **10000×** |
| 连接 | 串口线 | 网线 | 更可靠 |

### 兼容策略

- UART 0xC0帧协议与 TCP 0xC0帧协议**指令兼容**（MAGIC从1字节扩展为4字节）
- PC端 IDE 自动检测可用通道（TCP优先，UART备用）
- Phase 1期间保留双通道，逐步过渡
