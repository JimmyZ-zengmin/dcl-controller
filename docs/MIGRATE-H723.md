# DCL 引擎迁移方案：ESP32-S3 → STM32H723

> 目标：把 esp32-core0 的确定性引擎（100μs 硬拍 / 四域 / 表组态）迁移到 STM32H723，
> **架构零改动，只换移植层**。本文是实施指导，不是设计文档。
>
> 2026-09-10 起草。对应基线：esp32-core0 `7bb0b1e`（20 套件全绿）。

---

## 0. 一句话原则

**迁移的是"平台"，不是"引擎"。**

引擎的架构资产（100μs 拍、桶化分档、原语表、四域调度、双缓冲热重载、唯一写者语义、
Modbus 协议栈、DSL 编译器、20 套回归）**全部保留**。要重写的只有"和芯片说话的那一层"。

判据：**迁移后 Python 测试脚本一行都不用改**（协议不变），跑出的结果应与 S3 等价或更好。

---

## 1. 不变量（迁移中绝不能变）

| 不变量 | 说明 |
|---|---|
| 拍周期 100μs | 硬拍语义，分档 div0/1/2 的基准 |
| 表布局 | RouteEntry_t 16B / ParamEntry_t 16B / StateEntry_t 16B 字段顺序不变 |
| SHM 偏移 | OFF_* 各表偏移不变（脚本按偏移读） |
| 协议 | 0x01-0x62 命令帧格式、CRC、时序不变 |
| 语义 | 唯一写者、冷启动清理、Force 语义、通信域每拍限速 |

**为什么强调表布局/SHM 偏移**：审计、测试、LA 验证都依赖这些偏移和格式。
偏移一改，20 套件和 4 份审计记录全部失效。

---

## 2. 资源映射表

| 功能 | ESP32-S3（现状） | STM32H723 | 迁移要点 |
|---|---|---|---|
| 内核 | 双核 LX7 @240MHz | **单核 M7 @550MHz** | 双核→单核，见 §7 风险 |
| 拍定时器 | GPTimer | **TIM2 或 TIM5（32 位）** | 275MHz → 分辨率 **3.6ns** |
| 周期计数 | `get_ccount()`（CCOUNT） | **`DWT->CYCCNT`** | 测 ISR 周期/emax |
| ISR 代码 | IRAM（`IRAM_ATTR`） | **ITCM（0x00000000，64KB）** | 链接脚本段 |
| 数据表 | SHM 64KB（heap_caps INTERNAL） | **DTCM（0x20000000，128KB）** | 零等待，见 §4 |
| 大缓冲 | PSRAM 8MB | AXI SRAM（0x24000000，320KB） | 非关键数据 |
| 中断 | ESP-IDF IRAM ISR | **NVIC** + 优先级 | 见 §5 |
| 串口 | UART0（调试）/ UART1（Modbus） | USART1 / USART2 | LL 层寄存器操作 |
| ADC | ADC oneshot 12 位 | **ADC 16 位 + DMA** | AI 精度 12→16 位 |
| GPIO | `gpio_ll` | **GPIOx->BSRR / ODR** | 输出锁存基础 |
| 持久化 | NVS（partition） | **Flash 直接读写（双 sector）** | persist 语义保留 |
| 以太网 | 无 | **ETH MAC + DMA** | 未来 Modbus TCP |

---

## 3. 时钟树（已定案 2026-09-10：HSE 25MHz → 550MHz）

### 3.1 决定与理由

**外部 25MHz 晶振（HSE）→ PLL1 → CPU 550MHz（VOS0）**。三条理由：

**① 必须 HSE，不能用内部 HSI。** DCL 的核心承诺是"声明 100μs 就精确是 100μs"。
HSI（64MHz RC）精度是 % 级且**随温度漂移**，而晶振是 ppm 级——差约三个数量级：

| 时钟源 | 100μs 拍的实际误差 |
|---|---|
| HSE 晶振（±20ppm）| **2 ns** |
| HSI（±1% 级）| **1 μs** |

