# H723 PLC 内核 — 架构与机制参考

> 版本: 2026-09-11 · 基线提交 `0c9f5ce`（`ba3060b` 只加文档）
> 来源: 四路源码精读（引擎内核 / 协议与通信 / 组态·校验·持久化 / 顺序域与外设域），**每条都带 `文件:行`**
> 用途: 微调功能时的"改动地图 + 不变量清单"

---

# §0 先读这一段：当前状态与两个未决问题

## 0.1 能用的功能面

| | 状态 |
|---|---|
| 命令 | **21 条全实现**（详见 §5.2） |
| 自建套件 | **12 套 195 PASS / 0 FAIL** |
| 老范本平移套件 | **22/30**（失败 8 项 = 7 项平台专属 + 1 项半可修，无功能缺口） |
| 确定性 | 拍长抖摆 σ ≲ 1.6 cyc、与负载无关（LA 外部证据）；4 项成本/收益指标达标 |

## 0.2 未决问题 A：**DWT 时基可被调试器静默停掉**（重要，影响所有计时判据）

**现象**：跑完任何 pyocd 会话之后，`0x38` 读回 `pmin=0 pmax=0`（周期统计死），
而 `emax` 正常、`samples` 正常 ⇒ **只有周期路径坏**。

**机制（代码结构 + 板上读数，自洽）**：
- ISR 尾部 `main.c:763` 的 `g_per_prev = t0;` 在**门之外**每拍都写
  ⇒ `g_per_prev == 0` **当且仅当 `t0 == 0`**；
- 板上实测 `g_per_prev = 0` 而 `di = 7685`（非 0）⇒ 两者同时成立只可能是
  **`DWT_CYCCNT` 有一段时间不计数（读回常数 0）**；
- 固件 `dwt_enable()`（`clock.c:196-203`，含关键的 `DWT_LAR = 0xC5ACCE55` **解锁 CoreSight**）
  **只在启动跑一次**（`main.c:2247` 附近的冷启动初始化）⇒ 调试会话把时基停了之后
  **没人再使能**，直到下一次真复位。

**为什么它重**：时基本身是个"写一次就算"的状态 —— 它一死，
**一切 DWT 计时量（`pmin/pmax/emin/emax/ov`）都会静默读 0**，
而 `samples` / 协议 / 路由表 / 桶表**全部正常**。这是"配置全对但功能不可用"的同族，
而且它**正好是 `ov == 0` 这类判据的前提**（时基死了，`ov` 也永远不可能 >0）。

**建议处置（未实施）**：① 把已有的 `g_per_glitch_n`（时钟不连续计数，`main.c:389`）
确立为**任何 DWT 计时宣称的伴生判据**（`glitch == 0` ⇔ 时基全段活着），最好随 `0x38` 一起读走；
② 连续 N 拍 `di == 0` 就自愈（重跑 `dwt_enable()`），但**自愈必须配自愈计数**
（否则会掩盖"有人动了调试口"）；③ 验收脚本先断言 `glitch == 0`，否则**拒答**。

## 0.3 ~~未决问题 B~~ → **✅ 已解决 (2026-09-12)：根因 = VTOR 对齐不足，硬件取向量按对齐值掩码**

**原文保留（当时的困惑），结论追加在末尾。**

**证据**：给时基防护加了约 200 字节代码/数据之后，固件**上电即卡死**，
**A/B/A/B 交替 4 次**：旧档每次 `0x01 → ACK`，新档每次 `TIMEOUT`。

**已实测排除**（不是推理）：
- ISR 体积压过 ITCM 向量表 —— `_eitcm=0x1818` < `_vtor_itcm=0x1880`，余量 0x68；
- "新代码在卡死点之前执行" —— 新增代码全在主循环/拍中断里，而 `fp_init` 是一次 `memset`
  （`transport.c:27`），`uart1_init`（`uart.c:52-98`）**一个等待循环都没有**。

**现有证据互相矛盾**（所以机制没闭环）：PC = `0x080057d4` = startup 的 `Default_Handler`（弱默认
`B .`）；ICSR ⇒ VECTACTIVE=**44**（TIM2）；VTOR=`0x1880`（ITCM）；但两个向量表里异常 44 的槽
**都读到 `0x00000001`** = `TIM2_IRQHandler`(ITCM 0x0) | Thumb 位 —— **看似正确**；
CFSR/HFSR/BFAR 全 0（不是异常/总线错）。
★ 且 **pyocd 对 ITCM 的可见性从未验证**（项目既有结论：pyocd 只可靠读 **PPB 与 SRAM**）
⇒ 上述 ITCM 读数**可能本身就是假的**。

**为什么它比 A 更根本**：与项目已知族（"flash 取指成本 = f(地址 mod 32)"、
`SCAN_FLASH_PAD` 那个"落位敏感实验"开关）同源 —— 只要它存在，
**任何一次功能增强都可能随机制造出这类上电故障**，而症状看起来像"新代码写错了"。

**下一步**：机械二分（把那次改动拆成 5 个可分离小块逐个 build+flash 测 `0x01`），
找出触发点再谈机制。

> **★ 2026-09-12 已解决——真因不是"体积敏感"，是 VTOR 对齐不足**：
> ARM 规则（developer.arm.com 102283）：**VTOR 对齐必须是 2 的幂、且 ≥ 4×异常总数**。
> H723：150 IRQ + 16 系统 = **166 项** → 4×166 = 664 → 向上取 2 的幂 = **1024 字节**。
> 我们用 128 对齐 ⇒ 硬件把表当 32 项：TIM2(索引 44) 被掩码到 **44 mod 32 = 12**(保留项
> → Default_Handler) ⇒ 整机卡死。**表内容/VTOR/代码全对却跳错地方**的原因就在这。
> 之前"PC 看似正确地读了 slot[44]"的困惑，真因是 CPU 取向量时**根本没读 slot[44]**。
> **修法**（ld 脚本，三层）：① `.itcm_vectors` 对齐 **1024** ② 表**钉在 ITCM 末尾
> 0xFC00**（与代码大小解耦）③ `ASSERT(_eitcm <= _vtor_itcm)` 链接期挡越界。
> 另加**拍活体自检**（等 3 tick 不动就点灯）。验收：表钉 0xFC00 后全判据 PASS，
> `slot[44] @ 0xFCB0 = 0x00000001`（TIM2_IRQHandler）实读确认。
> ★ **教训**："体积敏感"是表象，"对齐不足"才是本质——**按芯片 IRQ 数算对齐，不是经验值**。

---

# §1 平台与运行时底座

## 1.1 硬件与内存

- MCU: **STM32H723ZGT6**，CPU 400MHz（编译期假定值见 `clock.h` 的 `CLK_CPU_HZ`）
- 内存分区（`ld/*.ld`，构建输出实测）: **ITCM 64KB @0x0** / **DTCM 128KB @0x20000000** /
  FLASH 1MB @0x08000000 / AXI SRAM 320KB
- 当前占用: ITCM 7KB（10.9%）· DTCM 69KB（53.9%）· FLASH 33316B（3.2%）
- **栈**: `_estack = ORIGIN(DTCM) + LENGTH(DTCM)` ⇒ 栈在 **DTCM 末端**（单周期、无总线仲裁）
- **SHM**: `g_shm` 在 DTCM，`SHM_SIZE = 0x8000`（32KB），链接段 `.dtcm_shm`

## 1.2 构建

