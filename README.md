# DCL — 确定性控制运行时（STM32H723 / Cortex-M7）

**一个用「静态路由表」替代「程序」的裸机控制运行时。**
没有 RTOS、没有调度器、没有 `while(1)` 大循环。全系统只有一个 **硬件定时硬拍（默认 100 µs，可配）**；
拍内按拓扑序扫描一张**编译期生成的静态路由表**，逐条完成「读源 → 算原语 → 写目标」，结果直接落到
GPIO / PWM / 通信。表里没有动态分支、热代码全部住在零等待 ITCM，**每拍花多少周期是编译期就能算出的量**。

**一次烧录，控制逻辑可以反复下发覆盖 —— 热更新 ≤ 1 拍。**

> **给第一次看的人**：如果你只想知道"这东西凭什么值得看"，请读下面两节
> （**① 本版最硬的实证** · **② 30 秒读懂**），大约 3 分钟。
> 其余各节是给要看细节的人的；每节开头都有一句"这一节回答什么问题"。

---

## ★★★ 一、本版最硬的实证（**2026-09-17 / 09-18 真机跑出来的**，每条都能自己复跑）

| 结论 | 实测数字 | 自己复跑 |
|---|---|---|
| **执行确定性**（结构给的，不是调优）| 拍基 **99.95 µs**（−0.05%）· 输出 σ = **3.6 ns** · 拍周期跨度 **90 ns** | `python tools/h723_jitter.py --port COMxx` |
| **闭环跑在片内程序里**（不是 PC 在环）| 环整条在 ③ 层**每拍**执行 ⇒ 相对 PC 在环 **×40** 带宽 | 见 §四 |
| ★ **失速边界（可预测）** | **从静止突加到 >16500 Hz 必失速**（拐点 **16500~16750 Hz = 619~628 rpm**）；**已在转动时突跳到 31 kHz 完全正常**；带斜坡可达 **≥50000 Hz** | `python tools/h723_stall_edge.py` |
| ★★★ **失步自动检测 + 恢复** | 突加 18000 Hz ⇒ **1.17 s 触发**（遮蔽窗 900 ms + 去抖 200 ms）、降额到 0.25、**轴恢复转动 99.2%** | 部署 `examples/h723_step_stall_recover.dcl` → `python tools/h723_stall_detect_verify.py` |
| ★ **频率是可预测的**（不是一个黑盒）| `rate_actual = 1e6 / floor(1e6/f)`，**7/7 精确命中**（1 µs 周期量化）| `python tools/h723_motion_calib.py` |
| ★ **跟随线性度** | 1~60 kHz 共 28 点，**无随速度增长的丢步，线性度 ≤0.5%** | `python tools/h723_stall_sweep.py` |
| **轨迹规划（已落地）** | 三角波速度曲线 / 阶梯正弦，**程序只管"何时换目标"，固件斜坡当积分器** | `python tools/h723_traj_verify.py --prog tri`（或 `--prog sine`）|
| **"走 N 个脉冲自停"** | 另一个定时器**硬件数脉冲**，零中断/零 DMA/零 ITCM | `python tools/h723_step_pulsecount_test.py` → **7/0** |
| **装置配置 fail-closed** | 极性未声明就**拒绝使能**；指令与引脚实读的失配**有归因计数** | `python tools/h723_step_failclosed_test.py` · `tools/h723_do_mask_owner_test.py` |
| **接一个新器件 = 上传一段配置** | 不用改固件、不用重烧：下发"每 N 拍读 设备/寄存器/长度 → 写 `SENSOR[z]`" | `python tools/h723_dev_bind_test.py --port COMxx` → **52/0/1** |
| ★★★ **每拍时间真的算得出来**（2026-09-18）| 模型 = `C_other(542 TB) + C_scan(op, 33~55 TB) + Σ m_op×条数 + k×转变数`，全部**实测标定**；**留点验证 0.04%**、跨 division 3.1%；转变代价 `k = 两个 op 的成本差`（**R²=1.0000**，六对零残差）| `python tools/exp_ek_holdout.py` · `exp_ep_op_pair_k.py` |
| ★★★ **预算门端到端验过**（含**决定性翻转实例**）| 交付档 128×145=18560 ≤ 26000 ⇒ **结构上不触发**（这就是"从未触发"的真相）；FLASH 档**具约束力**：`DIRECT 105/106`、`PID 60/61` 由源码算术**逐条复现**；**纯可加会放行超载程序**，含转变项后正确拒绝 | `python tools/exp_eo2_flip.py` · `exp_en_gate.py` |
| ★★★ **声明的时间量 = 跑出来的**（2026-09-18，三域 46 条判据）| `LPF τ`/`PID Ki`/`TIMER PT`/`SEQ 超时`（div0/1/2 三档）· 运动域 `限时`/斜坡斜率 · 「走 N 步」的 `N/f` 时长 —— 全部**实测 = 声明**（PID 积分斜率精度 **0.01~0.08%**；时长直接用固件拍号量：**0.9916 s vs 1.0000 s**）| `python tools/exp_eq_dt_semantics.py` · `exp_er_motion_time.py` · `exp_es_step_duration.py` |
| ★★ **③ 层真的能选档跑**（2026-09-18）| `OUTPUT … PERIOD=` 可用（末级也能降档）；`ABS`/变量阈值/科学计数法都能编；**在板三档速率逐档复现**：每拍 2.0000 / 每 10 拍 0.2000 / 每 64 拍 0.03125（偏差 **+0.0 / +0.1 / +0.2%**），且**他档增量恒 0** 作负对照 | `python tools/exp_ew_output_period.py` · `exp_ex_composed_blocks.py` |
| ★★ **拍长可配，且判据跟着走**（2026-09-18）| `bash build.sh -DDCL_TICK_US=200` ⇒ 拍长/相位数/`dt`/档除数**全部派生**（一处定义 + 3 条断言）；★ **运行期超载预算随拍长缩放**（`EXEC_BUDGET_CYCLES = 拍长 × 80%`，四条断言；**变异对照**证明"手写常数"在换档时被编译期抓住）| `python tools/exp_ez_exec_budget_scale.py` |