另外 UART 波特率（Modbus 要求总误差 <2~3%）与 USB 48MHz（±0.25%）也都需要晶振级精度。

**② 必须用整数模式，禁用 sigma-delta（分数）模式。** PLL 分数模式靠 dither 分频器凑
非整数比，会**主动引入周期抖动**——与本项目目标正好相反。本配置 M/N 比值为整数
（110/5 = 22），天然满足。

**③ 550 是"早点选、早排雷"**（2026-09-10 决策）。跑在规格上限，任何电源/去耦/时序
余量不足都会**在还有余力排查的时候暴露**，而不是等到产品阶段。代价是容量少 9%
（相对 500MHz 少 5000 cyc/拍，无实质影响——见 §3.4 备用落点）。

### 3.2 PLL1 配置（推导，无自由度）

约束（datasheet Table 38，PLL1 宽 VCO 挡）：

| 参数 | 范围 |
|---|---|
| PLL 输入 | 2 – 16 MHz |
| VCO 输出 | 192 – 836 MHz |
| PLL1P 输出 | VOS0 ≤ 550 / VOS1 ≤ 400 / VOS2 ≤ 300 / VOS3 ≤ 170 MHz |

推导：要 CPU = 550MHz，则 VCO 必须是 550 的整数倍；VCO 上限 836 → 只有 550×1 成立
（1100 超限）。故 **VCO = 550、DIVP1 = 1，没有第二选择**。25MHz 分到 [2,16] → DIVM1 = 5。

```
DIVM1 = 5   DIVN1 = 110   DIVP1 = 1
HSE 25MHz ÷5 → 5MHz  ×110 → VCO 550MHz  ÷1 → PLL1P 550MHz
```

```c
/* 关键寄存器（示意，实际以 LL/HAL 或裸寄存器实现为准） */
RCC->PLLCKSELR = (5u << RCC_PLLCKSELR_DIVM1_Pos) | RCC_PLLCKSELR_PLLSRC_HSE;
RCC->PLL1DIVR  = ((110u - 1u) << RCC_PLL1DIVR_N1_Pos) | ((1u - 1u) << RCC_PLL1DIVR_P1_Pos);
/* ★整数模式：不使能 PLL1FRACEN，FRACN 保持 0 */
```

### 3.3 派生时钟

| 节点 | 频率 | 得到方式 |
|---|---|---|
| CPU（cdcpre）| **550 MHz** | SYSCLK / 1 |
| HCLK / AXI | **275 MHz** | CPU / 2 |
| APB1~4 | **137.5 MHz** | HCLK / 2 |
| TIMxCLK | **275 MHz** | = HCLK（APB 预分频 ≤ 4 时）|
| DWT->CYCCNT | **550 MHz** | CPU 时钟 → **1.82ns 分辨率**（S3 为 4.17ns）|

**★ HCLK = CPU/2 的代价**：访问 AXI 上的对象时，每个总线周期要花 **2 个 CPU 周期**。
这就是"表必须进 DTCM"（§4.2）在**时钟层面**的理由——不只是 cache 抖动，还有总线速率比。

**注**：APB = 137.5MHz 是本配置**唯一的非整数节点**（550MHz 的必然结果）。不影响功能，
但心算 UART 分频时会遇到小数。若更在意"全链路整数节点"，可切 §3.4 的备用频率。

### 3.4 备用落点（改一处即可切换）

**把全部分频比集中在一个 `#define` 块**，切换频率只改这一个地方：

| CPU | VOS | 拍预算 | HCLK | APB | TIMxCLK | 100μs 计数 | DWT 1μs |
|---|---|---|---|---|---|---|---|
| **550** ★ | VOS0 | 55000 | 275 | 137.5 | 275 | 27500 | 550 |
| 500 | VOS0 | 50000 | 250 | 125 | 250 | 25000 | 500 |
| 480 | VOS0 | 48000 | 240 | 120 | 240 | 24000 | 480 |
| 400 | **VOS1** | 40000 | 200 | 100 | 200 | 20000 | 400 |

