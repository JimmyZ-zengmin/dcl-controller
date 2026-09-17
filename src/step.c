#include "step.h"
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
volatile uint32_t g_step_ena_pin_intent  = 1u;   /* 上电默认意图 = 高(不导通) */
volatile uint32_t g_step_dt_max = 0u;

/* ★★★ 运动能力面（程序面）—— 见 step.h 的说明。★ **默认脚手架 ⇒ 既有行为零变化**。 */
volatile uint32_t g_motion_src       = STEP_MOT_SRC_SCAFFOLD;
volatile uint32_t g_motion_cmd_n     = 0u;
volatile uint32_t g_motion_applied_n = 0u;
volatile uint32_t g_motion_rej_n     = 0u;

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
static void apply_rest_ena(void)
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
    __asm__ volatile("dsb" ::: "memory");
}

void step_set_rate(uint32_t hz)
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

void step_set_dir(uint32_t dir) { g_step_dir = dir ? 1u : 0u; act_bits(STEP_DIR_ACT, g_step_dir); }

/* ENA: 参数是"**希望驱动器使能**", 与物理电平之间隔一层极性（翻译只在 `drive_ena()` 里）。
 * ★★★ fail-closed: **极性未声明时拒绝 en=1**，并**保持物理失能**。
 *   为什么必须"拒绝"而不是"用一个默认极性"：本次事故的形态就是
 *   "**看起来接受了, 其实跑错**"(无报错、只有值是错的) —— 那是三者里最坏的一种。
 *   ★ en=0（去使能）**永远允许**：拒绝一个停机请求没有任何安全收益。 */
uint32_t step_set_ena(uint32_t en)
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

void step_set_deadline_ms(uint32_t ms)
{
    g_step_deadline_tick = ms;      /* 存"剩余毫秒"; step_tick 按拍差递减 */
    s_sync = 1u;                    /* ★ 必须: 否则下一次 step_tick 用旧 s_last 算出巨大 dt */
}

/* ★★ 安全停止 = **停脉冲** + 进入"按轴决定"的静止态。
 *   ★★★ 关键更正（审计缺陷 D1）: 原实现是 `step_set_ena(0)` —— 即"一停就失能"。
 *     在**垂直轴上这等于掉力滑车**；在水平轴上也让"停机位置"从微步分辨率
 *     退化到整步齿槽（0.225° → 1.8°）。⇒ 现在由 `stop_hold` 决定：
 *       `stop_hold=1` ⇒ **保持力矩**（只停脉冲，驱动器继续通电）
 *       `stop_hold=0` ⇒ 物理去使能（省热；仅水平轴且已评估时用）
 *   ★ 与极性无关的硬保证始终是 **`CC1E=0`（无脉冲）** —— 没有脉冲电机就不会转。 */
void step_stop_safe(void)
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
void step_tick(uint32_t tick_now)
{
    static uint32_t s_last = 0u;

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
            if (s_prev_bad != 0u) { g_step_ena_mismatch_n++; }
            s_prev_bad = 1u;
        } else {
            s_prev_bad = 0u;
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
    if (g_step_deadline_tick == 0u) { return; }
    uint32_t dm = dt / 10u;                 /* 拍 → 毫秒 */
    if (dm == 0u) { return; }
    if (g_step_deadline_tick > dm) { g_step_deadline_tick -= dm; }
    else { g_step_deadline_tick = 0u; step_stop_safe(); }
}

/* ══════════════════════════════════════════════════════════════════════════
 * ★★★ 运动能力面（程序面）—— `PLAN-step-motion-v1` Step 1
 *   ③层写 `ACTUATOR[12..15]` → 本服务把它落到硬件。见 step.h 顶部的完整理由。
 * ══════════════════════════════════════════════════════════════════════════ */

/* ★ 运动请求住在 **WIRE** 而不是 ACTUATOR —— 理由见 step.h（③层的输出面只有 `wire[]`）。 */
static float wire_get(uint32_t idx)
{
    if (s_base == NULL) { return 0.0f; }
    return *(volatile float *)(s_base + OFF_WIRE_MAP + idx * 4u);
}
static void wire_set(uint32_t idx, float v)
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
void step_service_motion(void)
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
