#include "step.h"
#include "itcm.h"   /* ★ DCL_ITCM —— step_tick_isr 进拍内需要 */
#include <stddef.h>
#include "regs.h"
#include "clock.h"
#include "engine.h"
#include "do.h"      /* DO_GPIO_PORT —— "指令 vs 引脚实读" 要读 GPIOE_IDR */

/* TIM3 计数时钟 = 1MHz (PSC = CLK_TIMXCLK_HZ/1e6 - 1)。
 * 步进频率 = 1MHz / (ARR+1) —— 50% 占空比由 CCR1 = (ARR+1)/2 给出。
 * 范围: ARR+1 最小 2 ⇒ 500kHz(远超需要); 最大 65536 ⇒ 15.3Hz。 */
#define STEP_TIMCLK_HZ  1000000u
#define TIM3  TIM3_BASE_ADDR      /* 与 hil.c 同一写法 (regs.h 只给了 *_BASE_ADDR) */
#define TIM4  TIM4_BASE_ADDR      /* ★ 脉冲计数器（从模式）—— 见 step.h 的"走 N 个脉冲"段 */

/* ══════════════════════════════════════════════════════════════════════════
 * ★★★ 出厂声明（PLAN-device-config-v1 的 P0 档）—— 见 step.h 顶部长注释
 *   DCL_STEP_ENA_POL  : -1 = **未配置** ⇒ fail-closed（拒绝使能 + 上电物理失能）
 *                        0 = 光耦导通(MCU 拉低) = 使能
 *                        1 = 光耦不导通(MCU 高) = 使能   ← ★ **本台实机**（实测）
 *   DCL_STEP_STOP_HOLD : 1 = 停止/上电**保持力矩**（默认；垂直轴必须）/ 0 = 去使能
 *   ★ 这两个量**正交**：极性只决定"怎么通电"，保不保力矩是按轴决定的安全策略。
 *   ★★ 为什么做成编译期声明而不是"代码里写死一个默认值"：
 *     工业界（雷赛 DMC2210 跳线 / ADLINK 限位 DIP / `8132.cfg`）把极性当**接线属性**
 *     显式给出 ⇒ 它必须是**可见、可查、必须声明**的东西，而不是藏在 .c 里的一个字面量。
 *     声明会出现在 `build.sh` 的"生效开关(读自 CMakeCache)"清单里。
 * ══════════════════════════════════════════════════════════════════════════ */
#ifndef DCL_STEP_ENA_POL
#define DCL_STEP_ENA_POL   (-1)    /* 缺省 = 未配置（**安全侧**：不许使能） */
#endif
#ifndef DCL_STEP_STOP_HOLD
#define DCL_STEP_STOP_HOLD 1       /* 缺省 = 保持力矩（保守：宁可发热，不要掉力滑车） */
#endif

volatile uint32_t g_step_rate_hz = 0u, g_step_dir = 0u, g_step_ena = 0u;
volatile uint32_t g_step_owns_tim3 = 1u;      /* ★ 让 hil 的输出臂让出 TIM3 */
volatile uint32_t g_step_deadline_tick = 0u, g_step_stop_n = 0u;
volatile uint32_t g_step_arr = 0u, g_step_ccr1 = 0u;
/* ★★ 极性 + "有没有声明" **必须分开两个量**：只有一个 u32 时表达不出"未配置"
 *    （用一个魔法值比如 0xFFFFFFFF 会被协议面/上位机当成"数值 0 极性"读走 ⇒ 又变成猜）。 */
volatile uint32_t g_step_ena_pol     = (uint32_t)((DCL_STEP_ENA_POL) < 0 ? 0 : ((DCL_STEP_ENA_POL) ? 1 : 0));
volatile uint32_t g_step_ena_pol_set = (uint32_t)((DCL_STEP_ENA_POL) < 0 ? 0 : 1);
volatile uint32_t g_step_stop_hold   = (uint32_t)((DCL_STEP_STOP_HOLD) ? 1 : 0);
volatile uint32_t g_step_ena_rc          = STEP_RC_OK;
volatile uint32_t g_step_ena_rej_n       = 0u;
volatile uint32_t g_step_ena_mismatch_n  = 0u;
volatile uint32_t g_step_ena_pin_intent  = 1u;   /* 上电默认意图 = 高(光耦不导通) */
/* ══════════════════════════════════════════════════════════════════════════
 * ★★★ 2026-09-17: `mismatch_n` 的**归因**（一个数变成"能指认谁"的一组数）
 *
 * 血证：`g_step_ena_mismatch_n` 曾涨到 **327616 后冻结**，当时只能读到这一个数 ⇒
 *   被解读成"有别的代码在驱 PE9"（听起来像野写者），实际是**上位机把 GPIO_MASK
 *   换成 0x00FF（`h723_do_hold_test.py`）/ 0x7FC00000（`h723_w1.py` 越界注入）**
 *   ⇒ DO 面不再驱 PE9 ⇒ 引脚停在旧电平。**真因离读数很远， потому就是读数没说清。**
 *
 * ⇒ 拆成三个**互斥**且**穷尽**的类别 + 三件现场快照：
 *      `mismatch_n ≡ hi_n + lo_n`（按实读电平分方向：是被人拉高还是拉低）
 *      `do_mask_step_drop_n`（do.c 独有）：上位机是否剔过保留位 —— **另问一个问题**，
 *        所以**不**并进上面那个和式（"一个计数只能回答一个问题"）。
 *    ★ 于是"PE9 不对"这句话，以后能直接读出**哪一半不对**、**当刻掩码长什么样**。
 * ══════════════════════════════════════════════════════════════════════════ */
volatile uint32_t g_step_ena_mismatch_hi_n = 0u;  /* 不一致且实读=1 ⇒ 有人把它拉**高** */
volatile uint32_t g_step_ena_mismatch_lo_n = 0u;  /* 不一致且实读=0 ⇒ 有人把它拉**低** */
volatile uint32_t g_step_mismatch_lmask    = 0u;  /* 不一致当刻的**生效掩码**(do 面同一函数) */
volatile uint32_t g_step_mismatch_lidr     = 0u;  /* 不一致当刻的 GPIOE_IDR 低 16 位 */
volatile uint32_t g_step_mismatch_ltick    = 0u;  /* 不一致当刻的拍号 */
volatile uint32_t g_step_dt_max = 0u;

/* ★★★ 运动能力面（程序面）—— 见 step.h 的说明。★ **默认脚手架 ⇒ 既有行为零变化**。 */
volatile uint32_t g_motion_src       = STEP_MOT_SRC_SCAFFOLD;
volatile uint32_t g_motion_cmd_n     = 0u;
volatile uint32_t g_motion_applied_n = 0u;
volatile uint32_t g_motion_rej_n     = 0u;