> **400MHz 是唯一能用 VOS1 的**（VOS1 上限 400）——真正把 VCORE 降一档，拿到功耗/热/EMI
> 余量。500 与 550 同为 VOS0，电压档相同，只是频率留了裕度。

### 3.5 三个必须做对的地方（排雷清单）

**① VOS0 使能序列**（最经典的"配了但跑不起来"）

★**注意：H72x/H73x 没有 overdrive 位**。H743 需要在 SYSCFG->PWRCR 置 ODEN，但
**RM0468 的 H72x/H73x 不需要**——VOS0 就是直接写 `PWR->D3CR.VOS`（libopencm3 原话：
"VOS0 is implemented on STM32H72x/3x with simple VOS setting"）。
（我最初按 H743 的经验写了 ODEN，查证 RM0468 后已纠正。）

**VOS 编码（RM0468 §6.8.6，与 H743 完全不同，不能照抄）**：

| VOS[15:14] | 档位 | 含义 |
|---|---|---|
| `0b00` | **Scale 0** | **最高性能，支持 550MHz ← 我们要写的** |
| `0b01` | Scale 3 | 复位默认值（实测本芯片读回即此值）|
| `0b10` | Scale 2 | |
| `0b11` | Scale 1 | |

**正确序列**（RM0468 §6.8.6 + stm32h7xx-hal 实现）：

```
1. 前置: D3CR.VOS 必须等于 CSR1.ACTVOS, 且 CSR1.ACTVOSRDY 已置位
2. 写 D3CR.VOS = 0b00 (Scale 0)
3. 等 D3CR.VOSRDY (bit13) 置位
4. 等 D3CR.VOS == CSR1.ACTVOS[15:14]  (确认已真正生效)
5. 等 CSR1.ACTVOSRDY (bit13) 置位
```

铁律（RM0468 原文）：**升性能时先改电压再升频率；降性能时先降频率再改电压。**
所以 VOS0 必须在切 PLL 之前完成。

**② Flash 等待态（FLASH_ACR.LATENCY）必须与 HCLK 匹配**
设错会 HardFault 或静默执行错指令。这也是"ISR/表进 TCM"的另一个理由——TCM 无等待态。

**③ CSS（时钟安全系统）必须开**
晶振失效时芯片默认切到 HSI 继续跑——**此时"100μs"已经变了**。应让 CSS 中断直接进
安全态/停机，而不是静默降级运行。

**④ RCC_CFGR.SW 的编码与 F4 不同**（★2026-09-10 实测确认）

```
0 = HSI    1 = CSI    2 = HSE    3 = PLL1
```
**不是** F4 的 `0=HSI / 1=HSE / 2=PLL1`。按 F4 记忆写会把"切 HSE"写成"切 CSI"而
静默失败（CSI 未使能时切换被拒）。
实测依据：写 SW=2 → SWS=2 且 DWT 实测 CPU ≈ 25MHz（=HSE 晶振）；写 SW=1 被拒。

**⑤ PWR_CSR1.ACTVOS / ACTVOSRDY 不能用作判据**（★2026-09-10 实测确认）

| 位 | 实测行为 |
|---|---|
| `CSR1.ACTVOS` | 恒为 `0b01`（Scale3），**完全不跟随** D3CR 变化 |
| `CSR1.ACTVOSRDY` | **只在 BYPASS 模式（PWR_CR3.BYPASS=1）才置位**；LDO 模式下恒为 0 |

RM0468 §6.8.6 的注释暗示要用它们确认 VOS0 生效，但硬套会**直接死锁**
（首次烧录即卡在此处）。正确判据用 **D3CR**：写 → 等 `VOSRDY`(bit13) → **回读
`VOS[15:14]` 确认写入生效**。

