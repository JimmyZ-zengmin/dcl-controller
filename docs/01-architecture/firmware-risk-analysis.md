# STM32H723 固件代码风险分析报告

> **审查日期**: 2026-07-14  
> **目标文件**: firmware/h723-core0/  
> **参考手册**: RM0468 (STM32H723 Reference Manual)  
> **风险等级**: 🔴 高危 / 🟡 中危 / 🟢 低危

---

## 📋 风险摘要

| 等级 | 数量 | 关键风险 |
|------|------|----------|
| 🔴 高危 | 3 | DTCM 未在链接脚本中定义、跨域 DMA 时序风险、SHADOW_GPIO 竞争条件 |
| 🟡 中危 | 5 | IWDG 策略缺陷、TIM1 时钟频率不一致、DMA 缓冲区对齐、FDCAN 配置、中断优先级 |
| 🟢 低危 | 4 | 代码风格、注释准确性、冗余代码、可读性 |

---

## 🔴 高危风险

### R001: DTCM 内存区域未在链接脚本中定义

**位置**: `STM32H723ZGTX_FLASH.ld`

**问题描述**:  
链接脚本只定义了 ITCM (0x00000000)、RAM (0x24000000) 和 FLASH (0x08000000) 三个内存区域。但代码大量使用 DTCM (0x20000000, 128KB) 存放关键数据（SHADOW_GPIO、ADC_RAW、路由表、传感器/执行器映射区）。

**风险**:
- DTCM 是零等待状态内存，核心数据结构设计为在 DTCM 中运行
- 但链接器不知道 DTCM 的存在，可能将其他变量分配到 DTCM 地址空间
- 启动代码 (`startup_stm32h723zgtx.s`) 可能没有初始化 DTCM
- 上电后 DTCM 内容是不确定的，需要显式清零

**影响**: 系统启动时 DTCM 数据为随机值，可能导致 GPIO 输出不确定、路由表执行错误、传感器数据异常。

**建议修复**:
```ld
/* 在 MEMORY 中添加 DTCM */
MEMORY
{
  ITCM    (xrw)    : ORIGIN = 0x00000000,   LENGTH = 64K
  DTCM    (xrw)    : ORIGIN = 0x20000000,   LENGTH = 128K
  RAM     (xrw)    : ORIGIN = 0x24000000,   LENGTH = 192K  /* AXI SRAM */
  FLASH   (rx)     : ORIGIN = 0x08000000,   LENGTH = 1024K
}

/* 添加 DTCM 数据段 */
.dtcm_data :
{
  . = ALIGN(4);
  _sdtcm = .;
  *(.dtcm_data)
  *(.dtcm_data*)
  . = ALIGN(4);
  _edtcm = .;
} >DTCM AT> FLASH
_sidtcm = LOADADDR(.dtcm_data);
```

同时在 startup 代码中添加 DTCM 初始化和清零。

---

### R002: DMA2 跨域访问 D3 (AHB4) 的时序风险

**位置**: `Src/dma/dma2.c` (Stream5 → GPIOE_ODR)

**问题描述**:  
DMA2 位于 D2 domain，GPIOE 位于 D3 domain (AHB4)。代码使用 DMA2 Stream5 从 DTCM 搬运数据到 GPIOE_ODR：

```c
DMA2_S5PAR  = (uint32_t)&GPIOE_ODR;  // 0x58021014 (AHB4, D3 domain)
DMA2_S5M0AR = DTCM_BASE + 0x00E0;    // 0x200000E0 (DTCM, D2 domain)
```

**风险**:
- 虽然 STM32H7 支持通过 D2-to-D3 AHB 桥跨域访问，但存在以下问题:
  1. **延迟不确定性**: 跨域访问需要经过桥接器，延迟比同域访问高
  2. **总线竞争**: D3 domain 的 APB4/AHB4 桥可能忙于其他传输
  3. **无错误检测**: 如果 D3 domain 时钟未正确配置，DMA 写入可能静默失败
  4. **RM0468 未明确保证**: 文档说 DMA 可访问 "all AHB and APB peripherals"，但未说明跨域访问的时序保证

**影响**: GPIO 输出可能延迟或丢失，导致控制时序不确定。

**建议方案**:

**方案 A (推荐): 改用 BDMA (D3 domain 本土 DMA)**
```c
// BDMA 位于 D3 domain，直接连接 AHB4，访问 GPIOE 零延迟
// 但 BDMA 通道有限，通常用于低速外设
BDMA_CH1CR  = ...;         // 配置 BDMA 通道 1
BDMA_CH1PAR  = (uint32_t)&GPIOE_ODR;
BDDA_CH1M0AR = DTCM_BASE + 0x00E0;
```

