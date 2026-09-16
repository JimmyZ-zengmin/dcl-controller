/* timebase.c — 生产时基的初始化与自检。设计说明见 timebase.h（含为什么不用 DWT）。 */
#include "timebase.h"

volatile uint32_t g_tb_dead_n   = 0u;
volatile uint32_t g_dwt_dead_n  = 0u;
volatile uint32_t g_tb_cyc_last = 0u;

/* 在当前时基上量一个短间隔 —— 用于**自检**（Δ=0 即死）。
 * n 是"等多久"的循环数；不追求精确，只要能明显非零即可。 */
static uint32_t tb_probe(uint32_t n)
{
    uint32_t a = tb_cyc();
    for (volatile uint32_t i = 0u; i < n; i++) { __asm__ volatile("nop"); }
    return tb_cyc() - a;
}

void tb_init(void)
{
#if DCL_TIMEBASE == TB_KIND_DWT
    /* ── 0 档 = **改前行为对照**：时基仍是 DWT。
     *    dwt_enable() 在别处调用；这里只做一次探针（它会被调试器会话随时关掉）。 */
    g_tb_cyc_last = tb_probe(200u);
#else
    /* ── 1 档 = 交付：TIM5 自由运行 32 位计数器 ── */
    RCC_APB1LENR |= RCC_APB1LENR_TIM5EN;
    TIM_CR1(TIM5_BASE) = 0u;                     /* 先停 */
    TIM_PSC(TIM5_BASE) = 0u;                     /* 不分频 ⇒ TIMxCLK = 200MHz ⇒ 5ns/tick */
    TIM_ARR(TIM5_BASE) = 0xFFFFFFFFu;            /* 满量程自由运行（不产生中断，我们只读 CNT）*/
    TIM_EGR(TIM5_BASE) = TIM_EGR_UG;             /* ★ 立刻把 PSC/ARR 装载进影子寄存器 */
    (void)TIM_CNT(TIM5_BASE);                    /* 读一次，稳定 */
    TIM_CR1(TIM5_BASE) = TIM_CR1_CEN;            /* 跑 */
    __asm__ volatile("dsb; isb" ::: "memory");

    /* ★ 自检（能失败的判据）：两次读之间必须推进。
     *   不推进 = 定时器时钟没起来（漏开 RCC / PSC 写错 / 影子没装载）⇒ 立刻留痕，
     *   而不是等到"所有统计看起来完美稳定"才发现。 */
    uint32_t d = tb_probe(400u);
    g_tb_cyc_last = d;
    if (d == 0u) { g_tb_dead_n++; }
#endif
}
