/**
 * main.c — DCL 引擎 H723 平台 · 阶段 0/1 引导
 *
 * 本阶段目标 (可验证的最小基线):
 *   ① HSE 25MHz 起振 → VOS0 → PLL1 → CPU 550MHz / HCLK 275MHz / TIMxCLK 275MHz
 *   ② TIM2 产生 **精确 100μs** 拍, 中断里翻转 PA8
 *   ③ 用逻辑分析仪测 PA8 → 外部独立验证"时钟树 + 拍"都对
 *
 * 为什么必须外部验证 (MIGRATE-H723.md §3.6):
 *   固件只能"声称"自己在 550MHz; PLL 锁得住但频率跑偏/抖动大是可能的
 *   (电源/去耦问题)。只有把信号引到 LA 上, 才能物理地看到真实频率。
 *
 * 接线: LA CHx ← PA8 (本文件 TICK_PIN), GND 共地
 *       预期波形: 周期 200μs 方波 (= 每 100μs 翻转一次)
 *
 * 失败指示: 时钟初始化失败时, PA8 按错误码闪 N 次, 停 1 秒, 循环
 *           (N = |错误码|, 见 clock.h CLK_ERR_*)
 */
#include <stdint.h>
#include "regs.h"
#include "clock.h"

/* ══════════ 输出脚 (LA 探针点) ══════════
 * PA8: 常见引出脚, 非 SWD/非 USB/非晶振, 安全。换脚只改这里。 */
#define TICK_PORT       0u          /* GPIOA */
#define TICK_BIT        8u

#define REG8(a)         (*(volatile uint8_t *)(a))
#define NVIC_IP(n)      REG8(0xE000E400UL + (n))

/* 启动状态 (供 pyocd 读取定位问题) */
volatile int      g_boot_status = 0;    /* clock_init 返回值 */
volatile uint32_t g_tick_count  = 0;    /* 拍计数 */
volatile uint32_t g_clock_hclk  = 0;    /* 实测 AXI 频率 */
volatile uint32_t g_stage       = 0;    /* ★执行进度: 高频调试用 — 看崩在哪一步 */

/* ---- 极简 GPIO 输出 (PA8) ---- */
static void tick_pin_init(void)
{
    RCC_AHB4ENR |= (1u << 0);                              /* GPIOAEN */

    uint32_t mod = GPIO_MODER(TICK_PORT);
    mod &= ~(3u << (TICK_BIT * 2u));
    mod |=  (1u << (TICK_BIT * 2u));                       /* 01 = 通用输出 */
    GPIO_MODER(TICK_PORT)  = mod;

    GPIO_OTYPER(TICK_PORT)  &= ~(1u << TICK_BIT);          /* 推挽 */
    GPIO_PUPDR(TICK_PORT)   &= ~(3u << (TICK_BIT * 2u));   /* 无上下拉 */

    uint32_t spd = GPIO_OSPEEDR(TICK_PORT);
    spd &= ~(3u << (TICK_BIT * 2u));
    spd |=  (3u << (TICK_BIT * 2u));                       /* 非常高 (边缘干净) */
    GPIO_OSPEEDR(TICK_PORT) = spd;
}

static inline void tick_pin_hi(void) { GPIO_BSRR(TICK_PORT) = (1u << TICK_BIT); }
static inline void tick_pin_lo(void) { GPIO_BSRR(TICK_PORT) = (1u << (TICK_BIT + 16u)); }

/* ---- TIM2: 精确 100μs 拍 ----
 * TIMxCLK = 275MHz; PSC=0; ARR = 27500-1 → 更新周期 = 27500/275MHz = 100.000μs
 * 注意 ARR 是"计数到 ARR 就溢出", 所以写 ARR = 周期计数 - 1。 */