> ★ **诚实标注（三条，都会让上面某些数字变小）**：
> 1. **"可算"是标定模型，不是纯计算**：常数由实测标定（留点验证 0.04% 证明它**能外推**），
>    而"不靠实测、纯从结构推"这条路线已被**证伪**（`docs/exp-EF-mop-deducibility.md`，负结果）。
>    准确说法：**部署期可算 + 留点 3% 以内 + 有能失败的门**。
> 2. **门在交付档上结构性不触发**（最大条数 128 × 最贵原语 145 = 18560 ≤ 26000，占 28%）——
>    所以"门的正确性"只能在 **FLASH 档**上验，而它已经验过。
> 3. **轨迹规划的"形状验收"尚未完成** —— PC 侧差分测速在本装置上被证明不可用
>    （编码器回填率与 PC 轮询率发生**采样拍频**，实测 `Δraw` 中位数与理论差 60 倍），
>    必须改用**片内黑匣子**重做，而环的保留跨度只有 ~0.4 s，装不下 1.2 s 周期。
>    见 [`docs/PLAN-closedloop-stepper-v2.md`](docs/PLAN-closedloop-stepper-v2.md) §阶段3。
> 4. ★ **闸门的"摊薄"口径依赖一处未声明的性质**：`engine_prog_budget` 按 `Σ ceil(成本/相位数)` 摊薄计价，而它成立**只因为** `engine_stage_program` 会把同档路由**轮转铺到 64 个相位**上 —— 这条依赖此前没有任何断言牵连（现由 [`tools/exp_ev_gate_worst_phase.py`](tools/exp_ev_gate_worst_phase.py) 的 V2/V3 钉住）。
>    推论：`PERIOD=` **只承诺"档位（速率）"，不承诺相位**。详见 [`docs/exp-EV-gate-phase-dependency.md`](docs/exp-EV-gate-phase-dependency.md)。

---

## 二、30 秒读懂

**它和常见做法哪里不一样**

