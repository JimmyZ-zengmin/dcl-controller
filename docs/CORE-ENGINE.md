# CORE-ENGINE — 核心引擎：数据链与组成 (2026-09-12)

> 本文以**数据链**为主线描述核心引擎：数据从哪来、流过谁、到哪去、每一步的契约。
> 配套: `FRAMEWORK-MAP.md`(组件总表) / `ARCH-H723.md`(平台) / `PLAN-io-into-engine.md`(I/O 拍内)。
> 所有偏移与结构体均引自 `src/engine.h`，函数引自 `src/engine.c` / `src/main.c` —— 无一凭记忆。

---

## 0. 设计公理（读全文前先记住四句话）

1. **一切数据都在 SHM 里**（DTCM `0x20008220` 起，32KB）。组件之间不传指针、不加锁 —— 通信就是"写我自己的槽"。
2. **单写者原则**: 每个 SHM 槽有且只有一个权威写者（多读者不限）。谁的槽谁负责，别越界写别人的。
3. **计算即查表**: 引擎 = N 条 **16 字节路由**的逐条解释。一条路由 = 「输入槽 → 原语 → 输出槽」。没有隐藏的控制流。
4. **时间预算是一等公民**: 每条路由有标价，**部署前算总账**（静态门），**运行后查超支**（动态门）。

---

## 1. 数据总线：SHM 布局（数据链的路基）

| 区段 | 偏移 | 内容 | 唯一写者 |
|---|---|---|---|
| **CTRL 控制块** | 0x00–0x3F | MAGIC/VERSION/HEARTBEAT(0x08)/RELOAD(0x0C)/ENGINE_RUN(0x0D)/N_ROUTES(0x0E)/PROG_MAGIC(0x14)/**GPIO_MASK(0x34)**/N_SEQ(0x38) | 协议命令 + ISR(HEARTBEAT) |
| **SENSOR_MAP** | 0x0040, 64×f32 | 输入数据（DI/AI/HIL 反馈） | di_poll / adc_poll |
| **ACTUATOR_STATUS** | 0x0140, 64×f32 | 执行器数据 | 扫描 / seq |
| **WIRE_MAP** | 0x0240, 128×f32 | 中间量（路由→路由的连线） | 扫描 / seq(步号镜像) |
| **SEQ_TABLE** | 0x4000, 64×16B | 顺序域步条目 | 部署 |
| **SEQ_CTRL** | 0x4400, 8×16B | 顺序实例控制块 | ISR(seq) + 协议(START) |
| **ROUTE_BUCKETS** | 0x4480 (active) / 0x4638 (staging) | 分档桶表（active/staging **成对**） | 部署 |
| **MB 通信域** | 0x4AA0–0x4DE0 | SET(写区)/CTRL/RX/TX/HOLD(读区) | mb_tick |
| **W5 观测** | 0x6E00 | HIL_DUTY(0x6E00) / HIL_FB_RAW(0x6E04) | hil / adc |

**为什么这样排**：读得最频繁的（SENSOR/WIRE/ACTUATOR）放在低偏移；顺序域与桶表在中间；
通信域在尾部。**ADDR 顺序 = 数据链顺序**（输入 → 中间 → 输出在低段连续，利于读-改-写的局部性）。

---

## 2. 数据链总图

```
 物理引脚                输入段(拍头)             计算段(受门控)              输出段(拍尾)
┌─────────┐   di_poll   ┌──────────┐   force    ┌──────────────┐   hil_out_poll  ┌─────────┐
│ DI PC0-3 │ ──────────▶ │SENSOR[3-6]│ ──(覆写)──▶ │ 128 条路由    │ ──WIRE[20]────▶ │ TIM3 PWM│
└─────────┘             └──────────┘            │ (引擎查表)    │                 └─────────┘
┌─────────┐   adc_poll  │SENSOR[8-10]            │  src→op→dst  │   do_poll       ┌─────────┐
│ ADC 4ch  │ ──────────▶ │SENSOR[2]               │  +ACTUATOR   │ ──ACTUATOR────▶ │GPIOE ×16│
└─────────┘             └──────────┘            └──────────────┘                 └─────────┘
                            ▲                        ▲    │seq(步号→译码路由)          │
                  SRC_HMI(留位)│                        │    ▼                           │
                  MB_SET(写区)─┘                  force_apply                    do_outputs_safe
                                                  (fmask/fval 覆写)              (停机清物理面)
```

**三条横切链**（不属于某一段，贯穿全链）：
- **控制面**: ENGINE_RUN / GATE / GPIO_MASK / SCAN_MODE —— 决定每段"跑不跑、管哪些"。
- **部署链**: PC 写 STAGING → 置 RELOAD 标志 → ISR **本拍** `engine_reload_active` 原子切换 → APPLIED_SEQ 回执。
- **安全链**: `eng_outputs_safe` 遍历**登记过的输出面回调**（hil_outputs_safe / do_outputs_safe）⇒ STOP/故障时各面归零。

---

## 3. 各组成部分职责（按数据链顺序）

### 3.1 输入链 —— 物理量 → SENSOR 槽

