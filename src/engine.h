/**
 * engine.h — DCL 引擎表结构与 SHM 布局 (H723 阶段 2)
 *
 * ★ 本文件是 esp32-core0 `components/core0/shared_mem.h` 的**同构子集**:
 *   容量宏、SHM 偏移、表条目结构、OP/SRC 码, 全部逐字节对齐。
 *   为什么: 迁移不变量 (MIGRATE-H723.md §1) —— 表布局与 SHM 偏移一改,
 *   S3 的 20 套回归、4 份审计记录、PC 侧按偏移读的脚本全部失效。
 *
 * 与 S3 的差异 (有意为之, 逐条说明):
 *   ① SHM 基址从"运行时 heap_caps 分配"变成**链接期 DTCM 静态区** (零等待无 cache)
 *   ② ISR 代码从 IRAM_ATTR 变成 .itcm_text 段
 *   ③ HMI 源 (SRC_HMI, 通信域写区) 本阶段未落地 — 通信域属阶段 4, 这里留位不实现
 */
#ifndef DCL_ENGINE_H
#define DCL_ENGINE_H

#include <stdint.h>
#include <stdbool.h>

/* ══════════ 容量 (与 esp32-core0 一致) ══════════ */
#define MAX_ROUTES    128
#define MAX_PARAMS    128
#define MAX_STATES    128
#define MAX_SENSORS   64
#define MAX_ACTUATORS 64
#define MAX_WIRES     128
#define MAX_LUT       256
#define MB_NREG       64    /* 通信域每区寄存器数 (阶段 2 只用于 SRC_HMI 的留位) */

/* ══════════ SHM 偏移 (逐字节对齐 esp32-core0 shared_mem.h) ══════════
 *
 * ★ 审计修正 (对照 esp32-core0 第二十七轮 OA20 同族):
 *   第一版这里**只**从 OFF_SENSOR_MAP(0x40) 开始定义, 0x00-0x3F 是一段
 *   "无名字、无断言"的 64 字节空洞。S3 的 OA20 正是这么来的 —— MB 区
 *   当时**一条断言都没有**, 于是控制块从 32B 扩到 40B 踩进 RX 缓冲无人发觉。
 *   现在把控制块区/计时区按 S3 的原偏移**显式命名并断言**, 空洞消失。
 *
 *   控制块区 (0x00-0x3F) 本阶段**只占位不实现** (deploy/热重载属阶段 3),
 *   但偏移先按不变量钉死 —— 偏移一改, S3 的 20 套回归与 14 章审计记录全失效。 */
/* ---- SHM 定址助手 (与 S3 同名同语义, 迁移期少一次心智换算) ----
 * ★ 一律带 base 参数: 本项目所有引擎函数都以 base 为唯一入口 (不用隐藏的 g_shm),
 *   这样"表在哪"永远由调用方显式给出, 便于将来第二块表区/自检。 */
#define SHM_U8(b, off)   (*(volatile uint8_t  *)((b) + (off)))
#define SHM_U16(b, off)  (*(volatile uint16_t *)((b) + (off)))
#define SHM_U32(b, off)  (*(volatile uint32_t *)((b) + (off)))
#define SHM_PTR(b, off)  (void *)((b) + (off))

/* ══════════ SHM 控制块 (0x00..0x3F) —— 每字段必须写清"谁写、什么语义" ══════════
 * ★★ 审计发现 C 的系统化升级 (2026-09-11):
 *   审计只报了 `OFF_CTRL_MAGIC` 一个"定义了却从不设置"。按同一手法对整个控制块
 *   做了一遍自查, 发现**共 11 个字段只定义、从不写入**:
 *     MAGIC / VERSION / HEARTBEAT / TIMING×(6) / GPIO_MASK
 *   这违反了本项目的基本纪律 —— **字段存在就意味着它被宣称有语义**。
 *   一个"恒 0 的 MAGIC"会让 PC 侧"SHM 是否就绪"的判据失去依据, 而错误方向
 *   指向"固件坏了"而不是"这个字段从来没人写"。
 *   ⇒ 处置分三类 (逐条在下面标注):
 *      [写] 有明确语义 → 补写入点 (MAGIC / VERSION / HEARTBEAT / TIMING×6)
 *      [留] 语义未定     → 显式标注"本平台未定义 + 不可达", 不假装有值 (GPIO_MASK)
 *      [废] 平台不适用   → 显式标注废弃原因 (本块暂无; S3 的 display/fs 域在别处)
 *   ★ 配套自查脚本已在审计回执里给出 (正则扫"宏被用于写"的位置), 可复跑防复发。 */

/* ---- [写] 就绪标志与布局版本 ----
 * ★ 为什么必须有 MAGIC: PC 侧需要一个**不断言任何内容、只回答"SHM 已初始化"**
 *   的判据。没有它, 上电初期的读数与"固件跑起来了但表是空的"无法区分 ——
 *   两者都表现为"读到 0"。S3 用 `SHM_U32(OFF_CTRL_MAGIC) = CTRL_MAGIC`。
 * ★ 为什么还要 VERSION: MAGIC 只答"是/否就绪", 不答"**哪一版**布局"。
 *   本项目承诺"SHM 与 S3 逐字节同偏移", 但偏移不变 ≠ 字段语义不变
 *   (W3 就往 0x38 加了 N_SEQ)。PC 脚本据此可显式判断"我认不认得这块 SHM",
 *   而不是靠字段内容反推。版本号只在**语义**变化时递增。 */
#define CTRL_MAGIC           0x44434C31u   /* 'DCL1' — 与 PROG_MAGIC 同族 */
#define SHM_LAYOUT_VERSION   0x00010000u   /* 1.0 — W3 加入 N_SEQ 后的语义版本 */

#define OFF_CTRL_MAGIC       0x00   /* u32 [写] CTRL_MAGIC: SHM 已初始化 (唯一就绪判据) */
#define OFF_CTRL_VERSION     0x04   /* u32 [写] SHM_LAYOUT_VERSION: 字段语义版本 (非偏移版本) */
#define OFF_CTRL_HEARTBEAT   0x08   /* u32 [写] **每拍无条件递增** = CPU + 定时器存活。
                                     *   ★★ #2 修复 (2026-09-11 迁移保真度审查): 这里原本
                                     *     写的是"引擎拍计数 — 仅在 gate && RUN 时递增",
                                     *     与范本**正好反义**。范本 `core0_isr.c:355` 把
                                     *     `HEARTBEAT += 1` 放在 `if (!run) return` **之前**,
                                     *     注释原文: "OA14 (P2, 审计): 心跳必须在 run 门外
                                     *     无条件翻 — 停机也翻 (外部可观测 CPU+定时器存活)"。
                                     *   ★ 同址反义是最危险的一类静默读错: 按"心跳看存活"
                                     *     写的上位机会把"引擎已停机"读成"CPU 死了"。
                                     *   ⇒ 现在两个量的契约与范本**完全一致**:
                                     *       0x08 HEARTBEAT = 每拍无条件 (存活)
                                     *       0x18 SAMPLES   = **仅 RUN 拍** (本次运行段)
                                     *     成对读可区分三态 (注意与修复前是**对调**的):
                                     *       0x08↑ 0x18↑   → 引擎在跑
                                     *       0x08↑ 0x18=停 → ISR 在跑但引擎已 STOP (正常停机态)
                                     *       0x08=停       → ISR 都没了 (固件死了/未启动)
                                     *   ★ 成本: 每拍一次 volatile 读改写 (DTCM) —— 与修复前
                                     *     同级 (原来也每拍写一次, 只是写在门内)。 */
#define OFF_CTRL_RELOAD      0x0C   /* u8  */
#define OFF_CTRL_ENGINE_RUN  0x0D   /* u8  */
#define OFF_CTRL_N_ROUTES    0x0E   /* u16 ★ 条数的**唯一权威来源** (见 g_active_routes 说明) */
#define OFF_CTRL_N_PARAMS    0x10   /* u16 */
#define OFF_CTRL_N_STATES    0x12   /* u16 */
#define OFF_CTRL_PROG_MAGIC  0x14   /* u32 */

/* ---- [写] 计时统计镜像 (0x18..0x33) ----
 * ★ H723 的**权威**计时数据住在 DTCM 的 C 全局 (g_isr_cyc_* / g_per_cyc_*),
 *   因为 ISR 每拍直接更新它们是零代价 (寄存器相对寻址), 而 SHM 要走基址+偏移。
 *   这一区是**镜像**, 由主循环周期性从 C 全局同步过来 —— 目的是让 PC 用一条
 *   0x22 READ_BURST 就能一次取走全部计时视图 (无需多次 0x38 往返)。
 *   ⇒ 两者的关系写死: C 全局 = 权威(每拍更新, 精度最高); SHM 区 = 采样镜像。
 *     审计若发现两者不一致, **以 C 全局为准**, 因为 SHM 是滞后的拷贝。 */
#define OFF_TIMING_SAMPLES      0x18   /* u32 [写] = g_isr_n = **仅 RUN 拍**的拍数。
                                        *   ★ #2 修复: 契约与范本 OFF_TIMING_SAMPLES 一致
                                        *     (范本在 `if (!run) return` **之后**递增, 且范本
                                        *      0x38 的 samples 就取自它: `main.c:242`)。
                                        *     STOP 后**冻结** —— 与 0x08 恰好互补, 见上。 */
#define OFF_TIMING_PERIOD_MIN   0x1C   /* u32 [写] = g_per_cyc_min    */
#define OFF_TIMING_PERIOD_MAX   0x20   /* u32 [写] = g_per_cyc_max    */
#define OFF_TIMING_EXEC_MIN     0x24   /* u32 [写] = g_isr_cyc_min    */
#define OFF_TIMING_EXEC_MAX     0x28   /* u32 [写] = g_isr_cyc_max    */
#define OFF_TIMING_LAST_PERIOD  0x2C   /* u32 [写] = g_per_cyc_last   */
#define OFF_TIMING_LAST_EXEC    0x30   /* u32 [写] = g_isr_cyc_last   */

/* ---- [留] 输出掩码 —— **本平台语义未定义, 当前不可达** (审计发现 H) ----
 * ★★ 现状必须说清 (否则它就是下一个"定义了却没人写"的坑):
 *   `eng_outputs_safe()` 里的位映射是 `mask >> (p*2) & 0xFFFF`, 每 port 只取 **2 位** ——
 *   而每个 GPIO port 有 **16 个引脚**, 语义对不上。
 * 根因: `u32 mask` 装不下 H723 的输出空间。S3 是**单端口** u32 (位 = 引脚),
 *   而 H723 有 GPIOA..GPIOK 共 11 个端口 × 16 引脚 = **176 位** ⇒ 需要 u32[6]。
 *   ★ 当前**不可达**: 没有任何代码写这个字段 (恒 0), `if (mask)` 直接短路, 所以
 *     "停机不清输出"这个隐患尚未成为事实。但一旦接真实 GPIO 执行器就会复发
 *     (P1-2 同族: 停机 ≠ 保持输出)。
 * ⇒ 定案前**不许**让任何路径写它 (写了就等于宣称一个错的语义)。
 *   接 GPIO 执行器时的两条路: ① 扩成 u32[6] (语义干净, 改 SHM 偏移) 或
 *   ② 明确"只用 GPIOA"+ 断言 `mask < (1<<16)` (零偏移代价)。
 *   下面的 _Static_assert 把"当前无人写"这件事钉住, 防止悄悄出现半吊子写入。 */