### 3.6 外部验证（用逻辑分析仪，不靠固件自报）

本项目铁律"宣称 = 实现"同样适用于时钟。三重验证：

| 手段 | 验什么 | 方法 |
|---|---|---|
| 寄存器回读 | VOSRDY / PLL1RDY / RCC_CFGR 实际值 | `pyocd` 读（无需写代码）|
| **MCO 输出 + LA** | **实际就跑在 550MHz** | 配 MCO 输出 SYSCLK（或分频）→ LA 测频率 |
| **拍心跳 + LA** | **100μs 拍真的准** | GPIO 每拍翻转 → LA 测周期（同 S3 方法）|

**MCO 这一条是"排雷"最直接的手段**：若板子电源/去耦有问题，PLL 可能锁得住但频率跑偏
或抖动偏大——把 MCO 引到 LA 上就看得见。这是**时钟层面的外部独立证据**。

### 3.7 相关事实（已查证）

**芯片版本确认支持 550MHz**：实测 `DBGMCU_IDCODE = 0x10016483` → REV_ID `0x1001` =
**Revision Z**（errata ES0491 只列 Rev A = 0x1000 / Rev Z = 0x1001），datasheet 标称
550MHz 不带版本前提 ✅

**SRAM 布局（ST 官方）**：ITCM 64KB 独占 + 最多 192KB 从 AXI 重映射 = **最多 256KB**；
DTCM **128KB 固定**；AXI SRAM 320KB；D2 AHB 32KB；D3 16KB；Backup 4KB。

**相关 errata（引脚/外设规划时注意）**：

| errata | 内容 | 对我们的影响 |
|---|---|---|
| `2.5.1` | DMA stream 在 USART/UART 传输时可能锁死（Rev A/Z 均有）| 以后 UART 若走 DMA 要注意；本方案是 PIO 轮询，影响小 |
| `2.2.6` | LSE 晶振会被 **PC13** 跳变干扰 | **别在 PC13 上放信号** |
| `2.1.3` | 数据 cache ECC 错误在 error bank 寄存器锁定时导致数据损坏 | 用 D-cache 时注意 |

---

## 4. 内存分区设计（迁移的核心价值）

### 4.1 地址地图（H723）

```
0x00000000  ITCM RAM    64 KB   零等待/无 cache  ← ISR 代码 + 引擎热路径
0x20000000  DTCM RAM   128 KB   零等待/无 cache  ← 所有表（§4.2）
0x24000000  AXI SRAM   320 KB   D1 域            ← 大缓冲、非关键数据
0x30000000  SRAM1/2     32 KB   D2 域            ← 通信 DMA 缓冲（ETH/USART）
0x38000000  SRAM4       16 KB   D3 域            ← 低功耗域
0x38800000  Backup SRAM  4 KB   VBAT 保持        ← 掉电保持小数据（可选）
0x08000000  Flash        1 MB                    ← 代码仓库 + persist 分区
```

### 4.2 DTCM 表分区（目标 1024 条规模）

```
偏移        区                  大小      说明
0x0000     OFF_CTRL            512 B    引擎控制/统计/通信域控制块
0x0200     OFF_SENSOR_MAP      256 B    64 × float32  (AI/DI/DHT/HIL)
0x0300     OFF_WIRE_MAP        512 B    128 × float32
0x0500     OFF_ACTUATOR_MAP    ...      执行器输出
0x0800     OFF_ROUTE_TABLE     16 KB    1024 × 16B  ← 每拍遍历
0x4800     OFF_PARAM_TABLE     16 KB    1024 × 16B  ← 每拍读
0x8800     OFF_STATE_TABLE     16 KB    1024 × 16B  ← 每拍读写
0xC800     桶表 + HMI/MB 区     4 KB
────────────────────────────────────────────────
合计                       ≈ 52 KB   （DTCM 128KB 余 76KB）
```