| | 常见做法 | 本运行时 |
|---|---|---|
| 确定性怎么来 | PLC/软 PLC：把周期拉到 **ms 级**，"藏住"抖动 | **不藏**：拍默认 **100 µs**（**可配**，判据随档缩放），**每拍成本编译期可算**，热代码在零等待 ITCM |
| 改控制逻辑 | 改代码 → 重新编译 → **重新烧录** | **上传一段配置/程序**，热更新 **≤1 拍**（持久化走 SD 的 A/B 双副本）|
| 接新器件 | 写驱动 → 烧固件 | **下发一条"绑定"**（设备地址/寄存器/长度/周期）⇒ 器件成了**数据**，不是代码 |
| 环路在哪 | PC 在环（读—算—写，受 USB/OS 调度）| **环在片内的 DCL 程序里**，每拍执行 |

**最小上手（4 条命令）**

```bash
bash build.sh                                                                 # ① 构建（6 道闸门）
pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex      # ② 烧录
python tools/h723_proto.py --port COMxx                                        # ③ 探活 → 预期 12/0
python tools/dclc.py examples/h723_step_bounded_position.dcl                   # ④ 上传一个闭环程序
```

★ **认口按能力字**（不要认"第一个 CH340"），且判据用**必备位掩码** `(cap & 0x0DF7) == 0x0DF7`
—— 能力字随版本增长（`0x0DF7 → 0x1DF7 → 0x3DF7 → 0x7DF7`），**等值判据会让你以为"板子没响应"**。
★ **串口是独占资源**：同一时刻只允许一个进程用（并发会让两边都读到串帧）。

---

## 三、它是什么（架构，一页）

**三层，各管一件事。** 判层是**强制**的：放错层 = 走弯路（本项目在 I2C 上真走过一次）。

| 层 | 是什么 | 改它的代价 |
|---|---|---|
| **① 引擎核心** | 硬拍驱动 + 静态路由表扫描 + 原语执行（烧进 flash）| **等于换固件** ⇒ 必走 A/B + 全套回归 |
| **② 外设能力** | 做"**通用事务 op**"（I2C/SPI/UART）+ **具名设备**，而不是"每芯片一条命令" | 随核心版本化 + 能力位 |
| **③ DCL 程序** | 逻辑/连续/顺序（**怎么算**都在这里），通信上传 + SD 持久化 | 上传即可，**不碰固件** |
| **④ 程序工程** | 上位机侧：`.dcl` 源 + 清单 + 编译产物 | — |

**判层表**：怎么算 ⇒ ③ ｜ 和外设怎么说话 ⇒ ②（**不是**新驱动）｜
需要新的**每拍行为/实时保证** ⇒ ① ｜ 只是"给上位机看的量" ⇒ SHM + `obs_anchor()`（**不是**加协议 op）

**它靠三件事做到确定性**：① 硬拍驱动（TIM2，实测抖动**个位数 ns**）·
② 静态拓扑 + 零等待 ITCM（**无 cache 不确定性**）· ③ 通信下发 + SD A/B 持久化（**改逻辑不重烧**）。

**★★ 铁律 0 —— 非侵入式交互（先读这条）**

> **观测不得改变被测对象。** 与单片机交互/观测时**优先走协议内通道**；
> 凡会改变目标状态的手段（HALT、复位、关时钟、改 DWT、留运行期配置），**只在协议通道表达不了时才用**，
> 且用完必须**显式恢复**并**核对已恢复**。

**为什么排第 0**：它的代价是"**静默的假故障**" —— 观测动作把目标推进到另一个状态，
而症状看起来像"固件坏了 / 新代码写错了"，**排查方向会被整轮带偏**。
（例：`pyocd` 会话会**静默停掉 `DWT_CYCCNT`**，于是所有计时量读 0 而其余一切正常。）
完整血证见 [`docs/README-full-legacy.md`](docs/README-full-legacy.md) 的「铁律 0」节。

---

## 四、闭环步进：**闭环跑在 DCL 程序里**（完整例子）

**先看这一份** → [`docs/STEPPER-DEMO.md`](docs/STEPPER-DEMO.md)（做了什么 / 15 分钟自己复跑 / 已知边界）

**例程文件在哪**（都是 `.dcl`，`python tools/dclc.py <文件>` 即可编译上传）：