**方案 B: 保留 DMA2 但增加验证**
```c
// 在 DMA 启动后验证首次传输
volatile uint32_t first_transfer = SHADOW_GPIO;
DMA2_S5CR |= 1;  // 启动 DMA
// 等待首次传输完成
{ uint32_t t = TIMEOUT; while ((DMA2_HISR & (1<<5)) && --t) {} }
if (GPIOE_ODR != first_transfer) {
  // DMA 传输失败，回退到直接写
  GPIOE_ODR = SHADOW_GPIO;
}
```

---

### R003: SHADOW_GPIO 读写竞争条件

**位置**: `Src/main.c` ISR 中的输出映射段

**问题描述**:
```c
// ISR 中写 SHADOW_CPU
SHADOW_GPIO = gpio_bits;  // CPU 写入 DTCM @ 0x200000E0

// 同时 DMA2 Stream5 在 TIM1_CH4 触发时读取同一地址
// DMA 读取 SHADOW_GPIO → 写入 GPIOE_ODR
```

**风险**:
- CPU 写 SHADOW_GPIO 和 DMA 读 SHADOW_GPIO 可能同时发生
- 虽然 ARM 架构对 32-bit aligned 访问是原子的，但 DMA 可能读到半更新的数据
- 在 CIRC 模式下，DMA 持续循环读取，竞争概率较高

**影响**: GPIO 输出可能出现毛刺或中间值。

**建议修复**:
```c
// 使用双缓冲策略
#define SHADOW_GPIO_A  (*(volatile uint32_t *)(DTCM_BASE + 0x00E0))
#define SHADOW_GPIO_B  (*(volatile uint32_t *)(DTCM_BASE + 0x00E4))
#define DMA_ACTIVE_SHADOW  (*(volatile uint32_t *)(DTCM_BASE + 0x00E8))  // DMA 正在读取的缓冲区

// ISR 中写非活动缓冲区
if (DMA_ACTIVE_SHADOW == (uint32_t)&SHADOW_GPIO_A) {
  SHADOW_GPIO_B = gpio_bits;
  DMA_ACTIVE_SHADOW = (uint32_t)&SHADOW_GPIO_B;
} else {
  SHADOW_GPIO_A = gpio_bits;
  DMA_ACTIVE_SHADOW = (uint32_t)&SHADOW_GPIO_A;
}
```

或者简化方案：确保 ISR 写 SHADOW_GPIO 发生在 DMA 触发窗口之外。

---

## 🟡 中危风险

### R004: IWDG 喂狗策略缺陷

**位置**: `Src/main.c` 主循环

**问题描述**:
```c
while (1) {
  uart_poll();      // 可能阻塞
  canopen_poll();   // 可能阻塞
  IWDG_KR = IWDG_KEY_RELOAD;  // 喂狗
}
```

**风险**:
- `uart_poll()` 和 `canopen_poll()` 可能阻塞（例如等待 DMA 数据）
- 如果阻塞时间超过 IWDG 超时，系统会复位
- IWDG 时钟 (LSI ~32kHz) 独立于系统时钟，精度有限
- 代码中 IWDG 预分频和重载值未在代码中配置（依赖 option byte 默认值）

**影响**: 系统可能意外复位。

**建议修复**:
1. 确保 IWDG 超时时间 > 主循环最大阻塞时间
2. 在 `uart_poll()` 和 `canopen_poll()` 中设置超时
3. 或改用窗口看门狗 (WWDG) 或完全不使用调试阶段的看门狗

---

### R005: TIM1 时钟频率不一致

**位置**: `Src/main.c` 和 `Src/tim1/tim1_pwm.c`

**问题描述**:
- `tim1_pwm.c` 注释: `ARR: 100us @ 136MHz`
- `main.c` 注释: `TIMER_HZ = 120000000` (APB2 = 120MHz)

TIM1 挂在 APB2 上。如果 APB2 = 120MHz，则 TIM1 时钟 = 240MHz (当 APB2 分频 > 1 时，定时器时钟 = APB2 × 2)。

但代码中 ARR = 11999，这对应 100μs @ 120MHz (11999+1)/120MHz ≈ 100μs。

**矛盾点**:
- 如果 TIM1 真的运行在 136MHz，100μs 对应 ARR = 13599
- 如果 TIM1 运行在 240MHz (APB2 的 2x)，100μs 对应 ARR = 23999