/* ★★★ "走 N 个脉冲"的账（硬件计数）—— 见 step.h */
volatile uint32_t g_step_goal        = 0u;   /* 目标步数（钳到 16 位）*/
volatile uint32_t g_step_pulses      = 0u;   /* 已走步数（TIM4_CNT 快照）*/
volatile uint32_t g_step_count_en    = 0u;   /* 计数中 */
volatile uint32_t g_step_goal_done_n = 0u;   /* 到点自停次数 */
volatile uint32_t g_step_goal_abort_n= 0u;   /* 因限时/停机而未到点的次数 */
volatile uint32_t g_step_goal_rej_n  = 0u;   /* 被拒（没有脉冲在跑 / 参数非法）*/
/* ★★ 2026-09-18（PLAN-completion 2.2）: 两个拍号 —— 让「实测时长 = 声明的 N/f」**可直接判定**
 *   （此前只能组合: 步数 ÷ 实现频率, 属间接证据）。
 *   ★ 精度边界: `arm_tick` 是**武装后第一次 step_tick** 记的 ⇒ 滞后 ≤1 主循环通过（~1.4 ms）,
 *     对 1 s 级时长是 0.14%; `stop_tick` 记在停脉冲那一刻, 无额外延迟。 */
volatile uint32_t g_step_arm_tick    = 0u;
volatile uint32_t g_step_stop_tick   = 0u;

/* ★★★ 轨迹规划（斜坡限幅）—— 见 step.h。★ **默认 0 = 关** ⇒ 既有行为逐位不变。 */
volatile uint32_t g_step_ramp_hz_s     = 0u;
volatile uint32_t g_step_rate_cmd      = 0u;
volatile uint32_t g_step_rate_out      = 0u;
volatile uint32_t g_step_ramp_active   = 0u;
volatile uint32_t g_step_ramp_done_n   = 0u;
/* ★★★ 2026-09-18: 斜坡推进的**余数累加器**（单位 = Hz·拍）。
 *   为什么需要它：修掉"向上取整到 1 ms"之后，`slope × dt / 10000` 对**慢斜率**会长期为 0
 *   （例如 100 Hz/s 在主循环 0.37 ms 下每次只有 0.0037 Hz）⇒ 必须把余数攒起来，
 *   否则慢斜率永远不动，而原来那个 `dv=1` 的兜底会把慢斜率**量化抬到 ~2.7 kHz/s**。
 *   ★ 定义也放进 `#if`：否则**对照档**（FIX=0）里它"定义了但没用到" ⇒ `-Werror` 直接编译失败
 *     ⇒ 那就没有对照可以比了（A/B 的第一版就栽在这）。 */
#if DCL_STEP_RAMP_FIX
static uint32_t s_ramp_acc = 0u;
#endif
/* ★★★ E-R (2026-09-18): **限时的拍余数累加器** —— 与斜坡同款, 但这是**另一处**语义。
 *
 * ## 缺陷（实测量化, `tools/exp_er_motion_time.py`）
 * 原实现是 `uint32_t dm = dt / 10u; if (dm == 0u) { return; }`
 * —— 把"拍→ms"做**整数截断**, 而 `step_tick` 由主循环每 **~0.37 ms（≈3.7 拍）** 调一次
 * ⇒ `floor(3.7/10) = 0` ⇒ **那一圈的时间被整块丢掉**, 只有偶尔的长圈才走 1 ms。
 *
 * | 条件 | 实测流逝率 | 声明 2000 ms 的限时实走 |
 * |---|---|---|
 * | **部署条件**（无上位机读） | **747 ms/s**（−25%） | **2677 ms** |
 * | 持续读串口 | **990 ms/s**（−1%） | 2020 ms |
 *
 * ⇒ 两条后果:
 *   ① 安全网（限时）在真实运行下比声明**松 34%**;
 *   ② **实现值依赖"有没有人在读串口"** —— 所有回归都在持续读, 于是它一直看起来是对的。
 *
 * ## 为什么是"同一个语义两处存放"的第二次
 * 正上方 20 行的**斜坡**路径在 2026-09-18 已经改成余数累加器（`s_ramp_acc`）,
 * 而**限时这一处没跟上** —— 与顺序档 dt 表（E-Q）**逐字同族**: 改了一处, 另一处留着。
 * ★ 全仓 `拍→ms` 的换算只有两处（本次已扫）: 斜坡的**对照分支**与这里。
 *
 * ## 修法
 * 把拍攒够 10 拍才换 1 ms, 余数留在累加器里 ⇒ **任意 dt 分布下都不丢时间**
 * （对照档仍是 `dt/10` 截断, 便于 A/B）。 */
static uint32_t s_dl_acc = 0u;

/* ★★★ 2026-09-18（④ 拍长可配）: 「拍 ↔ 秒/毫秒」的换算**从拍长派生**, 不许写死。
 *   原来斜坡写 `dv * 10000u`（"10000 拍 = 1 s"）、限时写 `s_dl_acc / 10u`（"10 拍 = 1 ms"）
 *   —— 两者都只在**拍长 = 100 µs** 时成立。拍长一改（这是规划中的事），
 *   **限时与斜坡会静默变成声明值的 1/N**，而且**没有任何断言会响**。
 *   ⇒ 现在由 `TICK_PERIOD_US` 派生; `clock.h` 的三条断言保证这两个除法是**精确**的
 *     （拍长必须整除 1e6 与 1000），否则当场编译失败。 */
#define STEP_TICKS_PER_S   (1000000u / (uint32_t)TICK_PERIOD_US)   /* 拍/秒 */
#define STEP_TICKS_PER_MS  (1000u    / (uint32_t)TICK_PERIOD_US)   /* 拍/毫秒 */
_Static_assert(STEP_TICKS_PER_S  >= 1u, "拍长过大: 1 秒不足 1 拍");
_Static_assert(STEP_TICKS_PER_MS >= 1u, "拍长过大: 1 毫秒不足 1 拍");

static uint8_t *s_base = NULL;
static uint32_t s_sync = 1u;   /* ★ 首次/重新武装后先对齐 s_last, 见 step_tick */

static void act_set(uint32_t idx, float v)
{
    if (s_base == NULL) { return; }
    *(volatile float *)(s_base + OFF_ACTUATOR_STATUS + idx * 4u) = v;
}

/* ★ 共阳接法下 "光耦导通 = MCU 拉低" ⇒ 逻辑 1 对应(不导通)= 高 = ACTUATOR 1.0f。
 *   ★★ 2026-09-17 删掉了一条**危险注释**：原文写"若与预期相反, 把本函数里置 0.0f/1.0f
 *     对调即可 (**两处**)" —— 那正是本项目反复吃亏的"**一个语义两处存放**"
 *     （改一处、漏一处 ⇒ 静默半失效）。现在极性的翻译**只出现在 `drive_ena()` 一处**，
 *     而"怎么改极性"变成**显式声明**（`DCL_STEP_ENA_POL` / `step_set_ena_pol()`），
 *     不需要、也不允许去改本函数。 */