#define OFF_CTRL_GPIO_MASK      0x34   /* u32 [留] 语义未定, 当前恒 0 且不可达 */
#define OFF_CTRL_N_SEQ          0x38   /* u8  Sequencer 实例数 (阶段 5) */
/* 0x39 空闲 (1 字节); 0x3A-0x3F 已被下面的 deploy 生效确认字段占满 ——
 * ★ A8 修正: 旧注释写"0x38-0x3F 空闲, 阶段 5 Sequencer", 与事实不符
 *   (0x3A/0x3C/0x3E 已是 DEPLOY_SEQ/APPLIED_SEQ/APPLIED_LAT)。
 *   阶段 5 落地 Sequencer 时应使用 0x39 与下面的保留区, 不要假设 0x3A-0x3F 可用。 */

#define OFF_SENSOR_MAP       0x0040   /* 64 × f32 */
#define OFF_ACTUATOR_STATUS  0x0140   /* 64 × f32 */
#define OFF_WIRE_MAP         0x0240   /* 128 × f32 */
#define OFF_LUT_DATA         0x0440   /* 256 × f32 */
#define OFF_ROUTE_TABLE      0x0840   /* 128 × 16B (ISR 只读) */
#define OFF_ROUTE_STAGING    0x1040   /* 128 × 16B (PC 写, ISR 热重载 memcpy) */
#define OFF_PARAM_TABLE      0x1840   /* 128 × 16B */
#define OFF_PARAM_STAGING    0x2040   /* 128 × 16B */
#define OFF_STATE_TABLE      0x2840   /* 128 × 16B */
#define OFF_STATE_STAGING    0x3040   /* 128 × 16B */
/* ---- 档桶索引区 (阶段 3, S3 OA15 治本; 起址与 S3 一致) ----
 * 路由表按 (div, phase) 归组: div0 全部在前, 其后 div1 (phase 0..9 各成桶),
 * 再其后 div2 (phase 0..63 各成桶)。桶表记录每桶的 [起始, 条数], 使 ISR 每拍
 * 只扫本拍激活的桶而非全表:
 *   [off1[10]][cnt1[10]][off2[100]][cnt2[100]] = 220 × u16 = 440B
 *   div0 段 = [0, off1[0]) —— off1[0] 即 div0 条数 (每拍全跑);
 *   div1 段 = [off1[m10], off1[m10]+cnt1[m10])      (m10  = tick%10)
 *   div2 段 = [off2[m100], off2[m100]+cnt2[m100])   (m100 = tick%64)
 *
 * ★★ 移植期发现并修正了 S3 的一个**宣称≠实现**缺陷 (详见 AUDIT-H723-stage2.md H9):
 *   S3 把 div2 相位声明为 **100 个**(桶表 100 槽, 注释写 "phase 0-99", 归组用
 *   `ph = q2 % 100`), 但 `period` 的 phase 字段只有 **6 位**
 *   (PERIOD_PHASE_MASK = 0x3F)。deploy 侧 `(uint8_t)(dv | (ph << 2))` 会把 ph≥64
 *   的高位直接截掉 → 实际存下的是 `ph & 0x3F`:
 *       · 槽 64..99 **恒空** (ISR 侧 `ph = (period>>2) & 0x3F` 只可能 0..63)
 *       · q2 = 0 与 q2 = 64 落到**同一个相位** → div2 > 64 条时相位重叠,
 *         每拍最坏 div2 条数 ≈ n/64 而非预算模型假设的 n/100 (低估最坏拍成本)
 *   `tools/verify_capacity.py` 的"慢档 PID×128 满表"正好压在这个点上,
 *   但该用例只看 emax 是否超预算(量级差太远), 所以从未暴露。
 *
 *   H723 的修法: **让宣称等于实现** —— div2 相位严格取 64 个
 *   (BUCKET_DIV2_PHASES = 64, 与 6 位字段一一对应)。
 *   桶表仍按 S3 的 220 × u16 保留 100 槽, 以维持 SHM 偏移不变, 但
 *   **槽 64..99 恒为 0 是被断言的** (由 engine_bucket_checksum 与外部独立预测比对证明)。
 *   副产品: 未来 deploy 预算模型对 div2 的摊薄系数必须**除以 64, 而不是 100**
 *   (div2 每条每拍成本从 ceil(cost/100) 升到 ceil(cost/64), +56%)。 */
#define ROUTE_BUCKET_U16       220
#define OFF_ROUTE_BUCKETS      0x4480
#define OFF_ROUTE_BUCKETS_ST   0x4638
#define OFF_ROUTE_BUCKETS_END  0x47F0
#define BUCKET_DIV1_PHASES     10
#define BUCKET_DIV2_PHASES     100  /* ★★ H9 的**第二次修正** (2026-09-11, 由 S3 回归 T17 发现):
                                     *   S3 的 div2 档周期 = **100 拍 = 10ms**。四条独立证据:
                                     *     ① S3 套件 T17 的期望值 (1 快 + 60 慢 ⇒ cnt2/cnt0≈0.6)
                                     *     ② `DT_SLOW = 0.01f` 的注释写着 10ms
                                     *     ③ 能力位宣称 "多周期 div 档 (100μs/1ms/10ms)"
                                     *     ④ 桶表尺寸 —— off2/cnt2 各 100 槽 (0x4638..0x47F0 本就够)
                                     *   ★ 上一次 H9 改动把**周期**改成了 64 拍 (6.4ms) —— 改错了方向:
                                     *     真问题是"路由的 phase 字段只有 6 位 ⇒ 相位 64..99 不可达",
                                     *     不是"周期该是 64"。周期一短, 档位语义就与宣称不符 (宣称≠实现),
                                     *     而 DT_SLOW 还按 10ms 算 ⇒ div2 上跑 TIMER/LPF 的原语 dt 也是错的。
                                     *   ⇒ 现在: 周期 = 100 拍 (10ms) ✓; phase 字段仍 6 位 (0..63),
                                     *     相位 64..99 恒空 —— 这正是 S3 的真实形态。 */
#define BUCKET_DIV2_PHASE_MAX  63   /* 路由 phase 字段上限 (6 位) —— 死槽判据与构造器都用它 */

/* ---- 未落地保留区 (显式命名, 不留"无名字的空洞") ----
 * ★ 审计 H3/A7 指出: 下面两段既没有名字也没有断言, 将来往这里放新域不会报错,
 *   "吃掉"邻区字节也不会有任何提示 (与 S3 的 OA20 同族 —— 那一族的本质就是
 *   "无人设防的区域迟早出事")。现在把空洞显式命名, 并把尺寸**钉成常量断言**:
 *   谁把邻区改大/改小, 这里立刻编译失败。 */
#define OFF_RSVD_DSL_DOMAIN     0x3840   /* 保留洞: 开头给 SEQ 区, 尾部仍未落地 */
#define OFF_RSVD_DSL_DOMAIN_SZ  (OFF_ROUTE_BUCKETS - OFF_RSVD_DSL_DOMAIN)   /* 0xC40 */

/* ---- 档级触发统计 (多周期) —— ★★ 偏移与 S3 **同址** (0x3854) ----
 * ★ 为什么不搬: S3 回归套件 (test_dcl.py 的 T17) **直接按 0x3854 读这三个计数**,
 *   而"脚本零改动"是迁移验收的硬条件 ⇒ 凡套件依赖的偏移一律保持同址。
 *   (0x3854 落在本平台 DSL 保留洞的前段, 与 SEQ 区 0x4000 不重叠, 天然可用。)
 * ★ 语义 (照 S3 core0_isr.c): 每个 u32 = 该档**累计执行的路由条次**(不是拍数)。
 *   为什么是"条次": 1 秒内 "1 条快档 + 60 条慢档" 应得 cnt0≈10000 / cnt2≈6000,
 *   比值 0.6 —— 这正是"分档真的按档跑"的可失败证据 (拍数比会是 0.01, 无区分力)。 */
#define OFF_TICK_STATS       0x3854   /* u32[3]: cnt0(100μs档) cnt1(1ms档) cnt2(10ms档) */
_Static_assert((OFF_TICK_STATS & 3u) == 0u, "SHM: OFF_TICK_STATS 需 4 字节对齐");
/* 用字面量 0x4000 而不是 OFF_SEQ_TABLE —— 后者在本文件里声明得更靠后, 此处还不可见 */
_Static_assert(OFF_TICK_STATS + 12u <= 0x4000u, "SHM: TICK_STATS 不得压到 SEQ 区(0x4000)");

/* ★ OFF_TIMING_OVERRUN —— 与范本**同址** (0x3850)。
 *   ★ 为什么不塞进上面 0x18..0x33 那个 timing 块: 该块在 H723 已排满 (0x34 起是保留的
 *     GPIO_MASK), 而范本把 OVERRUN 单独放在 0x3850。凡"套件或工具按绝对偏移读"的量
 *     一律同址 (与 OFF_TICK_STATS 同理), 否则外部判据会静默读到 0。
 *   语义 (照 S3 `core0_isr.c:365`): ISR 执行时长 > EXEC_BUDGET_CYCLES 的**次数**,
 *   每个 RUN 段开始时清零 (S3 在 core0_engine_start 里清; 本平台在 stats_reset 里清)。
 *   ★★ 它存在的意义 (审查二级 #5): H723 原来在 0x38 里**直接填 0** 冒充"没超预算",
 *     而 S3 套件 T9 的判据含 `ov == 0` ⇒ 那**一半是空判据** (不可能失败, 因为没实现)。
 *     现在它是真计数, 并配了"能失败"的对照构建 (FLASH 取指 + 全表扫 ⇒ 会超)。 */
#define OFF_TIMING_OVERRUN   0x3850   /* u32: 本 RUN 段内 ISR 超预算的次数 */
_Static_assert((OFF_TIMING_OVERRUN & 3u) == 0u, "SHM: OFF_TIMING_OVERRUN 需 4 字节对齐");
_Static_assert(OFF_TIMING_OVERRUN + 4u <= OFF_TICK_STATS, "SHM: OVERRUN 与 TICK_STATS 重叠");

