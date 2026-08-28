# v0.1.1 — 生产可用版（100µs @ 480 MHz）

> **更新自 v0.1.0**：修复 CPU 频率口径、补充快速上手文档，指向最新可用代码。
> 定位：确定性控制引擎研究（非工业产品）。

## 本次更新（vs v0.1.0）

- 🎯 **CPU 频率修正为 480 MHz**（生产配置，与固件 `SystemInit` 的 `CLOCK_HZ=480000000` 一致；v0.1.0 误标 544 MHz）
- 📖 **新增 `docs/QUICKSTART.md`**——15 分钟从零跑到第一个 100µs 程序（含 SWD / UART 双通道）
- 🔧 **修正 `tim1_pwm.c` 遗留的 544 MHz 时代寄存器值**（ARR 13599→11999 等，对齐 480 MHz 生产配置）
- ✍️ **厘清 1µs 引擎两口径**：解释器模式=已实测（136 MHz / ARR=135），JIT 编译块=研究（卡 UNDEFINSTR）

## 生产可用内容（本 release 的核心）

| 组件 | 说明 | 状态 |
|------|------|------|
| `firmware/h723-core0` | 100µs 确定性路由表引擎（TIM1 硬件 ISR + DTCM 零等待 + DMA 硬件输出） | ✅ 生产配置 |
| — CPU 480 MHz / TIM1 120 MHz / ARR=11999 | 100µs 固定周期 | ✅ 已实测 |
| — USART2 @ 115200 8N1 部署协议 | UART 部署/遥测 | ✅ 已验证 |
| — FDCAN1 CANopen 500 kbps | CAN 通信 | ✅ 已实现 |
| `ide/compiler` | DCL 语言 → 路由表二进制（35 原语） | ✅ 可用 |
| `ide/shell` + `ide/server` | SWD/UART 部署 CLI + RTT 监控 | ✅ 可用 |
| `tools/` | 抖动测量 / 调试脚本 | ✅ 可复现 |

## 已实测指标（bench 级）

| 指标 | 值 |
|------|-----|
| ISR 周期 | **100.000 µs**（TIM1 硬件锚点） |
| CPU 频率 | **480 MHz**（VOS0 + PLL，H723 规格内） |
| 输出链路抖动（DMA 搬运 + ITCM 零等待） | **< 7 ns（实测）** | L2 抖动确定性，见 `docs/00-vision/FUTURE-ROADMAP.md` |
| 周期采样 σ（ISR 入口，参考） | ~37 ns（仅测入口时刻，非输出） |
| 原语 | 35 个（PID / LPF / CMP / TIMER / COUNTER…） |
| 容量 | 路由 1024 / 参数 512 / 状态 256 / WIRE 1024 |

## 快速上手（详见 docs/QUICKSTART.md）

```bash
# 1. 编译固件（一次性）
cd firmware/h723-core0 && build.bat
py -3 -m pyocd flash -t stm32h723xx build/core0_h723.elf
py -3 -m pyocd reset -t stm32h723xx

# 2. 编译 DCL 程序
cd ide/compiler && python dcl_compiler.py reactor_control.dcl -o reactor_control.bin

# 3. 部署 + 启动（免重烧）
cd ide && python shell/main.py --cli
> :e reactor_control.dcl  # 加载
> :c                      # 编译
> :d                      # 部署
> :start                  # 启动引擎
> :m                      # 监控
```

无 ST-Link 可用 UART 烧录：`firmware/h723-core0/flash_uart.bat`（CH340 + BOOT0，115200）。

## 已知局限（诚实声明）

- 输出链路抖动 < 7 ns 为实测；架构上输出边沿理论下限 <0.5 ns（DMA 锁存），但尚无示波器实测记录；**循环周期级**确定性（< 10 ns 周期抖动）需外部晶振 + 硬件同步，超出当前范围
- 仅 bench 级验证，无温漂 / EMI / 72h 长期数据
- Ethernet（RMII + lwIP）研究分支因 PCB 信号完整性未通；可靠通道 = UART + SWD + CANopen
- "自然语言 → DCL 编译"大脑链路未完成
- `firmware/h723-core0-1us`（1µs / 544MHz 超频）与 `h723-ether-test` 为**研究分支**，非生产配置

## 下载

- Source code (zip) / (tar.gz) — 本 release 自动生成的源码快照