| 文件 | 是什么 | 状态 |
|---|---|---|
| ★ [`examples/h723_step_stall_recover.dcl`](examples/h723_step_stall_recover.dcl) | **失步自动检测 + 恢复**（每拍比"实测增量 vs 期望增量"，失步则断流+降额重发）| ✅ 上机通过 |
| [`examples/h723_step_traj_tri.dcl`](examples/h723_step_traj_tri.dcl) | **三角波速度曲线**（`SEQ` 翻目标 + 固件斜坡当积分器）| ✅ 程序运行，形状待黑匣子 |
| [`examples/h723_step_traj_sine.dcl`](examples/h723_step_traj_sine.dcl) | **阶梯正弦速度曲线**（6 段表）| ✅ 程序运行，形状待黑匣子 |
| [`examples/h723_step_bounded_position.dcl`](examples/h723_step_bounded_position.dcl) | **有界闭环定位**（最短弧折角 + 双极性 + 幅值钳位 + 误差带自停）| ✅ 已跑通 |
| [`examples/h723_step_closedloop_demo.dcl`](examples/h723_step_closedloop_demo.dcl) | P 控制入门版（更短，**适合先读**）| ✅ |

**它长什么样**（**全部用 DCL 原语** —— 没有 `ABS`/`MOD` 也能凑出最短弧）：

```
SENSOR ang FROM sensor[1]                            # AS5600 角度 0..360
ADD  a2   FROM ang  BY=n360                          # ang − 360
GE   wrap IN=ang THR=180.0                           # ang ≥ 180 ⇒ 折
SEL  fbk  G=wrap IN0=ang IN1=a2                      # ★ 折到 [-180,180) ⇒ 最短弧
SUB  err  FROM tgt BY=fbk                            # 带符号误差
MUL  eneg FROM err BY=mone ; MAX eab FROM err BY=eneg  # ★ 用 MAX 凑 |err|
MUL  raw  FROM eab BY=kp ; LIMIT hz0 IN=raw MN=300.0 MX=3000.0  # ★ 先对**正幅值**钳位
GE   drv  IN=eab THR=0.5 ; MUL hz FROM hz0 BY=drv    # 误差带内 ⇒ 0（自停）
OUTPUT o_hz TO wire[12] FROM hz                      # 运动请求（**程序面**；③层输出面只有 wire[]）
```

**怎么跑**（★ 中间那步是必需的：部署会清掉反馈绑定）：
```bash
python tools/dclc.py examples/h723_step_bounded_position.dcl   # 编译 + 上传 + START
python tools/h723_as5600_bind.py --port COMxx                  # ★ 必须写回反馈绑定
# 然后 op=19 sub=13 arg=1 切到程序面（默认 0 = 脚手架直控，既有行为不变）
```

### 它是什么水平（拿数字对标，不吹）

| 维度 | 本项目（**片内 DCL 程序**）| 工业伺服 / 运动控制器 | 同一块板走 **PC 在环** |
|---|---|---|---|
| **位置环执行频率** | **10 kHz**（100 µs 每拍）| 1 ~ 20 kHz | **13.5 Hz**（命令通路 26 命令/s）|
| **闭环带宽**（= 反馈更新率，**不是执行频率**）| **~550 Hz**（拍内模式）/ ~80 Hz（默认阻塞）| — | 13.5 Hz |
| **输出确定性** | σ **3.6 ns** | — | 受 USB / OS 调度影响 |
| **位置精度** | ±1 LSB（0.088°）| 取决于编码器 | 同硬件，但环慢 ⇒ 1 Hz 轨迹跟随误差 **±25°** |
| **轨迹规划** | ⚠️ **已落地**（三角波 / 阶梯正弦），形状验收待黑匣子 | ✅ 梯形 / S 曲线、多轴插补 | ✅（PC 算）|
| **多轴** | ❌ 单轴；资源够但要扩架构（见 §五 B 类）| ✅ | ✅ |
| **电流环** | ❌ 驱动器是**开环电流**（TB6600）| ✅ | ❌ |
| **失速边界** | **突加拐点 16500~16750 Hz（619~628 rpm）**；带斜坡 **≥50000 Hz** | 高得多 | 同硬件 |