/* ══════════ W3: 顺序域 SEQ 区 (Sequencer v0) ══════════
 * 落点 = 上面那个保留洞里的**尾部** (0x4000..0x4480, 与 S3 逐字节同偏移)。
 *
 * ★ 为什么偏移与 S3 一字不差: 本项目总目标是 "S3 的回归脚本尽量零改动"。
 *   S3 的 shared_mem.h 把 SEQ 区定在 0x4000/0x4400 (在其 F1 扩容之后实测定下的),
 *   而 H723 的洞 [0x3840,0x4480) 恰好能把这一整段装下 ⇒ 直接照抄, 不另起编号。
 *   ★ 洞内 0x3840..0x4000 (0x7C0 = 1984B) 保持未分配, 但**有名字也有尺寸断言**
 *     —— 审计 H3/A7 那一族教训: 无人设防的区域迟早出事。
 *
 * ★ 语义 (逐字照搬 S3, 一条都不能改):
 *   · seq **不热重载**: 0x44 写 ACTIVE, 首次 START 才生效 (step_cur 从 0 起)。
 *     顺序是"程序" —— 改程序 = STOP→下装→RUN, 这是 PLC 标准流程。
 *   · 运行期引擎**只读**本区; BUSY 拍 (擦写中) 整段跳过, 步号冻结保持
 *     (与"引擎 STOP 不清执行器"同哲学: 显式状态, 不留歧义)。
 *   · 步号以 **wire** 形式暴露 → 离散域与连续域通过 wire 握手, 输出译码
 *     下沉给已有路由网 (CMP 等), 不在 seq 内部开第二个写者 (D1/B1 地基不可拆)。 */
#define OFF_SEQ_TABLE        0x4000   /* 步条目表: 64 × 16B = 1KB */
#define OFF_SEQ_CTRL         0x4400   /* 实例控制块: 8 × 16B = 128B */
#define OFF_SEQ_END          0x4480   /* == OFF_ROUTE_BUCKETS (与桶表精确相接) */
#define MAX_SEQ_INST         8
#define MAX_SEQ_STEPS        64

/* ── W2.1: Force 域 (从 EXEC 保留洞里划出来, 落实"无人区迟早出事"的设防) ──
 *
 * ★ 布局依据 S3 语义 (shared_mem.h:84-85) 但**起址不同**: S3 用 0x3A00/0x3A10,
 *   在 H723 上 0x3A00 落在 SHM 的 **BUCKET 区内** (OFF_ROUTE_BUCKETS_ST=0x4638 ≤
 *   0x3A00 < OFF_ROUTE_BUCKETS? 不 —— 0x3A00 < 0x4480, 所以落在**保留洞**里,
 *   但 0x3A00 + 16 + 512 = 0x3C10 < 0x4480 也是洞内, 看似可行)。
 *   那为什么不用 S3 的偏移? 因为洞内布局与 PC 脚本无关 (没有任何 S3 工具按绝对
 *   偏移读 force) —— 而 OFF_FORCE 必须与 OFF_ROUTE_BUCKETS 之后的区**同族**,
 *   否则"桶表之后是 exec 域"这条结构性事实会被打乱。选择紧贴桶表末尾起址。
 *
 * ★ 尺寸: MASK = MAX_WIRES/32 = 4 × u32 = 16B; VAL = 128 × f32 = 512B。
 *   合起来 528B; 原 EXEC 洞 0x2B0 = 688B, 剩下 160B 作 tail。
 *   ⇒ 三者**必须精确相接** (下面的 _Static_assert 用 == 而不是 <=)。 */
#define FORCE_MASK_WORDS     (MAX_WIRES / 32)          /* 4 */
#define OFF_FORCE_MASK       0x47F0                    /* u32[4]: 128 bit 强制位图 */
#define OFF_FORCE_VAL        0x4800                    /* f32[128]: 强制值 (★ OA9 的核心) */
#define OFF_RSVD_EXEC_TAIL   0x4A00                    /* 剩余 0xA0 备用 */
#define OFF_RSVD_EXEC_TAIL_SZ (OFF_MB_SET - OFF_RSVD_EXEC_TAIL)   /* 0xA0 */

/* ══════════ W4: 通信域 Modbus 区 (0x4AA0..0x4DE0) ══════════
 *
 * ★★ 与 S3 的偏移**有意不同**, 理由与事实如下 (这是本区最容易被误改的一处):
 *
 *   S3 (components/core0/shared_mem.h:130-135, OA18 调整之后):
 *       MB_CTRL 0x4800[64] → MB_RX 0x4840[256] → MB_TX 0x4940[256]
 *       → MB_HOLD 0x4A40[128] → MB_SET 0x4AC0[128] → MB_END 0x4B40
 *   H723 (本文件):
 *       MB_SET 0x4AA0[128] → MB_CTRL 0x4B20[64] → MB_RX 0x4B60[256]
 *       → MB_TX 0x4C60[256] → MB_HOLD 0x4D60[128] → MB_END 0x4DE0
 *
 *   ① **为什么不能对齐**: S3 的 MB 区起点 0x4800 在 H723 上已被 **FORCE_VAL
 *      (0x4800 起 512B)** 占用 —— 两平台的 Force 域落点不同 (H723 从桶表之后的
 *      EXEC 洞里划, 见上文 W2.1 说明), 于是 MB 区只能另找位置。
 *      强行对齐意味着把已验证的 FORCE 域搬家并重跑 13 项判据 —— 不值。
 *   ② **对齐本来也不必要**: 已核实 **S3 的 PC 侧工具不依赖 MB 的 SHM 偏移** ——
 *      verify_modbus.py / verify_hmi.py 全部走**协议命令** (0x60 MB_INJECT 隧道
 *      模式 / 0x20 READ), 没有一处硬编码 0x4A40/0x4AC0 这类偏移。
 *      ⇒ "S3 回归脚本零改动"这个目标**不受本差异影响**。
 *   ③ ★ 更正一条过时注释 (2026-09-11, 与审计发现 G 同族):
 *      本区此前写着"原为 0x4AC0, 与 S3 的 0x4AA0 差 32 字节 ... 已改回 0x4AA0"。
 *      该说法**与 S3 当前源码不符** —— S3 在 OA18 (控制块 32B→40B) 之后把
 *      MB_SET 定在 **0x4AC0**, 并把整段后移。也就是说"0x4AA0 才是 S3 的值"
 *      这个前提在 S3 侧早已不成立。事实见上方两行对照表。
 *      ★ 它仍然保留 0x4AA0 是对的 —— 但理由不是"对齐 S3", 而是"该值已在
 *        本平台发布且无冲突" —— 注释必须说对理由, 否则下次有人照它去改 S3 侧。
 *
 * ★ 语义 (S3 原样, 与偏移无关):
 *   MB_HOLD = 读区 40001-40064 (wire 工程量镜像, ×100 取整, 只读)
 *   MB_SET  = 写区 40065-40128 (上位机设定值, DSL 显式引用 → SRC_HMI 源)
 *   MB_CTRL = MbCtrl_t 状态机控制块 (state/slave_addr/tick_budget/src/tx_uart…)
 *   MB_RX/TX= 帧收发缓冲 (各 256B; 单帧上限 MB_MAX_FRAME=128)
 */
#define OFF_MB_SET           0x4AA0   /* 写区 64 WORD = 128B (SRC_HMI 源) */
#define OFF_MB_CTRL          0x4B20   /* 控制块 (预留 64B; MbCtrl_t 当前 40B) */
#define OFF_MB_RX            0x4B60   /* RX 帧缓冲 256B */
#define OFF_MB_TX            0x4C60   /* TX 帧缓冲 256B */
#define OFF_MB_HOLD          0x4D60   /* 读区 64 WORD = 128B (wire 镜像, 只读) */
#define OFF_MB_END           0x4DE0   /* 通信域结束 */

/* ---- 通信域状态机常量 (与 S3 同名同值) ----
 * ★ MB_SILENT_TICKS 的推导必须写清, 否则后来人不知道它是怎么来的:
 *   3.5 个字符时间 @115200 8N1 = 3.5 × (10/115200) s = **304 μs**;
 *   本平台拍长 100 μs ⇒ 304/100 = 3.04 拍 ⇒ 取 **4** (向上保守)。
 *   ★ 若将来改拍长或波特率, 这个数**必须重算** —— 它是"帧边界"的唯一判据,
 *     算小会把一帧劈成两帧, 算大只是延迟一点响应 (后者无害, 前者致命)。 */
#define MB_ST_IDLE   0    /* 等帧 (RX 缓冲空) */
#define MB_ST_RX     1    /* 收字节中 (等 3.5 字符静默判帧尾) */
#define MB_ST_EXEC   2    /* 解析请求 (轻量) → 置构建上下文, 转 BUILD */
#define MB_ST_TX     3    /* 发响应 (限速逐字节) */
#define MB_ST_BUILD  4    /* 逐字节组装响应 + 增量 CRC, 每拍 ≤budget 字节 */
#define MB_EX_ILLEGAL_FUNC  0x01
#define MB_EX_ILLEGAL_ADDR  0x02
#define MB_EX_ILLEGAL_VAL   0x03
#define MB_EX_SLAVE_FAIL    0x04
/* ★★ MB_MAX_FRAME = **请求帧**上限。2026-09-11 外部审计 M2 修复: 128 → 255。
 *   为什么是 255 而不是 RTU 的 256:
 *     `MbCtrl_t.rx_len` 是 **uint8_t**(最多 255)。若设 256, 则 `rx_len < MB_MAX_FRAME`
 *     永真 ⇒ rx_len 到 255 再 ++ 会**回绕成 0** (静默丢帧/错帧)。
 *   而 qty≤123 的 0x10 写请求最长 = 9 + 2×123 = **255** ⇒ 255 已覆盖一切**合法**请求
 *   (qty=124 → 257B 本就超 RTU ADU 上限, 协议非法)。
 *   ★ 旧值 128 的后果 (M2): `0x10` 写 qty≥60 (len = 9+2qty ≥ 129) —— 一条**完全合法**
 *     的写请求 —— 在隧道被 NAK "mb: bad frame len", 在物理口被 `rx_len < 128` **静默截断**
 *     ⇒ CRC 必失败 ⇒ 无声无响应 (最难查的那一种)。
 *   ★ 代价: 请求 CRC 单拍校验的 WCET 上界从 ≤126B 升到 ≤253B —— 仍随本常量**收敛**,
 *     不是无界 (同 modbus.c 顶部的 WCET 说明)。 */
#define MB_MAX_FRAME   255    /* **请求帧**上限 (见上方 M2 说明; RX 缓冲 256B 放得下) */
/* ★★ MB_TX_SIZE: **响应帧**缓冲大小 = RTU ADU 上限 (256)。
 *   ★ 外部审计 W4 的 M1 (P1) 是"把这两个量当成同一个"造成的:
 *     响应长度由 qty 决定 (0x03: 3 + 2*qty + 2, qty≤125 ⇒ 最大 255),
 *     与**请求帧**长度无关。BUILD 若拿 MB_MAX_FRAME(128) 当组装界限,
 *     qty≥62 时 b_pos 永远到不了 total ⇒ 通信域永久 busy (合法读请求即可触发)。
 *   ★ 物理大小 = OFF_MB_TX 区 256B (见下方布局断言); 0x61 读取端缓冲
 *     (main.c h_mb_resp) 也必须按本值开, 否则 tx_len>128 会写穿栈。 */