- 入口: `bash build.sh`（cmake 4.0.3 + ninja + STM32CubeIDE GCC 7.3.1）
- **零警告硬闸门**: CMakeLists 里 `-Werror` + build.sh 再扫一遍完整日志（`warning` 计数 > 0 即失败）
- **每次显式传全部开关默认值**，构建后**打印从 `CMakeCache.txt` 读回的生效开关**
  （起因真事故：`-D` 是缓存的，"自检版"曾被当交付固件烧进去）
- 开关清单（`build.sh` 的 `DEFAULTS`）:
  `DCL_BOOT_PROFILE=0` · `DCL_BOOT_GATE=1` · `DCL_BOOT_SEL=1` · `DCL_BOOT_SCAN_MODE=0` ·
  `DCL_VTOR_ITCM=1` · `SCAN_FLASH_PAD=0` · `DCL_PA9_MODE=1` · `DCL_BOOT_BANNER=1` ·
  `DCL_BANNER_PERIOD=0` · `DCL_UART_SELFTEST=0` · `DCL_UART_BAUD=115200` ·
  `DCL_DEPLOY_SELFTEST=0` · `DCL_MIN_UART=0` · **`DCL_HIL_SAFE=1`**
- ★ **构建会间歇失败**: `cc1.exe: fatal error: can't open 'build\tmp\ccXXXXXX.s' ... Permission denied`
  —— 随机文件、随机名、**重跑即收敛**，与代码无关（疑似实时扫描锁新建临时文件）。**症状极像"刚改坏了代码"**。
- 烧录: `pyocd flash -t stm32h723xx -O connect_mode=under-reset <hex>`（**传 Windows 风格 `C:/...` 路径**）

## 1.3 启动流程（`main()`，`main.c:2180-2443`）

`g_stage` 是启动面包屑（32 步），用来定位"上电卡在哪"：

| stage | 动作 | 行 |
|---|---|---|
| 1 | 记录 `g_isr_itcm` | 2192 |
| 2 | `clock_init()`，失败则 `blink_error(err)` | 2197-2199 |
| 3 | `clock_get_hclk_hz()` | 2201 |
| 4 | 落位自检 `shm_layout_ok()` + 记录 scan 函数地址 | 2208 |
| — | `cold_start_reset()` → `shm_guard_paint()` → `mb_uart_enable()` → `persist_load()` | 2221-2240 |
| 5 | 计时统计冷启动初始化（**含 `g_per_prev = 0` 与 `dwt_enable()`**） | 2262 起 |
| 6 | 表装载（profile 或恢复） | 2283 |
| 8 | `fp_init(&s_parser)` + `uart1_init(UART_PCLK2, UART_BAUD)` | 2287-2289 |
| 10 | 组帧自检 `frame_build_selftest()` + 横幅 `proto_banner()` | 2294-2299 |
| 11 | deploy 自检（本构建未启用） | 2315 |
| 21-25 | `adc_init` → `ai_init` → `di_init` → `hil_init` → **注册 HIL 输出面** | 2319-2329 |
| 12 | 进主循环（每轮末置 `g_stage = 9`） | 2338 / 2479 |

## 1.4 时间基

- **拍 = TIM2 中断，100µs**（40000 cyc @400MHz）。`g_tick_count` 在 `main.c:557` 每拍 +1
- **拍输出脚 PA8**（`TICK_PORT=0`/`TICK_BIT=8`，`main.c:120-121`），按 `g_tick_count & 1` 翻转
  ⇒ 外部看到 **5kHz 方波，周期 200µs、占空比 50%**（LA 实测 50.00%）
- **DWT 周期计数** `DWT_CYCCNT`（`regs.h:208`，2.5ns 分辨率）是**所有计时量的唯一来源**
  ⇒ 见 §0.2：它可以被调试器静默停掉，而固件只在启动使能一次
- ★ 两个"预算"常量语义不同，**不要合并**:
  - `EXEC_DEPLOY_BUDGET = 26000`（`engine.h:566`）—— 下载期**静态门**（预测）
  - `EXEC_BUDGET_CYCLES = 32000`（`engine.h:578`）—— 运行期**动态判据**（实测，`ov` 计数用）

## 1.5 ITCM 落位（微调时最容易踩的地方）

| 符号 | 值 | 说明 |
|---|---|---|
| `_sitcm` | 0x0 | ITCM 起点；`TIM2_IRQHandler` 与 `engine_scan_itcm` 都在这里 |
| `engine_scan_itcm` | 0x51c | 分档扫描体（ITCM 版） |
| `_eitcm` | **0x1818** | ITCM 代码段末端 |
| `_vtor_itcm` | **0x1880** | **ITCM 里的向量表**（BSS，由代码填充；VTOR 指这里） |
| `_evtor_itcm` | 0x1c80 | 向量表末端（1KB，256 项） |
| `engine_scan_flash` | （flash） | 独占 `.scan_flash` 段，供落位实验 |

★ **余量只有 `0x1880 - 0x1818 = 0x68`（104 字节）**。往 ITCM 里加代码/表等于在向量表旁边堆砖头。
`engine_scan_flash` 的落位可用 `-DSCAN_FLASH_PAD=N` 调节（项目既有的"落位敏感实验"开关）。

---

# §2 数据面：SHM 布局总表

`SHM_SIZE = 0x8000`（32KB，`engine.h:401`）。访问宏 `SHM_U8/U16/U32/PTR(b,off)`（`engine.h:43-46`）。

## 2.1 控制块（0x00–0x3F）

| 偏移 | 类型 | 名称 | 含义 |
|---|---|---|---|
| 0x00 | u32 | `CTRL_MAGIC` | `'DCL1'`，SHM 已初始化（唯一就绪判据） |
| 0x04 | u32 | `SHM_LAYOUT_VERSION` | 字段语义版本（非偏移版本），当前 `0x00010000` |
| 0x08 | u32 | `HEARTBEAT` | **每拍无条件**递增 = CPU+定时器存活（门之外，`main.c:610`） |
| 0x0C | u8 | `RELOAD` | 热重载标志（单字节写 = 原子） |
| 0x0D | u8 | `ENGINE_RUN` | 引擎运行门 |
| 0x0E | u16 | `N_ROUTES` | 路由条数（**唯一权威来源**） |
| 0x10 | u16 | `N_PARAMS` | 参数条数 |
| 0x12 | u16 | `N_STATES` | 状态条数 |
| 0x14 | u32 | `PROG_MAGIC` | 程序魔数 |
| 0x18 | u32 | `TIMING_SAMPLES` | = `g_isr_n`，**仅 RUN 拍**（镜像） |
| 0x1C | u32 | `TIMING_PERIOD_MIN` | 拍周期最小（哨兵 0xFFFFFFFF → 读作 0） |
| 0x20 | u32 | `TIMING_PERIOD_MAX` | 拍周期最大 |
| 0x24 | u32 | `TIMING_EXEC_MIN` | ISR 执行最小 |
| 0x28 | u32 | `TIMING_EXEC_MAX` | ISR 执行最大 |
| 0x2C | u32 | `TIMING_LAST_PERIOD` | 末拍周期 |
| 0x30 | u32 | `TIMING_LAST_EXEC` | 末拍执行时长 |
| 0x34 | u32 | `CTRL_GPIO_MASK` | **[留] 语义未定，当前恒 0 且不可达**（见 §3.8） |
| 0x38 | u8 | `N_SEQ` | Sequencer 实例数 |
| 0x3A | u16 | `DEPLOY_SEQ` | 受理的部署序号 |
| 0x3C | u16 | `APPLIED_SEQ` | 已生效序号（ISR 切换完写入） |
| 0x3E | u16 | `APPLIED_LAT` | 生效延迟（拍） |

## 2.2 数据表区