static void act_bits(uint32_t idx, uint32_t active)
{
    act_set(idx, active ? 0.0f : 1.0f);
}

/* ══════════════════════════════════════════════════════════════════════════
 * ★★★ 不经极性翻译的**物理**动作：直接指定"光耦导通 / 不导通"。
 *   为什么必须有这一档（我第一版漏了它，是个真错误）：
 *     **极性未声明时，"使能/失能"这两个词本身是没有定义的** ——
 *     我们根本不知道哪个电平台阶对应哪个语义 ⇒ 那时**只能用物理语言说话**，
 *     若仍然走 `drive_ena(0)`（它内部按 pol 翻译，而 pol 此时是"未声明"的 0）
 *     就会算出一个**恰好相反**的引脚电平 ⇒ 所谓 fail-closed 反而把驱动器**通电**了。
 *   ★ 本项目的约定（TB6600 + 共阳 + 光耦正端 3.3V）：**光耦导通(MCU 拉低) = 失能**。
 *     ⇒ 未声明极性时取 **"导通"** 作为静止态。
 *   ★★ 这条依据来自**本驱动器**（`H723-MOTION-QUALITY-AUDIT` §6 的实测），
 *      换驱动器必须重新评估 —— 注意"安全态按器件"与"安全态按轴"是**两件事**：
 *        按器件: `导通 = 失能`（极性/接线语义）
 *        按轴  : 停止后要不要保力矩（`stop_hold`）
 *      ★ 与极性无关的**唯一**硬保证始终是 `CC1E=0`（无脉冲）。
 * ══════════════════════════════════════════════════════════════════════════ */
static void drive_opto(uint32_t conducting)
{
    g_step_ena_pin_intent = conducting ? 0u : 1u;   /* 导通 ⇒ 拉低 ⇒ PE9=0 */
    act_bits(STEP_ENA_ACT, conducting);
}

/* ══════════════════════════════════════════════════════════════════════════
 * ★★★ 脉冲计数器（`TIM4` 从模式）—— "走 N 个脉冲自停"的地基
 *
 * 为什么不是"每脉冲一次的中断"：TIM3 没有 RCR，而"每脉冲一次"的中断在 31 kHz 下
 * 会压垮拍预算；且 `gate_isr_itcm.py` 要求 ISR 住 ITCM 而 **ITCM 已 100% 占满**。
 * ⇒ 换一条**不需要 CPU 的路**：让**另一个定时器去数 TIM3 的更新事件**。
 *
 * 连线是**芯片内部**的（不用接线、不占引脚）：
 *   `TIM3_CR2.MMS = 010` ⇒ TIM3 的 **TRGO 输出 = 更新事件 UEV**（每个 PWM 周期一次 = 一个脉冲）
 *   `TIM4_SMCR = SMS(111 外部时钟模式1) | TS(010 = ITR2)` ⇒ **ITR2 = TIM3**（ST 通用 ITR 表）
 *   ⇒ **`TIM4_CNT` 每来一个 TIM3 更新事件 +1** ⇒ 它就是"已发多少个脉冲"。
 *
 * ★ 副作用：MMS 只影响 TRGO 这条内部线 ⇒ **对 TIM3 自身 PWM 输出无影响**。
 * ★ TIM4 不用任何通道引脚 ⇒ **GPIO 不用配**。
 * ★ TIM4 是 **16 位** ⇒ 单次目标上限 **65535 步**（40.9 圈 @1600 步/圈），超出**明确钳位**。
 * ══════════════════════════════════════════════════════════════════════════ */
static void step_count_init(void)
{
    RCC_APB1LENR |= RCC_APB1LENR_TIM4EN;

    TIM_CR1(TIM4)  = 0u;                           /* 先停 */
    TIM_PSC(TIM4)  = 0u;
    TIM_ARR(TIM4)  = 0xFFFFu;                      /* 满量程（自由上数，不回绕到 0）*/
    TIM_CNT(TIM4)  = 0u;
    TIM_SMCR(TIM4) = (7u << TIM_SMCR_SMS_SHIFT)    /* SMS=111 外部时钟模式 1 */
                   | (2u << TIM_SMCR_TS_SHIFT);    /* TS=010  ⇒ ITR2 = TIM3 */
    TIM_EGR(TIM4)  = TIM_EGR_UG;                   /* 立刻装载 PSC/ARR */

    /* TIM3 侧：把"更新事件"送上 TRGO（**只改 MMS 三位，其余位保留**）*/
    TIM_CR2(TIM3) = (TIM_CR2(TIM3) & ~(7u << TIM_CR2_MMS_SHIFT))
                  |  (2u << TIM_CR2_MMS_SHIFT);

    g_step_goal = 0u; g_step_pulses = 0u; g_step_count_en = 0u;
    __asm__ volatile("dsb" ::: "memory");
}

/** @brief 设目标步数并启动计数。n=0 ⇒ 取消。
 *  ★★ **要求当前有脉冲在跑**：没有脉冲时"走 N 步"没有意义（且会立刻判到点）⇒ **明确拒绝**，
 *     而不是"接受了但什么也没发生"（本项目纪律：拒绝必须可读）。 */
uint32_t step_set_remaining(uint32_t n)
{
    if (n == 0u) {                                  /* 取消 */
        TIM_CR1(TIM4) &= ~TIM_CR1_CEN;
        g_step_count_en = 0u; g_step_goal = 0u; g_step_pulses = TIM_CNT(TIM4);
        return STEP_RC_OK;
    }
    if (g_step_rate_hz == 0u) { g_step_goal_rej_n++; return STEP_RC_NOCOUNT; }

    TIM_CR1(TIM4) &= ~TIM_CR1_CEN;                  /* ① 停 */
    TIM_CNT(TIM4)  = 0u;                            /* ② 清零（必须在 CEN=1 之前）*/
    g_step_goal    = (n > 0xFFFFu) ? 0xFFFFu : n;   /* ★ 16 位上限，**明确钳位** */
    g_step_pulses  = 0u;
    g_step_count_en= 1u;
    __asm__ volatile("dsb" ::: "memory");
    TIM_CR1(TIM4) |= TIM_CR1_CEN;                   /* ③ 开始数 */
    return STEP_RC_OK;
}

uint32_t step_pulses_now(void) { return TIM_CNT(TIM4); }

/* ★ "希望驱动器使能" → 物理电平 的**唯一**翻译点（极性只在这里出现一次）
 *     conducing = (pol==0) ? energized : !energized      （conducing=1 ⇒ 光耦导通 ⇒ PE9=0）
 *   同时记下**我们意图的引脚电平** —— 它是"指令 vs 实读"核对的左半边。
 *   缺了它，就只有"实际"没有"指令"，于是"不一致"这件事**不可观测**。 */
static void drive_ena(uint32_t energized)
{
    uint32_t conducing = (g_step_ena_pol == 0u) ? (energized ? 1u : 0u)
                                                : (energized ? 0u : 1u);
    drive_opto(conducing);
}