★★ **引用上表必须带上这一句**：**"环在片内每拍跑（10 kHz 执行频率）"不等于"闭环带宽 10 kHz"** ——
带宽上限是**反馈源的更新率**（AS5600 由绑定表回填；默认阻塞路实测 163 Hz 天花板，
拍内模式实测 **1002~1114 Hz**）。真实对比是：**执行频率 ×700（13.5 Hz → 10 kHz）；
闭环带宽 ×6（阻塞）/ ×40（拍内）**。
详细推导与三种口径的定义见 [`docs/ASSESS-architecture-as-controller-2026-09-14.md`](docs/ASSESS-architecture-as-controller-2026-09-14.md)
与 [`docs/audit/H723-MOTION-QUALITY-AUDIT.md`](docs/audit/H723-MOTION-QUALITY-AUDIT.md)。

**⇒ 一句话定位**：**环频与确定性已是"工业伺服级"，运动功能是"安全 + 可规划"级**
（失步自检 ✅ / 轨迹规划 ✅ / 有界闭环 ✅ / 多轴 ❌ / 电流环 ❌）。
它证明的是"**把闭环从 PC 搬进片内程序面**"这件事成立；**不**声称它已是一台运动控制器。

---

## 五、能力边界（**三级写清** —— "没做"、"要扩架构"、"硬件真的缺"是三件事）

| 级 | 项 | 现状 | 怎么解 |
|---|---|---|---|
| **A** | **轨迹规划**（加减速 / 斜坡限幅）| ✅ 固件斜坡已落地（`op=19 sub=17 arg=Hz/s`，默认 0 ⇒ 既有行为逐位不变）；三角波/阶梯正弦例程已上机 | 形状验收待**片内黑匣子**重做（PC 侧差分测速已证不可用）|
| **A** | **"走 N 个脉冲自停"** | ✅ 已实现并上机（**7 PASS / 0 FAIL**）| 已解：**另一个定时器硬件数脉冲**（`TIM4` 外部时钟模式 1，`ITR2=TIM3`）零中断/零 DMA/零 ITCM |
| **A** | **失步自动检测 + 恢复** | ✅ 上机通过（1.17 s 触发 / 降额 0.25 / 轴恢复 99.2%）| 已解（整条逻辑在 ③ 层程序，**不改固件**）|
| **A** | 多圈行程 / ±180° 奇异点 | ✅ 能 | ③ 层做圈数累加器 / 用一个 `SR` 记住上次方向 |
| **B** | **多轴** | ⚠️ **能，但要扩架构**（`step.c` 单例 → 实例化；插补放 ③ 层）| ★ **三个真阻塞**：① 第二个编码器（AS5600 **地址固定 0x36** ⇒ 需 I2C mux/第二总线）② 第二个驱动器/电机（**台架没有**）③ ★ **原写"ITCM 64/64 KB 已满"——经实测更正：ITCM 只用了 14.32 KB / 64 KB（22.4%）**（账本 `python tools/mem_report.py`）⇒ **这条阻塞不成立**。<br>启动清单见 [`docs/PLAN-multi-axis-v1.md`](docs/PLAN-multi-axis-v1.md) |
| **A** | **`LUT` 查表** | ⚠️ 原语**通**（线性插值 + 越界夹取），但**表没有归属**：`0x23` 能上传，而 `deploy` 不碰它、SD/持久化不带它、`0x13 RESET` 清零它、`fill_tables` 会覆盖它 ⇒ 今天它住在**一块无主的易失内存**里 | 需先定"表住哪"：扩 deploy 载荷（推荐）/ 进持久化镜像 / 工具侧每次重下。见 [`docs/exp-EY-lut-provenance.md`](docs/exp-EY-lut-provenance.md) |
| **B** | `MOD`（取模）| ❌ 引擎 `ARITH` 六种模式里**没有**它 | 要动 ① 层原语 ⇒ **无明确需求前不做**（已登记） |
| **C** | **电流环** | ❌ **硬件缺**，非软件可补 | TB6600 只有 PUL/DIR/ENA ⇒ **须换驱动器**。★ 但本架构**能接**（走 ② 通用事务 op / CAN）⇒ 这是**选型**，不是架构缺陷 |

★ **A 类是"还没做"，B 类是"要扩架构"，只有 C 类是真的硬件缺。**
**⇒ 不声称现在是一台运动控制器；但也不把"没做"说成"做不到"。**

