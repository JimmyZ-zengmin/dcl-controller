# DCL — 确定性控制引擎（Deterministic Control Engine）

> 在通用 MCU 上研究"接近硬件级确定性"的控制引擎：100 μs 硬件定时周期、约 37 ns 周期抖动、用一张"路由表"而非"程序"驱动 GPIO/PWM。
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
| ISR 周期 | **100.000 μs**（可配至 1 μs） | TIM1 硬件定时器强制时间锚点 |
| 周期抖动 σ | **~37 ns（主峰）/ ~144 ns（含离群）** | 见 `docs/JITTER-MEASUREMENT.md` |
| 周期峰峰值 | **±83 ns** | 均值 99.998 μs |
| 最小稳定周期 | **5.00 μs（200 kHz）** | 见 `docs/DCL_ISR极限压测报告.md` |
| ISR 执行时间 | 4.86–4.99 μs（43 路由） | 同上 |
| CPU 频率 | 544 MHz（VOS0 + PLL 超频） | — |
| 原语 | 35 个操作码 | PID / LPF / CMP / TIMER / COUNTER / … |
| 容量 | 路由 1024 / 参数 512 / 状态 256 / WIRE 1024 | DTCM 布局 |

**诚实对标**（抖动与速度，详见 `docs/JITTER-MEASUREMENT.md` 与 `docs/DCL_ISR极限压测报告.md`）：

| 设备 | 周期 | 抖动 σ | 相对 DCL |
|---|---|---|---|
| **DCL** | 100 μs | ~37 ns | 基准 |
| 西门子 S7-1200 | 1 ms | ~25 ns | DCL 快 ~200–2000×，抖动略逊 |
| Beckhoff BX5100 | 50 μs | ~5 ns | 抖动差 ~4–8× |
| B&R X20 | 100 μs | ~10 ns | 抖动差 ~4–8× |
| FPGA (Zynq) | 1 μs | ~0.5 ns | 量级差距 |

> 结论：DCL 在**通用 MCU** 上把确定性和速度做到了远超传统 PLC 的水平，但抖动仍比专用硬件（Beckhoff/B&R/FPGA）差 4–8×。这是 MCU 方案的物理天花板，也是我们做这项研究的意义所在。

---

## 核心思想：为什么能做到近乎 0 抖动

> 一句话：**DCL 不追求「让 CPU 算得绝对准时」，而是「让 CPU 根本不负责输出时刻」。计算随便抖，输出由硬件在固定节拍锁存——抖动因此变得无关紧要。**

传统「定时器中断 → 中断里算完直接写 IO」的路径里，**「CPU 算完的时刻」就是「输出生效的时刻」**，所以 CPU 入口那点抖动（中断响应、总线竞争）会被原封不动传导到引脚（我在 ESP32 上实测过：输出抖动 462 ns p-p，恰好等于 ISR 入口抖动）。

DCL 的做法是把**计算和输出彻底解耦**：

- ISR 末尾只把结果写进 `SHADOW_GPIO`（DTCM 影子缓冲，**无 timing 要求**）；
- 一条**独立于 CPU 的硬件链路**负责输出：`TIM1` 在周期末尾（CC4≈97.5 μs）触发 → `DMAMUX` 路由到 `DMA2 Stream5` → DMA 硬件把 `SHADOW_GPIO` 搬到 `GPIOE_ODR`，引脚在同一刻翻转，**零抖动**；
- PWM 同理走 TIM1 预装载影子寄存器 + 更新事件统一装载。

解耦要成立，靠三根支柱撑着：① **硬件定时锚点**（TIM1 强制 100 μs，不在软件里）；② **DTCM 零等待**（无 cache miss，也天然避开多核缓存一致性噩梦）；③ **路由表无动态分支**（执行时间编译期可预算，无 M7 分支预测失败惩罚）。

**诚实边界**：README 里的「~37 ns 周期抖动」测的是 *ISR 入口* 的抖动；**物理引脚输出抖动实际 < 0.5 ns**（仅受晶振与锁存延迟影响）。周期测量的抖动 ≠ 输出的抖动——输出时刻由硬件锁存，与 CPU 入口无关。完整推导与代码位置见 👉 **[`docs/核心思想-零抖动方案.md`](docs/核心思想-零抖动方案.md)**。

---

## 目录结构

```
dcl-controller/
├── firmware/
│   ├── h723-core0/        # 生产固件：100 μs 确定性引擎（已验证）
│   ├── h723-core0-1us/    # 研究：1 μs "编译型 ISR"（运行时 JIT 路由表到 ITCM）⚠️ 未完成，卡在 UNDEFINSTR
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

### 1. 编译 DCL 程序

```bash
cd ide/compiler
python dcl_compiler.py reactor_control.dcl -o reactor_control.bin
# 额外：--json 看路由表展开，--c 看 C 等价代码
```

### 2. 烧录固件（STM32H723ZG @ 544 MHz，Cortex-M7）

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
  - L2 抖动确定性：DMA 搬运 + ITCM 零等待 ✅ 已实测（~37 ns）
  - L3 计算确定性：空间数据流（无 CPU 瓶颈）⬜ 研究方向
- **DTCM 128 KB 零等待 + ITCM 64 KB**：路由表与 ISR 都在零等待内存，消除 cache miss 引入的抖动。

内存布局、RouteEntry 结构、Actuator 映射见 `docs/MEMORY-MAP.md` 与 `AGENTS.md`。

---

## 已知局限（请务必先读）

本项目**不是**工业 PLC 替代品，以下局限是真实的：

1. **抖动仍比专用硬件差 4–8×**。温度/流量/压力 PID 场景足够，电子凸轮、飞剪等高要求场景不适用。
2. **闭环到物理执行器是近期才打通的**，仅在 bench 级验证，没有挂真实被控对象长期跑（无温漂 / EMI / 连续 72 h 数据）。
3. **高速通信（以太网）尚未打通**：受限于硬件 PCB 与杜邦线信号完整性，当前可靠通道只有 UART/SWD。
4. **1 μs JIT 研究未完成**：卡在 ITCM 自修改代码的 VFP 指令执行异常（UNDEFINSTR），属前沿探索。

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
