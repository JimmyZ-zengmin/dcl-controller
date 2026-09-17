#ifndef STEP_H
#define STEP_H
/* ═══════════ 步进脉冲源 (TB6600) (2026-09-15 建, 2026-09-17 修订) ═══════════
 * 接线: PUL←PA6(TIM3_CH1)  DIR←PE8  ENA←PE9
 *   共阳: 三个光耦**正端**并到板 **3.3 V**（★ 不是 5 V），MCU 拉低 = 光耦导通。
 *   ★★ 为什么必须 3.3 V 而不是 5 V：3.3 V 推挽的"高"**关不断** 5 V 光耦
 *     （链上仍有 5−3.3 = 1.7 V > LED 的 Vf）；**开漏也救不了** —— PE8/PE9/PA6 在
 *     `DS13313 Rev 5` Table 7 里是 `TT_ha`（**非 FT**），高阻会被外部拉到 ~4.5 V，
 *     **超绝对最大额定值**。⇒ 正端改接 3.3 V ⇒ 压差 0 ⇒ 彻底关断，且 MCU 只做 sink。
 *     （现场证据：`docs/audit/H723-MOTION-QUALITY-AUDIT.md` §8.7；细则见技能 `mcu-opto-input-bringup`）
 *
 * ★★★ 极性必须**显式声明**，没有"隐藏默认值"：
 *   `DCL_STEP_ENA_POL` 三态（-1 未配置 / 0 / 1）。TB6600 的 ENA 标注两种都有 ⇒
 *   未声明时 **fail-closed：拒绝使能 + 保持物理失能**，而不是"猜一个值"。
 *   ★ 血证（2026-09-16，现场"电机响但轴不动"）：隐藏默认 `0` 与实机接线相反 ⇒
 *     `ena(1)` 实际是**失能**，而 `ena(0)` / `step_stop_safe()` / "上电态"实际是**通电**
 *     ⇒ 「使能 / 失能 / 上电安全态」**三处同时反相**，现象与"驱动器坏了"完全同形。
 *     （A/B 与三处反相表：`H723-MOTION-QUALITY-AUDIT.md` §16）
 *   ★ 工业做法：极性是**接线属性**，用**板上跳线/DIP** 或**装置配置**显式给出
 *     （雷赛 DMC2210 有"板卡跳线设置"专章；ADLINK 限位 NO/NC 用板上 DIP）。
 *     本文件的编译期声明 + 运行期 `step_set_ena_pol()` 就是这两档的软件等价物。
 *
 * ★★★ 上电/停止态 = `DCL_STEP_STOP_HOLD` 决定，与极性**正交**：
 *   保力矩(1) ⇒ 通电；去使能(0) ⇒ 断电。**垂直轴去使能 = 掉力滑车** ⇒ 默认 1。
 *   ★ 旧注释曾写"PE8..PE11 全高 = 上电安全态" —— **那句与电气后果相反**：
 *     在 pol=1 下"高" = 光耦不导通 = **物理使能** ⇒ 上电即通电。
 *     真正与极性无关的硬保证只有一条：**`CC1E=0`（无脉冲）** ——
 *     没有脉冲，无论 ENA 怎么接、极性对不对，电机都不会转。
 *
 * ★★ 为什么不用"数脉冲"而用"控时间 + 编码器闭环"：
 *   TIM3 没有重复计数器(RCR)，软件无法精确数脉冲。而位置由 AS5600 闭环给出，
 *   所以本模块只暴露"**频率**"这一个口子 —— 走多远由闭环决定，不由脉冲数决定。 */
#include <stdint.h>

#define STEP_DIR_ACT   8u        /* PE8  → ACTUATOR[8] */
#define STEP_ENA_ACT   9u        /* PE9  → ACTUATOR[9] */
/* ★ 把 PE8..PE11 一起纳入 DO 面管辖: 好处是"**主动驱动成确定的电平**"而不是悬空
 *   (继电器模块的输入若悬空, 吸合与否不确定 —— 那才是真危险)。 */