| 组件 | 采样周期 | 写入槽 | 就绪门 | 关键点 |
|---|---|---|---|---|
| `di_poll` | **100 拍**（`tick%100==0`，相位锚定）| SENSOR[3..6]（经 3 次去抖）| 无（GPIO 直读恒安全）| 非阻塞 |
| `adc_poll` | **状态机**: 启动→等 3 拍→读→下通道，4 通道轮询 ⇒ 每通道 16 拍 | SENSOR[8..10]（AI）、SENSOR[2]（HIL 反馈 16 次均值）| `s_adc_ready` | **跨拍非阻塞**（单次转换 259µs > 拍长）；fADC=6.25MHz 为 40kΩ 源设 |

**契约**: 输入链只写 SENSOR，不读计算结果 —— 输入与计算**解耦**，扫描永远读到"本拍初已完成"的值。

### 3.2 计算链 —— SENSOR/WIRE → 原语 → WIRE/ACTUATOR

**每条路由 = 一条 16B 指令**（`RouteEntry_t`）:

| 字段 | 宽 | 语义 |
|---|---|---|
| src_type / src_index | 2B | 源: 0=SENSOR / 1=WIRE / 2=CONST / 3=HMI(留位返回0) |
| dst_type / dst_channel | 2B | 目标区与槽号 |
| **op** | 1B | 19 个原语之一（PID/TIMER/CNT/CMP/EDGE/SR/MUX/LUT/LPF/RATE/…) |
| param_idx | 2B | 参数槽（ParamEntry 16B = 4×f32，原语的系数）|
| state_offset | 2B | 状态槽（StateEntry 16B，PID 积分/TIMER 累计等跨拍状态）|
| **actuator_idx** | 2B | 直驱执行器槽（0 = 不驱动）|
| wire2_idx | 2B | 第二输出（双输出路由）|
| period | 1B | **div_idx(2bit) + phase(6bit)** —— 分档与相位 |

**执行方式**: 逐条解释（`engine_scan_itcm` / `engine_scan_flash` 双落位），
先 `scr[i] = act[i]` 归一，再按 **div 桶** 分段执行:
- **div0**: 每拍全跑；
- **div1**: 每 2 拍，**10 个相位桶**（BUCKET_DIV1_PHASES=10）；
- **div2**: 每 4 拍，**100 个相位桶**（phase 字段 6bit，上限 63）。

**预算双闸**（本项目独有强项）:
- **静态门（部署期）**: 每条路由标价 = 源成本 × 分档折算（DIV0=1 / DIV1=10 / DIV2=64），
  `MAX_ROUTES(128) × OP_COST_MAX_MEASURED(145) ≤ EXEC_DEPLOY_BUDGET(26000)` 是 **_Static_assert** ——
  超预算的表**根本编译不过**；
- **动态门（运行期）**: 本拍实测 > `EXEC_BUDGET_CYCLES(32000)` ⇒ `g_isr_overrun++`（事后可查）。
- **热重载原子的前提**就是这两闸: 新表部署前已证明"装得进拍"。

**强制（Force）**: `engine_force_apply` 在扫描**之前**统一覆写（fmask/fval）——
放扫描内会被分档分支漏掉（H9 教训: "凡每拍必须发生的动作不能挂在分支里"）。

### 3.3 顺序链 —— 条件 → 步进 → 译码 → 输出

