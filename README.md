# DCL — 确定性控制引擎（Deterministic Control Engine）

> 在通用 MCU 上研究"接近硬件级确定性"的控制引擎：100 μs 硬件定时周期、**输出链路抖动实测 < 10 ns**（DMA 搬运 + ITCM 零等待，与 CPU 计算解耦）、用一张"路由表"而非"程序"驱动 GPIO/PWM。
>
> 这是一项**研究项目**，不是工业产品。它的价值在于验证了"用架构消除不确定性"这一路线在 Cortex-M7 上可行，并提供了可复现的测量方法。

---

## 它是什么

传统 MCU 控制靠 RTOS 调度，周期通常在 ms 级、抖动数十 μs。本项目换了一条路：

- **系统引擎**（固件，一次部署）：一个 100 μs 的 TIM1 硬件定时器 ISR，在 ITCM 里顺序扫描一张**路由表（route table）**，逐条完成"读源 → 算原语 → 写目标"，结果直接落到 GPIO/PWM。
- **运行程序**（IDE 部署，可反复覆盖）：用户用 DCL 声明式语言写控制逻辑，编译器把它编译成路由表二进制，通过 SWD / UART 下发到 DTCM 运行区。

无 OS、无缓存不确定度、无动态分支——执行时间在编译期即可预算，而不是靠调度器"尽量公平"。

### 已实测的核心指标

| 指标 | 数值 | 说明 |
|---|---|---|
| ISR 周期 | **100.000 μs**（可配至 1 μs，仅属极限验证） | TIM1 硬件定时器强制时间锚点 |
| 输出链路抖动（DMA 搬运 + ITCM 零等待） | **< 10 ns（实测）** | L2 抖动确定性实测，见 `docs/00-vision/FUTURE-ROADMAP.md`（✅ 已测得）。输出时刻由硬件锁存，与 CPU 入口无关 |
| 周期采样 σ（ISR 入口，参考） | ~37 ns（主峰） | 仅测 ISR 入口时刻的波动，**不代表输出抖动**（见下方"诚实边界"） |
| 最小稳定周期 | **5.00 μs（200 kHz）** | 见 `docs/DCL_ISR极限压测报告.md` |
| ISR 执行时间 | 4.86–4.99 μs（43 路由） | 同上 |
| CPU 频率 | 480 MHz（VOS0 + PLL，H723 规格内） | 见 `firmware/h723-core0/Src/main.c`（`CLOCK_HZ = 480000000`） |
| 原语 | 35 个操作码 | PID / LPF / CMP / TIMER / COUNTER / … |
| 容量 | 路由 1024 / 参数 512 / 状态 256 / WIRE 1024 | DTCM 布局 |

**诚实对标**（循环周期级抖动，DCL 为实测值、其余为公开数据，详见 `docs/核心思想-零抖动方案.md`）：

| 设备 | 周期 | 抖动 | 说明 |
|---|---|---|---|
| **DCL（H723）** | 100 μs | **< 10 ns（实测）** | 输出链路（DMA 搬运 + ITCM 零等待） |
| 西门子 S7-1200 | 1 ms | ~25 ns | DCL 快 ~200–2000×，抖动更小 |
| Beckhoff BX5100 | 50 μs | ~5 ns | 与 DCL 同一量级 |
| B&R X20 | 100 μs | ~10 ns | 与 DCL 同一量级 |
| FPGA (Zynq) | 1 μs | ~0.5 ns（器件规格） | DCL 差一个量级（MCU 时钟树天花板） |

> 🔬 **关于「< 0.5 ns」**：架构上 DMA 锁存让输出边沿的理论下限可到 <0.5 ns（74LVC574 锁存器同款原理），但该数目前为**理论推断、尚无示波器实测记录**，故本 README 不将其列为指标，只列实测的 <10 ns。诚实比好看重要。

> ⚠️ **关于 37 ns 的澄清**：README 早期版本把"~37 ns 周期采样抖动"（2026-07-14 旧报告 `docs/JITTER-MEASUREMENT.md`，测的是 ISR **入口**时刻，且 TIM1@136MHz 与 DWT@240MHz 跨时钟域有量化台阶）当成了输出指标，这是**测量方法误导**——它既非 GPIO 输出抖动，也与最终输出质量无关。本项目**输出链路抖动实测 < 10 ns**（L2 抖动确定性，见 `docs/00-vision/FUTURE-ROADMAP.md`）。

---

## 核心思想：为什么能做到近乎 0 抖动

> 一句话：**DCL 不追求「让 CPU 算得绝对准时」，而是「让 CPU 根本不负责输出时刻」。计算随便抖，输出由硬件在固定节拍锁存——抖动因此变得无关紧要。**

传统「定时器中断 → 中断里算完直接写 IO」的路径里，**「CPU 算完的时刻」就是「输出生效的时刻」**，所以 CPU 入口那点抖动（中断响应、总线竞争）会被原封不动传导到引脚（我在 ESP32 上实测过：输出抖动 462 ns p-p，恰好等于 ISR 入口抖动）。