| 偏移 | 内容 | 只读? |
|---|---|---|
| 0x0040 | `SENSOR_MAP` 64×f32 | 引擎写 |
| 0x0140 | `ACTUATOR_STATUS` 64×f32（**仅 SHM 浮点槽，不是物理脚**） | 引擎写 |
| 0x0240 | `WIRE_MAP` 128×f32 | 引擎写 / FORCE 覆写 |
| 0x0440 | `LUT_DATA` 256×f32 | 填表 |
| 0x0840 | `ROUTE_TABLE` 128×16B | **ACTIVE 只读**（deploy 走 staging） |
| 0x1040 | `ROUTE_STAGING` 128×16B | 写 |
| 0x1840 | `PARAM_TABLE` 128×16B | ACTIVE |
| 0x2040 | `PARAM_STAGING` | 写 |
| 0x2840 | `STATE_TABLE` 128×16B | ACTIVE |
| 0x3040 | `STATE_STAGING` | 写 |

## 2.3 其余分区

| 偏移 | 内容 |
|---|---|
| 0x3840 | 保留洞 `RSVD_DSL_DOMAIN`（尺寸 0xC40）；其中 **0x3850 `TIMING_OVERRUN`**（u32，超预算次数）、**0x3854 `TICK_STATS`**（u32[3]，档级条次） |
| 0x4000 | `SEQ_TABLE` 64×16B（步条目） |
| 0x4400 | `SEQ_CTRL` 8×16B（实例控制块） |
| 0x4480 | `ROUTE_BUCKETS` 220×u16（ACTIVE 桶表） |
| 0x4638 | `ROUTE_BUCKETS_ST`（staging 桶表） |
| 0x47F0 | `FORCE_MASK` u32[4]（128 位图） |
| 0x4800 | `FORCE_VAL` f32[128] |
| 0x4AA0 | `MB_SET`（Modbus 写区 64 WORD） |
| 0x4B20 | `MB_CTRL`（`MbCtrl_t` 40B） |
| 0x4B60 | `MB_RX` 256B |
| 0x4C60 | `MB_TX` 256B |
| 0x4D60 | `MB_HOLD`（Modbus 读区 64 WORD，wire 镜像） |
| 0x4DE0 | `CMD_REQ`（免串口协议帧，`DEPLOY_REQ_MAX=4096`） |
| 0x5DE0 | `MACRO_CTRL` 16B |
| 0x5DF0 | `MACRO_CODE` 4KB |
| 0x6E00 | `HIL_DUTY` u32（实际写入 TIM3_CCR1 的计数值） |
| 0x6E04 | `HIL_FB_RAW` u32（反馈最近一次 ADC 原始码） |

★ **布局不变量**由 `_Static_assert` 逐段"精确相接"断言（`engine.h:744-760` 一带），
**任何新域必须同时改 ① 偏移宏 ② 布局断言 ③ `cold_start_reset()`**。

---

# §3 执行面：引擎内核

## 3.1 ISR 每拍完整时序（`TIM2_IRQHandler`，`main.c:528-765`）

| # | 动作 | 行 | 门控 |
|---|---|---|---|
| 0 | `t0 = DWT_CYCCNT` | 530 | 无条件 |
| 1 | 判 `UIF` | 532 | UIF |
| 2 | 清 `UIF`；`g_stage = 7` | 533-534 | — |
| 3 | **统计复位分支** `if (g_stat_reset){ stats_reset(); g_per_prev = 0; }` | 536-553 | `g_stat_reset` |
| 4 | 翻 PA8（`g_tick_count & 1`） | 555-556 | — |
| 5 | **`g_tick_count++`**（桶相位基准） | 557 | — |
| 6 | PA9 分频翻转 | 559-563 | `g_pa9_enable` |
| 7 | **热重载** `STAGING → ACTIVE` | 571-586 | `OFF_CTRL_RELOAD` |
| 8 | **`HEARTBEAT++`**（每拍无条件） | 610 | 无条件 |
| 9 | `g_engine_run_seen = ENGINE_RUN` | 612 | — |
| 10 | **引擎块** `if (g_engine_gate && g_engine_run_seen)` | 613-655 | `gate && RUN` |
| 10a | `ta = DWT_CYCCNT` | 614 | |
| 10b | **Force 拍首覆写** `engine_force_apply()` | 624 | 两条扫描路径之外 |
| 10c | **扫描分派**：`g_scan_mode` ? `engine_tick()` : 全表扫 `engine_scan_*` | 626-637 | |
| 10d | 记 `g_eng_*` 统计 | 638-654 | |
| 11 | **顺序域** `engine_seq_tick()` | 670-694 | `gate && RUN`（**不依赖 `n_routes`**） |
| 12 | **Modbus** `mb_tick()` | 706-707 | **run 门之外**（STOP 也能收帧） |
| 13 | `t1 = DWT_CYCCNT; di = t1 - t0` | 709-710 | |
| 14 | ISR 时长统计（`di==0 → g_per_glitch_n++`） | 711-723 | |
| 15 | **超预算** `if (di > EXEC_BUDGET_CYCLES) g_isr_overrun++` | 732 | |
| 16 | **`samples`** `if (gate && RUN) g_isr_n++` | 741 | 仅 RUN 拍 |
| 17 | 拍周期统计 `p = t0 - g_per_prev`（负增量单独计数） | 743-762 | `g_per_prev != 0` |
| 18 | `g_per_prev = t0` | 763 | 无条件 |

★ **`HEARTBEAT`(0x08) 每拍无条件 / `samples`(0x18) 仅 RUN 拍** —— 二者对偶可区分三态
（引擎在跑 / ISR 活着但她 STOP / ISR 都没了）。**别再把这两个的语义搞混**（曾对调过，见 §11）。

## 3.2 分档调度与桶表

- 三档: `div0` 每 100µs（`dt=0.0001`）/ `div1` 每 1ms（10 拍，`dt=0.001`）/
  `div2` 每 10ms（100 拍，`dt=0.01`）（`engine.h:541-547`）
- `period` 单字节: **低 2 位 = div**（`PERIOD_DIV_MASK=0x03`）+ **高 6 位 = phase**（`<<2`，掩码 `0x3F`）
- 桶表 220×u16（440B）: `off1=bkt+0` `cnt1=bkt+10` `off2=bkt+20` `cnt2=bkt+120`
  - `off1[0]` **兼作 div0 条数**：div0 段 = `[0, off1[0])` 每拍全跑
  - `div1 段 = [off1[ph1], +cnt1[ph1])`，`ph1 = tick % 10`
  - `div2 段 = [off2[ph2], +cnt2[ph2])`，`ph2 = tick % 100`
- **两条建桶路径**（`engine_build_buckets` `engine.c:298-353` / deploy 版 `engine_stage_program`
  `engine.c:843-911`）—— ⚠️ 二者曾对同一程序产出不同结果，见 §11
- 全表扫分支（`g_scan_mode=0`）扫**全部 `g_n_routes` 条**，靠 `ROUTE_FLAG_ACTIVE` 跳过未激活
  ⇒ **等于忽略档位语义**；deploy 后强制 `g_scan_mode = 1`（`main.c:576-579`）
- ⚠️ **`BUCKET_DIV2_PHASES=100` 而相位字段只有 6 位** ⇒ 槽 64..99 恒空（`BUCKET_DIV2_PHASE_MAX=63`）

## 3.3 `RouteEntry_t`（16B，`engine.h:453-477`）

