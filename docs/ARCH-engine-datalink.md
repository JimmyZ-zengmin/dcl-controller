# ARCH-engine-datalink — 核心引擎的数据链与架构 (2026-09-12)

> 视角: **数据怎么流**。回答"引擎由哪些部分组成、每部分负责什么、一拍之内数据如何移动"。
> 配套: `docs/ARCH-H723.md`(平台与落位) / `docs/FRAMEWORK-MAP.md`(组件与微调) /
> `docs/PLAN-io-into-engine.md`(I/O 搬进拍内的方案与过程)。
> 所有结构体尺寸都有 `_Static_assert` 守住, 与 PC 侧逐字节对齐。

---

## 一、数据容器: SHM 是一块 32KB 的"共享内存地图"

DTCM 里的 `g_shm`(`0x20008220`, 32KB)。**所有跨域数据交换都经过它, 没有例外**
—— 这是"可观测"与"PC 可读写"的地基。分区:

| 偏移 | 区 | 尺寸 | 谁写 | 谁读 |
|---|---|---|---|---|
| 0x0000 | **CTRL** (MAGIC/VERSION/HEARTBEAT/RELOAD/RUN/n_routes/…) | 0x18 | 协议 / ISR | 全部 |
| 0x0018 | **TIMING** (samples/pmin/pmax/emin/emax/last_*) | 0x1C | ISR | PC |
| 0x0034 | `GPIO_MASK` (DO 管辖位) | 4 | 协议 | do_poll / 安全态 |
| 0x0038 | `N_SEQ` (顺序实例数) | 1 | 协议 | ISR |
| **0x0040** | **SENSOR_MAP[64] f32** | 256B | **输入段** (di_poll/adc_poll) | 扫描 / seq |
| **0x0140** | **ACTUATOR_STATUS[64] f32** | 256B | **扫描** | 输出段 (do_poll) / PC |
| **0x0240** | **WIRE_MAP[128] f32** | 512B | **扫描 / seq** | 扫描(反馈) / 输出段 |
| 0x0440 | LUT_DATA[256] f32 | 1KB | 协议 | 原语 LUT |
| **0x0840** | **ROUTE_TABLE[128]×16B** | 2KB | 协议(deploy) / ISR(热重载) | **扫描** |
| 0x1040 | ROUTE_STAGING[128]×16B | 2KB | 协议 | 热重载 memcpy |
| 0x1840 | PARAM_TABLE[128]×16B | 2KB | 协议 | 原语 |
| 0x2040 | PARAM_STAGING | 2KB | 协议 | 热重载 |
| 0x2840 | STATE_TABLE[128]×16B | 2KB | 原语(跨拍状态) | 原语 |
| 0x3040 | STATE_STAGING | 2KB | 协议 | 热重载 |
| 0x4000 | **SEQ 区** (步表 + 实例控制块) | — | 协议 / ISR | seq |
| 0x6000+ | MB 区 (Modbus 帧/保持寄存器) | — | 协议 / ISR | 协议 |
| 0x6E00 | W5 观测区 (HIL duty / ADC raw) | 8 | ISR | PC |

**★ 双缓冲模式**: `TABLE`(活) + `STAGING`(暂存) —— PC 永远只写 STAGING,
ISR 在**一个原子点**把 STAGING memcpy 进 TABLE ⇒ 这就是"≤1 拍热重载"的实现方式。

---

## 二、三个定长数据模型 (16 字节, 全部有静态断言)

### 2.1 `RouteEntry_t` —— 一条**计算规则** (数据链的原子单位)

| 字段 | 作用 |
|---|---|
| `src_type` / `src_index` | **输入从哪来**: SENSOR[i] / WIRE[j] / CONST |
| `dst_type` / `dst_channel` | **输出到哪去**: WIRE[j] (内部信号) |
| `op` | **算什么**: 19 原语之一 (PID/LPF/TON/CMP/MUX/…) |
| `param_idx` | → `PARAM_TABLE[i]` 的 4 个 float (阈值/增益/时间常数…) |
| `state_offset` | → `STATE_TABLE[i]` 的 4 个 float (**跨拍状态**: PID 积分项/LPF 历史/计时) |
| `actuator_idx` | → `ACTUATOR_STATUS[k]` 的浮点槽 (**不是物理引脚**) |
| `wire2_idx` + `flags` | **第二输入** (比较/算术的 B 侧); 必须过 `wire2_valid()` |
| **`period`** | **调度**: `div_idx(2bit) + phase(6bit)` —— 见第四节 |
| `flags` | ACTIVE / FORCE 相关 / WIRE2 有效 |