DCL 的做法是把**计算和输出彻底解耦**：

- ISR 末尾只把结果写进 `SHADOW_GPIO`（DTCM 影子缓冲，**无 timing 要求**）；
- 一条**独立于 CPU 的硬件链路**负责输出：`TIM1` 在周期末尾（CC4≈97.5 μs）触发 → `DMAMUX` 路由到 `DMA2 Stream5` → DMA 硬件把 `SHADOW_GPIO` 搬到 `GPIOE_ODR`，引脚在同一刻翻转，**零抖动**；
- PWM 同理走 TIM1 预装载影子寄存器 + 更新事件统一装载。

解耦要成立，靠三根支柱撑着：① **硬件定时锚点**（TIM1 强制 100 μs，不在软件里）；② **DTCM 零等待**（无 cache miss，也天然避开多核缓存一致性噩梦）；③ **路由表无动态分支**（执行时间编译期可预算，无 M7 分支预测失败惩罚）。

**诚实边界**：早期报告里的「~37 ns 周期采样抖动」测的是 *ISR 入口* 时刻的波动（且跨时钟域，见 `docs/JITTER-MEASUREMENT.md`），**不代表 GPIO 输出质量**；**输出链路（DMA 搬运 + ITCM 零等待）抖动实测 < 10 ns**。周期采样的抖动 ≠ 输出的抖动——输出时刻由硬件锁存，与 CPU 入口无关。架构上输出边沿的理论下限为 <0.5 ns（DMA 锁存），但该值尚无示波器实测记录，不作指标。完整推导与代码位置见 👉 **[`docs/核心思想-零抖动方案.md`](docs/核心思想-零抖动方案.md)**。

**核心思想四篇**（确定性闭环：输入 → 计算 → 输出，写给第一次点进来的访客，从"为什么牛"讲起）：

1. [数据确定性：数据是哪一刻的](docs/核心思想-数据确定性.md) — 输入侧：采样时刻由硬件锁存（TIM1_TRGO→ADC→DMA），一拍延迟语义
2. [确定性：拿掉不确定性的源](docs/核心思想-确定性.md) — 计算侧：输出为什么总在准点发生
3. [低周期：在更短时间塞进同样的计算](docs/核心思想-低周期.md) — 计算侧极限：周期为什么能压到 5 μs（实测）/ 极限验证到 1 μs（136 MHz）
4. [零抖动：计算与输出解耦](docs/核心思想-零抖动方案.md) — 输出侧：边沿为什么稳（实测 < 10 ns）

---

## 目录结构

```
dcl-controller/
├── firmware/
│   ├── h723-core0/        # 生产固件：100 μs 确定性引擎（已验证）
│   ├── h723-core0-1us/    # 研究：1 μs 极限验证版（解释器模式实测跑通；JIT 编译块研究中）— 证明架构边界，非生产配置（生产用 100 μs）
│   ├── h723-ether-test/   # 研究：以太网 PHY 测试（硬件未定型，未验证）
│   └── tests/             # UART / 抖动 Python 测试
├── ide/
│   ├── compiler/          # DCL 编译器：声明式源码 → 路由表二进制
│   ├── server/            # WS 部署 + RTT 非侵入监控服务
│   ├── shell/             # CLI / GUI 入口
│   └── web/               # React + Monaco Web IDE
├── tools/                 # ~80 个 pyocd 调试 / 抖动测量脚本
├── DCL-HPC/servo-drive/   # 研究：伺服连续电流环架构
├── docs/                  # 文档体系（理论 / 架构 / 抖动测量 / 研究交接）
├── AGENTS.md              # 项目权威指南（架构、内存映射、坑）
└── CLAUDE.md              # 开发上下文
```

---

## 快速开始

> 📖 **新用户先看 [docs/QUICKSTART.md](docs/QUICKSTART.md)**——15 分钟从零跑到第一个 100µs 程序（含 UART 烧录/部署两种通道）。

### 1. 编译 DCL 程序

```bash
cd ide/compiler
python dcl_compiler.py reactor_control.dcl -o reactor_control.bin
# 额外：--json 看路由表展开，--c 看 C 等价代码
```

### 2. 烧录固件（STM32H723ZG @ 480 MHz，Cortex-M7）

```bash
cd firmware/h723-core0
build.bat                       # 需 arm-none-eabi-gcc（GNU Tools for STM32）
py -3 -m pyocd flash -t stm32h723xx build/core0_h723.elf
py -3 -m pyocd reset -t stm32h723xx
```

> ⚠️ pyOCD target **必须**是 `stm32h723xx`（非默认 `cortex_m`），否则外设寄存器读出 0xFF/0x00。
> ⚠️ IWDG 看门狗上电自动启动（~512 ms），`main()` 第一行必须喂狗。
> 详见 `AGENTS.md` 的 Critical Gotchas。