#define STEP_DO_MASK   0x0F00u

/* ---- `step_set_ena()` 的返回码（对外可读；**不要**用哨兵值） ---- */
#define STEP_RC_OK            0u   /* 已执行 */
#define STEP_RC_ENAPOL_UNSET  1u   /* ★ fail-closed: ENA 极性未声明 ⇒ 拒绝使能，且保持物理失能 */

void     step_init(uint8_t *base);
void     step_tick(uint32_t tick_now);    /* 主循环调用: 限时截止 + "指令 vs 引脚实读"核对 */

void     step_set_rate(uint32_t hz);      /* 0 = 停脉冲 */
void     step_set_dir(uint32_t dir);
/* en=1 = "希望驱动器使能"。★ 未声明极性时 en=1 会被**拒绝**(返回 STEP_RC_ENAPOL_UNSET)；
 *   en=0（去使能）**永远允许** —— 拒绝一个"停机"请求没有任何安全收益。 */
uint32_t step_set_ena(uint32_t en);
uint32_t step_ena_pol_is_set(void);       /* 0 = 极性未声明（此时一律不许使能） */

/* ★ ENA 有效电平做成**显式声明**（调用本函数即视为"已声明"，相当于"插跳线"这个动作）:
 *   pol=0: 光耦导通(MCU 拉低) = 使能   pol=1: 光耦不导通(MCU 高) = 使能
 *   ★ 实测一次就能定，不必重烧固件；但**不设**时不许使能（fail-closed）。 */
void     step_set_ena_pol(uint32_t pol);
/* 停止/上电时是否**保持力矩**（按轴）。★ 垂直轴必须为 1（去使能 = 掉力滑车）。 */
void     step_set_stop_hold(uint32_t hold);
void     step_set_deadline_ms(uint32_t ms);  /* 0 = 不限时 */
/* 停脉冲 + 按 `stop_hold` 决定静止态（保力矩 ⇒ 通电 / 否则 ⇒ 物理去使能）。 */
void     step_stop_safe(void);

/* ══════════════════════════════════════════════════════════════════════════
 * ★★★ 运动能力面（**程序面**）—— `PLAN-step-motion-v1` Step 1
 *
 * ## 为什么需要它
 * `0x39 op=19` 是**诊断脚手架**（未进 SHM、无 `obs_anchor()`、不占能力位、③层不得依赖）
 * ⇒ ③层（DCL 程序）**够不到"运动"** ⇒ **"闭环步进的 DCL 程序"在架构上写不出来**。
 * （核实过的另一条路也不通：HIL 的 PWM 通道**共享同一个 TIM3**，但它只写 `CCR1` 占空比，
 *   `ARR` 由 `hil_init` 写死 1 kHz ⇒ ③层改不了频率。）
 *
 * ## 做法：把运动请求落在 `WIRE[12..15]`（**零语义扩展、零编译器改动**）
 * ★★★ 为什么**不是** `ACTUATOR[12..15]`（我第一版就是这么设计的，**被编译器推翻**）：
 *   `tools/dclc.py` 的 `OUTPUT <name> TO <dst> FROM <sig>` **只接受 `wire[<n>]`**
 *   —— **③层的输出面只有 `wire[]`**。写 `TO actuator[12]` 直接**编译失败**。
 *   而 `wire[]` 恰好是"引擎内部信号"的槽：**谁读它由服务方决定** ⇒ 正好是本模块要的语义。
 * - ★ 代价（如实登记）：`WIRE[]` 的作用域是**本程序**，任何把 `wire[12..15]` 当普通信号的
 *   route 都会与运动请求**抢槽** ⇒ **本程序内这 4 个槽必须只有一个写者**（判据 M7）。
 * - **单写者干净**：③层写 12..15；`step.c` 是 8/9 的唯一写者 —— 两边不重叠（红线 2）。
 *
 * ## ★★★ 运动源必须**显式**（否则会打坏既有的一切）
 * 如果本服务**无条件**把槽值应用到硬件，那么 `op=19 sub=1`（脚手架／**所有运动回归套件**）
 * 设的频率会被**每圈覆盖回 0** ⇒ 整个运动验收套件全废。
 * ⇒ `op=19 sub=13 arg=0` = **脚手架直控【默认，保持现状】**／`1` = **程序面**。
 * ⇒ 默认 0 ⇒ **既有行为零变化**。
 * ══════════════════════════════════════════════════════════════════════════ */