> 已撤回的数字（留案，避免别人再引用）：曾写"带斜坡 ~1100 rpm、突加 627 rpm ⇒ 可用转速 +75%"——
> **该测量是采样混叠伪影**（31000 Hz 下 2 s 走 38 圈，而命令通路只有 26 命令/s），
> 且 `main.c` 注释里"突加 31000 Hz 只能用到 627 rpm"一句**把"拉起拐点"与"突加可用转速"混为一谈**，
> 二者均已按实测改写。定案过程见 [`docs/PLAN-closedloop-stepper-v2.md`](docs/PLAN-closedloop-stepper-v2.md) 阶段 2。

---

## 六、怎么保证"能信"（**本系统最值得看的部分**）

1. **构建 6 道闸门**（任一不过即失败）：零警告 · ISR 调用树（ISR 可达 ⇒ 必须住 ITCM）·
   应答缓冲区越界（静态）· ③层静态判据（程序不得出现引脚号/地址/寄存器名）·
   **契据可机检**（8 类主张：结构 / 拒绝码 / 能力位 / 留位 / 完备性 / 文档漂移 / 判据存在性 / **门面一致性**）。
2. **每条判据都能失败**：配了变异体（注入固件缺陷看判据是否变红）· 反空判据 · 逐字节比对。
3. **"覆盖不到"判 SKIP，不是 PASS**；闸门"覆盖不足"判**无效**，不是"干净"。
4. **未覆盖项必须显式声明**（`--allow-uncovered <理由>`）—— 不允许沉默地留着。
5. **改契据/门面而不同步 [`docs/claims.md`](docs/claims.md) ⇒ 构建会红** —— 这一步把"忘了同步"
   从"下一个人照错的做"变成"构建过不去"。
6. ★ 实测纪律（血证换来的）：**"期望为 0"的判据必然假绿 ⇒ 必须先配"正对照"**；
   **差分量的分子分母必须同口径**；**测量期间不许做别的事**（命令通路是上位机墙钟定时的）。
7. ★★ **"抓坏判据的判据"自己也要能被证伪**（2026-09-18）：审计 1.1 内置 `--selftest-11`（四份合成样例证明"分支内 / 守卫落空 / 真无条件 / 他函数同名"能各归各位）；**台架依赖**的判据必须登记在 `.workbuddy/rig-dependent.json` 并被审计机械校验（条目指向不存在的判据 ⇒ FAIL）；**手写的派生量**由编译期断言挡（`EXEC_BUDGET_CYCLES == 展开式`，含变异对照）。

---

## 七、★★ "抖动"在本项目有**三个不同的量**（别混用、别相减）

| # | 名称 | 实测值 | 量的是**哪个时刻** | 权威源 |
|---|---|---|---|---|
| ① | **输出抖动 σ**（拍内执行确定性）| **3.6 ns**（CPU 直写档，为**引脚**本身）| **拍内**：从"算完"到**引脚真的翻转** | [`docs/ASSESS-architecture-as-controller-2026-09-14.md`](docs/ASSESS-architecture-as-controller-2026-09-14.md) |
| ② | **拍周期抖动**（跨拍调度确定性）| `period_min/max` = **19990 / 20008** ⇒ **跨度 90 ns** | **跨拍**：这一拍**什么时候开始** | [`docs/STEPPER-DEMO.md`](docs/STEPPER-DEMO.md) |
| ③ | **1 档（影子 + MDMA）引脚抖动** | 写影子 **13.0 ns** → **引脚 54~60 ns** | 同 ①，但经 MDMA 锁存 | 同 ① |

**① 与 ② 不能相减**（一个是"拍内漂多少"，一个是"拍与拍之间隔多久漂多少"）。
（② 的 90 ns 对速度环的影响：90 ns / 100 µs = **0.09%**。）