**影响**: 实际周期可能是 50μs 或 200μs，与设计不符，导致:
- ADC 触发时刻错误
- DMA 搬运到 GPIO 的时刻错误
- 所有基于 100μs 的控制逻辑失效

**建议修复**:
```c
// 确认 AP2 分频设置，重新计算 ARR
// 如果 APB2 = 120MHz, ARR = 11999
// 如果 TIM1 时钟 = 240MHz, ARR = 23999

// 在 SystemInit 后添加验证:
volatile uint32_t tim1_clk = get_tim1_clock();
if (tim1_clk != 120000000) {
  // 重新配置 TIM1 ARR/PWR
  TIM1_ARR = (uint16_t)(tim1_clk / 10000 - 1);  // 100us period
}
```

---

### R006: DMA2 Stream5 配置不一致

**位置**: `Src/dma/dma2.c` 和 `Src/main.c`

**问题描述**:
- `dma2.c` 中: `cr |= (2 << 10);` PSIZE = 32-bit (DMA_SxCR)
- `main.c` 中: `cr |= (2 << 11);` 也是 PSIZE = 32-bit

两处代码都配置 PSIZE，但 bit 位移不同:
- `dma2.c` 使用 `(2 << 11)` 对应 DMA_SxCR 的 PSIZE[12:11]
- `main.c` 使用 `(2 << 11)` 也是 PSIZE

实际上两处都使用 (2 << 11)，但 dma2.c 中有 `(1 << 8)` 用于 CIRC，main.c 中 `(1 << 6)` 用于 DIR。

**潜在问题**:
- dma2.c 和 main.c 重复初始化 DMA Stream5
- 如果 `dma2_s5_gpio_init()` 在 `main.c` 之前调用，main.c 会覆盖其配置
- 两套配置可能不一致

**影响**: 难以追踪哪些配置实际生效。

**建议修复**: 统一在 `dma2.c` 中配置，或完全在 `main.c` 中配置，避免双重初始化。

---

### R007: ADC 配置中 EXTSEL 位错误

**位置**: `Src/main.c`

**问题描述**:
```c
ADC1_CFGR  = (1 << 0) | (1 << 1) | (0 << 3) | (10 << 10);
//                              ^^^^^^^^     ^^^^^^^^
//                              RES=12bit    EXTSEL[10:6]?
```

**问题**:
- EXTSEL 应该是 5-bit 字段 (bits [10:6])，但 `(10 << 10)` 设置的是 bit 10-14
- RM0468: EXTSEL[4:0] 位于 bits [10:6]
- 正确值应该是 `(正确EXTSEL值 << 6)`

**影响**: ADC 触发源可能不是 TIM1_TRGO，导致 ADC 不启动或触发错误。

**建议修复**:
```c
// 确认 TIM1_TRGO 对应的 EXTSEL 值
// 通常 TIM1_TRGO = 10010 (18) 或类似值
ADC1_CFGR = (1 << 0) |   // DMAEN
            (1 << 1) |   // DMACFG (circular)
            (0 << 3) |   // RES = 12-bit
            (正确值 << 6);  // EXTSEL = TIM1_TRGO
```

---

### R008: DMA ISR 标志清除时序

**位置**: `Src/main.c` ISR 开始处

**问题描述**:
```c
void TIM1_UP_IRQHandler(void) {
  // ... 记录抖动 ...
  GPIOE_ODR ^= (1 << 2);   // 翻转 PE2
  uint32_t t0 = ccnt();
  TIM1_SR = 0;             // 清 UIF
  ADC1_ISR = (1 << 4);     // 清 OVR
  // ...
}
```

**问题**:
- 清除 TIM1_SR 和 ADC1_ISR 在中断处理**早期**，但可能未完成清除就进入下一步
- DMA 传输完成标志 (LIFCR/HIFCR) 在初始化时清除，但 ISR 中未处理

**影响**: 如果 DMA 传输中发生错误，ISR 无法检测到，错误会累积。

**建议修复**:
```c
void TIM1_UP_IRQHandler(void) {
  // 先检查 DMA 状态
  if (DMA2_HISR & ((1 << 5) | (1 << 3))) {  // TEIF5 | DMEIF5
    // DMA 错误，清除并报告
    DMA2_HIFCR = (1 << 5) | (1 << 3);
    // 记录错误
  }
  
  // 清除 TIM1 和 ADC 标志
  TIM1_SR = 0;
  ADC1_ISR = (1 << 4);
  // ...
}
```