#define MB_TX_SIZE     256
#define MB_TICK_BUDGET 4      /* 每拍最多处理字节数 (限速, WCET 上界) */
#define MB_SILENT_TICKS 4     /* 静默拍数 ≥ 3.5 字符 (见上方推导) */

/* ---- 通信域控制块 (40B, OFF_MB_CTRL) ----
 * ISR 每拍推进状态机; PC 侧可用 0x22 读本块观察通信域状态与统计。
 * ★ 布局与 S3 **逐字节相同** (这是本区唯一与 S3 严格对齐的东西 —— 偏移可以不同,
 *   但结构体必须同, 因为 PC 工具会按字段偏移解析 0x61 的返回)。 */
typedef struct __attribute__((packed, aligned(4))) {
    uint8_t  state;        /* MB_ST_* 状态机当前态 */
    uint8_t  slave_addr;   /* 从站地址 (1-247; 0=广播不响应) */
    uint8_t  rx_len;       /* RX 缓冲已收字节 */
    uint8_t  rx_pos;       /* RX 已消费偏移 (隧道模式逐拍推进, EXEC 后归零) */
    uint8_t  tx_len;       /* TX 缓冲待发字节 */
    uint8_t  tx_sent;      /* TX 已发出字节 */
    uint8_t  silent;       /* 连续无新字节的拍数 (3.5 字符判定) */
    uint8_t  enabled;      /* 1=通信域使能 */
    uint8_t  tick_budget;  /* 每拍字节上限 (限速, 默认 MB_TICK_BUDGET) */
    uint32_t frames_rx;    /* 统计: 完整帧数 */
    uint32_t frames_tx;    /* 统计: 发出响应数 */
    uint32_t err_crc;      /* 统计: CRC 校验失败 */
    uint32_t err_exc;      /* 统计: 异常响应数 */
    uint8_t  src;          /* RX 源: 0=UART FIFO, 1=隧道注入(0x60) */
    uint8_t  tx_uart;      /* TX 去向: 0=TX 缓冲(0x61 读回), 1=物理口
                            * (LA 验证用: 注入走隧道 + 响应真发 → 抓 PA2) */
    /* ---- 响应构建上下文 (组装+CRC 分摊到多拍, 不再单拍完成) ---- */
    uint8_t  b_func;       /* 本次响应对应的请求功能码 (0x80 位=异常) */
    uint16_t b_start;      /* 请求起始地址 (或 06/16 的回显地址) */
    uint16_t b_qty;        /* 请求寄存器数 (或 06 的回显值 / 异常的异常码) */
    uint16_t b_pos;        /* 响应构建进度 (字节, 0..b_len+2) */
    uint16_t b_len;        /* 响应主体长度 (不含 CRC 2 字节) */
    uint16_t crc_acc;      /* 增量 CRC16 累加器 (跨拍) */
    uint8_t  rsv[2];       /* 补齐到 40B (packed 下不自动补, 需显式) */
} MbCtrl_t;
_Static_assert(sizeof(MbCtrl_t) == 40, "MbCtrl_t must be 40 bytes (PC 侧按字段偏移解析)");

/* ★ MB 区布局断言。
 * ★★ 这里第一条**用 <= 而刻意不用 ==** (与 S3 同): MB_CTRL 是**预留 64B**,
 *   而当前 MbCtrl_t 只有 40B —— 留的 24B 是明确的"结构体增长余量", 不是无人区。
 *   (本项目在其它区坚持用 == 是因为那些区**不允许**有缝; 这一处有缝是设计意图,
 *    所以要把"缝"本身也钉死 —— 见下面第二条断言。)
 *   ★ 踩坑记录: 第一版照抄其它区的 == 写法, 于是编译期直接失败
 *     ("static assertion failed: MbCtrl_t 必须紧接 RX 缓冲") —— 这正是断言的价值:
 *     它没让一个"预留与结构体尺寸不符"的布局悄悄过去。 */
_Static_assert(OFF_MB_CTRL + sizeof(MbCtrl_t) <= OFF_MB_RX, "SHM: MbCtrl_t 超出 MB_CTRL 预留");
_Static_assert(OFF_MB_RX - OFF_MB_CTRL == 64u, "SHM: MB_CTRL 预留必须恰好 64B (S3 同口径)");
_Static_assert(OFF_MB_RX   + 256             == OFF_MB_TX, "SHM: MB RX 256B 必须紧接 TX 缓冲");
_Static_assert(OFF_MB_TX   + 256             == OFF_MB_HOLD, "SHM: MB TX 256B 必须紧接 HOLD 区");

/* ---- W3 免串口协议帧暂存区 (原落在 0x4B20 = OFF_MB_CTRL, W4 落地后必须让位) ----
 * ★ 挪到 MB_END 之后: 它是"调试器直写 + 主循环消费"的暂存区, 与通信域无耦合,
 *   放在通信域尾部既保持独立, 又让 MB 区成为一整块连续区域 (便于断言与理解)。
 *   ★ 尺寸仍是 4096: 0x10 deploy 的最大载荷约 3KB, 留余量。
 *     (它现在定义在 engine.h 而非 main.c —— 因为下面的布局断言需要它。) */
#define OFF_CMD_REQ          0x4DE0   /* 免串口帧暂存区 (cmd + payload) */
#define DEPLOY_REQ_MAX       4096     /* 单帧载荷上限 (字节) */
#define OFF_MB_TAIL          0x5DE0   /* 暂存区之后到 SHM 末尾, 备用 */
#define OFF_MB_TAIL_SZ       (SHM_SIZE - OFF_MB_TAIL)             /* 0x2220 */

#define SHM_SIZE             0x8000   /* 32KB (S3 为 64KB; H723 DTCM 128KB 充裕) */

/* ══════════ W5: macro 字节码域 (驻 MB_TAIL 备用区内) ══════════
 * ★★ 与 S3 的差异必须说清 (否则就是"宣称>实现"):
 *   S3: 64KB flash 分区 (macro_data) + 64KB RAM 缓冲 —— 因为有 8MB PSRAM。
 *   H723: **无 PSRAM** (MIGRATE §7 已列为风险), 故瘦身为 **4KB 字节码**, 且直接
 *     驻 SHM (RAM), 不走 flash:
 *       代价① 容量 64KB → 4KB (demo 足够; PC 侧行为一致)
 *       代价② **掉电丢失** (S3 是 flash 持久)。本域随 cold_start_reset 的 memset
 *              一起清零 —— 即 0x13 RESET / deploy 装载 / 上电都会清掉已上传的程序。
 *              这是 v0 的**明确边界**, 不是遗漏; flash 持久化留待后续 (要动 flash.c)。
 *   ★ 位置: OFF_MB_TAIL 原注释就是"备用", 其前段给 macro, 不影响通信域/暂存区。 */
#define OFF_MACRO_CTRL        0x5DE0   /* 控制块 (MacroCtrl_t, 16B) */
#define OFF_MACRO_CODE        0x5DF0   /* 字节码缓冲 4KB (PC 可用 0x22 burst 读回) */
#define OFF_MACRO_CODE_SZ     0x1000
#define OFF_MACRO_END         0x6DF0   /* == 0x5DE0 + 16 + 0x1000 */
#define MACRO_MAX_CODE        512      /* 单次 0x40 一次性执行的字节码上限 (同 S3) */
_Static_assert(OFF_MACRO_CTRL + 16u == OFF_MACRO_CODE, "SHM: MACRO_CTRL 必须紧接 CODE");
_Static_assert(OFF_MACRO_CODE + OFF_MACRO_CODE_SZ == OFF_MACRO_END, "SHM: MACRO_CODE 尺寸不符");
_Static_assert(OFF_MACRO_END <= SHM_SIZE, "SHM: MACRO 域越出 SHM 末尾");

/* ---- macro 控制块 (16B) ----
 * ★ 与 S3 macro_loop 的 OFF_MACRO_* 单字段不同, 这里聚成一个块 (H723 风格:
 *   可整体读写、便于 0x22 burst 观察)。字段语义与 S3 同名量一致:
 *     run=OFF_MACRO_RUN, len=OFF_MACRO_LEN, loop_ms=OFF_MACRO_LOOP_MS,
 *     err=OFF_MACRO_ERR, loop_cnt=OFF_MACRO_LOOP_CNT。 */
typedef struct __attribute__((packed, aligned(4))) {
    uint8_t  run;        /* 1 = 循环执行中 */
    uint8_t  err;        /* 最近一次执行错误码 (rc<0 → -rc; 0 = 无错) */
    uint16_t len;        /* 当前字节码长度 (0 = 无程序) */
    uint16_t loop_ms;    /* 循环间隔 ms (最小 10, 与 S3 同) */
    uint16_t rsv;        /* 补齐 */
    uint32_t loop_cnt;   /* 已执行轮数 (单调递增, 供 PC 判"真的在跑") */
    uint32_t last_tick;  /* 上次执行时的 g_tick_count (100μs/拍) — 内部节拍 */
} MacroCtrl_t;
_Static_assert(sizeof(MacroCtrl_t) == 16, "MacroCtrl_t must be 16 bytes");

/* ---- W5 观测面 (macro 区之后, 仍在 MB_TAIL 备用空间内) ----
 * ★★ 为什么必须有它 (本项目铁律"凡'写过了就算'的状态必须补一个能被外部读走的量"):
 *   HIL 的输出臂把占空比写进 **TIM3_CCR1 硬件寄存器** —— 协议侧读不到, 于是
 *   "PWM 真的按 u 变了"就成了**不可验证的宣称**。这里给它一个 SHM 镜像,
 *   使输出臂在**零外部仪器**下也能被逐值核对 (PC 用 0x22 burst 读回)。
 *   ★ 与 S3 的差异: S3 的 LEDC 占空比同样不可读, 这是本平台补的观测。 */
#define OFF_HIL_DUTY        0x6E00   /* u32: 最近写入 TIM3_CCR1 的计数值 (0..ARR+1) */
#define OFF_HIL_FB_RAW      0x6E04   /* u32: HIL 反馈**最近一次** ADC 原始码 (排障用:
                                      * 与 0x37 扫描同一通道的读数直接对照) */
#define OFF_W5_OBS_END      0x6E08
_Static_assert(OFF_MACRO_END <= OFF_HIL_DUTY, "SHM: W5 观测区与 MACRO 区重叠");
_Static_assert(OFF_HIL_FB_RAW + 4u <= SHM_SIZE, "SHM: W5 观测区越出 SHM 末尾");


