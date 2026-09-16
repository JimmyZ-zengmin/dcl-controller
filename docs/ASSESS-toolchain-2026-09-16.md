# 工具链评估 —— "pyocd 老让板子出毛病"的根因与三层修法

- 日期：2026-09-16 · 触发：用户观察"工具链不太干净，pyocd 老是让板子出毛病"
- 结论：**这不是 pyocd 的缺陷，是主流调试器的统一设计选择**；而**根子在"拿调试单元当生产时基"**。
- 实测背景：本次会话里 `0x38` 的 `pmin/pmax` 变成 0/sentinel，`h723_jitter` 报 4/5；
  断电重上电后立刻恢复（`pmax=40014`）。**换任何工具都不会根治，只有改时基来源才根治。**

---

## 1. 根因（三个独立来源，互相印证）

### ① pyOCD 维护者本人在 issue #1540 里的说明（**决定性证据**）
> "Currently on disconnect, **`DEMCR.TRCENA` is written to 0** to disable extra debug logic, **including DWT**.
>  During connect, there would be some delay before `DEMCR.TRCENA` is set to 1."
> "**This should only happen if you are calling `pyocd reset` or equivalent**, so that it's performing a
>  full connect/disconnect sequence. Doing a reset while pyocd remains connected (eg in gdb) should not
>  affect debug logic at all."
> — <https://github.com/pyocd/pyOCD/issues/1540>

★ 这**逐字解释了我们看到的现象**：`pyocd reset` = 完整 connect → reset → **disconnect**，
而 disconnect **就是要清 `DEMCR.TRCENA`**（连带着 DWT 一起关）。
⇒ 所以"`pyocd reset` 之后 DWT 仍然是死的"**不是意外，是它设计如此**：
   它在退出的那一刻，把我们刚复位启动、固件刚刚重新打开的东西**又关了一次**。

### ② SEGGER 的知识库（**J-Link 同款行为，且写明理由**）
> "**by default J-Link clears all debug enable bits (e.g. the `DEMCR.TRCENA`)** ... on debug session close...
>  J-Link needs to clear all debug bits on debug session close to make sure that the WFI / WFE instructions
>  enter low power modes correctly and **do not leave certain clocks enabled which would result in a higher
>  power consumption of the chip**."
> 若确实需要在应用里用周期计数器："J-Link can be configured to **not** clear the debug bits on debug
>  session close ... via `SetDbgPowerDownOnClose = 0`."
> — <https://kb.segger.com/J-Link_Cortex-M_application_uses_cycle_counter>

### ③ ARM 架构手册（另一条独立的"会冻住"的原因）
> "The DWT unit **suspends CYCCNT counting when the processor is in Debug state**."
> — ARMv7-M ARM, `DWT_CYCCNT`

⇒ **两条会各自独立地把 `CYCCNT` 冻住**：
  (a) 调试器**主动清 `TRCENA`**（会话退出时）—— 会一直冻到下次冷启动重开；
  (b) 核**处于 Debug state（halt）时暂停计数** —— 一 halt 就停（本项目早已记过这条）。

### ★ 所以"换工具"能换掉什么、换不掉什么
- **换不掉**：任何**主流**调试器在会话收尾时都会尽量把调试单元关掉（省电是设计目标）。
  所以 **probe-rs / OpenOCD 是否也清，必须实测**，不能假设；
- **能换掉**：**退出行为的可配置性**。目前已知有开关的：
  - J-Link：`SetDbgPowerDownOnClose = 0`
  - pyOCD：会话选项 **`resume_on_disconnect=False`** ⇒ 文档原话："If False, the target CPU states
    are left unchanged and **any enabled debug hardware (DWT, ITM) remains enabled**."
    （对应提交 `cf61d59`，2023-05；实现上把清 `DEMCR` 的动作**门控在 `resume` 为真时**才做）

---

## 2. 三层修法（按"根治程度"排序）

### 第一层 ★★★ 固件层：**别拿调试单元当生产时基**（唯一根治）
`DWT` 是**调试**外设，它的可用性由调试器/调试域决定 —— 让**出货固件的观测与判据**依赖它，
等于把"能不能量准"交给一个随时会被外部工具关掉的单元。本项目铁律第 2 条
（"凡 DWT 派生的判据必须含'时钟在走'"）**是对症**，但**不是治本**。

**建议**：用一个**自由运行的 32 位硬件定时器**做"生产周期计数器"（H723 上 TIM5 是 32 位且空闲；
TIM2 已作拍、TIM3 已给步进）。PSC=0，以定时器时钟自由运行，ISR 里读 `TIM5_CNT`。
- 分辨率：定时器时钟（H723 上 APB 定时器时钟最高 275 MHz）⇒ **3.6 ns/tick**
  —— 对本项目要测的量（40000 周期 ≈ 100 µs 的拍周期）**相对误差 0.0036%**，够用。