**容量结论**：
- DTCM 128KB 支持约 **2000 条**路由（含参数/状态三表）
- 而 H723 拍时间只能跑 ~290 条 div0 → **内存不再是瓶颈**（S3 是 128 条上限）

### 4.3 为什么必须 DTCM（而不是 AXI SRAM）

| 内存 | 访问 | 确定性 |
|---|---|---|
| **DTCM** | 独立总线，**零等待**，无 cache | ✅ 每次访问时间恒定 |
| AXI SRAM | 经 AXI 总线矩阵，有仲裁 | ⚠️ 峰值可能被其他主设备插队 |
| AXI/外部 + cache | 命中/未命中不定 | ❌ 延迟不可预测 |

**引擎每拍要遍历整张路由表**——放在非确定性内存里，"确定性"这个词就名不副实了。

### 4.4 链接脚本要点（GCC）

```ld
MEMORY {
  ITCMRAM (xrw) : ORIGIN = 0x00000000, LENGTH = 64K
  DTCMRAM (xrw) : ORIGIN = 0x20000000, LENGTH = 128K
  RAM_D1  (xrw) : ORIGIN = 0x24000000, LENGTH = 320K
  FLASH   (rx)  : ORIGIN = 0x08000000, LENGTH = 1024K
}

SECTIONS {
  /* ISR 与引擎热路径 → ITCM */
  .itcm_text : { *(.itcm_text) *(.itcm_text.*) } >ITCMRAM AT> FLASH
  /* 表与关键数据 → DTCM */
  .dtcm_data : { *(.dtcm_data) *(.dtcm_data.*) } >DTCMRAM AT> FLASH
}
```

代码中用节属性指定：

```c
#define ITCM_FUNC  __attribute__((section(".itcm_text")))
#define DTCM_DATA  __attribute__((section(".dtcm_data")))

ITCM_FUNC void ISR_Handler(void) { ... }        /* 拍中断 + 引擎主循环 */
DTCM_DATA static RouteEntry_t g_routes[MAX_ROUTES];   /* 路由表 */
```

> **注意**：S3 的 `IRAM_ATTR` / `heap_caps_malloc(DMA|INTERNAL)` 都要换成上面的方式。
> 建议做一个 `port_mem.h` 把两者统一成同一个宏（便于双平台共存期）。

---

## 5. 100μs 拍设计

### 5.1 定时器配置

```
TIMxCLK  = 275 MHz        (APB 预分频 >1 时，定时器时钟 = 2 × APB)
ARR      = 27500 - 1      → 100.000 μs   (27500 / 275MHz)
分辨率   = 1 / 275MHz     = 3.63 ns
```

**用 TIM2 或 TIM5（32 位计数器）**：32 位在需要长周期/微调时更灵活；16 位也够
（27500 < 65536），但 32 位便于将来做"多拍调度"（如 10 拍周期用同一个计数器相位）。

> 对比 S3：GPTimer 通常 80MHz → 12.5ns 分辨率。**H723 的拍边界精度提高 3.4 倍**。

### 5.2 中断优先级（NVIC）

| 优先级 | 中断 | 理由 |
|---|---|---|
| 0（最高）| 拍定时器 TIMx | 硬拍必须抢占一切 |
| 1 | 输出锁存 DMA 完成（若启用）| 需紧跟拍 |
| 2-3 | USART/ETH | 通信域在拍内轮询，中断只做触发 |
| 4+ | 其余外设 | |

**关键**：NVIC 优先级分组设为"抢占优先级尽可能多"（如 `NVIC_PRIORITYGROUP_4`），
确保拍中断能抢占任何其他中断。

### 5.3 抖动控制（这是迁移最该拿到的东西）

```
S3 现状:  ISR 代码在 IRAM(✓无 cache) + SHM 在内部 SRAM(经 cache ⚠)  → 测量 99.9973μs
H723 目标: ISR 代码在 ITCM(✓) + 表在 DTCM(✓)                          → 目标 99.9995μs+
```

