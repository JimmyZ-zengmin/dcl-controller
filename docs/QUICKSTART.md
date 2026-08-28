# DCL 快速上手（v0.1.0）

> 目标：从拿到源码到跑通第一个 100µs 确定性控制程序，约 15 分钟。
> 本指南只覆盖**生产可用配置**：100µs 固定周期 @ 480 MHz CPU + UART 部署。

---

## 0. 你需要什么

| 硬件 | 说明 |
|------|------|
| STM32H723ZG 开发板 | 本项目目标芯片（Nucleo-H723ZG 或兼容板） |
| ST-Link（板载即可） | SWD 烧录固件 + RTT 监控 |
| USB-TTL（CH340/CP2102） | UART 部署（可选，但推荐） |
| 杜邦线若干 | UART 接线 + GPIO 观察 |

| 软件 | 说明 |
|------|------|
| Python 3.10+ | IDE 工具链 |
| `arm-none-eabi-gcc` | 编译固件（GNU Tools for STM32 / CubeIDE 自带） |
| pyOCD | `pip install pyocd`，SWD 烧录/监控 |
| pySerial | `pip install pyserial`，UART 部署 |

---

## 1. 烧录固件（一次性）

固件 = 系统引擎（ISR 路由引擎 + UART 部署协议 + CANopen），烧一次即可，之后只部署运行程序、**不用再烧**。

```bash
cd firmware/h723-core0
build.bat                      # 编译 main.c + startup → build/core0_h723.elf
py -3 -m pyocd flash -t stm32h723xx build/core0_h723.elf
py -3 -m pyocd reset -t stm32h723xx
```

> ⚠️ **pyOCD target 必须是 `stm32h723xx`**，不是默认 `cortex_m`——否则外设寄存器读出 0xFF/0x00，一切看起来都是坏的。
> ⚠️ 板载 IWDG 看门狗上电即启动（~512ms 超时），固件已处理喂狗，正常无需干预。

### 1.1 没有 ST-Link？用 UART 烧录

`flash_uart.bat` 走 CH340 + STM32 内置 bootloader：

```
接线:  CH340 TX → PA10 (MCU RX)
       CH340 RX → PA9  (MCU TX)
       GND     → GND
步骤:  BOOT0 跳线置 HIGH → 按 RESET → 运行 flash_uart.bat → 烧完 BOOT0 置回 LOW → RESET
```

---

## 2. 编译你的第一个 DCL 程序

```bash
cd ide/compiler
python dcl_compiler.py reactor_control.dcl -o reactor_control.bin
# 可选: --json 看路由表展开, --c 看 C 等价代码
```

`reactor_control.dcl` 是示例程序（温度/流量控制回路）。你自己的程序照它的语法写即可。

---

## 3. 部署运行程序（核心体验：免重烧改逻辑）

```bash
cd ide
python shell/main.py --cli
```

在 CLI 里：

```
:e reactor_control.dcl     # 加载源码
:c                         # 编译 → 路由表二进制
:d                         # 部署（SWD 直写 DTCM 运行区）
:start                     # 启动引擎（100µs 定时 ISR 开始扫描路由表）
:m                         # 监控 RTT 状态
```

看到 `S=... P=23978..24022 R=40 E=1` 就是引擎在跑：
- `S` SAMPLES 已执行周期数
- `P` PERIOD 周期（DWT cycles，23978..24022 ≈ 100µs @480MHz）
- `R` 活跃路由数
- `E` 引擎状态

### 3.1 用 UART 部署（无 ST-Link 场景）

USB-TTL 接 **USART2（PD5=TX, PD6=RX）@ 115200 8N1**，协议帧：
```
PC→H723: [0xC0] [CMD] [LEN:2B LE] [PAYLOAD] [CRC16:2B LE]
```
（CRC-16/CCITT, poly=0x1021, init=0xFFFF）

IDE CLI 的 `:d` 在 SWD 不可用时走此通道。

---

## 4. 验证输出（输出链路抖动实测 <7ns）

引擎把输出写进 DTCM 影子寄存器，由 **TIM1_CC4 硬件事件 → DMA2 Stream5** 在周期末尾搬进 GPIOE_ODR——输出时刻由硬件锁定，与 CPU 计算时刻无关。

```bash
# 非侵入监控（不 halt 芯片）
py -3 -m pyocd rtt -t stm32h723xx -a 0x20008000 -s 0x1000
```

> ⚠️ 不要用 `pyocd halt` 读 GPIOE_ODR——halt 会冻结 TIM1/DMA，读到的值是假的。要用 RTT 或示波器/逻辑分析仪看 PE2 heartbeat。

---

## 5. 实测指标（bench 级，非产品级）

| 指标 | 值 |
|------|-----|
| ISR 周期 | **100.000 µs**（TIM1 硬件定时锚点） |
| CPU 频率 | **480 MHz**（VOS0 + PLL，H723 规格内） |
| 输出链路抖动（DMA 搬运 + ITCM 零等待） | **< 7 ns（实测）** | L2 抖动确定性，见 `docs/00-vision/FUTURE-ROADMAP.md` |
| 周期采样 σ（ISR 入口，参考） | ~37 ns（仅测入口时刻，非输出） |
| 原语 | 35 个（PID / LPF / CMP / TIMER / COUNTER…） |
| 容量 | 路由 1024 / 参数 512 / 状态 256 / WIRE 1024 |

---

## 6. 常见问题

**Q: 编译固件报错 / 找不到 arm-none-eabi-gcc？**
`build.bat` 里 GCC 路径写死为 CubeIDE 1.5.1 的安装路径。装 CubeIDE 或改 `build.bat` 第一行的 `set "ST=..."` 为你自己的工具链路径。

**Q: 部署后没输出？**
- 确认固件已烧且引擎启动（`:m` 看 S 是否增长）
- 确认你的 DCL 程序把结果写到了**执行器通道**（`actuator_idx`: 1-4=PWM, 32-63=GPIOE），只写 WIRE 是不产生物理输出的

**Q: 以太网能用吗？**
不能。Ethernet（RMII + lwIP）是研究分支，因 PCB/杜邦线信号完整性未打通。当前可靠通道：**UART（已验证）+ SWD + CANopen（实现）**。

**Q: 1µs 周期怎么开？**
`firmware/h723-core0-1us` 是研究分支（544MHz 超频极限验证），非生产配置。生产固定 100µs。

---

## 7. 去探索

- `docs/核心思想-零抖动方案.md` — 为什么输出边沿稳（实测 <7ns）
- `docs/核心思想-确定性.md` — 为什么输出总准点
- `docs/核心思想-低周期.md` — 为什么能压到 5µs 实测 / 1µs 极限
- `AGENTS.md` — 系统引擎 vs 运行程序 / 构建 / 坑