#define STEP_MOT_SLOT_RATE    12u   /* WIRE[12] 请求: 脉冲频率 Hz（0 = 停）*/
#define STEP_MOT_SLOT_DIR     13u   /* WIRE[13] 请求: 方向（>0.5 ⇒ 1）*/
#define STEP_MOT_SLOT_ENA     14u   /* WIRE[14] 请求: 使能（>0.5 ⇒ 1；未声明极性 ⇒ 被 fail-closed 拒）*/
#define STEP_MOT_SLOT_LIMIT   15u   /* WIRE[15] 请求: 限时 ms（0 = 不限）*/
#define STEP_MOT_SLOT_RATE_AP  64u  /* WIRE[64] 镜像: 已应用的频率（Hz）*/
#define STEP_MOT_SLOT_LIMIT_AP 65u  /* WIRE[65] 镜像: 已应用的限时余量（ms）*/
/* ★★★ 镜像为什么必须 >63：`dclc.py` 的**自动分配上限是 64**（wire[0..63]）⇒
 *   把镜像放在 16/17 会被 FB 自动分配**抢槽**（实测：`LT lo = wire[16]`、`LOGIC drive = wire[17]`）
 *   ⇒ 引擎每拍覆写 ⇒ 镜像失效。⇒ 约定 **64/65 为"固件保留观测槽"**，程序侧不要钉它们。
 *   ★ 请求槽 12..15 则**安全**：它们被 `OUTPUT` **显式钉住**，自动分配会跳过（dclc 的先钉后配）。 */
#define STEP_MOT_SRC_SCAFFOLD  0u
#define STEP_MOT_SRC_PROGRAM   1u

/* 运动服务 —— **主循环**调用（与 `step_tick` 同处）。只在槽值变化时动作。 */
void     step_service_motion(void);
/* ★★★ 2026-09-17: **冷启动登记**（`cold_start_reset()` 里调用）。
 *   本函数负责把"DO 面管辖掩码"(`OFF_CTRL_GPIO_MASK = STEP_DO_MASK`) 重新登记 ——
 *   它是"引擎管哪些引脚"的登记，而 `step_init()`（唯一设它的地方）**不在冷启动入口里**。
 *   ⇒ 修前：`0x13 RESET` 的 memset 把它清 0 ⇒ **DO 面不再驱动 PE8~PE11** ⇒
 *     `PE9` 恒 0 = 光耦导通 = **驱动器失能** ⇒ **所有运动都不工作**（实测症状：
 *     `ena=1/intent=1` 而 `PE9=0/IACT=0`，`GPIO_MASK=0x0000`）。
 *   ★ 这条纪律本项目写过："新增域必须登记到单一冷启动入口"（本函数里 mb/macro 都登记了，
 *     而 `GPIO_MASK` 是 step 模块设的 ⇒ **必须由 step 自己登记**）。 */
void     step_cold_reset(uint8_t *shm);   /* ★ 与 macro_reset(g_shm) 同范式：带 base */

void     step_set_motion_src(uint32_t src);
uint32_t step_motion_src(void);