**三条硬规则**：
1. ISR 及其调用的引擎函数 → ITCM（`ITCM_FUNC`）
2. 每拍读写的表（路由/参数/状态/桶）→ DTCM（`DTCM_DATA`）
3. 拍中断内**不调用任何 HAL 库函数**（HAL 可能在 Flash 执行 + 有分支）

### 5.4 输出锁存（可选，L2 增强）

老项目已验证的机制可移植：

```
拍中断写输出缓存 → TIM 上溢事件(UEV) 硬件请求 DMA → DMA 搬缓存到 GPIOx->ODR → 输出沿
```

价值：输出沿由**硬件定时**决定，CPU/ISR 抖动被完全隔离（老项目实测 <4ns）。
在 H723 上同样可行（TIM1/TIM8 + DMA + GPIO ODR）。

---

## 6. 代码分层：哪些复用、哪些重写

### 6.1 直接复用（预计 80%+）

| 模块 | 文件 | 说明 |
|---|---|---|
| 原语表 | `primitives.h` | 20 个原语纯算法，无平台依赖 |
| 引擎主循环 | `core0_isr.c` 的扫描逻辑 | 桶化分档、域调度、Force 扫描 |
| Modbus 协议栈 | `modbus.c` | 只依赖"字节从哪来"（已解耦）|
| DSL 编译器 | `tools/dclc.py` | PC 端，与芯片无关 |
| 回归套件 | `tools/verify_*.py` | 走协议，不碰芯片 |
| LA 验证 | `tools/verify_la_modbus.py` | 同上 |

### 6.2 必须重写（移植层）

| 模块 | S3 实现 | H723 实现 |
|---|---|---|
| `port_timer.c` | GPTimer + `esp_timer` | TIM2/5 寄存器 |
| `port_mem.h` | `IRAM_ATTR` / `heap_caps` | `ITCM_FUNC` / `DTCM_DATA` |
| `port_uart.c` | `uart_ll` | USART LL / 寄存器 |
| `port_adc.c` | `adc_oneshot` | ADC + DMA |
| `port_gpio.c` | `gpio_ll` | GPIOx->BSRR/ODR |
| `port_persist.c` | NVS / partition | Flash 扇区读写 |
| `port_cyc.c` | `get_ccount()` | `DWT->CYCCNT` |

**建议**：为这几个模块定义统一接口头（`port_*.h`），S3 和 H723 各一份实现——
这样**双平台可共存**，迁移期间可对照跑，也能保留 S3 作为参考实现。

---

## 7. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| **双核 → 单核** | S3 上通信域在 Core0 任务、引擎在 Core1 ISR；H723 只有一核 | 已提前设计好：`mb_tick()` **在 run 门外、计时前**，本身就是"每拍跑、不依赖另一核"；通信域不存在跨核问题 |
| **无 PSRAM** | S3 有 8MB PSRAM，H723 只有 320KB AXI SRAM | 大缓冲（display/macro/日志）需瘦身或外接 SDRAM（FMC）|
| persist 迁移 | NVS → 裸 Flash 扇区 | H723 1MB Flash 划两扇区做双副本（比 S3 的 persist 分区更简单）|
| 开发环境 | ESP-IDF → STM32CubeIDE / CMake+OpenOCD | 建议 CMake + arm-none-eabi-gcc + OpenOCD（便于 CI）|
| 时钟配置复杂 | H723 时钟树比 S3 复杂（PLL/D1D2D3 域） | 用 CubeMX 生成初始化，但**引擎代码不用 HAL** |
| H7 的 cache | M7 有 I/D cache，可能引入不确定性 | 三条硬规则（§5.3）把热路径**全部移出 cache 范围** |

---

## 8. 实施步骤（每步可验证，不可跳步）

### 阶段 0：工具链与基线（1-2 天）
- [ ] `arm-none-eabi-gcc` + OpenOCD + CMake 工程跑通
- [ ] 点灯 + 串口打印
- [ ] **DWT->CYCCNT 可用性验证**（读周期计数，对照已知延时）
- 验收：能测出"一段已知代码的周期数"