- ★ **把 DWT 保留为"第二条独立路径"**：两条路径读数一致才算数 —— 这正是本项目自己的纪律
  （`i2c_bb.h:8` 的"两条独立路径对上才算数"），而且**顺手就得到了 DWT 死活的判据**。

### 第二层 ★★ 工具链纪律：把"最后一步"交给板子自己
现状（`ARCH-H723.md` / memory 里的流程）是 `pyocd flash` → `pyocd reset`。
**`pyocd reset` 必须从"收尾步骤"里去掉** —— 它就是那个"复位完再顺手把调试域关掉"的动作。

**建议流程（工具无关）**：
```
① 构建        bash build.sh
② 烧录        pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex
③ ★ 让板子自己重启：断电重上电，或按板上 RESET 键
④ 全部验证    只走协议（0x38 / 0x22 / 0x39 …）—— 不碰调试器
```
★ 本项目其实**早就在往这个方向走**（`h723_client.py` 的 `revive_if_dead()` 注释、验收套件全程纯串口）
—— 这次只是把"最后一步"也补齐。**③ 之后不要再开任何 pyocd 会话**，否则等于把 ③ 白做。

**若确实需要调试器在场时的时基**，再考虑：
- pyOCD：`-Oresume_on_disconnect=false`（★ 副作用：不再自动 resume ⇒ 可能把核留在 halt，
  要配合 §③ 的"板子自己重启"使用）
- J-Link：`SetDbgPowerDownOnClose = 0`
- OpenOCD / probe-rs：**待实测**（见 §4）

### 第三层 ★★ 判据层：把"时基在走"变成**交付固件里可读的量**（本次发现的缺口）
现状：交付固件里 **`0x39 op=7`（重新校时）不存在**（只在实验补丁 `exp-2026-09-14-mdma-trigger`）
⇒ **时基死了，在协议面上无法自证** ⇒ 一个死掉的时基会让所有统计**看起来"完美稳定"**
（这正是本项目铁律第 1 条要防的那类**空判据**）。

**建议**：在 `0x38` 的尾部扩展里加一个 **`timebase_dead`**（或 `0x39 op=21` 返回两次计数采样 + Δ），
语义 = "两个采样点之间计数没变"。**判据必须是"两次读 + Δ≠0"，不能是"读到一个非零值"**
（`g_per_cyc_*` 住 DTCM、跨复位不丢 ⇒ 单次读会看到**上一纪元的残留值**，看起来是活的 —— 本次已实测踩到）。

---

## 3. 工具选择参考（只列与"干净"相关的差异）

| 工具 | 探针支持 | 与本问题的关系 |
|---|---|---|
| **pyOCD** | CMSIS-DAP / ST-Link / J-Link | ★ 已定位：退出时清 `DEMCR.TRCENA`；有 `resume_on_disconnect` 开关 |
| **OpenOCD** | 最广 | 脚本化能力强（`program ... verify reset exit`）；**是否清调试位待实测** |
| **probe-rs** | CMSIS-DAP / ST-Link / J-Link | 单 Rust 二进制、烧录快 2–5×、带 RTT；**退出行为待实测** |
| **STM32CubeProgrammer CLI** | ST-Link（本板是 CMSIS-DAP，**不适用**） | `-c port=SWD mode=UR -w fw.hex -v -hardRst` 一条命令搞定；若将来换 ST-Link 是好选择 |
| **DAPLink 拖盘（MSC）** | 仅 DAPLink 固件探针 | ★ **完全不建调试会话** ⇒ 原理上最"干净"，但要确认本板探针是否支持 |

★ 本板探针是 **Luxiaoban Flash Pro（CMSIS-DAP）** ⇒ 现实可选是 pyOCD / OpenOCD / probe-rs 三家。

---

## 4. 实测结果（不猜，逐条量 —— 已跑过的写结果）

| # | 待验项 | 结果 |
|---|---|---|
| **1** | `pyocd reset -Oresume_on_disconnect=false` 能否保住 DWT？ | ❌ **不能**（pyOCD **0.45.1**，实测：命令前后 `pmin` 由 3155 → **0**）。<br>★ 这条**否定**了"翻个开关就修好"的希望；#1540 里那条 `DEMCR` 路径**不是唯一路径**（或该选项对 `reset` 子命令不生效）。<br>★ 附带发现：该选项下板子**没有被留在 halt**（协议仍响应）⇒ 副作用也不是文档说的那样，行为需按版本实测。 |
| **2** | `pyocd flash` 之后**只断电重上电**（不开会话）⇒ DWT 恢复？ | ✅ **恢复**（实测 `pmax=40014`）。这是目前**唯一可靠**的恢复手段。 |
| **3** | OpenOCD / probe-rs 的会话收尾是否也清调试位？ | ⬜ 未测（需另装工具） |
| **4** | 本板探针（Luxiaoban Flash Pro）是否暴露 DAPLink MSC 拖盘？ | ⬜ 未测（若有 ⇒ "完全不建会话"的烧录路径可用） |