| 偏移 | 字段 | 说明 |
|---|---|---|
| 0 | `src_type` u8 | `SENSOR=0 / WIRE=1 / CONST=2 / HMI=3`（HMI 在 H723 被拒）|
| 1 | `src_index` u8 | 按 src_type 解释；**CONST 时它是 param 槽号** |
| 2 | `dst_type` u8 | 只有 `DST_WIRE=2` 合法 |
| 3 | `dst_channel` u8 | 目标 wire，上界 `MAX_WIRES=128` |
| 4 | `op` u8 | 0x00..0x12（19 个） |
| 5 | `flags` u8 | `ACTIVE=0x01` / `WIRE2=0x02` |
| 6 | `param_idx` u16 | 参数表索引 |
| 8 | `state_offset` u16 | 状态槽，0=无槽（有状态原语必需） |
| 10 | `actuator_idx` u16 | **SHM 浮点槽索引** `ACTUATOR_STATUS[0..63]`，0=不驱动，上界 64 |
| 12 | `wire2_idx` u16 | 第二输入；`wire2_valid()` = `((flags&WIRE2)||wire2_idx) && wire2_idx<128` |
| 14 | `period` u8 | div(2bit) + phase(6bit) |
| 15 | `reserved` u8 | 显式恒写 0（使逐字节校验和不依赖填充） |

## 3.4 19 个原语与成本（`primitives.h:34-226`；成本 `engine.c:736-758`）

| op | 名称 | 语义要点 | 成本 cyc |
|---|---|---|---|
| 0x00 | DIRECT | 直通 | 56 |
| 0x01 | CMP | 六模式（`value_b` 选 1:>= 2:< 3:<= 4:== 5:!= 其它:>），输出 0/1 | 76 |
| 0x02 | HYST | 滞环（ON=`value_a`, OFF=`value_b`） | 79 |
| 0x03 | CLAMP | 限幅 `[value_a, value_b]` | 74 |
| 0x04 | LPF | α=dt/(τ+dt)；τ>0 正常，τ=0→α=1，τ<0→α=0 | 97 |
| 0x05 | PID | 位置式+梯形积分+微分；**条件积分防 windup**；积分夹 ±100，输出夹 [0,100] | **145**（最贵） |
| 0x06 | RATE | `(src-state_a)/dt` | 68 |
| 0x07 | DEADBAND | 死区 | 76 |
| 0x08 | MUX | `i=(int)value_a & (MAX_WIRES-1)`，返回 `wire[i]` | 70 |
| 0x09 | EDGE | 上升/下降/双边 | 87 |
| 0x0A | LUT | 线性插值，f 夹 [0,254] | 89 |
| 0x0B | CNT | CTU/CTD/CTUD，`wb` 为 R/LD/CD | 83 |
| 0x0C | TIMER | TON/TOF/TP，单位秒 | 80 |
| 0x0D | ARITH | 加/减/乘/**除零返 0**/max/min | 74 |
| 0x0E | SCALE | `value_a*src + value_b` | 67 |
| 0x0F | AND | 布尔（>0.5）双输入 | 77 |
| 0x10 | OR | 布尔双输入 | 78 |
| 0x11 | NOT | `src>0.5→0` 否则 1 | 71 |
| 0x12 | SR | 双稳态（`value_a`=0 置位优先 / 1 复位优先） | 85 |

- 越界 op 的成本兜底 = `PID*2 = 290`（`engine.c:766-770`）
- 双输入原语（需要 `wire2`）: `AND / OR / ARITH / SR / CNT`（`engine.h:677-680`）
- `OP_COST_MAX_MEASURED = 145` 必须恒等于 `k_op_cost_itcm[OP_PID]`（有机械检查 + `_Static_assert`）

## 3.5 有状态原语与状态表

- **9 个**: `LPF / PID / HYST / RATE / DEADBAND / EDGE / CNT / TIMER / SR`（`engine.h:702-707`）
- `StateEntry_t` = 4×f32 = 16B（`engine.h:490-497`）；`MAX_STATES = 128`
- 索引 = `state_offset`（0 = 无槽哨兵）；ISR 侧越界落 `s_state_fallback` 兜底槽（`engine.c:252-253`）
- **校验：有状态原语必须挂非 0 槽**，否则 deploy 拒绝

## 3.6 Force（强制/释放 wire）

- **拍首覆写**（`engine.c:407-426`，调用 `main.c:624`）：读 4 个 mask 字，全 0 则零成本快路径；
  只遍历置位位 `__builtin_ctz`，把 `FORCE_VAL[i]` 写进 `WIRE_MAP[i]`
- **写端屏蔽**：路由写 dst 与顺序域写 out_wire 前都查
  `msk = FORCE_MASK[dw>>5]`，`if (!(msk & (1<<(dw&31)))) wm[dst]=res;`
  ⇒ 被强制的 wire 不被路由覆写（**就地 volatile 读**，不用拍首快照）
- **设置端** `h_force_w2`（`main.c:1631-1672`）: mode=1 → 置 mask + 写 `FORCE_VAL` + **立即写一次 `WIRE_MAP`**
  （只写 WIRE_MAP 会在下一拍被 `FORCE_VAL` 抹掉，而"强制 0"恰是常见用例）；
  mode=0 → 清 mask + `FORCE_VAL=0`（清残留）
- `eng_force_clear` 在 **deploy / RESET / SEQ_DEPLOY** 时调用

## 3.7 热重载（`engine_reload_active`，`engine.c:938-968`）

**严格顺序**（每一项都不可换位）：
1. 路由 `STAGING → TABLE`（`nr*4` 字）
2. **桶 `BUCKETS_ST → BUCKETS`（必须与路由同一拍切换）**
3. 参数 `STAGING → TABLE`
4. **状态表先全清再拷**（新程序绝不继承旧运行状态 —— 否则饱和积分残留会造成上电满功率冲击）
5. `PROG_MAGIC = 'DCL1'`
6. `APPLIED_SEQ = DEPLOY_SEQ`（生效确认）

**触发**：deploy 置 `RELOAD=1`（单字节 = 原子）；ISR 在**扫描之前**检查并整段切换
⇒ **本拍就用新表**，不出现"已受理但这一拍还用旧表"的中间态。代价是这一拍 ISR 变长（可观测
`g_reload_cyc`，实测 ~3.6µs）

## 3.8 输出安全态与"物理输出面注册表"

- 触发点: `0x12 STOP`（`main.c:1591`）与 `0x13 RESET`（`main.c:1625`）
- `eng_outputs_safe()`（`engine.c:1078-1112`）做两件事:
  1. 执行器数组 `ACTUATOR_STATUS[0..63]` 归零
  2. **遍历注册表调用全部已注册的物理输出面**
- **注册表**（`engine.c:1061-1073`，容量 `ENG_MAX_OUT_SURFACES=4`）:
  各域在 init 后 `eng_register_output_surface(fn)`；当前注册的是 HIL PWM（`main.c:2294`）
- `eng_output_surface_count()`（登记数）与 `g_safe_surfaces_ran`（执行数）**对照**才能区分
  "没登记"与"登记了没跑"
- ⚠️ `OFF_CTRL_GPIO_MASK` 语义未定且不可达（旧的 BSRR 清位路径已按审计发现 H 移除，
  因为"每 port 只取 2 位"的位映射对不上 16 引脚，且 u32 装不下 176 位）
  ⇒ **当前 `eng_outputs_safe` 不清任何 GPIO**，只清数组 + 跑注册面

---

# §4 组态与下载

## 4.1 DSL 编译器 `tools/dclc.py`