**一句话: 一条路由 = 「读一个源 → 用某个原语(带参数+状态)算 → 写一个 wire/actuator」。**

### 2.2 `SeqStepEntry_t` —— 顺序域的一"步"
`cond_type/cond_idx`(转移条件源) · `flags`(末步回卷/超时使能) ·
`param_idx`(value_a=转移阈值, value_b=超时秒) · `jump_idx`(分支目标, v1)
**语义: 停在此步时周期评估转移条件; 条件满足或超时 ⇒ 推进一步。**

### 2.3 `SeqCtrl_t` —— 顺序实例的运行状态
`step_base/n_steps`(本实例在步表的位置) · `step_cur`(当前步) ·
`out_wire`(**步号镜像到哪个 wire**) · `period`(调度, 同 RouteEntry) ·
`run`(START 置位) · `step_tick`(本步已停留的**激活拍数**, u32)
**★ `step_tick` 用 u32 是 OA5 的修复: u16 在快档下 6.55s 就回卷 ⇒ 长停留会静默卡步。**

---

## 三、数据链: 从现场到执行器 (完整回路)

```
 物理输入                     SENSOR_MAP              WIRE_MAP              物理输出
 ┌────────┐  di_poll/adc_poll ┌─────────┐  路由扫描  ┌──────────┐  hil_out_poll ┌──────┐
 │DI PC0-3│──────────────────▶│SENSOR[3..6]│─────────▶│          │──do_poll────▶│PE0-15│
 │AI PA0/1/4│────────────────▶│SENSOR[8..10]│        │WIRE[0..127]│      (PWM)  └──────┘
 │ADC 反馈 │─────────────────▶│SENSOR[2]  │          │          │        │      ┌──────┐
 └────────┘                  └─────────┘            └──────────┘        └─────▶│TIM3  │
                                  ▲                       ▲                    │  CCR1│
                                  │                       │                    └──────┘
                            PARAM_TABLE               STATE_TABLE
                          (静态系数 4×f32)          (跨拍状态 4×f32)
```

**三条关键规则**:
1. **SENSOR 只被输入段写** —— 扫描是纯读者 ⇒ 一拍之内数据不会"边算边变"。
2. **WIRE 是引擎内部信号总线** —— 扫描写、扫描也读(前一条路由的输出可以是后一条的
   输入) ⇒ **表序即求值顺序**(这就是部署期"归组排序"的原因)。
3. **ACTUATOR 是"命令值"不是"引脚值"** —— 物理输出由输出段(do_poll/hil_out_poll)
   在**扫描之后**读它并把命令变成引脚动作 ⇒ 输出用的一定是**本拍刚算出来的值**。

---

## 四、调度层: 分档 + 相位 (决定"什么时候算")

**`period` 字段的两半**: `div_idx`(2bit) 选档, `phase`(6bit) 选相位。

| 档 | `div_idx` | 周期 | `dt` 传给原语 | 用途 |
|---|---|---|---|---|
| 快 | FAST | **每拍** (100µs) | `DT_FAST` | 快速逻辑/联锁 |
| 中 | MID | **每 2 拍** (200µs) | `DT_MID` | 一般闭环 |
| 慢 | SLOW | **每 4 拍** (400µs) | `DT_SLOW` | 慢回路/滤波 |

**★ `dt` 一并传进原语** —— 所以 PID 积分、LPF 时间常数、RATE 斜率在**不同档位下
自动用对时间尺度**, 不需要程序里手写系数。

**桶化 (部署期一次 `engine_build_buckets`)**: 把 ACTIVE 路由**预排序**成连续段
`[div0 全部][div1 各 phase 桶][div2 各 phase 桶]`, 并算好每桶的 `offset/count`。
⇒ **每拍只跑三段**: `div0 全部 + div1 本拍 phase + div2 本拍 phase`,
**不需要每拍过滤**(扫描体内只判 ACTIVE, 不判档位)。
⇒ 相位(phase)让同档的路由**错开在不同拍**, 把负载摊平 —— 这是"分摊"而不是"跳过"。

---

## 五、一拍之内的数据流动 (谁在什么时候读写)