---

## 🟢 低危风险

### R009: 代码注释与实际不符

**位置**: 多处

**示例**:
- `Src/main.c` 注释 "100μs TIM1"，但实际取决于时钟配置
- `dma2.c` 注释 "Stream 0 保留"，但代码未处理此限制
- `registers.h` 注释 "H723: AHB1ENR @ 0xD8 (H743:xD0!)"，但分号错误

---

### R010: 硬编码测试路由与生产代码混合

**位置**: `Src/main.c`

**问题**: 
- `deploy_test_routes()` 函数硬编码了 32 条测试路由
- 在生产部署时，这些路由会与 DCL 部署的路由混合

**影响**: 可能导致意外的 GPIO 输出行为。

---

### R011: FDCAN1 接收 FIFO 深度配置

**位置**: `Src/main.c`

**问题**:
```c
FDCAN1_RXF0C = (1<<31) | (FDCAN1_RX_FIFO0_OFFSET / 4); /* 4个元素 */
```

如果元素大小不是 4 字节，计算可能有误。

---

### R012: 启动代码 DTCM 使能

**位置**: `Startup/startup_stm32h723zgtx.s`

**问题**: 
- 启动代码通常需要使能 DTCM 时钟
- 如果 RCCR 或 DTCM 相关寄存器未配置，DTCM 可能未上电

---

## 📊 风险矩阵

| 风险 ID | 类别 | 可能性 | 影响 | 优先级 | 状态 |
|---------|------|--------|------|--------|------|
| R001 | 内存管理 | 高 | 高 | 🔴 P0 | 必修 |
| R002 | DMA/总线 | 中 | 高 | 🔴 P0 | 必修 |
| R003 | 并发/竞争 | 中 | 中 | 🔴 P0 | 必修 |
| R004 | 看门狗 | 中 | 高 | 🟡 P1 | 建议修 |
| R005 | 时钟配置 | 中 | 高 | 🟡 P1 | 建议修 |
| R006 | DMA 配置 | 低 | 中 | 🟡 P2 | 建议修 |
| R007 | ADC 配置 | 中 | 高 | 🟡 P1 | 建议修 |
| R008 | ISR 处理 | 低 | 中 | 🟡 P2 | 建议修 |
| R009 | 代码文档 | 低 | 低 | 🟢 P3 | 可选 |
| R010 | 功能设计 | 低 | 中 | 🟢 P3 | 可选 |
| R011 | 外设配置 | 低 | 低 | 🟢 P3 | 可选 |
| R012 | 启动流程 | 中 | 高 | 🟡 P1 | 建议修 |

---

## 🔧 修复优先级建议

### 第一阶段 (立即修复)
1. **R001**: 在链接脚本中添加 DTCM 定义，确保启动代码初始化 DTCM
2. **R002**: 验证 DMA2 跨域访问是否可靠，或改用 BDMA
3. **R003**: 实现 SHADOW_GPIO 双缓冲或时序隔离

### 第二阶段 (尽快修复)
4. **R005**: 确认 TIM1 实际时钟频率，重新计算 ARR
5. **R007**: 修正 ADC EXTSEL 位配置
6. **R004**: 优化 IWDG 喂狗策略

### 第三阶段 (计划修复)
7. **R006**: 统一 DMA 配置位置
8. **R008**: 增强 ISR 错误处理
9. **R012**: 验证启动代码 DTCM 使能

---

## 📝 验证建议

### 功能验证
1. 使用逻辑分析仪测量 GPIO 输出时序，确认 100μs 周期
2. 验证 DMA 传输完成中断是否触发
3. 测试 IWDG 在 UART 阻塞时的行为

### 性能验证
1. 测量 DMA 跨域访问延迟 (D2→D3)
2. 测试 SHADOW_GPIO 竞争条件 (高频写入 + DMA 读取)
3. 验证 ADC 触发时刻与 TIM1 的关系

### 稳定性验证
1. 长时间运行测试 (24h+)
2. 边界条件测试 (最大路由数、最高 GPIO 翻转频率)
3. 故障注入测试 (断开 CAN、阻塞 UART)

---

## 📚 参考文档

- RM0468: STM32H723 Reference Manual (Section 2: Memory and bus architecture)
- AN5293: STM32H7 Series system architecture and performance
- ES0491: STM32H723 errata sheet

---

*文档生成时间: 2026-07-14*  
*审查人: AI Code Review System*