/* ══════════ 路由条目 (16B packed) —— 与 S3 逐字节相同 ══════════ */
typedef struct __attribute__((packed, aligned(4))) {
    uint8_t  src_type;
    uint8_t  src_index;
    uint8_t  dst_type;
    uint8_t  dst_channel;
    uint8_t  op;
    uint8_t  flags;
    uint16_t param_idx;
    uint16_t state_offset;
    uint16_t actuator_idx;  /* ★ 语义 (H723): **SHM 浮点槽索引** ACTUATOR_STATUS[0..63],
                             *   不是物理引脚 —— 本平台没有 GPIO 执行器面 (见下方
                             *   ENG_MAX_OUT_SURFACES 段与 eng_outputs_safe 的说明)。
                             *   `0` = 本路由不驱动执行器。上界 = MAX_ACTUATORS(64),
                             *   越界在 engine_route_validate **下载期拒绝**
                             *   (范本是 u32 位图所以上界 32, 照搬会误杀合法的 32..63)。 */
    uint16_t wire2_idx;
    uint8_t  period;   /* offset 14: div_idx(2bit) + phase(6bit) */
    uint8_t  reserved; /* offset 15: S3 里这是编译器的**尾部填充字节** (15 个字段
                        * + aligned(4) → sizeof 补齐到 16)。这里显式命名, 使
                        * 逐字节校验和**不依赖填充内容** —— 否则任何"逐字段赋值"
                        * 的改动都会让填充变脏, 校验和随之漂移 (实测被这一步绊到过)。 */
} RouteEntry_t;

_Static_assert(sizeof(RouteEntry_t) == 16, "RouteEntry_t must be 16 bytes");
_Static_assert(_Alignof(RouteEntry_t) == 4, "RouteEntry_t alignment must be 4");

/* ---- 参数条目 (16B) ---- */
typedef struct __attribute__((packed, aligned(4))) {
    float value_a;
    float value_b;
    float value_c;
    float value_d;
} ParamEntry_t;

_Static_assert(sizeof(ParamEntry_t) == 16, "ParamEntry_t must be 16 bytes");

/* ---- 状态条目 (16B) ---- */
typedef struct __attribute__((packed, aligned(4))) {
    float state_a;
    float state_b;
    float state_c;
    float state_d;
} StateEntry_t;

_Static_assert(sizeof(StateEntry_t) == 16, "StateEntry_t must be 16 bytes");

/* ══════════ W3: Sequencer 数据结构 (与 S3 逐字节同) ══════════ */

/* ---- 步条目 (16B packed) —— 表序 = 步序, 每实例一段连续槽 ----
 * ★ 每条 = 一格。停留于某步时, 其转移条件被周期评估 (按实例档位)。
 * ★ 条件**只有两种** (v0): 阈值转移 / 超时强推, 取先满足者。分支与并行 → v1。 */
typedef struct __attribute__((packed, aligned(4))) {
    uint8_t  cond_type;    /* 0=SENSOR 1=WIRE 2=无(仅超时) */
    uint8_t  cond_idx;     /* 源索引 */
    uint8_t  flags;        /* bit0=末步loop回卷 bit1=timeout_en bit2=启用(预留) */
    uint8_t  reserved;     /* 显式命名: 使逐字节比对不依赖填充 */
    uint16_t param_idx;    /* param.value_a=转移阈值(>); value_b=超时秒 */
    uint16_t state_offset; /* 保留 0 (v0 计时在实例控制块, 不逐步占 STATE 槽) */
    uint16_t jump_idx;     /* 0=线性下移 (v1: 分支目标) */
    uint32_t reserved2;    /* ★ 必须是 u32 —— 4×u8 + 3×u16 + u32 = 16B。
                            *   (第一版写成 u16 → sizeof=12, 被下面的 _Static_assert
                            *    当场拦下。这就是断言的用处: 布局错了不能"编译过就算".) */
} SeqStepEntry_t;

_Static_assert(sizeof(SeqStepEntry_t) == 16, "SeqStepEntry_t must be 16 bytes");
_Static_assert(_Alignof(SeqStepEntry_t) == 4, "SeqStepEntry_t alignment must be 4");

/* ---- 实例控制块 (16B) —— 仅 0x44 写 + ISR 读写, 不进帧, 布局可重排 ----
 * ★★ step_tick 必须是 **u32 且放最后** —— 这不是排版偏好, 是 S3 审计 OA5 的修复:
 *   u16 在快档 (dt=100μs) 下 65535×100μs = 6.55 秒就回卷 → 超过 6.55s 的停留
 *   会**静默卡步** (step_tick 归零, 超时永远差一点点, 看得像"条件没满足")。
 *   这个 bug 只在"长停留"场景暴露, 短测试全绿 —— 同族于本项目"判据看起来在报
 *   固件故障, 其实是量程不够"。⇒ 直接用 u32 (2^32 × 100μs ≈ 5 天, 够)。 */
typedef struct __attribute__((packed, aligned(4))) {
    uint16_t step_base;    /* 本实例在 SEQ_TABLE 的起始槽 */
    uint16_t n_steps;
    uint16_t step_cur;     /* 当前步号 (0-based 内部; 对外 wire 值 = step_cur+1) */
    uint16_t out_wire;     /* 步号镜像 wire — B1 登记为本实例唯一生产者 */
    uint8_t  period;       /* offset 8: div_idx(2bit) + phase(6bit), 同 RouteEntry */
    uint8_t  run;          /* bit0=启用 (START 置位) */
    uint16_t reserved;
    uint32_t step_tick;    /* 本步已停留的**激活拍**数 (×dt = 秒) — 见上面 OA5 说明 */
} SeqCtrl_t;

_Static_assert(sizeof(SeqCtrl_t) == 16, "SeqCtrl_t must be 16 bytes");
_Static_assert(_Alignof(SeqCtrl_t) == 4, "SeqCtrl_t alignment must be 4");

/* ---- period 字段位定义 ---- */
#define PERIOD_DIV_IDX_FAST  0   /* 1×: 每 100μs */
#define PERIOD_DIV_IDX_MID   1   /* 10×: 每 1ms */
#define PERIOD_DIV_IDX_SLOW  2   /* 100×: 每 10ms (★★ H9 第二次修正 —— 旧值 64×(6.4ms) 是误修,
                                  *   见 BUCKET_DIV2_PHASES 的说明。真问题是 phase 字段只有 6 位,
                                  *   不是周期该是 64。) */
#define PERIOD_DIV_MASK      0x03
#define PERIOD_PHASE_SHIFT   2

/* ══════════ deploy 预算模型 (阶段 3.2) ══════════
 * 条数限制 ≠ 成本限制: 128 条 PID 是 128 条, 但成本是 DIRECT 的 2.6 倍。
 * 门的作用是保证**任何可部署程序**每拍执行都在确定性预算内。
 *
 * ★ 除数: div1 摊 ÷10 / **div2 摊 ÷64** (S3 用 100 是 H9 缺陷的一部分, 不许抄)。
 * ★ 预算来源: `k_op_cost_itcm[]` 全部是**本平台实测值**
 *   (tools/h723_op_sweep.py 两点法, 19/19 原语, 表校验和逐 op 与 Python 预测吻合)。
 *   S3 那张表是 240MHz 上的数 (DIRECT=234), 照抄就是"宣称≠实现"。 */
#define OP_COST_DIV0         1
#define OP_COST_DIV1         10
#define OP_COST_DIV2         64   /* ★ 不是 100 (H9) */
#define SRC_COST_FALLBACK    20   /* 未实测源类型的保守兜底 (cycles/条) */

/* 部署门: 引擎扫描每拍 ≤ 此值。拍长 40000 cyc (100μs @400MHz)。
 * 余下 14000 cyc (35μs) 留给: 骨架 ISR (~45) + 热重载那一拍 (~3000) +
 * 通信域/顺序域/传感域 (阶段 4) + 安全余量。
 * 依据: S3 的同名门是 16000/24000 = 67%; 这里 26000/40000 = 65%, 口径一致。 */
#define EXEC_DEPLOY_BUDGET   26000

/* ★★ 每拍"超预算"判据的阈值 (ISR 执行时长超过它 ⇒ OVERRUN 计数 +1)。
 *   ★ 与上面 EXEC_DEPLOY_BUDGET **不是**同一个量, 名字必须分开 (本项目踩过四次
 *     "一常量两用": MB_MAX_FRAME / NVIC_ISER 位移 / USART1 BRR / BUCKET_DIV2_PHASES):
 *       EXEC_DEPLOY_BUDGET = **下载期静态门**: 程序预算超它 ⇒ 拒绝 deploy (不等式, 预判)
 *       EXEC_BUDGET_CYCLES = **运行期动态判据**: 本拍真跑超它 ⇒ OVERRUN +1 (实测, 事后)
 *     一个管"能不能下发", 一个管"跑起来有没有超"。合并会导致: 改动其中一处会静默
 *     改变另一处的语义, 而两处都叫"预算"。
 *   ★ 取值: 拍长 40000 cyc，取 **80% = 32000**（留 8000 cyc = 20µs 余量给热重载那一拍
 *     与协议/顺序/传感域）。范本 `core0_isr.c` 的同名量是 **20000/24000 = 83%** ——
 *     同一个口径(留余量), 数值各按各的拍长。 */
#define EXEC_BUDGET_CYCLES   32000u

/* 本平台实测的**最贵原语**成本 (PID, 见 engine.c 的 k_op_cost_itcm)。
 * 改原语表/新增更重的原语时必须同步更新 —— 它参与下面那条绊线断言。 */
#define OP_COST_MAX_MEASURED   145   /* = 实测最贵原语 PID (2026-09-11 一次性审计重测;
                                      *   旧值 140 是 W2~W5 之前的数, 见 engine.c 表注释) */

/* ══════════════ 绊线断言: 预算门当前"具不具约束力" ══════════════
 * ★★ 这是一个**故意的反向断言**, 语义要说清楚:
 *   128 条上限 × 最贵原语 145 cyc = **18560** cyc = 拍长的 **46.4%** —— 也就是说
 *   **在 MAX_ROUTES=128 的前提下, 任何合法程序都不可能把拍吃满**, 预算门
 *   当前**永远不会触发**。它是一条"未来的门"。
 *   ★ 审计发现 G (2026-09-11): 这里原本写"145 cyc = 18560" —— 而
 *     (另注: 该数字随 `OP_COST_MAX_MEASURED` 变动 —— 2026-09-11 一次性审计把常量
 *      140→145 时这里也同步改了。**这类"注释算术"已经错过两次**, 所以现在有一条
 *      机械检查盯着它: tools/h723_audit_full.py 的 4.3 断言
 *      `OP_COST_MAX_MEASURED == k_op_cost_itcm[OP_PID]` —— 常量与表一旦漂移当场 FAIL。)
 *     `OP_COST_MAX_MEASURED` 早在 A3 重测后就改成了 140 (DIRECT 56→50, PID 相应下调),
 *     注释没跟上。**注释数字与常量不一致本身就是一种"自洽的假验证"**:
 *     读者复核"145×128=18560"算得没错, 于是不会再去质疑 145 这个**输入**是不是当前值
 *     (与 BRR 那条"算得对但公式错 16 倍"的注释同族)。
 *     ⇒ 已改正, 并把两个数写成同源表述 (引用常量名), 避免再次各自漂移。
 *   为什么仍然保留它: ① 引擎成本模型必须在扩容前就位 (S3 的 OA12→OA22 就是
 *   成本模型漏维度反复返工); ② DTCM 能放 ~2000 条路由 —— **一旦扩容, 这个门
 *   立刻变成真门**, 到那时它就必须被实测验证"真的拦得住超载"。
 *   ⇒ 这条断言的作用是**在扩容/加重量级原语的那一刻失败**, 逼人回来重新评估:
 *     届时必须做一次超载实验 (构造 >门 的程序, 确认 NAK + 确认拍没被拉长),
 *     而不是相信一个从没被触发过的判据。
 *   断言通过 = "门还不具约束力, 无需动作"; 断言失败 = "门现在是真的了, 去验证它"。*/
