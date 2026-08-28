# Changelog

本文件记录研究性发布的里程碑。版本号不代表产品成熟度，仅标记代码快照。

## [v0.1.1] — 2026-08-27 · Production-ready 100µs @ 480 MHz

修正 v0.1.0 的频率口径并补齐入门文档，指向最新可用代码。

- **CPU 频率修正为 480 MHz**（生产配置，与 `main.c` `CLOCK_HZ=480000000` 一致；v0.1.0 误标 544 MHz）
- 新增 `docs/QUICKSTART.md`——15 分钟从零跑到第一个 100µs 程序（SWD + UART 双通道）
- 修正 `firmware/h723-core0/Src/tim1/tim1_pwm.c` 遗留 544 MHz 时代寄存器值（ARR 13599→11999 等）
- 厘清 1µs 引擎口径：解释器模式=已实测（136 MHz / ARR=135），JIT 编译块=研究（卡 UNDEFINSTR）

## [v0.1.0] — 2026-08-27 · Initial research release

首个开源快照。定位为**确定性控制引擎研究**，非工业产品。

### 核心内容
- **`firmware/h723-core0`** — 100µs 确定性路由表引擎（TIM1 硬件定时 ISR，DTCM 零等待，无 OS / 无动态分支）
- **`ide/compiler`** — DCL 声明式语言 → 路由表二进制编译器（35 个原语 opcode）
- **`ide/server` + `ide/shell`** — SWD / UART 部署 + RTT 非侵入监控
- **`tools/`** — pyocd 抖动测量 / 调试脚本（可复现测量方法）
- **`docs/`** — 理论、架构、抖动测量、诚实自审、`1µs-JIT` 研究交接、核心0 自有技术手册
- **研究分支**：`h723-core0-1us`（前沿 1µs 周期探索）、`h723-ether-test`、

### 已实测指标（bench 级，非产品级）
| 指标 | 值 |
|---|---|
| ISR 周期 | 100.000 µs（可配至 1 µs） |
| 输出链路抖动（DMA 搬运 + ITCM 零等待） | **< 7 ns（实测）**（L2 抖动确定性，见 `docs/00-vision/FUTURE-ROADMAP.md`） |
| 周期采样 σ（ISR 入口，参考） | ~37 ns（仅测入口时刻，非输出） |
| 最小稳定周期 | 5.00 µs（200 kHz） |
| ISR 执行时间 | 4.86–4.99 µs（43 路由） |
| CPU 频率 | 480 MHz（VOS0 + PLL，H723 规格内；`1µs` 研究分支为 544 MHz 超频） |
| 原语数 | 35 |
| 容量 | 路由 1024 / 参数 512 / 状态 256 / WIRE 1024（DTCM） |
| 通信 | USART2 115200 8N1（部署协议，已验证）· FDCAN1 CANopen 500kbps（实现）· 以太网 RMII（研究未通） |

### 诚实已知局限
- 输出链路抖动 < 7 ns 为实测；架构上输出边沿理论下限 <0.5 ns（DMA 锁存），但尚无示波器实测记录；循环周期级确定性（< 10 ns 周期抖动）需外部晶振 + 硬件同步，超出当前范围
- 仅在 bench 验证，无温漂 / EMI / 连续 72h 长期可靠性数据
- 闭环到物理执行器为近期才通，未挂真实被控对象长期跑
- "自然语言 → DCL 编译"大脑链路未完成
- Ethernet 高速通信因 PCB/杜邦线问题未打通

### 许可证
自有代码 MIT。`firmware/h723-core0/lib/` 含 ST HAL（ST 许可）、lwIP（BSD）、jQuery（MIT），各自保留原许可。