/* ★★ 静止态（上电 / 停止 / 限时到期）该处于什么电气状态？
 *      · 极性**未声明** ⇒ **光耦导通**（= 本驱动器的失能态；见 `drive_opto()` 的说明）
 *      · 已声明 ⇒ 由按轴策略 `stop_hold` 决定（1 = 保持力矩 ⇒ 通电）
 *   ★ 这一条把"上电态"从"极性顺带决定"里**解耦**出来：
 *     垂直轴去使能 = 掉力滑车 ⇒ 必须能"停脉冲但保力矩"。 */
DCL_ITCM static void apply_rest_ena(void)
{
    if (g_step_ena_pol_set == 0u) { g_step_ena = 0u; drive_opto(1u); return; }
    uint32_t en = g_step_stop_hold ? 1u : 0u;
    g_step_ena = en;
    drive_ena(en);
}

void step_init(uint8_t *base)
{
    s_base = base;

    /* ① 接通 DO 面: 掩码就是"哪些引脚归引擎管"。此前**没有任何代码写它(恒 0)**
     *    ⇒ do_poll 每拍早退, DO 面从来没被驱动过 (engine.c 自己也标着"当前不可达")。
     *    ★ 现在把 PE8..PE11 交出去 ⇒ DIR/ENA/继电器 才真的能动。 */
    if (base != NULL) {
        SHM_U32(base, OFF_CTRL_GPIO_MASK) = STEP_DO_MASK;
    }

    /* ② 把四个脚驱动成**确定的电平**（不悬空 —— 继电器输入若悬空, 吸不吸合不确定,
     *    那才是真危险）, 必须在使能 TIM3 之前。
     *    ★★★ 2026-09-17 更正: 原文写"先把四个脚驱动成'不导通'(高) —— 这是**上电安全态**"。
     *      那句话**与电气后果相反**: 本接线(pol=1)下"高" = 光耦不导通 = **物理使能**
     *      ⇒ 上电即通电(现场所见"电机自己在响"), 而轴因 CC1E=0 不转。
     *      真正与极性无关的硬保证只有 **`CC1E=0`(无脉冲)**（见 ③）。
     *    ⇒ 现在: DIR 及其余脚 → 高(确定电平); **ENA 走 `apply_rest_ena()`** ——
     *      极性未声明 ⇒ 物理失能; 已声明 ⇒ 由 `stop_hold`(按轴) 决定保力矩/去使能。 */
    for (uint32_t i = 8u; i < 12u; i++) { act_set(i, 1.0f); }
    g_step_dir = 0u;
    apply_rest_ena();

    /* ③ TIM3: PSC 定 1MHz; **通道先关掉(无脉冲)**; 只有 step_set_rate 才会打开 */
    RCC_APB1LENR |= RCC_APB1LENR_TIM3EN;
    TIM_PSC(TIM3)   = (uint32_t)(CLK_TIMXCLK_HZ / STEP_TIMCLK_HZ) - 1u;
    TIM_ARR(TIM3)   = 1000u - 1u;                 /* 名义 1kHz, 但通道关着 ⇒ 不出脉冲 */
    TIM_CCR1(TIM3)  = 500u;
    TIM_CCER(TIM3) &= ~TIM_CCER_CC1E;             /* ★★ 无脉冲 = 上电安全 */
    TIM_CR1(TIM3)   = TIM_CR1_ARPE | TIM_CR1_CEN;
    TIM_EGR(TIM3)   = TIM_EGR_UG;
    g_step_arr = TIM_ARR(TIM3); g_step_ccr1 = TIM_CCR1(TIM3);
    g_step_rate_hz = 0u;

    /* ④ 脉冲计数器（TIM4 从模式）—— 见 step.h 的"走 N 个脉冲"段 */
    step_count_init();

    __asm__ volatile("dsb" ::: "memory");
}

/* ══════════════════════════════════════════════════════════════════════════
 * 频率落地的**两条路径**（分开的原因见 step.h 的"轨迹规划"段）
 *   ① `step_rate_apply`       —— **完整**：会动 `CC1E`（起/停/换频率时用）
 *   ② `step_rate_apply_light` —— **轻量**：只写预装载寄存器（斜坡推进用）
 *      ★ 不关 `CC1E`、不写 `EGR.UG` ⇒ **既不切断脉冲，也不多产生更新事件**
 *        （后者会污染"走 N 个脉冲"的硬件计数 `TIM4_CNT`）
 * ══════════════════════════════════════════════════════════════════════════ */
DCL_ITCM static void step_rate_apply(uint32_t hz)
{
    if (hz == 0u) {
        TIM_CCER(TIM3) &= ~TIM_CCER_CC1E;         /* 停脉冲 */
        g_step_rate_hz = 0u;
        __asm__ volatile("dsb" ::: "memory");
        return;
    }
    uint32_t arr1 = STEP_TIMCLK_HZ / hz;          /* ARR+1 */
    if (arr1 < 2u)      { arr1 = 2u; }            /* 上限 500kHz */
    if (arr1 > 65536u)  { arr1 = 65536u; }        /* 下限 15.3Hz */
    TIM_CCER(TIM3) &= ~TIM_CCER_CC1E;             /* ★ 先关再改, 避免半个脉冲 */
    __asm__ volatile("dsb" ::: "memory");
    TIM_ARR(TIM3)  = arr1 - 1u;
    TIM_CCR1(TIM3) = arr1 / 2u;                   /* 50% 占空比 */
    TIM_EGR(TIM3)  = TIM_EGR_UG;
    TIM_CCER(TIM3) |= TIM_CCER_CC1E;              /* 开脉冲 */
    g_step_arr = TIM_ARR(TIM3); g_step_ccr1 = TIM_CCR1(TIM3);
    g_step_rate_hz = STEP_TIMCLK_HZ / arr1;       /* ★ 回报**实际**频率, 不是请求值 */
    __asm__ volatile("dsb" ::: "memory");
}

/** @brief **斜坡专用**：只改预装载寄存器（`ARPE`/`OC1PE` ⇒ 下个更新事件生效）。
 *  ★★ `hz==0` 时退回完整路径（要真的关掉 `CC1E` 才能停）。 */