### 阶段 1：拍定时器 + 空拍骨架（2-3 天）
- [ ] TIM2 配 100μs 中断，ISR 只翻一个 GPIO
- [ ] 用 DWT 测空拍周期数
- [ ] **外部验证**：LA 抓该 GPIO，测拍周期与抖动
- 验收：拍周期 100μs ± <0.1μs；对照 S3 的"空拍 49 cyc 骨架"

### 阶段 2：内存分区 + 表扫描（3-5 天）
- [ ] 链接脚本分 ITCM/DTCM
- [ ] 表放 DTCM，ISR 放 ITCM
- [ ] 移植路由扫描（先不做分档，全表扫）
- [ ] DWT 测"单条路由"周期数（对照 S3 的 234 cyc）
- 验收：单路由周期数有数；表在 DTCM 后抖动收窄

### 阶段 3：协议栈 + 回归跑通（3-5 天）★ 关键里程碑
- [ ] 移植 UART + 协议帧（0x01-0x62）
- [ ] persist 移植
- [ ] **跑通全部 20 套回归**（脚本不改！）
- 验收：20 套件全绿 —— 这一步过了，"迁移成功"基本成立

### 阶段 4：四域补齐（3-5 天）
- [ ] 桶化分档
- [ ] Sequencer
- [ ] 通信域 (Modbus) + LA 外部验证
- 验收：LA 再次逐字节验证 Modbus 帧

### 阶段 5：AI + 确定性实测（2-3 天）
- [ ] ADC 16 位 + DMA → SENSOR
- [ ] **拍抖动对比 S3**（LA 长样本）
- [ ] div0 容量实测（目标 ~290 条）
- 验收：抖动 < S3；容量 > S3

---

## 9. 验收标准（怎么证明迁移是对的）

### 9.1 功能等价（必须）
- 20 套回归全绿，**脚本零改动**
- Modbus LA 外部验证 7/7

### 9.2 确定性质变（迁移的价值）
| 指标 | S3 现状 | H723 目标 |
|---|---|---|
| 拍周期 | 99.9973 μs | **99.999+ μs** |
| 拍抖动主带占比 | 100% | 100%（且分布更窄）|
| 内存访问 | 经 cache | **DTCM 零等待**（可测周期数恒定）|
| 单路由周期 | 234 cyc | 目标 **<150 cyc** |

### 9.3 容量提升
| 指标 | S3 | H723 |
|---|---|---|
| 拍预算 | 24000 cyc | **55000 cyc** |
| div0 容量 | 68 条 | 目标 **~290 条** |
| 表总量 | 128 条 | DTCM 支持 ~2000 条 |

---

## 10. 迁移顺序建议（务实版）

```
第 1 步: 阶段 0+1 —— 先证明"拍能准"（这是命根子）
第 2 步: 阶段 2+3 —— 证明"表和协议能跑"（回归全绿）
第 3 步: 阶段 4+5 —— 补齐四域 + 量化提升
```

**不要**在阶段 1 没过就急着移植协议栈——拍不准，后面全是伪命题。

**建议保留 S3 版本**作为对照基线，直到 H723 全绿；两平台通过 `port_*.h` 共存。

---

## 11. 附：本次迁移能顺带拿到的

| 能力 | 说明 |
|---|---|
| **AI 12→16 位** | ADC 硬件升级，精度提升 16 倍 |
| **以太网** | ETH MAC，为 Modbus TCP / 确定性帧铺路 |
| **FMAC/CORDIC** | PID/LPF 等原语可硬件加速 |
| **ECC 全内存** | 工业级数据可靠性（S3 无）|
| **-40~+85°C 版本可选** | 工业温度（部分型号）|
| **4ns 输出锁存** | 老项目机制可复用，拍输出抖动隔离 |