- 语法: 一行一条，注释 `#` / `//`；信号名小写/数字/下划线，FB 大写
- 支持的语句: `SENSOR` `HMI` `CONST` `SCALE` `PID` `ALARM` `GT/GE/LT/LE/EQ/NE` `LPF` `HYST`
  `RATE` `DEADBAND` `TON/TOF/TP` `CTU/CTD/CTUD` `R_TRIG/F_TRIG` `LIMIT`
  `ADD/SUB/MUL/DIV/MAX/MIN` `SR/RS` `SEL` `LOGIC` `OUTPUT` `SEQ`
- 参数写法 `KEY=值`；时间字面量 `3s/500ms/100us/2m/1h`（裸数字=秒）
- **`PERIOD=<100us|1ms|10ms>`** 后缀指定档位（只允许这三档），缺省继承当前档
- 输出 = **deploy 载荷**（不是帧）: `[nr:u16][np:u16][ns:u16] + routes + params`
- 字段约定（`dclc.py:679-690`）: **`actuator_idx` 编译器恒写 0**；
  `state_offset` 有状态原语从 **1** 起分配（0 是哨兵）；`param_idx` 连续分配；
  `flags = 1 | (2 if wire2_valid)`；`reserved` 恒 0

## 4.2 deploy 载荷格式（`0x10`）

```
[nr:u16][np:u16][ns:u16][routes nr×16B][params np×16B][states ns×16B]
```
`params` 每项 = 4×f32（`value_a..value_d`，按原语解释，例: `PID=Kp,Ki,Kd,SP`）
响应 ACK = `[seq:u16][budget:u32]`（6B）

## 4.3 下载期校验链

**`h_deploy` 的 10 道门**（`main.c:922-1019`，按顺序）：

| # | 判据 | 拒绝串 |
|---|---|---|
| 1 | `n < 6` | `short` |
| 2 | counts 超上限 | `counts exceed max` |
| 3 | 载荷长度不足 | `payload short` |
| 4 | 每参数高 8 位 == 0xFF（非有限） | `param not finite` |
| 5 | 逐条 `engine_route_validate`（仅 ACTIVE 条） | 见下表 |
| 6 | LPF 的 τ ≠ 0 | `lpf tau must be >0` |
| 7 | dst 唯一写者（同一 wire 两条路由 = 结果取决于表序 → 非确定性） | `dst conflict` |
| 8 | 跨档速率（`SRC_WIRE` 且生产者档位比消费者慢 ⇒ 欠采样/混叠） | `rate mismatch` |
| 9 | `engine_prog_budget > EXEC_DEPLOY_BUDGET` | `exec budget exceeded` |
| 10 | `engine_stage_program` → 清 force → `DEPLOY_SEQ++` → `RELOAD=1` → ACK | — |

**`engine_route_validate` 的全部校验项**（`engine.c:792-841`）：

| 判据 | 拒绝串 |
|---|---|
| `param_idx >= 128` | `param_idx out of range` |
| `state_offset >= 128` | `state_offset out of range` |
| 有状态原语但 `state_offset == 0` | `stateful op needs state_offset` |
| `dst_channel >= 128` | `dst_channel out of range` |
| `wire2_idx >= 128` | `wire2_idx out of range` |
| **`actuator_idx >= 64`**（0 合法） | `actuator_idx out of range` |
| `(period & 3) > 2` | `bad div` |
| src_index 越界（按 SENSOR/WIRE/CONST 分别判） | `src_index(...) out of range` |
| `SRC_HMI`（H723 未实现） | `SRC_HMI not implemented on H723` |
| 其它 src_type | `bad src_type` |
| `dst_type != DST_WIRE` | `bad dst_type` |
| op 不在 19 原语白名单 | `bad op` |
| `op_needs_wire2` 但 `!wire2_valid` | `this op needs wire2 source ...` |

★ **`SRC_HMI` 是"显式拒绝"而非"给恒 0 假值"** —— 未实现的能力必须在**下载期**失败，
不能在运行时静默给一个恒 0 的合法信号。

## 4.4 预算模型（`engine.c:772-787`）

```
per_tick = Σ_ACTIVE ceil( (op_cost(op) + src_cost(src_type)) / div_mult )
   div_mult: div0=1, div1=10, div2=64      ← 注意 div2 是 64 不是 100
```
- `k_op_cost_itcm[0x13]` 是本平台**实测**值（不是照抄 S3）
- `OP_COST_MAX_MEASURED = 145`（PID）参与一条**绊线断言**:
  `MAX_ROUTES(128) × 145 = 18560 ≤ EXEC_DEPLOY_BUDGET(26000)`
  ⇒ 断言"**预算门当前不具约束力**"；一旦它失败，说明门变成真门，
  **必须回去做超载实验**验证它能拦住，而不能相信一个从未触发过的判据

---

# §5 通信面

## 5.1 帧格式与解析

```
[SYNC:1][CMD:1][LEN:2 LE][PAYLOAD:LEN][CRC16:2 LE]
SYNC: 请求 0xC0 / 响应 0xC1
CRC16-CCITT: poly 0x1021, init 0xFFFF, MSB-first
覆盖范围: 除 SYNC 外的全部（请求 [CMD][LEN][PAYLOAD]；响应 [sts][len][payload] = 3+n 字节）
上限: FRAME_PAYLOAD_MAX = 6150, FRAME_TOTAL_MAX = 6156
状态码: ACK = 0x00, NAK = 0xFF
```
解析状态机 7 态: `0 WAIT_SYNC / 1 CMD / 2 LEN_LO / 3 LEN_HI / 4 PAYLOAD / 5 CRC_LO / 6 CRC_HI`；
`fp_feed` 返回 `0=等待 / 1=好帧 / -1=坏帧`；**CRC 用分块累加**（不分块会要 6KB 栈缓冲 ⇒ 爆栈）。

## 5.2 21 条命令

| 码 | 名称 | 载荷 → 响应 |
|---|---|---|
| 0x01 | GET_VERSION | 空 → `[fw:u16][cap:u16]` |
| 0x10 | DEPLOY | 见 §4.2 → `[seq:u16][budget:u32]` |
| 0x11 | START | 空 → 空 |
| 0x12 | STOP | 空 → 空 |
| 0x13 | RESET | 空 → 空 |
| 0x20 | READ | `[addr:u32]` → `[val:u32]` |
| 0x21 | WRITE | `[addr:u32][val:u32]` → `[addr:u32]` |
| 0x22 | READ_BURST | `[addr:u32][count:u16]` → `count×u32`（count 1..256） |
| 0x23 | WRITE_BURST | `[addr:u32][count:u16][count×u32]` → `[addr:u32]` |
| 0x24 | FORCE | `[idx:u16][mode:u8][val:f32]` → 空 |
| 0x36 | PIN_SELFTEST | `[method:u8?]` → AI 3ch×2 u16 + DI 4ch×2 u8（20B） |
| 0x37 | ADC_SCAN | `[ch_start:u8][count:u8]` → `count×u16`（ch 0..19） |
| 0x38 | ENGINE_STATUS | 空 → **39B**，见 §5.3 |
| 0x40 | MACRO | 字节码 → 栈内容（每值 4B） |
| 0x41 | MACRO_UPLOAD | `[loop_ms:u16][code...]` → `[code_len:u16][loop_ms:u16]` |
| 0x42 | MACRO_CTRL | `[action:u8]`（0=stop 1=start）→ 空 |
| 0x43 | PERSIST | 空=查询 / `[mode=1]`=落盘 → 24B（见 §9） |
| 0x44 | SEQ_DEPLOY | 见 §7 → 空 |
| 0x60 | MB_INJECT | 原始 RTU 帧 → 空（忙碌 NAK `mb: busy`） |
| 0x61 | MB_RESP | 空 → `[state][tx_len][tx…][rx u32][tx u32][crc u32][exc u32]` |
| 0x62 | MB_CFG | `[src][tx_uart?][budget?]` → `[src][tx_uart][budget]` |