static void tick_timer_init(void)
{
    RCC_APB1LENR |= (1u << 0);                 /* TIM2EN */

    TIM_CR1(TIM2_BASE) = 0;                    /* 先停 */
    TIM_PSC(TIM2_BASE) = 0;                    /* 不分频 */
    TIM_ARR(TIM2_BASE) = CLK_TICK_TIMCNT - 1u; /* 27500-1 */
    TIM_EGR(TIM2_BASE) = TIM_EGR_UG;           /* 立即把 PSC/ARR 装入影子寄存器 */
    TIM_SR(TIM2_BASE)  = 0;                    /* 清 UG 顺带产生的 UIF */

    NVIC_IP(IRQ_TIM2)  = 0;                    /* 最高抢占优先级 (硬拍必须抢占一切) */
    NVIC_ISER          = (1u << IRQ_TIM2);     /* 使能 TIM2 中断 */

    TIM_DIER(TIM2_BASE) = TIM_DIER_UIE;        /* 允许更新中断 */
    TIM_CR1(TIM2_BASE)  = TIM_CR1_CEN;         /* 启动 */
}

/* ---- 拍中断: 翻转 PA8 + 计数 ----
 * 这就是将来引擎 ISR 的外壳 (阶段 2 起往里面加路由扫描)。
 * 放在这里翻转引脚, LA 测到的就是**真实的拍周期**, 不是固件自报的数字。 */
void TIM2_IRQHandler(void)
{
    if (TIM_SR(TIM2_BASE) & TIM_SR_UIF) {
        TIM_SR(TIM2_BASE) = ~TIM_SR_UIF;       /* TIM 的 SR 是 rc_w0: 写 0 清除 */
        g_stage = 7;                   /* ★ISR 真的在跑 (最高级别证据) */
        if (g_tick_count & 1u) tick_pin_hi();
        else                   tick_pin_lo();
        g_tick_count++;
    }
}

/* ---- 失败指示: 闪 N 次 (N = |err|) ---- */
static void blink_error(int err)
{
    if (err < 0) err = -err;
    if (err == 0) err = 1;
    for (;;) {
        for (int i = 0; i < err; i++) {
            tick_pin_hi();
            for (volatile uint32_t d = 0; d < 400000u; d++) { }
            tick_pin_lo();
            for (volatile uint32_t d = 0; d < 400000u; d++) { }
        }
        for (volatile uint32_t d = 0; d < 3000000u; d++) { }   /* 组间长停 */
    }
}

/* ---- SystemInit: startup 在 .data 初始化**之前**调用 ----
 * 这里只做"与内存状态无关"的最小设置 (FPU)。
 * 时钟初始化放在 main 里 —— 那里能正常报告错误, 且 .data/.bss 已可用。 */
void SystemInit(void)
{
    /* 使能 FPU: CP10/CP11 全访问 (否则浮点指令触发 UsageFault) */
    SCB_CPACR |= (0xFu << 20);
    __asm__ volatile("dsb; isb");
}

int main(void)
{
    tick_pin_init();
    g_stage = 1;

    /* ① 时钟: HSE → VOS0 → PLL1 → 550MHz */
    int err = clock_init();
    g_boot_status = err;
    g_stage = 2;
    if (err != CLK_OK) blink_error(err);       /* 不返回 */

    /* ② 实测频率 (从 RCC 寄存器反推, 不是常量) */
    g_clock_hclk = clock_get_hclk_hz();
    g_stage = 3;

    /* ③ DWT 周期计数 (测量用, 1.82ns 分辨率) */
    dwt_enable();
    g_stage = 4;

    /* ④ 100μs 拍 + PA8 输出 */
    tick_timer_init();
    g_stage = 5;

    /* ⑤ 主循环: 只维护一个慢速"活着"的指示。
     *    真正的验证靠 LA 测 PA8 —— 不靠这里的任何自报。 */
    for (;;) {
        g_stage = 6;                       /* 主循环活着的证据 */
        __asm__ volatile("wfi");
    }
}