DCL_ITCM static void step_rate_apply_light(uint32_t hz)
{
    if (hz == 0u) { step_rate_apply(0u); return; }
    uint32_t arr1 = STEP_TIMCLK_HZ / hz;
    if (arr1 < 2u)      { arr1 = 2u; }
    if (arr1 > 65536u)  { arr1 = 65536u; }
    TIM_ARR(TIM3)  = arr1 - 1u;                   /* 预装载 ⇒ 下个更新事件生效 */
    TIM_CCR1(TIM3) = arr1 / 2u;                   /* 预装载 ⇒ 不会出现半个脉冲 */
    /* ★★★ 2026-09-17 修（实测抓到）：**从静止起步时 `CC1E` 还是关的** ——
     *   轻量路径只写预装载寄存器，**不碰 `CC1E`**，于是"ARR 改了、一根脉冲都没发"：
     *   实测 `sub=17(斜坡)+sub=1(10000)` ⇒ `out=10000 / actual=10000` 而 **`CC1E=0`**。
     *   现象极具迷惑性：**两个软件量都对，引脚上什么都没有**。
     *   ⇒ 这里补一句"若通道关着就先开"（仍然**不写 `EGR.UG`** ⇒ 不产生额外更新事件）。*/
    if ((TIM_CCER(TIM3) & TIM_CCER_CC1E) == 0u) {
        TIM_CCER(TIM3) |= TIM_CCER_CC1E;
    }
    g_step_arr = TIM_ARR(TIM3); g_step_ccr1 = TIM_CCR1(TIM3);
    g_step_rate_hz = STEP_TIMCLK_HZ / arr1;
    __asm__ volatile("dsb" ::: "memory");
}

/* ★★★ 加减速（斜坡限幅）：**所有**频率写入口都经过这里 ⇒ 两条通路（脚手架 `sub=1` /
 *   程序面 `wire[12]`）**同时**获得限幅。斜坡**关**（默认 0）或**要求停**（hz==0）
 *   ⇒ 立即生效 —— 停必须是立即的（安全语义），不能"慢慢降到 0"。 */
DCL_ITCM void step_set_rate(uint32_t hz)
{
    g_step_rate_cmd = hz;
    if (g_step_ramp_hz_s == 0u || hz == 0u) {
        g_step_rate_out    = hz;
        g_step_ramp_active = 0u;
        step_rate_apply(hz);
    }
    /* 斜坡开且 hz!=0 ⇒ 只记目标，由 `step_tick` 每个主循环推进一步 */
}

DCL_ITCM void step_set_dir(uint32_t dir) { g_step_dir = dir ? 1u : 0u; act_bits(STEP_DIR_ACT, g_step_dir); }

/** @brief 设斜坡斜率（Hz/s）。**0 = 关**（立即生效到目标）—— 默认就是 0。
 *  ★ 打开后**立即把输出对齐到当前硬件频率**，避免"上一段的残留"被当成爬坡起点。 */
void step_set_ramp(uint32_t hz_per_s)
{
    g_step_ramp_hz_s = hz_per_s;
    if (hz_per_s == 0u) {
        /* 关斜坡：立刻把输出拉到目标（如果有目标）*/
        g_step_ramp_active = 0u;
        g_step_rate_out    = g_step_rate_cmd;
        step_rate_apply(g_step_rate_cmd);
    } else {
        g_step_rate_out    = g_step_rate_hz;   /* ★ 以**当前硬件频率**为起点 */
        g_step_ramp_active = (g_step_rate_out != g_step_rate_cmd) ? 1u : 0u;
    }
}

/* ENA: 参数是"**希望驱动器使能**", 与物理电平之间隔一层极性（翻译只在 `drive_ena()` 里）。
 * ★★★ fail-closed: **极性未声明时拒绝 en=1**，并**保持物理失能**。
 *   为什么必须"拒绝"而不是"用一个默认极性"：本次事故的形态就是
 *   "**看起来接受了, 其实跑错**"(无报错、只有值是错的) —— 那是三者里最坏的一种。
 *   ★ en=0（去使能）**永远允许**：拒绝一个停机请求没有任何安全收益。 */
DCL_ITCM uint32_t step_set_ena(uint32_t en)
{
    if (en != 0u && g_step_ena_pol_set == 0u) {
        g_step_ena = 0u;
        g_step_ena_rej_n++;
        /* ★ 未声明极性 ⇒ 只能用**物理**语言: 光耦导通 = 本驱动器的失能态。
         *   （不能走 `drive_ena(0)` —— 它按一个"未声明"的 pol 翻译, 会算出相反的电平。） */
        drive_opto(1u);
        g_step_ena_rc = STEP_RC_ENAPOL_UNSET;
        return STEP_RC_ENAPOL_UNSET;
    }
    g_step_ena = en ? 1u : 0u;
    drive_ena(g_step_ena);
    g_step_ena_rc = STEP_RC_OK;
    return STEP_RC_OK;
}

uint32_t step_ena_pol_is_set(void) { return g_step_ena_pol_set; }

/* ★ 调用即视为"**显式声明**" —— 这就是软件里的"插跳线"动作（工业上那一档是板上 DIP）。
 *   声明之前 `ena(1)` 一律被拒（见上）。 */
void step_set_ena_pol(uint32_t pol)
{
    g_step_ena_pol = pol ? 1u : 0u;
    g_step_ena_pol_set = 1u;
    step_set_ena(g_step_ena);
}

/* ★ 按轴策略: 停止/上电时是否保持力矩。★ 改这个要**重新评估垂直轴**（去使能 = 掉力滑车）。
 *
 * ★★★ 2026-09-17 修（我自己的缺陷）：原来只改 `g_step_stop_hold` 就返回 ⇒ **引脚不变**。
 *   实测：`sub=12 arg=0`（改为"休息态断电"）之后读回 `PE9` 仍是 **1（通电）** ——
 *   因为当前静止态是上一次 `step_stop_safe()` 按**旧**策略算出来的。
 *   ⇒ 这正是本项目最恨的**"看着设了、其实没设"**（与 `enapol` 默认值同族）。
 *   ⇒ 现在：**若当前没有脉冲在跑（即处于静止态），立刻按新策略重算**，
 *     与 `step_set_ena_pol()` 的做法保持一致（极性改了也立刻重新作用）。 */
void step_set_stop_hold(uint32_t hold)
{
    g_step_stop_hold = hold ? 1u : 0u;
    if (g_step_rate_hz == 0u) { apply_rest_ena(); }   /* 正在静止 ⇒ 立即生效 */
}

DCL_ITCM void step_set_deadline_ms(uint32_t ms)
{
    g_step_deadline_tick = ms;      /* 存"剩余毫秒"; step_tick 按拍差递减 */
    s_dl_acc = 0u;                  /* ★ 余数清零: 否则上一段的 ≤9 拍会漏进新限时 */
    s_sync = 1u;                    /* ★ 必须: 否则下一次 step_tick 用旧 s_last 算出巨大 dt */
}

/* ★★ 安全停止 = **停脉冲** + 进入"按轴决定"的静止态。
 *   ★★★ 关键更正（审计缺陷 D1）: 原实现是 `step_set_ena(0)` —— 即"一停就失能"。
 *     在**垂直轴上这等于掉力滑车**；在水平轴上也让"停机位置"从微步分辨率
 *     退化到整步齿槽（0.225° → 1.8°）。⇒ 现在由 `stop_hold` 决定：
 *       `stop_hold=1` ⇒ **保持力矩**（只停脉冲，驱动器继续通电）
 *       `stop_hold=0` ⇒ 物理去使能（省热；仅水平轴且已评估时用）
 *   ★ 与极性无关的硬保证始终是 **`CC1E=0`（无脉冲）** —— 没有脉冲电机就不会转。 */