extern volatile uint32_t g_motion_src;        /* 0 = 脚手架（默认）/ 1 = 程序面 */
extern volatile uint32_t g_motion_cmd_n;      /* 槽值**变化**次数（≠"每圈空转"）*/
extern volatile uint32_t g_motion_applied_n;  /* 真正下给硬件几次 */
extern volatile uint32_t g_motion_rej_n;      /* 被拒次数（主要是 fail-closed）*/

/* ══════════════════════════════════════════════════════════════════════════
 * ★★★ "走 N 个脉冲自停" —— **硬件计数，零中断/零 DMA/零 ITCM**
 *
 * 我原先把它列进"做不到"（TIM3 无 RCR + ITCM 已满 ⇒ 加不了"每脉冲一次"的中断）。
 * ★ 那条**结论错了**：不需要中断 —— 让**另一个定时器去数 TIM3 的更新事件**即可。
 *   `TIM4` 的 ITR 表含 **ITR2 = TIM3** ⇒ 配"外部时钟模式 1"后 `TIM4_CNT` 就是一个**脉冲计数器**
 *   （连线全在芯片内部，不占引脚、不用接线）。
 *
 * ★ 到点怎么停：**主循环**看 `TIM4_CNT >= goal` ⇒ 停脉冲（延迟 ≤1 圈 ≈0.37 ms）。
 *   ⇒ 会**多走几步**，但**那不是误差**：`TIM4_CNT` 精确记下"实际走了多少"，可读回、可补偿。
 *   闭环里"走 N 步"本来就是**粗定位**（精定位由编码器收尾）。★ v2 可做硬件精确停
 *   （`TIM4` 的 CC 比较事件 → 一次 DMA 把 0 写进 `TIM3_CCR1`），**不需要 MDMA**。
 * ★ 16 位 ⇒ 单次 ≤ 65535 步；超出**明确钳位**（不是静默）。
 * ══════════════════════════════════════════════════════════════════════════ */
#define STEP_RC_NOCOUNT 2u   /* ★ 当前没有脉冲在跑 ⇒ "走 N 步"无意义（拒绝，且可见） */
uint32_t step_set_remaining(uint32_t n);   /* 设目标步数并启动计数；0 = 取消 */
uint32_t step_pulses_now(void);            /* 硬件计数器快照 */
extern volatile uint32_t g_step_goal;
extern volatile uint32_t g_step_pulses;
extern volatile uint32_t g_step_count_en;
extern volatile uint32_t g_step_goal_done_n;   /* 到点自停次数 */
extern volatile uint32_t g_step_goal_abort_n;  /* 未到点（被限时/停机打断）次数 */
extern volatile uint32_t g_step_goal_rej_n;    /* 被拒次数 */

/* ══════════════════════════════════════════════════════════════════════════
 * ★★★ 轨迹规划（加减速 / 斜坡限幅）—— README 能力边界 **A 类第一项**
 *
 * ## 为什么这一条最值钱
 * 实测：**突加** 31000 Hz ⇒ 只能用到 **627 rpm**（比值 0.565，**掉步**）；
 *      **带斜坡** ⇒ **~1100 rpm**。⇒ **缺斜坡 = 可用转速被砍 47%**。
 *
 * ## 为什么放固件、而不是 ③ 层"凑"
 * 斜坡的本质是"**请求频率随时间变化，且变化率有上限**"。
 * 而**频率的写入口只有一个**（`step_set_rate`，来自脚手架 `sub=1` **和** 程序面 `wire[12]`）
 * ⇒ **在它里面加限幅，两条通路同时受益**；放 ③ 层则脚手架那条路受益不到。
 *
 * ## ★★★ 实现上唯一必须小心的地方：改 `ARR` 的方式
 * `TIM3` 已配 `ARPE`（ARR 预装载）与 `OC1PE`（CCR1 预装载）⇒ **写它们在下个更新事件才生效**
 * ⇒ 斜坡推进**只写 `ARR`/`CCR1`** 就够，**既不关 `CC1E`、也不写 `EGR.UG`**：
 *   · 关 `CC1E` 会**切断脉冲**（每个斜坡步都断一次 ⇒ 丢步）；
 *   · 写 `EGR.UG` 会**多产生一个更新事件** ⇒ 被 `TIM4` 记成**多一个脉冲**
 *     ⇒ 会把"走 N 个脉冲"的硬件计数**污染**。
 * ⇒ 所以斜坡走一条**轻量路径** `step_rate_apply_light()`，与"要停/要起"的完整路径分开。
 *
 * ## 默认关闭（`0`）⇒ **既有行为逐位不变**
 * 与"运动源"同款纪律：**新行为必须显式打开**。`op=19 sub=17 arg=Hz/s`（0 = 关）。
 * ══════════════════════════════════════════════════════════════════════════ */