**★ 交付档是 `DCL_DO_LATCH = 0`（CPU 直写 BSRR）**：实测**引脚 σ 3.6 ns**，而 1 档（影子 + MDMA 锁存）
是 **54~60 ns** ⇒ **差 15 倍**。根因不是"MDMA 慢"，而是那条锁存链**没有按设计工作**：
`MDMA CTBR.TSEL` 扫遍 0…15 **全部照常工作** ⇒ **通道在自循环、不消费任何请求源**
⇒ **引脚时刻 = CPU 最后一次写影子 + 0.12 µs** ⇒ 设计承诺的"**输出由硬件定时器锚定**"**未达成**。
⇒ 决策：默认 0 档（无功能损失，输出沿还提前 ~300 ns，并消掉 ~350 万次/秒的 AHB4 写）。
完整证据见 [`docs/ARCH-TIMELINE-CPU-MDMA.md`](docs/ARCH-TIMELINE-CPU-MDMA.md)。

---

## 八、它解决什么问题（为什么不是又做一个 PLC）

| 路线 | 怎么拿确定性 | 代价 |
|---|---|---|
| **PLC / 软 PLC** | 把周期拉到 ms 级，用长周期"藏住"抖动 | 响应慢；要做 µs 级闭环就得换路线 |
| **本运行时** | **不藏**：拍默认 100 µs（**可配**），**每拍成本编译期可算**，热代码在 ITCM | 需要"编译期就确定"的设计纪律 |

**它明确不是什么**（详见 [`docs/SUPPORTED-SCOPE.md`](docs/SUPPORTED-SCOPE.md) §3）：
不是实时 Linux / 不是通用 PLC 替代品 / **不承诺"发请求即得值"**（事务跨拍，必须用就绪门）/
**不承诺内部 flash 掉电保持**（已按设计降级 ⇒ 持久化一律走 SD）/
**不承诺"输出由硬件锚定"**（该说法已被实测推翻，见 §七）。

---

## 九、硬件与接线

| 项 | 说明 |
|---|---|
| 板 | 鹿小班 **LXB723ZG-P1**（STM32H723ZGT6，1 MB Flash） |
| 晶振 | **HSE 25 MHz**（板上无源晶振，已实测反推确认） |
| 调试 | DAPLink（CMSIS-DAP）→ `pyocd`，目标名 **`stm32h723xx`** |
| 逻辑分析仪 | Saleae Logic2，接线 **`CH4 ← PA8`**（拍输出），GND 共地 |

**串口（PC 直连，板子 H1 排针）**

| CH340 | H1 脚 | 信号 |
|---|---|---|
| RXD | **6** | PA9 = USART1_TX |
| TXD | **5** | PA10 = USART1_RX |
| GND | 任一 GND | 共地（**必接**）|

★ 时钟初始化失败时固件会在 **PA8 上闪 `|错误码|` 次**（错误码见 `src/clock.h` 的 `CLK_ERR_*`）
⇒ 不依赖 SWD 也能定位卡在哪一步。

---

## 十、一条命令查全部 + 目录结构

```bash
bash tools/h723_full_regress.sh          # 20+ 套件串行、独占串口、末尾自动探活
DCL_PORT=COM7 bash tools/h723_full_regress.sh   # 端口可用环境变量覆盖
```
★ 判据是"**有没有出现新的失败模式**"，不是"PASS 数不低于某值"；文件头写清了**哪些失败是设计内的**
（内部 flash 持久化已降级 / modbus 需 485 回路 / w5 的 HIL 输出臂需接线 / 若干已知测试缺陷）。

```
src/          引擎核心（engine / isr / transport / dev_bind / i2c_sm / prog_store / sd / blackbox …）
tools/        验收与工具（**每个结论都能用这里的脚本复跑**）+ h723_full_regress.sh
docs/         契据 / 发布说明 / 可信范围 / 入门 / 架构 / 内存地图 / 状态 / 审计 / 规划
examples/     ③层 DCL 示例程序（**不得出现引脚号**，构建期静态判据会拒绝）
ld/ startup/  链接脚本与启动文件（显式定义 DTCM / ITCM 段）
build.sh      构建（含 6 道闸门）
```

---

## 十一、文档地图（**每份文档是哪个问题的权威源**）

