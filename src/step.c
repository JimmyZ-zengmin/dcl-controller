#include "step.h"
#include <stddef.h>
#include "regs.h"
#include "clock.h"
#include "engine.h"

/* TIM3 计数时钟 = 1MHz (PSC = CLK_TIMXCLK_HZ/1e6 - 1)。
 * 步进频率 = 1MHz / (ARR+1) —— 50% 占空比由 CCR1 = (ARR+1)/2 给出。
 * 范围: ARR+1 最小 2 ⇒ 500kHz(远超需要); 最大 65536 ⇒ 15.3Hz。 */
#define STEP_TIMCLK_HZ  1000000u
#define TIM3  TIM3_BASE_ADDR      /* 与 hil.c 同一写法 (regs.h 只给了 *_BASE_ADDR) */

volatile uint32_t g_step_rate_hz = 0u, g_step_dir = 0u, g_step_ena = 0u;
volatile uint32_t g_step_owns_tim3 = 1u;      /* ★ 让 hil 的输出臂让出 TIM3 */
volatile uint32_t g_step_deadline_tick = 0u, g_step_stop_n = 0u;
volatile uint32_t g_step_arr = 0u, g_step_ccr1 = 0u;
volatile uint32_t g_step_ena_pol = 0u;    /* 0 = 拉低即使能 (多数 TB6600 的标注) */
volatile uint32_t g_step_dt_max = 0u;

static uint8_t *s_base = NULL;
static uint32_t s_sync = 1u;   /* ★ 首次/重新武装后先对齐 s_last, 见 step_tick */

static void act_set(uint32_t idx, float v)
{
    if (s_base == NULL) { return; }
    *(volatile float *)(s_base + OFF_ACTUATOR_STATUS + idx * 4u) = v;
}

/* ★ 共阳接法下 "光耦导通 = MCU 拉低" ⇒ 逻辑 1 对应(不导通)= 高 = ACTUATOR 1.0f。
 *   ★ ENA 的有效极性在 TB6600 上正/反两种都有 ⇒ 必须**实测**:
 *     上电后用手转电机轴: 转得动 = 当前 ENA 电平是"失能"; 锁死 = "使能"。
 *     若与预期相反, 把本函数里置 0.0f/1.0f 对调即可 (两处)。 */
static void act_bits(uint32_t idx, uint32_t active)
{
    act_set(idx, active ? 0.0f : 1.0f);
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

    /* ② 先把四个脚驱动成"不导通"(高) —— 这是**上电安全态**, 必须在使能 TIM3 之前 */
    for (uint32_t i = 8u; i < 12u; i++) { act_set(i, 1.0f); }
    g_step_dir = 0u; g_step_ena = 0u;

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

/* en=1 ⇒ "光耦导通" = MCU 拉低 ⇒ 按多数 TB6600 的标注这是**使能**。
 * ★ 极性未实测前**不要**依赖它做安全 —— 安全靠"无脉冲"。 */
/* ENA: 参数是"**希望驱动器使能**", 与物理电平之间隔一层极性 */
void step_set_ena(uint32_t en)
{
    g_step_ena = en ? 1u : 0u;
    uint32_t conducing = (g_step_ena_pol == 0u) ? g_step_ena : (g_step_ena ^ 1u);
    act_bits(STEP_ENA_ACT, conducing);          /* conducing=1 ⇒ MCU 拉低 ⇒ 光耦导通 */
}
void step_set_ena_pol(uint32_t pol) { g_step_ena_pol = pol ? 1u : 0u; step_set_ena(g_step_ena); }

void step_set_deadline_ms(uint32_t ms)
{
    g_step_deadline_tick = ms;      /* 存"剩余毫秒"; step_tick 按拍差递减 */
    s_sync = 1u;                    /* ★ 必须: 否则下一次 step_tick 用旧 s_last 算出巨大 dt */
}

void step_stop_safe(void)
{
    step_set_rate(0u);
    step_set_ena(0u);
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
