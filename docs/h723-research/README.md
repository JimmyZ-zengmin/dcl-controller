# 核心0 V2.0 — 1μs 零抖动增路由专项

> 基于 core0_h723 544MHz 基线，目标：1μs 周期下最大化路由容量

## 基线 (来自 core0_h723)

| 指标 | 值 |
|------|-----|
| CPU | STM32H723ZGT6 @ 544MHz |
| ISR 周期 | 1μs (ARR=135 @136MHz TIM1) |
| 当前路由 | 4 条 (SCALE+LPF+PID+CLAMP) |
| ISR 执行 | 0.73μs (398/544 cyc) |
| ISR 开销 | ~340 cyc |
| 抖动 | 0 (PERIOD_EXACT=100%) |

## 目标

通过 B.4 汇编优化将 ISR 开销从 340cyc 压到 145cyc，
1μs 周期下路由数从 4 条提升到 8-10 条。

## 项目结构

```
├── Src/main.c              # ISR 引擎 + 原语 + 测试程序
├── Startup/startup_*.s      # ITCM 拷贝启动代码
├── STM32H723ZGTX_FLASH.ld   # 含 ITCM section 的 linker script
├── B4-汇编优化规划.md        # 汇编优化方案
└── README.md                # 本文件
```

## 与 core0_h723 的区别

- core0_h723: 100μs 基线项目，完整调试历史
- core0_h723_1us: 1μs 专项，只关注汇编优化和路由容量