### 4.1 ★ 由 #1 的否定结果推出的**新做法**：让固件自己"重新武装时基"
既然调试器不可避免地会把时基关掉，而恢复它的唯一手段又要求"人跑过去断电"——
那就**给固件一条协议命令，让它自己把时基重新打开**。这样整条验证流程**完全不需要碰调试器**：

```
0x39 op=21 sub=0  →  重新使能时基（DEMCR.TRCENA + DWT_CTRL.CYCCNTENA + LAR 解锁）并清统计
0x39 op=21 sub=1  →  返回两次 CYCCNT 采样与 Δ
                     ★ 判据：**Δ ≠ 0 才算时基活着**（"读到非零值"不算 —— 见第三层）
```
★ 这同时把 §2 第三层那个缺口（交付固件无法自证时基在走）一起补上。
★ 规模很小（约 30 行），且可先落在已声明为脚手架的 `0x39` 族里；
  将来若要进正式面，应改为 `0x38` 尾部加 `timebase_dead`。


---

## 5. 一句话结论
> **"pyocd 让板子出毛病"是表象：真正被弄坏的是"我们的固件把调试单元当成了生产时基"。**
> 换工具只能改"什么时候被关"；**把时基换成一个普通的硬件定时器，才是把这件事从根上拿掉。**

---

## 6. ★★★ 第一层已落地并实测（2026-09-16，`DCL_TIMEBASE` A/B）

**实施**：新增 `src/timebase.{c,h}` —— 生产时基 = **TIM5**（APB1，32 位，自由运行，PSC=0 ⇒ 5 ns）。
ISR 改为读 `tb_cyc()`；**同时读 DWT 作为第二条独立路径**，两条互相监看：
`g_dwt_dead_n`（DWT 不动/时基在动）与 `g_tb_dead_n`（反之）—— 于是"DWT 死了"成了一个
**可计数、可读走**的量。`flash.c` 的超时判据也改走时基，且超时常量改为**按时间**表达
（原先把 `400000000` 写死在常量里，正是"一个常量两个语义"族）。
新增 `0x39 op=21` 读时基健康度（档/频率/自检 Δ/两个 dead 计数/pmin/pmax/emax/ov）。

**A/B（同一块板、同一套判据；两臂都在"DWT 已被调试器弄死"的条件下）**

| 观测 | **1 档 TIM5（交付）** | 0 档 DWT（改前行为） |
|---|---|---|
| `0x39 op=21` 时基档 | 1 | 0 |
| 自检 Δ（两次采样差） | **1834**（≠0 ⇒ 活） | **0**（⇒ 死） |
| `0x38` `pmin/pmax` | **19990 / 20010**（=99.95~100.05 µs） | **0 / 0**（哨兵，量不出任何东西） |
| `emax`（ISR 最长） | **818 tick = 4.09 µs** | 0 |
| `ov`（超预算次数） | **0** | 0 |

★ 834/818 tick ≈ **1636 cyc** —— 与 `FRAMEWORK-MAP` §3.1 记录的 `isr_max 1509~1655 cyc`
**几乎一致** ⇒ 新时基与历史数据**同源可信**。
★★ **`h723_jitter`：交付档 9 PASS / 0 FAIL（在 DWT 已死的情况下）**；0 档则整段失效。
  套件已改为按 `0x39 op=21` 的 `TB_HZ` **自适应换算阈值**（"拍 20000 计数"而非写死 40000），
  因为**同一物理容差在不同档下的计数不同**（原 ±64 cyc @400MHz = ±160 ns = ±32 tick @200MHz）。

**★ 顺带修掉的一个真实"可达故障"**：`flash.c` 用超时判据兜"控制器卡住"，而紧跟其后的
**有界喂狗只在预算内喂**。DWT 一冻 ⇒ `(now-t0) > timeout` **永不成立** ⇒ 超时永不触发
⇒ **无限喂狗** ⇒ 卡死时主循环永不返回、**看门狗也失效**。换时基后这条路径不再成立。

**★ 过程中自己踩的两个坑（留档）**
1. **混单位**：头部 `t0` 改成 `tb_cyc()` 之后，尾读 `t1` 忘了一起改 ⇒ `di = DWT_CYCCNT - TIM5_CNT`
   **两个不同计数器相减** ⇒ `emax=3.7e9`、`ov` **每拍都判超预算**（15050/15050）。
   ★ 而 `ov` 不是纯观测：它喂给动态预算门 ⇒ **DWT 一死会让引擎自认为每拍超支**。
2. **烧了旧镜像**：一条命令里 `build.sh` 失败（已知的间歇性 TMP 问题），我却直接 `pyocd flash`
   ⇒ 对照臂读到的是**上一版固件**的结果（`tb=1` 却标着"0 档"）。
   ★ 根因是我**把 grep 的输出当成了构建结果**，没有先把 `BUILD_EXIT` 当门。
   ⇒ 口诀重申：**构建闸门不要用管道把门；烧录前先确认 build 的退出码。**