未实现命令 → `default: nak("bad cmd")`（**显式 NAK 而非超时**）。
`0x51/0x52/0x53`（display）标 `DCL_RESERVED`，本平台不实现。

## 5.3 `0x38` 逐偏移（总长 39B）

**前 31 字节与老范本 S3 同布局且同语义**：

| 偏移 | 字段 |
|---|---|
| 0-3 | `samples`（仅 RUN 拍） |
| 4-7 | `period_min`（哨兵 → 0） |
| 8-11 | `period_max` |
| 12-15 | `exec_min`（哨兵 → 0） |
| 16-19 | `exec_max` |
| 20-21 | `n_routes`（u16） |
| 22 | `run`（**S3 语义** = `ENGINE_RUN`） |
| 23-26 | `shm_addr` |
| 27-30 | `overrun`（超预算次数，真计数） |

**H723 尾部扩展（byte 31 起）**：

| 偏移 | 字段 |
|---|---|
| 31-32 | `deploy_seq` |
| 33-34 | `applied_seq` |
| 35-36 | `reload_lat` |
| 37 | `engine_gate`（H723 专有扫描门） |
| 38 | `out_surfaces`（物理输出面登记数） |

## 5.4 能力位

`fw_ver` 实报 **`DCL_FW_VERSION_H723 = 0x0200`**（与 S3 的 `0x0107` 分谱系）。

| 位 | 宏 | 状态 |
|---|---|---|
| 0 | `MULTICYCLE` | IMPL |
| 1 | `HOTRELOAD` | IMPL |
| 2 | `PERSISTENT` | IMPL |
| 3 | `STATE_COLD` | **NOTYET** |
| 4 | `WIRE2_FLAG` | IMPL |
| 5 | `VERINFO` | IMPL |
| 6 | `SEQ` | IMPL |
| 7 | `FORCE` | IMPL |
| 8 | `COMM` | IMPL |
| 9 | `HMI` | **NOTYET** |
| 10 | `AI` | IMPL |
| 11 | `MACRO` | IMPL（H723 扩展位） |

`DCL_CAP_H723_IMPL = 0x0DF7`；两条清单**不能同时含某一位**，由
`_Static_assert((IMPL & NOTYET) == 0u, ...)` 兜底（`transport.h:198-199`）。

## 5.5 UART 层

- `uart1_init`（`uart.c:52-98`）: GPIOA + USART1 时钟；PA9/PA10 配 AF7；均上拉；PA9 高速档；
  `PRESC=0`，`BRR = (pclk2 + baud/2)/baud`（**OVER8=0，BRR 就是分频值本身，不 ×16**）；
  先清 ORE/TC 标志、读一次 RDR 清 RXNE；最后 `CR1 = UE|TE|RE` 再 `|= RXNEIE`；
  **`NVIC_IPB(IRQ_USART1) = 0x80`（低于 TIM2 的 0）**，用 `nvic_enable_irq()` 而**禁止裸移位**
  （IRQ37 ≥ 32，裸移位是 UB —— 这是 A1 事故的教训）