DCL_ITCM void step_stop_safe(void)
{
    step_set_rate(0u);
    apply_rest_ena();
    g_step_deadline_tick = 0u;
    g_step_stop_n++;
}

/* 限时截止 —— 用 **g_tick_count 的拍差**换算毫秒, 而不是数"自己被调了几次":
 *   主循环一圈的耗时并不固定 (AS5600 轮询、协议、黑匣子都不等长),
 *   "数调用次数"会把截止时间拉长到无法预期 (实测 AS5600 轮询让 10ms 慢成 10.75ms)。
 *   ★ 拍差是硬件时基, 与主循环快慢无关 ⇒ 5 秒就是 5 秒。 */
/* ★ 主循环调用（**只做诊断**）：控制部分已搬进拍内，见 `step_tick_isr`。
 *   ★ 为什么诊断留主循环：它要与 DO 面**已经写下去的**电平比 —— 拍内同序时可能还没写。 */
void step_diag_tick(uint32_t tick_now)
{
    /* ══════════════════════════════════════════════════════════════════════
     * ★★★ "指令 vs 引脚实读" 核对 —— 对齐成熟运动控制的 `SVON` 输出 + `RDY` 输入，
     *     以及 PLCopen `MC_Power` 的"请求使能后**等 Statusword 到位**"。
     *   判据 = **我们意图的 PE9 电平**(`g_step_ena_pin_intent`) vs **IDR 里 PE9 的真电平**。
     *   ★ 为什么放这里、而不是在 `drive_ena()` 里立刻比：
     *     引脚是由 **DO 面在拍上**真正写下去的（`do_poll` 读 ACTUATOR → 写 GPIOE_ODR），
     *     `drive_ena()` 返回时读到的是**旧值** ⇒ 立刻比会**必然误报**。
     *     本函数跑在主循环，ISR 已经把本拍的电平写下去了。
     *   ★ 这是**诊断量**、不是控制量 ⇒ 放主循环不违"不得把实时量挂主循环"。
     *   ★ 连续 2 次不一致才计数：DO 面与本函数之间可能差一个循环，单次会有瞬态。
     *   ★ 为什么值得加：本次事故里 `ena`(逻辑位) 与 `PE9`(实读) **都**在应答里，
     *     但没有任何判据比较它们 ⇒ "逻辑说明使能、物理上是失能"这件事**不可观测**，
     *     于是它藏了一整天。⇒ 现在"不一致"本身是一个会涨的计数器。
     * ══════════════════════════════════════════════════════════════════════ */
    {
        static uint32_t s_prev_bad = 0u;
        uint32_t actual = (GPIO_IDR(DO_GPIO_PORT) >> 9) & 1u;
        if (actual != g_step_ena_pin_intent) {
            if (s_prev_bad != 0u) {
                g_step_ena_mismatch_n++;
                /* ★ 归因 + 现场快照：让"PE9 不对"这句话能指认方向与当刻掩码。
                 *   ★ 掩码取自 `do_mask_effective()`（**与 do_poll 用的同一个函数**）——
                 *     若这里自己再算一遍 SHM 字段，就会出现"读回来的"与"用上去的"分叉，
                 *     那正是本项目"一个绑定跨两个寄存器"的老族。 */
                if (actual != 0u) { g_step_ena_mismatch_hi_n++; }
                else              { g_step_ena_mismatch_lo_n++; }
                g_step_mismatch_lmask = do_mask_effective();
                g_step_mismatch_lidr  = (uint32_t)GPIO_IDR(DO_GPIO_PORT) & 0xFFFFu;
                g_step_mismatch_ltick = tick_now;
            }
            s_prev_bad = 1u;
        } else {
            s_prev_bad = 0u;
        }
    }
}

/* ══════════════════════════════════════════════════════════════════════════
 * ★★★ 拍内调用（2026-09-21）—— 运动控制的下半段：到点自停 / 斜坡推进 / 限时截止
 *
 * ## 为什么必须进拍（这是"有效控制周期"的缺口）
 *   这三件事原来由**主循环**调用 ⇒ **有效控制周期 = 主循环周期**，而不是拍长。
 *   实测（上机，`0x39 op=19` 的 `+64`）: `g_step_dt_max` = **387 拍 = 38.7 ms**，
 *   而拍长只有 **100 µs** ⇒ **387×**。
 *   ⇒ 后果：`wire[]` 里的闭环输出最多要等 38.7 ms 才落到 TIM3。
 *     项目自己查明的"**控制周期 ⇒ 极限环**"机制，其周期量取的就是这个值，不是 100 µs。
 *
 * ## 进拍后的额外收益
 *   每拍被调一次 ⇒ `dt` **恒为 1** ⇒ 斜坡与限时都变成**精确的拍计数**；
 *   主循环抖动不再进入时间量（`s_dl_acc` 余数累加器仍在，但不再承重）。
 *
 * ## 与 `IO_IN_ISR` 的关系
 *   复用既有的 A/B 开关（`IO_IN_ISR=0` = 改前行为：全部由主循环驱动）。
 *   不新增开关 —— 语义本来就是"**I/O 驱动位置**"。
 * ══════════════════════════════════════════════════════════════════════════ */