void     step_set_ramp(uint32_t hz_per_s);     /* 0 = 关（立即生效到目标）*/
extern volatile uint32_t g_step_ramp_hz_s;     /* 斜率上限 Hz/s；0 = 关 */
extern volatile uint32_t g_step_rate_cmd;      /* 请求的目标频率（斜坡的终点）*/
extern volatile uint32_t g_step_rate_out;      /* 斜坡输出（已交给硬件的）*/
extern volatile uint32_t g_step_ramp_active;   /* 1 = 正在爬坡 */
extern volatile uint32_t g_step_ramp_done_n;   /* 到目标次数 */

extern volatile uint32_t g_step_rate_hz, g_step_dir, g_step_ena, g_step_owns_tim3;
extern volatile uint32_t g_step_deadline_tick, g_step_stop_n, g_step_arr, g_step_ccr1;
extern volatile uint32_t g_step_ena_pol;
extern volatile uint32_t g_step_dt_max;   /* step_tick 见过的最大 dt (拍) —— 诊断用 */

/* ★★★ 2026-09-17 新增：让"极性有没有声明 / 指令和实际对不对得上"变成**可读的量**。
 *   动机：本次事故里 `ena`(逻辑位) 与 `PE9`(引脚实读) 都在应答里，但**没有任何判据用它**
 *   ⇒ 逻辑位说"使能"、物理上却是"失能"，而两者都不是恒 0/1 ⇒ 不满足"空判据"特征 ⇒ 没人怀疑。
 *   ⇒ 现在把"不一致"本身变成计数器（PID 界的说法：这是 **fail-loud**，不是 fail-silent）。*/
extern volatile uint32_t g_step_ena_pol_set;     /* 0 = 极性未声明 */
extern volatile uint32_t g_step_stop_hold;       /* 1 = 停止/上电保持力矩 */
extern volatile uint32_t g_step_ena_rc;          /* 最近一次 step_set_ena 的返回码 */
extern volatile uint32_t g_step_ena_rej_n;       /* 被 fail-closed 拒绝的次数 */
extern volatile uint32_t g_step_ena_mismatch_n;  /* "指令 != 引脚实读" 的次数 */
extern volatile uint32_t g_step_ena_pin_intent;  /* 我们**意图**的 PE9 电平 (0/1) */
/* ★★★ 归因（2026-09-17）—— `mismatch_n ≡ hi_n + lo_n`；现场快照让"哪一半不对"可读。
 *   加它的直接原因是 327616 那个数**只说了"不对"，没说"谁把它弄成这样的"**。 */
extern volatile uint32_t g_step_ena_mismatch_hi_n;  /* 不一致且实读=1 ⇒ 被人拉高 */
extern volatile uint32_t g_step_ena_mismatch_lo_n;  /* 不一致且实读=0 ⇒ 被人拉低 */
extern volatile uint32_t g_step_mismatch_lmask;     /* 当刻**生效**掩码 (do 面同一函数) */
extern volatile uint32_t g_step_mismatch_lidr;      /* 当刻 GPIOE_IDR 低 16 位 */
extern volatile uint32_t g_step_mismatch_ltick;     /* 当刻拍号 */
#endif