- **SeqCtrl_t**（每实例 16B）: step_base/n_steps/**step_cur**/**out_wire**(步号镜像)/period/run/**step_tick**(u32 激活拍数)。
- **SeqStepEntry_t**（每步 16B）: 转移条件只有两种（v0）—— **阈值转移**（SENSOR/WIRE > param.value_a）
  或 **超时强推**（param.value_b 秒），取先满足者；bit0=末步回卷；jump_idx=0 线性下移（v1 分支）。
- 步号写进 `out_wire`（该实例唯一生产者）→ **译码路由**把步号变成输出（seq 与路由通过 WIRE 握手，互不知晓）。
- **推进节拍**按实例的 div 档位（快档 dt=100µs ⇒ `step_tick` 用 u32，6.55s 回卷的坑已修）。

### 3.4 输出链 —— WIRE/ACTUATOR → 物理量

| 输出面 | 驱动 | 周期 | 原子性 | 安全态 |
|---|---|---|---|---|
| TIM3 PWM（HIL 执行器） | `hil_out_poll`: WIRE[20] → 占空比 → CCR1 | 每拍 | CCR1 单写 | STOP ⇒ u=0（HIL_SAFE=1）|
| **GPIOE ×16（DO）** | `do_poll`: ACTUATOR[0..15] > 0.5 打包 → **BSRR 单次原子写**；非管辖位不下发 | 每拍 | BSRR 天然原子 | STOP ⇒ `do_outputs_safe` 清管辖位 |

**共同纪律**: 输出臂读的是**本拍刚算出**的 WIRE/ACTUATOR（放扫描之后）；每个域自带就绪门；
`eng_outputs_safe` 的登记回调负责各自的物理清零 —— **一个输出面一个驱动者**。

### 3.5 通信链 —— mb_tick（每拍，门外）

- **限速**: `MB_TICK_BUDGET = 4 字节/拍` ⇒ 一帧最多 4 拍内被消化（WCET 上界显式化）。
- **HOLD 读区**（0x4D60）: `mb_refresh_hold` 每 100 拍把 WIRE 镜像进去 ⇒ 上位机轮询读 = 方案 1 的数据通道已存在一半。
- **SET 写区**（0x4AA0）: 上位机设定值 ⇒ DSL 的 `SRC_HMI` 源（**当前引擎留位返回 0**，未接线）。

### 3.6 部署链 —— STAGING → ACTIVE（≤1 拍）

PC 写 STAGING（表 + 桶表都成对）→ 置 `RELOAD` 标志 → ISR 在扫描**之前**做 `engine_reload_active`
（整块 memcpy，代价计入 `g_reload_cyc`，一拍变长但 < 拍长预算）→ `APPLIED_SEQ` 回执 → 清标志。
**静态校验发生在部署之前**（`engine_route_validate` + 预算 _Static_assert），坏表根本到不了 STAGING。

---

## 4. 控制面字段速查（CTRL 0x00–0x3F）

| 字段 | 写者 | 读者 | 语义 |
|---|---|---|---|
| HEARTBEAT | ISR 每拍（门外无条件）| PC | CPU+定时器存活（**不是**"引擎在跑"）|
| ENGINE_RUN | 0x11/0x12 | ISR | 与 gate 合取才是引擎真跑 |
| RELOAD | 部署命令 | ISR | 1 = 本拍切换 ACTIVE |
| N_ROUTES | 部署 | ISR/PC | 条数唯一权威来源 |
| GPIO_MASK | 协议 | do_poll/do_outputs_safe | PE 管辖位（定案②）|
| PROG_MAGIC | 部署 | PC | "这份表确实部署过"的证据 |

---

## 5. 原语库（19 个 OP，数据链的"算子"）

**逻辑**: AND / OR / NOT / EDGE(R/F 触发) / SR(置位优先) / SR_RESET_DOM
**比较**: CMP（GT/GE/LT/LE/EQ/NE）
**连续**: ARITH(四则) / PID / LPF / RATE(斜率) / SCALE / CLAMP / DEADBAND / HYST / LUT(查表)
**计数定时**: CNT / TIMER
**选择**: MUX / DIRECT(直通)

19 块 × 4 参数槽 × 16B 状态槽 —— IEC 61131-3 常用块的等价覆盖。

---

## 6. 不变量（改引擎必须守的 8 条）

1. 单写者：改任何槽的写者前，先全局搜旧写者。
2. ISR 段顺序 = 数据依赖（输入在扫描前，输出在扫描后）。
3. 每拍必须发生的动作**不得挂在分支里**（H9）。
4. 新 SHM 字段 ⇒ `_Static_assert` 布局断言 + PC 侧同步。
5. 新观测变量 ⇒ `obs_anchor()` 登记（否则 gc 回收，nm 里消失）。
6. 静态门常量与成本表**同源**（`OP_COST_MAX_MEASURED == k_op_cost_itcm[OP_PID]`，漂移当场 FAIL）。
7. 跨拍状态用 u32（6.55s 回卷事故；u16 的教训）。
8. 判据必须能失败（恒 0 先怀疑"没被走到"）。

---

## 7. 讨论点（开放的设计取舍 —— 供讨论）

| # | 议题 | 现状 | 备选 |
|---|---|---|---|
| ① | **多输入算子**: 一条路由 1 入 1 出(+可选第二出)。PID 等多参靠 param 槽，但"两个变量相加"需要两条路由 + 中间 WIRE | 树形展开进路由表 | 原语加"第二输入源"字段？(路由会变宽) |
| ② | **ACTUATOR 双路径**: 路由可 `actuator_idx` 直驱，也可 `dst=ACTUATOR` —— 两种写法语义重叠 | 都通 | 收敛成一种？ |
| ③ | **div2 相位**: phase 字段 6bit(≤63) 但桶数 100 ⇒ 63 之后的桶**永远空** | 已知(H9) | phase 扩到 8bit 或桶数降到 64 |
| ④ | **SRC_HMI 留位**: 引擎读到恒 0，MB_SET 区已备好 | 未接线 | 接线后"上位机设定值"即可参与运算 |
| ⑤ | **seq 条件 v0**: 只有阈值/超时；分支/并行在 v1 | 已知 | jump_idx 字段已预留 |
| ⑥ | **单 float 类型**: 无 BOOL/INT 擦除模型（P1b 的核心议题） | float 一统 | ST 前端需要类型系统 |
| ⑦ | **预算模型**: 线性叠加 vs 实测（cache/分支影响）| 保守标价 | 动态重标定？ |
| ⑧ | **ENGINE_RUN 跨上下文**: 协议写/ISR 读，靠 SHM 单字节 + volatile，无额外屏障 | 单字节原子, 实践安全 | 需要的话上 `__atomic` |