_Static_assert((uint32_t)MAX_ROUTES * OP_COST_MAX_MEASURED <= EXEC_DEPLOY_BUDGET,
               "★ 预算门开始具约束力: 必须实测验证它能拦住超载 (见本断言上方注释)");

/* ══════════ H723 扩展: deploy 生效确认字段 (S3 的 0x39-0x3F 当时空闲) ══════════
 * ★ 这是对 S3 那笔语义债的偿还点: S3 的 "ACK = 已受理 ≠ 已生效" —— 上位机收到 ACK
 *   之后无法知道配置**什么时候**开始生效, 只能假设"大概很快"。
 *   H723 让"已生效"变成一个**可观测的量**: 每次受理 deploy 递增一个序号并回给 PC,
 *   ISR 真正切换完 ACTIVE 表后把这个序号写进 APPLIED_SEQ;
 *   PC 轮询 APPLIED_SEQ == 自己的序号 ⇒ 生效**被证明**, 而不是被假设。 */
#define OFF_CTRL_DEPLOY_SEQ   0x3A   /* u16: 固件受理的部署序号 (每次 deploy 递增) */
#define OFF_CTRL_APPLIED_SEQ  0x3C   /* u16: ISR 已切换生效的序号 (= DEPLOY_SEQ 即已生效) */
#define OFF_CTRL_APPLIED_LAT  0x3E   /* u16: 从置 RELOAD 到 ACTIVE 切换完成, 跨了几拍 */
#define PERIOD_PHASE_MASK    0x3F

/* ---- flags 位定义 ---- */
#define ROUTE_FLAG_ACTIVE   0x01
#define ROUTE_FLAG_WIRE2    0x02

/* ---- 原语操作码 (与 S3 完全一致) ---- */
#define OP_DIRECT   0x00
#define OP_CMP      0x01
#define OP_HYST     0x02
#define OP_CLAMP    0x03
#define OP_LPF      0x04
#define OP_PID      0x05
#define OP_RATE     0x06
#define OP_DEADBAND 0x07
#define OP_MUX      0x08
#define OP_EDGE     0x09
#define OP_LUT      0x0A
#define OP_CNT      0x0B
#define OP_TIMER    0x0C
#define OP_ARITH    0x0D
#define OP_SCALE    0x0E
#define OP_AND      0x0F
#define OP_OR       0x10
#define OP_NOT      0x11
#define OP_SR       0x12
/* ★ 上界语义必须一眼可辨 (原版 OP_MAX=0x12 恰好**等于**最大值 OP_SR, 而代码里
 *   是按"含"用 `op <= OP_MAX` —— 极易被后来人写成 `< OP_MAX` 而静默漏掉 SR)。
 *   两个名字并存: OP_MAX 含上界, OP_MAX_EXCL 排他上界。新增原语时**两个都要改**,
 *   下面的 _Static_assert 会强制这一点。 */
#define OP_MAX      0x12   /* 最高有效操作码 (含) */
#define OP_MAX_EXCL 0x13   /* 排他上界 = OP_MAX+1 (数组尺寸/数量统计用这个) */
_Static_assert(OP_MAX + 1u == OP_MAX_EXCL, "OP_MAX and OP_MAX_EXCL must be adjacent");

#define OP_ARITH_ADD 0
#define OP_ARITH_SUB 1
#define OP_ARITH_MUL 2
#define OP_ARITH_DIV 3
#define OP_ARITH_MAX 4
#define OP_ARITH_MIN 5
#define OP_SR_SET_DOM    0
#define OP_SR_RESET_DOM  1

/**
 * @brief 该原语是否需要"第二输入" (从 wire 数组的 wire2_idx 取)
 *
 * ★★ 为什么要有这个函数 (A3 / S3 的 M1 族, 2026-09-10):
 *   第二输入有两条落地路径 —— flags 里的 ROUTE_FLAG_WIRE2 显式标志, 与
 *   wire2_idx 非 0 的隐式约定。**只查后者会出事**: `wire2_idx == 0` 既是
 *   "没接第二输入"的默认值, 又是合法索引 wire[0] 本身, 二者无法区分 →
 *   引擎会静默读 wire[0] 当第二输入 (S3 上花了 M1→F2→N-A→OA1 **四轮**才修掉)。
 *   审计实测 (H723): ARITH(CONST 10, wb=wire[0]=7) 输出 17.0, 而语义应为 10.0。
 *   ⇒ ISR 与 deploy 校验**必须共用这一个判据**。
 *
 * ★ AND/OR 是布尔双输入; ARITH 的右操作数是 wb; SR 的复位输入是 wb;
 *   CNT 的减计数/复位输入是 wb。S3 的 route_validate 只拦了 AND/OR,
 *   ARITH/SR/CNT 三类漏了 —— 这里补齐 (S3 侧也值得回写)。
 */
static inline int op_needs_wire2(uint8_t op)
{
    return (op == OP_AND || op == OP_OR || op == OP_ARITH || op == OP_SR || op == OP_CNT);
}

/**
 * @brief 第二输入判据 (与 S3 最终形态逐字一致) —— ISR 与校验必须共用
 * @param flags      RouteEntry_t.flags
 * @param wire2_idx  RouteEntry_t.wire2_idx
 * @return 1 = 第二输入有效, 可以读 wire[wire2_idx]; 0 = 无第二输入, 用 0.0f
 */
static inline int wire2_valid(uint8_t flags, uint16_t wire2_idx)
{
    return ((flags & ROUTE_FLAG_WIRE2) || wire2_idx) && (wire2_idx < MAX_WIRES);
}

/**
 * @brief 该原语是否**有状态** (需要 state 槽)
 *
 * ★ 有状态原语若不挂状态槽, ISR 会把 `&s_state_fallback` 传进去 —— 多个无槽路由
 *   会**共用同一个兜底槽**, 互相污染 (S3 T22 实证)。deploy 侧必须拒绝
 *   "有状态原语 + state_offset==0" 的载荷 (见 engine_route_validate)。
 *   (原在 engine.c 里, 移到此处以供表填充与校验共用同一份清单 —— 两处各写一份
 *    正是"改一处忘另一处"的温床。)
 */
static inline int op_is_stateful_h(uint8_t op)
{
    return (op == OP_LPF || op == OP_PID || op == OP_HYST || op == OP_RATE ||
            op == OP_DEADBAND || op == OP_EDGE || op == OP_CNT || op == OP_TIMER ||
            op == OP_SR);
}

/* ---- 源类型 ---- */
#define SRC_SENSOR  0
#define SRC_WIRE    1
#define SRC_CONST   2
#define SRC_HMI     3   /* 通信域设定区 — 阶段 4 落地 (本阶段 read_source 返回 0) */

/* ---- 目标类型 ---- */
#define DST_WIRE    2

/* ---- dt 感知 (秒) —— 与 S3 primitives.h 同口径 ---- */
#define DT_FAST  0.0001f   /* div0: 100μs */
#define DT_MID   0.001f    /* div1: 1ms  */
#define DT_SLOW  0.01f     /* div2: 10ms */

/* ══════════ 编译期布局断言 (S3 A4 纪律: 任何区域不得重叠) ══════════
 * ★★ 覆盖范围必须**无缝**, 而且相邻区**必须精确相接** —— 所以下面用 `==` 而不是
 *   `<=` (第一版用 `<=`, 于是"某区被悄悄改大/改小、留下看不见的空隙"不会报错;
 *   审计 H3 就是这么发现 [0x3840,0x4480) 与 [0x47F0,0x4AA0) 两段"无人区"的)。
 *   用 `==` 之后: 任何尺寸改动只要不与邻区严丝合缝, 编译直接失败。 */
_Static_assert(OFF_CTRL_N_SEQ    + 8   == OFF_SENSOR_MAP,      "SHM ctrl block must end exactly at 0x40");
_Static_assert(OFF_SENSOR_MAP      + MAX_SENSORS   * 4 == OFF_ACTUATOR_STATUS, "SHM SENSOR_MAP must abut next region");
_Static_assert(OFF_ACTUATOR_STATUS + MAX_ACTUATORS * 4 == OFF_WIRE_MAP,        "SHM ACTUATOR_STATUS must abut next region");
_Static_assert(OFF_WIRE_MAP        + MAX_WIRES     * 4 == OFF_LUT_DATA,        "SHM WIRE_MAP must abut next region");
_Static_assert(OFF_LUT_DATA        + MAX_LUT       * 4 == OFF_ROUTE_TABLE,     "SHM LUT_DATA must abut next region");
_Static_assert(OFF_ROUTE_TABLE     + MAX_ROUTES    * 16 == OFF_ROUTE_STAGING,  "SHM ROUTE_TABLE must abut next region");
_Static_assert(OFF_ROUTE_STAGING   + MAX_ROUTES    * 16 == OFF_PARAM_TABLE,    "SHM ROUTE_STAGING must abut next region");
_Static_assert(OFF_PARAM_TABLE     + MAX_PARAMS    * 16 == OFF_PARAM_STAGING,  "SHM PARAM_TABLE must abut next region");
_Static_assert(OFF_PARAM_STAGING   + MAX_PARAMS    * 16 == OFF_STATE_TABLE,    "SHM PARAM_STAGING must abut next region");
_Static_assert(OFF_STATE_TABLE     + MAX_STATES    * 16 == OFF_STATE_STAGING,  "SHM STATE_TABLE must abut next region");
/* 状态 staging 之后是**保留区** (0x3840-0x447F), 所以这里只能断言"不相交" */
_Static_assert(OFF_STATE_STAGING   + MAX_STATES    * 16 <= OFF_RSVD_DSL_DOMAIN,  "SHM STATE_STAGING overruns reserved hole");
/* ★ W3: 保留洞 [0x3840,0x4480) 现在**被 SEQ 区分走尾部** —— 所以不能再断言
 *   "洞的尺寸 == 0xC40 且洞尾 == BUCKET 头"(那条太粗, SEQ 一改也过)。
 *   改成三段精确相接: [0x3840 .. SEQ 头) 是剩余洞, [SEQ 头 .. SEQ_END) 是 SEQ 区,
 *   且 SEQ_END 必须**精确等于** BUCKET 头。任何一段尺寸变动立刻编译失败。 */