DCL_ITCM void step_tick_isr(uint32_t tick_now)
{
    static uint32_t s_last = 0u;

    /* ═══ ★★★ "走 N 个脉冲"到点自停 ═══
     * 判据 = **硬件计数器** `TIM4_CNT`（不是估算）⇒ "实际走了多少"永远可读回。
     * ★ 停脉冲在主循环 ⇒ 会**多走几步**。**那不是误差，是已知量**：
     *   `g_step_pulses` 记下真实步数，闭环的精定位本来就由编码器收尾。
     * ★★★ 2026-09-18（E-S 更正）: 原注释写"多走 ≤1 圈（≈0.37 ms）"——
     *   **0.37 ms 是名义周期, 不是上界**。真正的界是**本次停脉冲前主循环的最大间隔**
     *   （可读: `g_loop_gap_max`）。实测（f=5000 Hz, 声明 40 ms）:
     *   名义过冲 0~27 ms; 注入一次大块读（阻塞 ~100 ms）⇒ 过冲 **72~104 ms**（282 倍）。
     *   ⇒ 设计规则: 要求过冲 ≤ k 步 ⇒ `f ≤ k / 主循环最大间隔`。详见 `docs/exp-ES-step-duration.md`。
     * ★ 与"限时"的关系：限时先到会走 `step_stop_safe()` ⇒ 这里把 goal 判为"未到点"（abort）
     *   ⇒ 两种停法**可区分**（否则"到点"和"被限时打断"会混成一个计数）。
     * ★★ 2026-09-18（PLAN-completion 2.2）: **补两个拍号**（`g_step_arm_tick`/`g_step_stop_tick`）
     *   —— 在此之前"声明的时长 N/f"只能**组合**出来（步数 ÷ 频率），无法独立判定；
     *   有了这两个拍号，「实测时长 = N/f」第一次成为**可直接量**的量（关掉 E-S 的最后一个 SKIP）。
     *   ★ 精度边界（如实标注）: 武装拍号是**武装后第一次 step_tick** 记的 ⇒ 滞后 ≤1 个主循环
     *     通过（~1.4 ms）; 停止拍号记在停脉冲**那一刻**（同函数内，无额外延迟）。 */
    {
        static uint32_t s_goal_prev = 0u;
        if (g_step_count_en && !s_goal_prev) { g_step_arm_tick = tick_now; }
        s_goal_prev = g_step_count_en;
    }
    if (g_step_count_en) {
        uint32_t p = TIM_CNT(TIM4);
        g_step_pulses = p;
        if (p >= g_step_goal) {
            TIM_CR1(TIM4) &= ~TIM_CR1_CEN;
            g_step_count_en = 0u;
            g_step_stop_tick = tick_now;          /* ★ 停脉冲那一刻的拍号 */
            step_set_rate(0u);
            g_step_goal_done_n++;
        } else if (g_step_rate_hz == 0u) {          /* 脉冲被别人停了 ⇒ 未到点 */
            TIM_CR1(TIM4) &= ~TIM_CR1_CEN;
            g_step_count_en = 0u;
            g_step_stop_tick = tick_now;          /* ★ 未到点也记（两种停法共用拍号, 由计数区分） */
            g_step_goal_abort_n++;
        }
    }

    /* ★★★ 2026-09-15 踩坑: s_last 初值 0 ⇒ **首次调用** 的 dt = g_tick_count 本身 (可能已经
     *   几十万), 除以 10 后把整个限时**一次吃光** ⇒ 脉冲刚起就被停。
     *   症状极具迷惑性: 寄存器显示"起过脉冲", 拉长看却是"1 秒内就没了";
     *   而它同时让"轴不动"的结论**失效** —— 因为脉冲几乎没输出过。
     *   ⇒ 首次 (或重新武装后) 必须先对齐一次再计时。 */
    if (s_sync) { s_last = tick_now; s_sync = 0u; return; }
    uint32_t dt = tick_now - s_last;        /* 单位: 拍 (1 拍 = 100µs) */
    s_last = tick_now;
    if (dt > g_step_dt_max) { g_step_dt_max = dt; }
    /* ★★★ 钳位: 单次 dt 若远超一个合理的"主循环一圈"(>1000 拍 = 100ms), 说明要么是
     *   首次调用 (s_last 还是初值 0), 要么主循环刚被长阻塞过 (SD 刷盘/落盘)。
     *   不钳位的话那一次 `dm = dt/10` 会**一口吃掉整个限时** —— 现象就是"脉冲刚起就停",
     *   而寄存器读回来完全正常 (起过脉冲、频率也对), 极难查。
     *   ★ 钳到 100ms 是取舍: 宁可让限时**稍微延长**, 也不要让它被瞬间吃光
     *     (前者最多晚停 100ms, 后者等于没有限时保护)。 */
    if (dt > 1000u) { dt = 1000u; }

    /* ═══ ★★★ 加减速：斜坡推进（**必须在下面 `deadline_tick==0 ⇒ return` 之前**：
     *   "不限时"时也要能爬坡）═══
     *   · 每圈最多变 `斜率(Hz/s) × dt(ms) / 1000`，至少 1 Hz（否则 dt 小时永远不动）；
     *   · 输出与目标相等 ⇒ 停（`g_step_ramp_active=0`）并记一次"到目标"；
     *   · ★ 落地走**轻量路径**（只写预装载寄存器）⇒ 不切断脉冲、不产生额外更新事件
     *     ⇒ **不污染"走 N 个脉冲"的硬件计数**。 */
    if (g_step_ramp_hz_s != 0u && g_step_rate_out != g_step_rate_cmd) {
#if DCL_STEP_RAMP_FIX
        /* ★★★ 2026-09-18 修复：原来的 `dt_ms = (dt + 9u) / 10u` 把"拍→ms"**向上取整到 ≥1 ms**，
         *   而本函数由**主循环**每 **~0.37 ms（≈3.7 拍）** 调一次 ⇒ `dt_ms` 被算成 1 ms
         *   而真实间隔只有 0.37 ms ⇒ **每次多爬 2.7 倍** ⇒ 声明 12000 Hz/s 实测 **~32 kHz/s**。
         *   血证（四个独立量互证，见 MEMORY §5.31）：位置域半周期位移比 **升1.40×/降0.43×**、
         *   T5 累积均值 **686 ≈ 方波(0.15A↔A)均值**、`sub=19` 的 `rate_out` **几乎无中间值**、
         *   反认最佳斜率 **30000**（≈12000×2.7）。
         *   ⇒ 改成按**真实 `dt`（单位=拍，10 kHz）**算，并用**余数累加器**保证任意斜率都不丢精度
         *     （原来那个 `if (dv == 0u) dv = 1u;` 会把慢斜率量化抬到 ~2.7 kHz/s 的下限）。 */
        s_ramp_acc += (uint32_t)((uint64_t)g_step_ramp_hz_s * (uint64_t)dt);
        uint32_t dv = s_ramp_acc / STEP_TICKS_PER_S;  /* ★ 派生: 原来是写死的 10000 */
        s_ramp_acc -= dv * STEP_TICKS_PER_S;
#else
        uint32_t dt_ms = (dt + 9u) / 10u;              /* 拍→ms，向上取整 */
        uint32_t dv    = (g_step_ramp_hz_s * dt_ms) / 1000u;
        if (dv == 0u) { dv = 1u; }
#endif
        if (g_step_rate_out < g_step_rate_cmd) {
            uint32_t t = g_step_rate_out + dv;
            g_step_rate_out = (t > g_step_rate_cmd) ? g_step_rate_cmd : t;
        } else {
            uint32_t t = (g_step_rate_out > dv) ? (g_step_rate_out - dv) : 0u;
            g_step_rate_out = (t < g_step_rate_cmd) ? g_step_rate_cmd : t;
        }
        step_rate_apply_light(g_step_rate_out);
        g_step_ramp_active = (g_step_rate_out != g_step_rate_cmd) ? 1u : 0u;
        if (g_step_ramp_active == 0u) { g_step_ramp_done_n++; }
    }

    if (g_step_deadline_tick == 0u) { s_dl_acc = 0u; return; }
    /* ★★★ E-R (2026-09-18): 拍 → 毫秒 —— **必须带余数累加器**（见 s_dl_acc 处的长注释）。
     *   原写法 `dm = dt / 10u; if (dm == 0u) return;` 在主循环 ~3.7 拍/圈下**每圈丢 0.37 ms**
     *   ⇒ 实测流逝率 747 ms/s（部署条件）而不是 1000。 */
    s_dl_acc += dt;
    uint32_t dm = s_dl_acc / STEP_TICKS_PER_MS;
    if (dm == 0u) { return; }               /* 还不够 1 ms ⇒ 余数留着, 下圈继续攒 */
    s_dl_acc -= dm * STEP_TICKS_PER_MS;
    if (g_step_deadline_tick > dm) { g_step_deadline_tick -= dm; }
    else { g_step_deadline_tick = 0u; step_stop_safe(); }
}