| 阶段 | 写什么 | 读什么 | 门控 |
|---|---|---|---|
| **拍头** | 心跳/tick/统计基准 | — | 无 |
| **输入段** | `SENSOR[2..10]` | 物理 GPIO/ADC | **门之外**(停机也采) |
| force (拍首) | `WIRE[j] = FORCE_VAL` | FORCE_MASK/FVAL | `gate&&RUN` |
| **扫描** | `WIRE[dst]`, `ACTUATOR[ai]`, `STATE[i]` | `SENSOR`/`WIRE`/`PARAM`/`LUT` | `gate&&RUN` |
| **seq** | 步号 wire, `STATE`(v1) | `SENSOR`/`WIRE` | `gate&&RUN` |
| **输出段** | 引脚/外设(TIM3_CCR1/GPIOE) | `WIRE[20]` / `ACTUATOR` | 无(域内部自查) |
| **通信** | MB 区 | MB 区 | **门之外**(停机可通信) |
| 拍尾 | ISR 时长/拍周期 | DWT | 无 |

**⇒ 一条铁律贯穿**: **输出段永远在扫描之后** —— 保证"用的是本拍的算出来的值",
而不是上一拍的。这也是为什么 I/O 搬迁时位置不能随便挪。

---

## 六、横切保护 (不属于任何一段, 但每条数据流都过)

| 机制 | 位置 | 作用 |
|---|---|---|
| **Force 写端屏蔽** | 扫描体写 WIRE 前 | 被强制的 wire 不允许被路由覆写(就地读 mask, 非栈快照) |
| **NaN 防护** | 扫描体 | `_finite_f(src/wb)` ⇒ 非有限值归 0 |
| **`wire2_valid()`** | 扫描体 | 第二输入必须查标志, 否则静默读到 wire[0] (A3 实锤) |
| **归零回退** | 扫描体 | `state_offset` 非法时用 `s_state_fallback`, 不越界 |
| **校验和 `acc`** | 扫描体返回 | 防死代码消除 + 给"真算过"一个外部证据 |
| **调度校验和** | `engine_bucket_checksum` | 桶布局与表字节序绑定, 布局错了能发现 |
| **预算保护** | `engine_prog_budget` | 部署期拒收超预算程序(静态) + 运行期 OVERRUN 计数(动态) |
| **安全态** | `eng_outputs_safe` | 停机时逐个调各域登记的 `*_safe()`(现含 hil + do) |
| **SHM 布局断言** | `_Static_assert` × N | 结构尺寸/偏移/重叠在**编译期**挡住 |

---

## 七、组成部分职责一览

| 部分 | 职责 | 入口 |
|---|---|---|
| **数据容器** | SHM 分区 + 双缓冲 | `OFF_*` |
| **数据模型** | 路由/步/参数/状态 (16B 定长) | `RouteEntry_t` … |
| **调度器** | 分档 + 相位 + 桶化 | `engine_build_buckets` |
| **扫描体** | 跑路由: 读源→算→写目的 | `engine_scan_itcm` / `_flash` |
| **原语库** | 19 个 IEC 运算 | `prim_exec` (primitives.h) |
| **顺序域** | 步进器 + 译码 | `engine_seq_tick` |
| **部署/重载** | 校验 + STAGING→TABLE | `engine_reload_active` |
| **保护层** | force/NaN/预算/安全态/校验和 | 横切 |
| **观测层** | 计时/统计/心跳/校验和 | `g_*` + obs_anchor |
| **I/O 域** | 采样与输出 (di/adc/hil/do) | `*_poll` / `*_tick` |

---

## 八、可讨论的几个点 (留给讨论)

1. **WIRE 只有 128 个而 SENSOR/ACTUATOR 各 64** —— 若程序大, WIRE 会成为瓶颈?
2. **表序即求值顺序** —— 强约束(编译器要排序)。是否需要 v1 的拓扑排序?
3. **`phase` 只有 6bit(0..63)** —— 慢档 4 拍下够用; 若要更多档位分相则要扩字段。
4. **STATE 表 128×16B 是全局共享的** —— 原语的跨拍状态没有"实例隔离"概念,
   由编译器分配槽位。人工写表容易撞槽。
5. **force 与安全态的交互** —— 停机时 force 还生效吗? (当前 `eng_outputs_safe` 清
   物理面, 但 WIRE 里的 FORCE_VAL 保留)