| 你要问 | 权威源 |
|---|---|
| **我用它到底能靠什么、不能靠什么**（可信范围 + 使用入口 + 遇错怎么救）| [`docs/SUPPORTED-SCOPE.md`](docs/SUPPORTED-SCOPE.md) |
| 这一版新增/变了什么、**已知问题**、每个数字的复跑命令 | [`docs/RELEASE-v2.1.0.md`](docs/RELEASE-v2.1.0.md) |
| 从零跑通（构建 / 烧录 / 第一个程序）| [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md) |
| 需求"应该是什么"（**契据**）| [`docs/REF-program-contract.md`](docs/REF-program-contract.md) |
| 契据里**哪几条能被机器验** | [`docs/claims.md`](docs/claims.md)（构建时强制一致）|
| 引擎内部怎么运作（四公理 + 八不变量）| [`docs/CORE-ENGINE.md`](docs/CORE-ENGINE.md) |
| 架构 / 命令表 / 内存地图 | [`docs/ARCH-H723.md`](docs/ARCH-H723.md) · [`docs/MEMORY-LAYOUT.md`](docs/MEMORY-LAYOUT.md)（★ **SHM 偏移以 `src/engine.h` 为唯一权威**）|
| **闭环步进的实验计划与逐阶段结论（含撤回的数字）** | [`docs/PLAN-closedloop-stepper-v2.md`](docs/PLAN-closedloop-stepper-v2.md) |
| **多轴：就绪度评估 + 启动清单** | [`docs/PLAN-multi-axis-v1.md`](docs/PLAN-multi-axis-v1.md) |
| 运动品质全套实测（§1–§18）| [`docs/audit/H723-MOTION-QUALITY-AUDIT.md`](docs/audit/H723-MOTION-QUALITY-AUDIT.md) |
| 最近发生了什么、怎么定位到的（按日期快照）| [`docs/STATUS-2026-09-16.md`](docs/STATUS-2026-09-16.md)　★ **本版新增的实证见上面 §一**，逐阶段结论见 [`docs/PLAN-closedloop-stepper-v2.md`](docs/PLAN-closedloop-stepper-v2.md) |
| **2026-09-18 这一天做了什么**（日程 + 五条战线成果 + 数字 + 当天自查出的 9 条自身缺陷 + **不能宣称的 7 条**）| [`docs/DAY-2026-09-18.md`](docs/DAY-2026-09-18.md) |
| **完善计划与完成度**（阶段 1~4，每项带判据与证据）| [`docs/PLAN-completion-2026-09-18.md`](docs/PLAN-completion-2026-09-18.md) |
| **③ 层表达力**：`OUTPUT … PERIOD=` / `ABS` / 变量阈值 / 数字文法 | [`docs/exp-EW-output-period.md`](docs/exp-EW-output-period.md) · [`docs/exp-EX-composed-blocks.md`](docs/exp-EX-composed-blocks.md) |
| `LUT` 表归属 · 拍长缩放（耦合 #6）| [`docs/exp-EY-lut-provenance.md`](docs/exp-EY-lut-provenance.md) · [`docs/exp-EZ-exec-budget-scale.md`](docs/exp-EZ-exec-budget-scale.md) |
| 历史长篇（含逐日流水、铁律 0 血证）| [`docs/README-full-legacy.md`](docs/README-full-legacy.md) |

---

## 许可与状态

内部项目（`JimmyZ-zengmin/dcl-controller`）。**未解决的问题逐条列在**
[`docs/RELEASE-v2.1.0.md`](docs/RELEASE-v2.1.0.md) §6 —— 请在使用前读一遍，
那是"我知道它哪里不行"的完整清单。

**版本**：`v2.1.0`（基线 `v2.0.0`）｜**能力字**：`0x7DF7`｜**构建闸门**：6 道｜**上机判据**：126 条
（★ 这是 **v2.1.0 发布时**的计数；2026-09-18 之后新增的判据见上面 §一 与
[`docs/DAY-2026-09-18.md`](docs/DAY-2026-09-18.md) —— **不在此处累加**，免得给出一个无法复核的数）

**交付档指纹**（100 µs 档 hex md5）：`6daa7e65a778ac308267785104426f17`　——`tools/h723_restore_delivery.sh` 用它守"恢复出来的确实是交付档"；**改部署期代码必须同步该脚本的 `EXPECT_MD5`**（不同步会让该脚本中止, 恢复路径静默失效）。