/* ══════════════════════════════════════════════════════════════════════════
 * ★★★ 运动能力面（程序面）—— `PLAN-step-motion-v1` Step 1
 *   ③层写 `ACTUATOR[12..15]` → 本服务把它落到硬件。见 step.h 顶部的完整理由。
 * ══════════════════════════════════════════════════════════════════════════ */

/* ★ 运动请求住在 **WIRE** 而不是 ACTUATOR —— 理由见 step.h（③层的输出面只有 `wire[]`）。 */
DCL_ITCM static float wire_get(uint32_t idx)
{
    if (s_base == NULL) { return 0.0f; }
    return *(volatile float *)(s_base + OFF_WIRE_MAP + idx * 4u);
}
DCL_ITCM static void wire_set(uint32_t idx, float v)
{
    if (s_base == NULL) { return; }
    *(volatile float *)(s_base + OFF_WIRE_MAP + idx * 4u) = v;
}

/* ★★★ 请求值缓存 + **失效函数** —— 切换运动源时必须失效（见 step_service_motion 的说明）。 */
static uint32_t s_mot_hz = 0xFFFFFFFFu, s_mot_dir = 0xFFFFFFFFu;
static uint32_t s_mot_en = 0xFFFFFFFFu, s_mot_lim = 0xFFFFFFFFu;
static void motion_cache_invalidate(void)
{
    s_mot_hz = 0xFFFFFFFFu; s_mot_dir = 0xFFFFFFFFu;
    s_mot_en = 0xFFFFFFFFu; s_mot_lim = 0xFFFFFFFFu;
}

/* ★★★ 见 step.h：冷启动登记 DO 面管辖掩码（否则 `0x13 RESET` 后运动全废）。*/
void step_cold_reset(uint8_t *shm)
{
    /* ★★★ 语义: "上电/任何清零路径之后, DO 面管辖掩码应回到 STEP_DO_MASK"。
     *   为什么必须由 step 自己登记: 该掩码**只有 step 模块设**（`step_init`），
     *   而 `step_init` **不在冷启动入口里** ⇒ `0x13 RESET` 的 memset 清 0 后永不恢复。
     *   修前实测: `0x13 RESET` ⇒ `GPIO_MASK=0x0000` ⇒ DO 面不驱动 PE8~PE11 ⇒
     *   `PE9` 恒 0 = 光耦导通 = **驱动器失能** ⇒ **所有运动都不工作**。
     *   ★ 用 `shm` 入参而不是隐藏的 `g_shm`（engine.h 的范式：所有引擎函数带 base）。 */
    if (shm == NULL) { return; }
    *(volatile uint32_t *)(shm + OFF_CTRL_GPIO_MASK) = STEP_DO_MASK;
    __asm__ volatile("dsb" ::: "memory");
}

void     step_set_motion_src(uint32_t src)
{
    uint32_t want = (src != 0u) ? STEP_MOT_SRC_PROGRAM : STEP_MOT_SRC_SCAFFOLD;
    if (want != g_motion_src) { motion_cache_invalidate(); }
    g_motion_src = want;
}
uint32_t step_motion_src(void) { return g_motion_src; }

/* ★ 主循环调用。语义：
 *   · **只在槽值变化时动作** —— ★ 判据要能回答"这个计数是不是每圈在空转涨"
 *     （本项目铁律："为可解释性加的计数会顺手抓住静默故障"）。
 *   · 镜像槽（16/17）**每次刷新**（不是只在变化时）—— 否则限时到期自动停脉冲后镜像会陈旧。
 *   · 顺序：**先方向/使能，再频率**（让驱动器先进入确定状态再起脉冲）。 */
DCL_ITCM void step_service_motion(void)
{
    if (g_motion_src != STEP_MOT_SRC_PROGRAM) { return; }

    float rf = wire_get(STEP_MOT_SLOT_RATE);
    float df = wire_get(STEP_MOT_SLOT_DIR);
    float ef = wire_get(STEP_MOT_SLOT_ENA);
    float lf = wire_get(STEP_MOT_SLOT_LIMIT);
    uint32_t hz  = (rf <= 0.0f) ? 0u : (uint32_t)rf;   /* ★ NaN 走 <= 分支 ⇒ 视为"停"(安全侧) */
    uint32_t dir = (df > 0.5f) ? 1u : 0u;
    uint32_t en  = (ef > 0.5f) ? 1u : 0u;
    uint32_t lim = (lf <= 0.0f) ? 0u : (uint32_t)lf;

    /* ★ 镜像每次刷新：让程序面能回答"我下的指令，硬件这边已经变成什么"（跨拍就绪门）。 */
    wire_set(STEP_MOT_SLOT_RATE_AP,  (float)g_step_rate_hz);
    wire_set(STEP_MOT_SLOT_LIMIT_AP, (float)g_step_deadline_tick);

    /* ★★★ "变化"的判定 = **请求值 vs 上次请求值**（不是" vs 硬件现状"）：
     *   · 与硬件比会在"限时到期自动停脉冲"之后**立刻把脉冲拉回来** ⇒ 限时（安全网）失效；
     *   · 而"与上次请求比"必须配 **切换源时失效缓存** —— 否则"上一次留下的请求"会被当成
     *     "没有变化" ⇒ 程序面第一次下发的值（恰好与上次相同）**不会被应用**。
     *     实测（2026-09-17）：重新部署同一个程序后 `wire[12]=15 Hz` 而 `rate=0`。
     *     ★ 与"重放旧表被静默忽略"（req_seq 撞号）是**同一族**：都是"把'值相同'当成'无事发生'"。 */
    if (hz == s_mot_hz && dir == s_mot_dir && en == s_mot_en && lim == s_mot_lim) { return; }
    g_motion_cmd_n++;
    s_mot_hz = hz; s_mot_dir = dir; s_mot_en = en; s_mot_lim = lim;
    step_set_dir(dir);
    if (step_set_ena(en) != STEP_RC_OK) { g_motion_rej_n++; }   /* 未声明极性 ⇒ fail-closed */
    step_set_rate(hz);                    /* 0 = 停脉冲；>0 由 step_set_rate 内部钳位 */
    step_set_deadline_ms(lim);
    g_motion_applied_n++;
}