_Static_assert(OFF_RSVD_DSL_DOMAIN <= OFF_SEQ_TABLE,                             "SHM SEQ table must start inside the reserved hole");
_Static_assert(OFF_SEQ_TABLE % 4 == 0,                                          "SHM SEQ table must be 4-byte aligned");
_Static_assert(OFF_SEQ_TABLE      + MAX_SEQ_STEPS * 16 == OFF_SEQ_CTRL,          "SHM SEQ_TABLE must abut SEQ_CTRL");
_Static_assert(OFF_SEQ_CTRL       + MAX_SEQ_INST  * 16 == OFF_SEQ_END,           "SHM SEQ_CTRL must abut SEQ_END");
_Static_assert(OFF_SEQ_END        == OFF_ROUTE_BUCKETS,                          "SHM SEQ region must abut route buckets exactly");
_Static_assert(OFF_RSVD_DSL_DOMAIN + OFF_RSVD_DSL_DOMAIN_SZ == OFF_ROUTE_BUCKETS, "SHM DSL reserved-hole size mismatch");
_Static_assert(OFF_ROUTE_BUCKETS   + ROUTE_BUCKET_U16 * 2 == OFF_ROUTE_BUCKETS_ST,  "SHM ROUTE_BUCKETS must abut staging");
_Static_assert(OFF_ROUTE_BUCKETS_ST + ROUTE_BUCKET_U16 * 2 == OFF_ROUTE_BUCKETS_END, "SHM ROUTE_BUCKETS_ST must abut end");
_Static_assert(OFF_ROUTE_BUCKETS_END == OFF_FORCE_MASK,                        "SHM FORCE_MASK must start right after buckets end");
_Static_assert(OFF_FORCE_MASK      + FORCE_MASK_WORDS * 4 == OFF_FORCE_VAL,    "SHM FORCE_VAL must abut FORCE_MASK");
_Static_assert(OFF_FORCE_VAL       + MAX_WIRES * 4 == OFF_RSVD_EXEC_TAIL,      "SHM FORCE_VAL must abut exec tail");
_Static_assert(OFF_RSVD_EXEC_TAIL  + OFF_RSVD_EXEC_TAIL_SZ == OFF_MB_SET,      "SHM exec tail must abut MB_SET");
/* W4 通信域: 五段必须**精确相接** (用 == 而非 <= —— 留缝就等于留无人区) */
_Static_assert(OFF_MB_SET   + MB_NREG * 2 == OFF_MB_CTRL,                     "SHM MB_SET must abut MB_CTRL");
_Static_assert(OFF_MB_CTRL  + 64u         == OFF_MB_RX,                       "SHM MB_CTRL must abut MB_RX (预留 64B)");
_Static_assert(OFF_MB_RX    + 256u        == OFF_MB_TX,                       "SHM MB_RX must abut MB_TX");
_Static_assert(OFF_MB_TX    + 256u        == OFF_MB_HOLD,                     "SHM MB_TX must abut MB_HOLD");
_Static_assert(OFF_MB_HOLD  + MB_NREG * 2 == OFF_MB_END,                      "SHM MB_HOLD must abut MB_END");
/* 免串口暂存区紧随通信域之后 (W4 起; W3 时它曾占 0x4B20 = 现在的 MB_CTRL) */
_Static_assert(OFF_MB_END   + DEPLOY_REQ_MAX == OFF_MB_TAIL,                  "SHM CMD_REQ must abut MB tail");
_Static_assert(OFF_MB_TAIL  + OFF_MB_TAIL_SZ == SHM_SIZE,                     "SHM MB tail must end exactly at SHM_SIZE");
/* ★ 保留区尺寸钉成常量: 邻区一改, 这三条立刻失败 (它们就是"无人区"的哨兵) */
_Static_assert(OFF_RSVD_DSL_DOMAIN_SZ  == 0xC40u, "DSL hole size changed from 0xC40 - did you resize a neighbour?");
_Static_assert(OFF_RSVD_EXEC_TAIL_SZ   == 0x00A0u, "EXEC tail size changed from 0xA0 - did you resize a neighbour?");
_Static_assert(OFF_MB_TAIL_SZ          == 0x2220u, "MB tail hole size changed from 0x2220 (W4 挪过 CMD_REQ)");
/* 反向断言: 控制块区的每个字段都必须落在区内 (防止上面某个宏被改大而不自知) */
_Static_assert(OFF_CTRL_MAGIC + 4 <= OFF_CTRL_N_SEQ + 8, "SHM ctrl field overruns 0x40");
_Static_assert(OFF_TIMING_LAST_EXEC + 4 <= OFF_CTRL_GPIO_MASK + 4, "SHM timing region overlaps GPIO_MASK");
/* deploy 生效字段必须落在 0x3A-0x3F 且不越界到 SENSOR_MAP */
_Static_assert(OFF_CTRL_APPLIED_LAT + 2 == OFF_SENSOR_MAP, "SHM deploy seq fields must exactly fill 0x3A-0x3F");

/* ══════════ 引擎扫描 (两份实例: FLASH 与 ITCM, 见 engine.c) ══════════
 * @param base   SHM 基址 (DTCM 内)
 * @param first  起始路由下标 (档桶调度会传桶起点)
 * @param count  扫描条数 (0 起)
 * @return       校验和 (证明"真的算过" —— 防死代码消除 + 提供运行证据)
 */
typedef uint32_t (*engine_scan_fn)(uint8_t *base, uint32_t first, uint32_t count);

extern uint32_t engine_scan_flash(uint8_t *base, uint32_t first, uint32_t count);
extern uint32_t engine_scan_itcm (uint8_t *base, uint32_t first, uint32_t count);

/** @brief 按 profile 填充参数表/状态表/路由表 (冷启动与重配置共用)
 *  profile: 0 = 全 DIRECT (对照 S3 的 234 cyc 基线)
 *           1 = 19 原语轮转 (混合程序, 真实成本谱)
 *           2 = 全 PID (最重档, 探预算上界)
 *           3 = **三档混合** · 全 DIRECT  (div 0/1/2 各约占 1/3)
 *           4 = **三档混合** · 19 原语轮转 */
void engine_fill_tables(uint8_t *base, int profile);

/* ══════════ 阶段 3: 档桶调度 (S3 OA15 治本语义) ══════════ */

/** @brief 按 (div, phase) 归组重排路由表 + 生成桶索引
 *  ★ 幂等: 对已归组的表再跑一次结果不变 (对源表按序扫描再落桶 = 稳定排序)。
 *  ★ 用 OFF_ROUTE_STAGING 当暂存 (它就是为"重排/热重载"预留的 2KB)。
 *  @return 归组后的条数 (应 == nr) */
uint32_t engine_build_buckets(uint8_t *base, uint32_t nr);

/** @brief 桶表校验和 (FNV-1a over 220 × u16) —— 供外部独立预测比对 */
uint32_t engine_bucket_checksum(const uint8_t *base);

/** @brief H9 断言: 桶表里"不可达槽" (div2 相位 64..99) 的非零个数, 期望恒为 0
 *  —— 这是"div2 相位数 = 6 位字段能表达的 64 个"这一修正的**可失败判据**:
 *     若哪天有人把相位数改回 100 而不改字段宽度, 这里立刻不为 0。 */
uint32_t engine_bucket_dead_slots(const uint8_t *base);

/** @brief ★ 分档调度: 本拍只跑
 *         [div0 全部] + [div1 桶 tick%10] + [div2 桶 tick%100]
 *  @param tick      拍号 (调用方保证单调递增)
 *  @param impl      用哪份实例 (FLASH / ITCM)
 *  @param nrun_out  可选: 本拍**实际执行**的路由条数 (正向证据: 与外部预测比对)
 *  @return          三段校验和的异或
 */
uint32_t engine_tick(uint8_t *base, uint32_t tick, engine_scan_fn impl,
                     uint32_t *nrun_out);

/** @brief ★ W2.2 拍首 Force 覆写 —— **必须在任何扫描之前**调用, 且两条扫描路径
 *         (engine_tick 分档调度 / engine_scan_* 全表扫) **都要**调。
 *
 * 语义: 被强制的 wire 先钉成 FORCE_VAL, 然后才扫路由 ⇒ 下游读到的是强制值。
 *
 * ★★ 为什么是独立函数而不是塞进 engine_tick (第一版的教训):
 *    ISR 有两条扫描路径, 塞进其中一条 ⇒ "换 scan_mode 就静默失效"。
 *    实测 2026-09-11: fmask/fval 全对但 wire 不变, 症状是"SHM 全对效果为零"。
 *    ⇒ 凡"每拍都必须发生"的动作, 不能挂在分支里, 要做成统一的**前置步骤**。
 *
 * ★ 无强制位时是零成本快路径 (一次读 + 一次或运算即返回)。
 * ★ 放 ITCM (热路径)。 */
void engine_force_apply(uint8_t *base);

/* ══════════════════ W3: Sequencer (顺序域) ══════════════════
 * 语义与 S3 逐字相同 (DESIGN-sequencer §7): 每实例按自己的 (div,phase) 档位
 * 被评估, 停留于某步时判"条件转移 / 超时强推", 满足则步号 +1。
 * 步号镜像到 out_wire (1.0 起) —— 输出译码**下沉给路由网** (CMP 等),
 * seq 内部不开第二写者 (B1 地基不可拆)。 */

/** @brief 顺序域扫描段 —— 在**路由扫描之后、计时统计之前**调用, 与路由同拍。
 *
 *  ★ 为什么是独立入口而不是并进 engine_tick: 与 engine_force_apply 同族理由 ——
 *    ISR 有两条扫描路径 (分档 / 全表扫), 且**纯 seq 程序 (n_routes=0) 也必须跑**。
 *    若塞进 engine_tick, 那么"n_routes=0 的纯顺序机"这一整类程序会静默不推进。
 *
 *  ★ 成本 (S3 §7.2): 每实例每**激活拍**读 1 条步条目 + 1 次条件比较 + 至多 1 次
 *    float 写 ≈ 几十 cyc; 8 实例全挂慢档时均摊每拍 < 2% 预算。
 *    非激活拍以 (div,phase) 门直接跳过 (一次取模 + 一次比较)。
 *
 *  ★ BUSY 拍语义: 由调用方在 BUSY 时不调用 —— 步号**冻结保持**
 *    (与"引擎 STOP 不清执行器"同哲学: 显式状态, 不留歧义)。
 *
 *  ★ 写 out_wire 时受 FORCE_MASK 屏蔽 (与路由写端同一判据) —— 否则"被强制的
 *    wire"会被 seq 每拍改掉, 强制形同虚设 (OA9 同族)。
 *
 *  @param base  SHM 基址
 *  @param tick  拍号 (与路由扫描同一个 tick, 保证相位一致)
 *  @return      步号镜像写入次数 (0 = 本拍无实例推进; 供外部核对"真的动过")
 */
uint32_t engine_seq_tick(uint8_t *base, uint32_t tick);

/** @brief SHM 静态区 (定义在 engine.c, 链接段 .dtcm_shm / DTCM 0x20000000) */
extern uint8_t g_shm[SHM_SIZE];

/** @brief 审计发现 H 的观测面: 停机清输出时 GPIO_MASK 非 0 的次数 (应恒为 0)
 *  ★ 这是一个**否定性声称的外部证据**: 它恒 0 ⇔ 从没人往语义未定的 GPIO_MASK
 *    写过值。若非 0 ⇒ 有路径绕过了定案, 属必须查的 bug。 */