### 3. 部署运行程序（不用重烧固件）

```bash
cd ide
python shell/main.py --cli
> :e reactor_control.dcl     # 加载
> :c                         # 编译
> :d                         # 部署（SWD 直写 DTCM 运行区）
> :start                     # 启动引擎
> :m                         # 监控 RTT
```

UART 备用部署：USB-TTL 接 USART2（PD5/PD6, 115200 bps），帧 `[0xC0][cmd][len:2B][payload][CRC16:2B]`。

### 4. 非侵入监控

```bash
py -3 -m pyocd rtt -t stm32h723xx -a 0x20008000 -s 0x1000
# 输出形如：S=183000 P=23978..24022 R=40 E=1  (SAMPLES / PERIOD / ROUTES / ENGINE)
```

---

## 架构要点

- **空间换时间**：MCU 不"跑程序"，而是跑一张路由表。每条路由 = 一个"源 → 原语 → 目标"的数据流节点，编译期用 Kahn 算法拓扑排序保证生产者先于消费者。
- **确定性三层次**（详见 `docs/FUTURE-ROADMAP.md`）：
  - L1 周期确定性：硬件定时器强制时间锚点 ✅ 已验证
  - L2 抖动确定性：DMA 搬运 + ITCM 零等待 ✅ 已实测（< 10 ns）
  - L3 计算确定性：空间数据流（无 CPU 瓶颈）⬜ 研究方向
- **数据的时间语义**：传感器/ADC 输入由 **TIM1_TRGO + DMA 在硬件侧采样锁定**（CPU 不参与），ISR 读到的是**上一周期**的采样快照——一拍延迟，采样与计算解耦；内部 **WIRE 信号**同周期按拓扑序流式传递（编译器 Kahn 排序，生产者先于消费者）；周期 N 的输出基于周期 N-1 的传感器采样，与 PLC 扫描 / 运动控制器语义一致。见 `docs/00-vision/CORE-THEORY.md`（火车模型）
- **DTCM 128 KB 零等待 + ITCM 64 KB**：路由表与 ISR 都在零等待内存，消除 cache miss 引入的抖动。

内存布局、RouteEntry 结构、Actuator 映射见 `docs/MEMORY-MAP.md` 与 `AGENTS.md`。

---

## 已知局限（请务必先读）

本项目**不是**工业 PLC 替代品，以下局限是真实的：

1. **输出链路抖动 < 10 ns 是实测结果（DMA 搬运 + ITCM 零等待），但循环周期级确定性（< 10 ns 周期抖动）仍受晶振约束**：温度/流量/压力 PID 场景足够；若需晶振级确定性，需要外部晶振 + 硬件同步，超出本方案当前范围。早期 37 ns 口径已澄清为 ISR 入口采样误导（见上）。
2. **闭环到物理执行器是近期才打通的**，仅在 bench 级验证，没有挂真实被控对象长期跑（无温漂 / EMI / 连续 72 h 数据）。
3. **高速通信（以太网）尚未打通**：受限于硬件 PCB 与杜邦线信号完整性，当前可靠通道只有 UART/SWD。
4. **1 μs 极限验证版已实测跑通（非生产配置）**：`h723-core0-1us` 在**解释器模式**（`USE_COMPILED_ISR=0`，ARR=135 @136 MHz = 136 cycles）下实测跑通 1 μs、8 路由零抖动，证明架构在 136 cycles 极限下仍成立。**JIT 编译块模式**（`USE_COMPILED_ISR=1`，路由表→ITCM 机器码）仍在研究中——运行时生成的 VFP 指令从 ITCM 执行会触发 `UNDEFINSTR`（见 `docs/h723-research/HANDOVER.md`），`DSB+ICIALLU` 一致性修复解决的是取指一致性，并非该根因。日常部署用 100 μs（输出链路抖动 <10 ns 实测，可长期稳定）。

`docs/DCL闭环自查报告.md` 是一份**诚实的自审**（含历史致命问题清单）。其中多数致命项已在当前代码中修复（如 `OP_REG` 空实现、`BIT*` 原语未实现、`CMP` 仅支持 `>`、OUTPUT→执行器映射断路），但请把它当作"研究透明度"而非"现状描述"来读。

---

## 第三方代码

`firmware/h723-core0/lib/` 包含第三方组件，各自保留原许可证：

- **STM32CubeH7 HAL / CMSIS**（STMicroelectronics）— ST 许可证，允许带声明再分发
- **lwIP**（瑞典计算机科学研究所）— BSD 风格许可证
- **jQuery** 等文档/工具依赖 — MIT

本项目自有代码以 MIT 许可证发布（见 `LICENSE`）。ST 官方参考手册（RM0468 等 PDF）**未随仓库分发**，请自行从 ST 官网获取。

---

## 许可证

本项目自有代码：MIT License（见 `LICENSE`）。