- 环形缓冲 `RX_RING_SZ=512`；**满则丢弃并计数，不覆盖**
- `USART1_IRQHandler`（ITCM）: 逐标志处理 PE/FE/NE/**ORE 先于 RXNE**（清 ORE 后要**重读 ISR**
  才拿到新数据），只塞缓冲，**不解析、不回调**
- 发送 `uart1_write`: 每字节等 **TXE**；**最后一字节后等 TC**（保证完整出线）
- 主循环 `proto_poll()` 排空缓冲 → `fp_feed` → `proto_dispatch`

---

# §6 通信域：Modbus RTU 从站

- 状态机 5 态: `IDLE(0) / RX(1) / EXEC(2) / TX(3) / BUILD(4)`
- 跳转: IDLE/RX 收字节（受 `rx_len < MB_MAX_FRAME(255)`）→ `silent=0`；静默累计 ≥
  `MB_SILENT_TICKS(4)` 且 `rx_len≥4` → EXEC；EXEC 轻量解析 → BUILD（或静默丢弃）；
  BUILD 逐字节组装 + **增量 CRC**；`b_pos >= MB_TX_SIZE(256)` 则丢弃 + `err_exc++` + 回 IDLE
- 寄存器映射（`idx = start - 40001`）:
  - `40001-40064` **读区**（`MB_HOLD`）= `wire[0..63]` 工程量 ×100 取整，**只读**
    （写它返回异常 02）；每 100 拍（10ms）由主循环 `mb_refresh_hold` 刷新
  - `40065-40128` **写区**（`MB_SET`）= 上位机设定值，可读写
- qty 边界: `0x03` → `qty==0 || qty>125` 异常 03；`0x10` → `qty==0||qty>123||bc!=qty*2`
  异常 03；`idx+qty > 128` 异常 02
- **两个常量必须分开**: 请求上限 `MB_MAX_FRAME=255` / 响应组装上限 `MB_TX_SIZE=256`
  （曾经一个常量两用 ⇒ 一条合法 qty≥62 读请求让通信域**永久 busy**）
- CRC: poly `0xA001`，init `0xFFFF`，LSB-first，低字节在前
- **`mb_tick` 在 run 门之外** —— 否则 STOP 态注入一帧会把状态机钉死在 RX，通信域变砖

---

# §7 顺序域：Sequencer

- SHM: `0x4000 SEQ_TABLE`（步条目 64×16B）/ `0x4400 SEQ_CTRL`（实例 8×16B）；
  上限 `MAX_SEQ_INST=8` / `MAX_SEQ_STEPS=64`；实例数在 `0x38 N_SEQ`
- `SeqCtrl_t`（16B）: `+0 step_base` `+2 n_steps` `+4 step_cur`（0-based；对外镜像 = cur+1）
  `+6 out_wire` `+8 period`（div+phase）`+9 run`(bit0) `+10 reserved` `+12 step_tick`(u32，×dt=秒)
- `SeqStepEntry_t`（16B）: `+0 cond_type`(0=SENSOR/1=WIRE/2=仅超时) `+1 cond_idx` `+2 flags`
  (bit0=末步回卷 / bit1=使能超时) `+3 reserved` `+4 param_idx`(value_a=转移阈值, value_b=超时秒)
  `+6 state_offset`(v0 保留 0) `+8 jump_idx`(0=线性下移) `+10 reserved2`
- **`0x44` 载荷**: `[n_seq:u8][n_steps_total:u16][目录 n_seq×6B][步表 n_steps×16B]`，
  长度必须**精确等于** `3 + n_seq*6 + n_steps*16`
- 部署约束: **必须 STOP 态**；不热重载；只标 dirty；`step_off` 必须连续等于累计和；
  `out_wire=0` 是保留哨兵；`out_wire` 与路由 dst 冲突即拒（两表都查）；`cond_type==2` 必须使能超时
- **ISR 推进**（`engine_seq_tick`）: 每实例按 (div, phase) 门控（div0 每拍 / div1 `tick%10` /
  div2 `tick%100`）；越界逐项 continue；**条件 `v > value_a` 才推进**；未推进且使能超时则
  `step_tick++`，`step_tick*dt >= value_b` 强推；**一次至多推进 1 步**（WCET 上界 = 实例数）；
  输出镜像 `wire[out_wire] = step_cur+1`，**但被 force 的 wire 不写**
- **START 时 arm**（`h_start_w1`，在置 `ENGINE_RUN=1` **之前**）: `step_cur=0` `step_tick=0` `run=1`
  + 镜像初值 1.0；STOP 只清 `ENGINE_RUN`，**步号冻结保持**（现场信息）

---

# §8 外设域

## 8.1 macro VM（`macro.c`）

- 指令集: `0x00 nop` / `0x01-0x03` 配 输出·输入·开漏 / `0x04` 写电平 / `0x05` 读电平→push /
  `0x06` 忙等 N 周期 / `0x07` 忙等 N ms / `0x08` push u32 / `0x09` drop /
  `0x10` load(addr) / `0x11` store(addr,val) / `0x30-0x33` 读·写 SENSOR·ACTUATOR·WIRE /
  `0xFF` 结束 / `0x20-0x23` SPI **未迁移 → 返回 -4**
- 栈深 `MACRO_STACK_DEPTH=16`；上溢返回 -1；空栈 POP 返回 0（不报错），但需要 ≥2 个操作数的 op 显式返回 -1
- **pc 越界**每个带立即数 op 前置检查
- **裸地址写守卫**: 必须 4B 对齐且落在 `[shm, shm+SHM_SIZE-4]`，否则 -2
- **非有限值守卫**: 写 float 区/SENSOR/ACTUATOR/WIRE 时拒 NaN/Inf → -3
- 返回码: `-1` 截断/栈错 · `-2` 裸地址越界 · `-3` 非有限 · `-4` 未迁移 · `-5` 未知
- `0x40` 一次性（返回栈内容）；`0x41` 上传（`[loop_ms u16][code]`）；`0x42` 控制启停；
  循环由**主循环** `macro_tick` 驱动，`interval = max(loop_ms,10)*10` 拍；出错**自停**

## 8.2 ADC / AI

- `AI_NCH=3`，槽基 `AI_SENSOR_BASE=8` ⇒ `PA0(INP16)→SENSOR[8]` `PA1(INP17)→SENSOR[9]`
  `PA4(INP18)→SENSOR[10]`；换算 `raw*3.3/65535`
- `adc_read`（`adc.c:82-96`）: 先写 1 清 EOC/OVR → `ADC_SQR1` 设 SQ1=ch → `ADSTART` →
  轮询 EOC（上限 5e6）→ **超时返回 -1（用状态码，不用哨兵值）** → 读 `DR & 0xFFFF`
- **★ `ADC_PCSEL = 0x000FFFFF`（全开 20 通道）** —— 漏了它 **ADC 看不到任何引脚**，
  而 `CR/CFGR/CCR/MODER` 会全部看起来正确（这是"该写而没写"的经典标本）
- `ai_tick` 每 100 拍（10ms）采一轮；超时按 0V
- `0x36` 自检: 逐通道上/下拉各读一次；`0x37` 扫描: `[ch0][cnt]`，ch 0..19

## 8.3 DI

- 引脚 `PC0/PC1/PC2/PC3`；**内部上拉、悬空=1、接 GND=0**
- 槽 `DI_SENSOR_BASE=3` ⇒ `SENSOR[3..6]`
- 去抖: `DI_DEBOUNCE=3`（连续 3 次一致才提交；不同则重新计数）
- 每 10ms **无条件回填** `SENSOR[3..6]`（自愈 `cold_start_reset` 的清零）

## 8.4 HIL

- PWM: `PA6 = TIM3_CH1`（AF2）；`PSC` → 1MHz，`ARR` → 1kHz；分辨率 1024（10bit）
- 输出臂取 `WIRE_MAP[20]`；**受 `ENGINE_RUN` 门控**（未运行 ⇒ u=0）；
  回写镜像 `OFF_HIL_DUTY`
- 反馈: `PA5 = ADC1_INP19`，16 次平均 → `SENSOR[2]`；原始码镜像 `OFF_HIL_FB_RAW`；
  **反馈不受门控**（停机也要能读现场）
- `hil_outputs_safe()`: **幂等**；`TIM_CCR1=0` **并把镜像也写 0**（否则"停机已进安全态"读不出来）

## 8.5 周期与节流总表

| 任务 | 节流 | 驱动 |
|---|---|---|
| `engine` 扫描 + `engine_seq_tick` + `engine_force_apply` | 每拍（内部按 div/phase 门） | **ISR**（gate && RUN 门内） |
| `mb_tick` | **每拍无条件** | **ISR**（**门之外**） |
| `HEARTBEAT++` | **每拍无条件** | **ISR**（门之外） |
| `proto_poll` / `macro_tick` / `ai_tick` / `di_tick` / `hil_tick` | 内部自节流（10ms 级） | **主循环**（每轮无条件调用，未到间隔即早退） |
| `mb_refresh_hold` | `g_tick_count % 100 == 0` | 主循环 |
| PER-段计时镜像 / 自动落盘裁决 / `g_reinit` | 每轮 | 主循环 |

---

# §9 持久化

- **扇区**: 副本 A = sector 6 @`0x080C0000`，副本 B = sector 7 @`0x080E0000`（各 128KB）
- `PersistHdr_t`（32B）: `magic 'PLCP'` / `version 0x0200` / **`seq`（单调）** / `crc32`（覆盖 payload）/
  `n_routes/n_params/n_states` / `prog_magic`
- **单调 seq 的作用**: 读两份取 **seq 大者**恢复；写**seq 小的那份**，新 seq = max+1
  ⇒ **任何时刻至少一份完整**（擦到一半掉电也能恢复 —— 结构性保证，不是 CRC 运气）
- **落盘成功的唯一判据 = 回读 `memcmp` 比对**；`flash_lock()` 由 `out:` 无条件执行
  （"无论成败都落锁"是结构事实）
- **运行期 0 flash 操作**: `persist_save` 先查 `ENGINE_RUN`，RUN 时只置 dirty 返回；
  deploy 只登记 dirty、**绝不落盘**（擦 128KB 要 1~4 秒）
- **自动落盘（T26）**: 0x43 **纯查询**时"**登记**"请求（登记处刻意不排除 RUN 态，
  否则计数恒 0 ⇒ 判据变空）；**主循环"裁决"**（RUN → 放弃并计数 `g_persist_auto_gate++`）；
  且必须**等 ACK 完整发完（TC）**才动手，否则 ACK 被卡在 1.5s 擦除里

---

# §10 观测与判据体系

## 10.1 验收套件（12 套 195 PASS / 0 FAIL）

`h723_audit_m234`(12) · `h723_proto`(12) · `h723_w1`(28) · `h723_w2_probe`(14) ·
`h723_seq`(27) · `h723_persist`(26) · `h723_modbus`(15) · `h723_macro`(18) ·
`h723_w5`(18) · `h723_t26`(11) · `h723_r1_actuator`(5) · `h723_jitter`(9)

★ **跑串口套件一律显式 `--port COM14`**（`find_port()` 只按 VID `1A86` 匹配，
而 CH343(COM7，ESP32) 也是 1A86 ⇒ 会认错口）

## 10.2 LA 外部复核（需 Saleae + CH4←PA8，单通道，≤24 MS/s）

```
python tools/h723_tick_la.py --mode measure --setup mixed --port COM14
python tools/h723_tick_la.py --mode idle      # 悬空对照: 核心 held ⇒ 应 ~0 跳变
```

## 10.3 判据纪律（微调时最该看的一段）

**0. ★★ 铁律 0 —— 非侵入式交互：观测不得改变被测对象**（完整版见 `README.md` 的"铁律 0"）
   - 优先走**协议内通道**（`0x38`/`0x22`）；会改变目标状态的手段（HALT/复位/关时钟/改 DWT/
     留运行期配置）只在协议表达不了时才用，用完**显式恢复 + 核对已恢复**
   - **pyocd 工具收尾必须 `-c go`**
   - **任何 DWT 计时结论先断言时基活性**（`g_per_glitch_n == 0`），否则**拒答**
     ★ 待办：`0x38` 尚未暴露该量 ⇒ 应加进尾部
   - 血证：pyocd 会话可静默停掉 `DWT_CYCCNT`，让所有计时量读 0 而其余一切正常（§0.2）

1. **宣称必须等于实现** —— 宣称 > 实现的东西要么做真要么删
2. **判据必须能失败** —— 每加一条判据都要问"什么条件下它会红？"；
   配一条**对照构建/对照配置**去证明它真的会红（本项目的 A/B 惯例：
   `-DDCL_VTOR_ITCM=0`、`-DDCL_HIL_SAFE=0`、`-DDCL_BOOT_SEL=0 -DDCL_BOOT_PROFILE=2`）
3. **不接受"一个量两种语义"** —— 已踩过四次: `MB_MAX_FRAME`(请求/响应) ·
   `NVIC_ISER` 位移 · `USART1 BRR` · `BUCKET_DIV2_PHASES`(周期/相位模)
4. **"配置全对"≠"功能可用"** —— 凡"写一次就算"的状态，都要补一个**能被外部读走**的量
5. **对端视角才是判据** —— 跨边界（协议/持久化/帧）必须用**对端的实现**去校验
6. **陈旧文档就是会骗人的宣称** —— 过时数字要**显式作废**，不是默默留着
7. **工具坏了会伪装成被测对象故障** —— 判据报红时，先用另一条已知良好的工具在同端口跑一次
8. **从超时/失败测量里算出的量（均值/斜率/方差）都是噪声**

---

# §11 已知缺陷与未决项

## 11.1 迁移保真度审查（对照老范本 S3）的状态

| 级别 | 项 | 状态 |
|---|---|---|
| 一级 | **#1 停机安全态**（含子项④ actuator 校验） | ✅ 闭环 `eebeba7` + `9588557` |
| 一级 | **#2 `0x08`/`0x18` 同址反义** | ✅ 闭环 `a2140cd` |
| 一级 | #3 FORCE 迁址（"零改动"承诺已破） | ⬜ **未决** |
| 一级 | #4 div2 相位 / 两条建桶路径矛盾 | ⏸ **用户搁置**（留给时间槽深度优化） |
| 二级 | **#5 `ov` 恒 0（T9 半条是空判据）** | ✅ 闭环 `ba759e7`（真计数，对照 18343 vs 0） |
| 二级 | #6 TICK_STATS 语义漂移 · #7 确定性可被 `g_engine_sel` 关 · #8 桶扫丢 `nr` 上界 · #9 `OP_COST_DIV2` 注释 | ⬜ 4 项未决 |
| 三级 | #10 macro 持久性口径 · #11 SRC_HMI · #12 `flash.h` 宣称 · #13 TC-vs-TXE · #14 `MB_MAX_FRAME` 注释 | ⬜ 5 项未决 |
| **新增** | **A: DWT 时基可被静默停掉** / **B: 平台对体积变化敏感** | ⬜ 见 §0.2 / §0.3 |

## 11.2 已实测的新发现（顺手记档）

- `engine_bucket_dead_slots()` 把 `off2[p]`（**前缀和/"桶起始偏移"**，只要有路由就非零）
  也当死槽数 ⇒ **任何非空程序恒报 36**（实测板上 `g_bucket_zero_slots = 0x24`）
  ⇒ 这条"死槽不变量"判据**恒红、零判别力**
  ★ 修这条**不依赖 A/B 选择**（只数 `cnt2` 即可），但"哪些槽算死"取决于相位方案的取舍
- `0x41/0x42`（macro 循环）的注释写着"**同 S3**"，但**范本没有这两个命令号**（范本只有 `0x40`）
  ⇒ 注释把自己的"扩展"说成了"对齐"，口径待核
- `0x38` 的 `pmin/pmax` 实际测的是 **ISR 入口间隔**（含两次中断入口延迟之差），
  **不是拍长**；拍长抖摆要 LA 测引脚边沿才有（σ ≲ 1.6 cyc）

## 11.3 老范本自身的债（不影响 H723）

- 范本 `MB_MAX_FRAME=128` **一常量两用**（请求上限 + 响应组装界限）⇒ 合法 `qty≥63` 读请求
  让其通信域**永久 busy**（H723 已按"两个语义不同的量必须两个常量"拆开，**范本未回填**）
- 范本 `apply_gpio` 只覆盖 GPIO0-31（u32 位图）而 `MAX_ACTUATORS=64` ⇒ 32..63 静默无效
- 范本 `period` 的 phase 只有 6 位而 div2 桶有 100 槽（空表无害，口径不一致）

---

# §12 微调时的"改动检查清单"

**改任何东西之前，先看这张表：改这类东西必须同步改什么。**

| 你要动的东西 | 必须同步检查 |
|---|---|
| **SHM 新增/移动字段** | ① 偏移宏 ② 相邻区的"精确相接" `_Static_assert` ③ `cold_start_reset()` 登记 ④ 若套件按绝对偏移读它 → **必须与 S3 同址** |
| **新增原语** | ① op 码（`engine.h` 白名单 + `primitives.h` 的 `prim_exec` 分派 + `op_is_stateful_h` / `op_needs_wire2`）② `k_op_cost_itcm` 实测成本 ③ `OP_COST_MAX_MEASURED` 与那条机械断言 ④ `verify_offline.py`/DSL 编译器的映射表 ⑤ 是否需要有状态 |
| **新增输出面（引擎能驱动的物理输出）** | 在 init 后 `eng_register_output_surface(自己的安全态)` —— **不登记 ⇒ 停机不进它的安全态**；并让安全态**幂等** + **回写可读镜像** |
| **新增域/新内存区** | ① `_Static_assert` 布局断言 ② `cold_start_reset()` 单一入口 ③ 若跨复位不丢（DTCM）⇒ 统计量必须显式清零 |
| **改协议命令/能力位** | ① `transport.h` 命令表 + 能力位 ② `IMPL ∩ NOTYET == 0` 断言 ③ 0x38 尾部若有扩展 → 长度 ④ 老范本平移套件是否按绝对偏移/长度读它 |
| **改 `0x38` 布局** | 前 31 字节**必须与 S3 同布局同语义**；新量一律往**尾部**追加（客户端用 `len >=` 适配） |
| **改预算常量** | `EXEC_DEPLOY_BUDGET`（静态门）与 `EXEC_BUDGET_CYCLES`（动态判据）**是两个量**；改成本表必须重测（成本表是代码的函数） |
| **往 ITCM 加代码** | ★ ITCM 余量只有 **0x68 字节**（`_eitcm=0x1818` vs `_vtor_itcm=0x1880`）；且 §0.3 说明本平台对体积变化敏感 —— **改完必须实测上电 + 跑套件** |
| **改热路径（ISR）成本** | ① 用 `0x38` 的 `emax` / `ov` 看有没有破预算 ② 能用主循环做的别放 ISR（`mb_refresh_hold`/计时镜像/自愈类动作都是这个原则） |
| **改时序相关判据** | 先断言**时基干净**（`g_per_glitch_n == 0`），否则**拒答**而不是报"抖动很小" |
| **改完** | `bash build.sh`（零警告）→ 刷录 → **12 套全跑** → 再谈"完成" |