extern volatile uint32_t g_safe_mask_nonzero;

/** @brief 上一次 eng_outputs_safe() **实际执行**的物理输出面个数
 *  ★ 与 `eng_output_surface_count()` (登记数) 对照使用:
 *      登记数 = 0        ⇒ 没有任何域登记过 ⇒ 停机不会清任何物理输出 (配置错误)
 *      登记数 > 执行数   ⇒ 注册表满了被静默丢弃, 或调用路径没跑到 (必须查)
 *    两个数一起读才能区分"没登记"与"登记了却没跑" —— 只读一个都分不清。 */
extern volatile uint32_t g_safe_surfaces_ran;

/** @brief 冷启动清零 (.dtcm_shm 是 NOLOAD, 上电内容不确定 → 必须显式清)
 *  ★ 单一入口纪律 (S3 第二十六轮收口): "新增任何域必须在此登记"。
 *    本实现直接整段 memset(SHM_SIZE), 所以天然完整 —— 但**新域若放在 SHM 之外**
 *    必须回到这里显式登记。engine_fill_tables() 也走这个入口, 不允许自带 memset。 */
void cold_start_reset(void);

/** @brief 落位自检: 1 = 声明位置与 .dtcm_shm 段首吻合且落在 DTCM 域内 */
int shm_layout_ok(void);

/** @brief 路由表校验和 (FNV-1a, 覆盖 op/flags/src_type/state_offset)
 *  ★ 审计修正 (对照 S3 第二十七轮 OA23 "判据恒真"):
 *    第一版用 `路由[0].op` 当"表换成功了吗"的哨兵 —— 但 profile 0(全DIRECT) 与
 *    profile 1(19原语轮转, 首元素恰好也是 DIRECT) 的 route[0].op **都是 0**,
 *    哨兵在混合程序组上恒等 → 判据**不具备可失败性**。
 *    改成整表校验和后, 工具可以在 Python 里**独立重算**期望值再比对 ——
 *    这同时给出"表内容正确"的正向证据 (不只是"表变了")。 */
uint32_t engine_table_checksum(const uint8_t *base);

/** @brief 表内 ACTIVE 路由条数 (期望 = 装了几条就是几条) */
uint32_t engine_active_routes(const uint8_t *base);

/* ══════════════════ 阶段 3.2 — deploy 路径 ══════════════════ */

/** @brief 单条路由合法性校验。返回 NULL = 合法, 否则返回**可直接回给 PC 的原因文本**。
 *  原则 (与 S3 一致): 错误配置在**下载时显式失败**, 而不是等到运行时静默越界。
 *  H723 适配: 去掉了 S3 的 force/GPIO 安全掩码判据 (本阶段没有这两个域),
 *  但**保留了 SRC_HMI 的拦截** —— 引擎侧 read_source 对 SRC_HMI 是留位返回 0,
 *  若放行就会得到"恒 0 的假信号"而不是报错 (H5 未决项的正面处理)。*/
const char *engine_route_validate(const RouteEntry_t *r);

/** @brief 计算程序每拍均摊执行成本 (Σ ceil((op_cost+src_cost)/div倍率)), 过滤 ACTIVE。
 *  @param payload deploy 载荷起点 (**注意不是整个帧**, 是 [nr][np][ns] 之后的第一个路由)
 *  @param nr      载荷里的路由条数 */
uint32_t engine_prog_budget(const uint8_t *payload, uint16_t nr);

/** @brief 本平台实测的单条原语成本 (cycles/条, ITCM, 全表扫)。
 *  @param op 操作码; 越界返回保守值。 */
uint16_t engine_op_cost(uint8_t op);

/** @brief 热重载: STAGING → ACTIVE (路由 + 桶表 + 参数 + 状态)。
 *  ★ 必须在**关掉扫描**的前提下调用, 否则 ISR 可能扫到半张表。
 *  调用方负责把 SHM 控制块的 RELOAD 标志清掉。 */
void engine_reload_active(uint8_t *base);

/** @brief deploy 的 STAGING 装载 + 归组重排 + 桶表生成 (校验**之前**不要调)。
 *  @param payload [nr][np][ns] 之后的第一个路由
 *  @param nr/np/ns 条数 (调用方已校验 ≤ MAX_*)
 *  @return 实际写入 ACTIVE 的路由条数 (非 ACTIVE 的不写 → 与 nr 可能不同) */
uint16_t engine_stage_program(uint8_t *base, const uint8_t *payload,
                              uint16_t nr, uint16_t np, uint16_t ns);

/** @brief 栈边界哨兵: 在 _shm_end 之上铺 128 字节魔术字, 被踩返回 0
 *  ★ 设防缺口修正: SHM 与栈之间原本**没有任何保护**, 栈溢出会静默踩表
 *    (同 OA20 族: "无人设防的区域迟早出事")。 */
void shm_guard_paint(void);
int  shm_guard_ok(void);

/* ══════════════════ W1: SHM 读写命令的地址守卫 ══════════════════
 * S3 `main.c:83-140` 的同构实现。**逐字搬的是判据, 不是地址表** ——
 * S3 写的是 S3 的地址 (0x60004000 GPIO / 0x60009000 LEDC / 0x60000000 外设),
 * 在 H723 上一个都不成立, 照抄会放行一片**根本不存在的外设区**, 写进去
 * 触发 BusFault (S3 上 LX7 可能只是静默错位, H7 上直接 HardFault)。
 *
 * ★★ 为什么要有白名单 (而不是"SHM 之外一律拒"):
 *    0x20-0x23 的存在意义就是"用协议访问 SHM **和** 外设寄存器" ——
 *    S3 的 T8 就是靠 0x20 读 GPIO_OUT_REG 拿外部证据的。所以必须有外设白名单。
 * ★★ 为什么外设白名单**必须窄**:
 *    H723 的 RCC(0x58024400) / PWR(0x58024800) / FLASH(0x52002000) 全在同一片
 *    APB3/AHB4 里。放行整个 0x58000000-0x58025000 就等于让 PC 能写 PLL 分频、
 *    关掉 VOS、擦 Flash —— 一次误写就是砖。这里**显式排除**这三个。 */

/** @brief 单地址可访问性 (4B 对齐 + SHM 内 或 白名单外设区) */
int eng_valid_addr(uint32_t a);

/** @brief 区间可访问性 (burst 用: 起止必须落在**同一个**合法区内, 防跨区越界) */
int eng_valid_range(uint32_t a, uint32_t bytes);

/** @brief 该 SHM 偏移是否属于 float 数据区 (决定是否要拦 NaN/Inf) */
int eng_shm_off_is_float(uint32_t off);

/** @brief 0x21/0x23 写放行: SHM float 区只收有限值, 其余一律放行 */
int eng_write_allowed(uint32_t a, uint32_t v);

/** @brief float 位模式有限性 (指数 8 位全 1 = ±Inf/NaN)
 *  ★ 用**位模式**而不是浮点比较: 不依赖 FPU 状态, 且在 ISR 里也安全。
 *  ★ 必须由 engine.h 统一提供 —— 0x21 写守卫、0x24 强制守卫、ISR 的 src/wb
 *    有限性检查是**同一个判据**, 各写一份就是"改一处忘一处"的温床 (S3 审计常客)。 */
static inline int is_finite_bits(uint32_t v)
{
    return ((v & 0x7F800000u) != 0x7F800000u);
}

/* ══════════ 物理输出面注册 —— 「停机 = 进安全态」的结构性保证 ══════════
 * ★★★ 为什么需要这个机制 (2026-09-11 迁移保真度审查 一级 #1):
 *   范本里「停机 = 进安全态」是 P1 不变量, 靠 `eng_outputs_safe()` 清 GPIO 位图实现。
 *   迁到 H723 后**新增了一个真实物理输出面** —— HIL 的 PWM (`TIM3_CCR1` / PA6),
 *   而安全态还停在"清 S3 那个 GPIO 位图"的模型上(且该位图在 H723 语义未定、不可达)。
 *   ⇒ 结果: **STOP 之后 PWM 保持最后占空比不动** —— 停机不进安全态, 电机/阀门会继续动。
 *   注意这不是"某人忘了改一行", 而是**契约形状本身有洞**: 契约写的是"清某个寄存器",
 *   于是每新增一个输出面就漏一个。⇒ 契约改成"**覆盖全部已注册的物理输出面**"。
 * ★ 纪律 (新增输出面时): init 之后必须调用 `eng_register_output_surface(自己的安全态)`。
 *   不注册 = 停机不进它的安全态。注册数可被外部读走 (0x38 尾部 byte 38), 所以
 *   "我把所有面都改了"不是一句无法核对的宣称。 */
#define ENG_MAX_OUT_SURFACES 4
/** @brief 注册一个物理输出面的安全态处理 (幂等: 重复注册同一函数只记一次的空位不保证) */
void eng_register_output_surface(void (*fn)(void));
/** @brief 已注册的输出面个数 (观测: 证明契约真的覆盖了 N 个面) */
uint32_t eng_output_surface_count(void);

/** @brief 输出安全态: **全部已注册物理输出面归零** + 执行器数组归零 (STOP/RESET 用)
 *  ★ S3 用 GPIO_OUT_W1TC (只清不置); H723 无此寄存器, 等价物是
 *    **BSRR 的高 16 位** (`GPIOx_BSRR = mask << 16`)。低位是置位, 高位是清位 ——
 *    写高位就天然"只清不置", 掩码外的引脚不受影响。
 *  ★ 但 H723 当前**没有** GPIO 执行器输出: 引擎只写 SHM 的 ACTUATOR_STATUS 浮点槽,
 *    `OFF_CTRL_GPIO_MASK` 语义未定且不可达 (见上方该字段说明) ⇒ 上述 BSRR 路径不可达。
 *    真正在动的是**注册进来的物理面** (当前 = HIL PWM)。两者都要覆盖。 */
void eng_outputs_safe(void);

/* ══════════════════ W2: Force (wire 强制/释放) ══════════════════
 * 语义: "PC 把某个 wire 钉在固定值上, 引擎照常跑但不许改它" —— 用于现场
 * 调试/开环验证 (没有真实传感器时给控制器一个假输入)。
 * ★ 与 STOP 的安全态无关: STOP 是清零; FORCE 是钉住一个**由 PC 指定的**值,
 *   所以它比 STOP 危险 (值不是 0, 执行器可能真的动) —— 因此 deploy/RESET 必须清它。 */

/** @brief 清空全部 force 位与值 (deploy / RESET / SEQ_DEPLOY 调用)
 *  ★ 必须在**关掉扫描**或至少与 ISR 无竞争的前提下调用 (写 MASK 的 4 个字
 *    不是原子的 —— 中途被拍中断会看到半更新的掩码)。本实现由协议侧串行调用,
 *    且 ISR 只读不写 MASK, 所以最坏情况是"少强制一拍"。 */
void eng_force_clear(uint8_t *base);

#endif /* DCL_ENGINE_H */
