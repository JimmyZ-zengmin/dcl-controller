# 待解决问题清单

> **更新日期**: 2026-07-13 | **状态**: 核心功能验证通过

---

## 🔴 高优先级

### 1. DMA Stream 5 M0AR 写入无效
**现象**: 固件写入 `DMA2_S5M0AR = DTCM_BASE + 0x00E0 = 0x200000E0`, 但读回始终为 `0x000000A0`
**影响**: DMA 路径失效, 当前由 ISR 直接写 `GPIOE_ODR` 兜底, 功能正常
**根因**:
- DMA 保持在 EN=1 状态, STM32H7 下 EN=1 时 M0AR 寄存器写保护
- 需先清 EN (`DMA2_S5CR = 0`) 再写 M0AR
**修复方向**:
```c
// 正确的 DMA 配置顺序:
DMA2_S5CR = 0;           // 1. 先禁能
while (DMA2_S5CR & 1);   // 2. 等待 EN 清零
DMA2_S5M0AR = 0x200000E0; // 3. 写 M0AR
DMA2_S5PAR = 0x58021014;  // 4. 写 PAR (GPIOE_ODR)
DMA2_S5NDTR = 1;         // 5. 传输次数
DMA2_S5CR = config;      // 6. 配置 + 使能
```

### 2. ADC_RAW 地址冲突
**现象**: `ADC_RAW` 定义在 `DTCM_BASE + 0x0290`, 位于 `ACTUATOR_STATUS[0..63]` (0x0200-0x02FF) 范围内
**影响**: ADC DMA 写入会破坏 ACT[36], 当前 ADC 在 `#if 0` 状态, 暂未触发
**修复**: 将 ADC_RAW 移到 DTCM 空闲区域, 如 `DTCM_BASE + 0x0040` (TIMING 之后)

---

## 🟡 中优先级

### 3. PE2 诊断翻转代码 (已识别, 待清理)
**位置**: `main.c` ISR 入口: `GPIOE_ODR ^= (1 << 2);`
**影响**: 每 100μs 翻转 PE2, 干扰 GPIO 输出示波器观察
**状态**: 功能上无影响 (ISR 后面会正确覆盖), 但示波器看 ISR 触发沿有毛刺
**修复**: 删除或移至 `#if 0` 诊断模式

### 4. SHADOW_GPIO 地址冲突 (已修复)
**修复**: SHADOW_GPIO 移到 `DTCM+0x00E0`
**待验证**: 全 256 路由压力下无地址踩踏 (待压力测试)

### 5. 内存布局系统化 (待实施)
**现状**: 所有地址硬编码在 main.c, 易冲突
**修复**: 新建 `dtcm_layout.h`, 统一管理

---

## 🟢 低优先级 / 优化项

### 6. DMA 硬件触发 vs ISR 直接写 (待决策)
**现状**: ISR 内 `GPIOE_ODR = gpio_out` + DMA Stream 5 同时存在
**问题**: 两者写同一地址, 可能冲突
**建议**: 二选一
- 方案 A: 修复 DMA, 移除 ISR 直接写 (硬件触发, 零 CPU)
- 方案 B: 永久禁用 DMA Stream 5, 始终 ISR 直接写 (简单可靠)

### 7. 编译器 ↔ 固件同步 (待完善)
**现状**: 编译器生成 binary, deploy() 直写 DTCM
**改进**: 考虑通过 UART 命令通道实现更可靠的部署协议

---

## ✅ 已解决问题 (2026-07-13)

### P0: 544MHz 时钟不稳定 (已修复)
**根因**: PLL VCOSEL=0 用于 448MHz VCO, 超出规格
**修复**: 改为 VCOSEL=1 (宽范围 192-836MHz)
**验证**: PERIOD_MIN=54384 cycles (100μs), jitter=66ns
**文档**: `dev-log-2026-07-13-clock-fix.md`

### P1: PE2 诊断翻转导致 GPIO 输出异常 (已定位)
**根因**: PE2 在 ISR 入口翻转, 但后续被正确值覆盖
**结论**: 理论上不应影响逻辑正确性, 之前的 PE2 "粘滞高" 可能由其他原因导致
**验证**: 当前 GPIO 测试通过, PE 输出正确

### P2: Wire 值全为 0 (已定位)
**根因**: CONST 传感器值在 PARAM_TABLE 中, 但 SENSOR_MAP 从未被赋值
**解决**: 测试时使用 `set_sensor()` 手动注入 SENSOR_MAP
**结论**: 部署流程正确, 只是 CONST 传感器的自动加载机制待完善

---

## 相关文件

- `firmware/h723-core0/Src/main.c` — 固件主文件
- `firmware/h723-core0/Src/dma.c` — DMA 配置
- `docs/dev-log-2026-07-13-clock-fix.md` — 时钟修复日志
- `docs/dev-log-2026-07-13-gpio-verify.md` — GPIO 验证日志
- `docs/dev-log-2026-07-13-gpio-debug.md` — GPIO 调试日志
- `ide/compiler/diag_m0ar.py` — DMA M0AR 诊断
- `ide/compiler/diag_gpio_chain.py` — GPIO 链路诊断
