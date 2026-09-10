/**
 * main.c — DCL 引擎 H723 平台 · 阶段 0/1
 *
 * 阶段 0 (已完成): 时钟树 (HSE→VOS0→PLL1→400MHz) + 100μs 拍 + LA 外部验证
 * 阶段 1 (本文件): 空拍骨架 + **DWT 精确测量拍开销与抖动**
 *   · ISR 入口→出口的 CPU 周期数 (空拍成本)
 *   · 相邻拍之间的 CPU 周期数 (固件侧抖动, 与 LA 交叉验证)
 *   · DWT 自身标定 (读对开销 + 已知 NOP 循环线性度) —— 证明计数可信
 *   · PA9 线路验证 (LA CH1)
 *
 * ★ 口径 (重要):
 *   频率/时序类结论**以 LA 外部证据为准** (README / docs/)。
 *   本文件这些 DWT 数字用来说明"引擎内部花了多少周期",
 *   不用来宣称"频率对不对" —— 那是 LA 的活。
 *
 * ★ ISR_MODE (编译期切换, 测完记得改回 1):
 *     0 = 纯骨架 (无 DWT)            → 骨架成本 (自报不了, 由 mode2 减开销推算)
 *     1 = 骨架 + DWT 测量 + 统计      → ★默认: 自报开销/抖动
 *     2 = 骨架 + DWT 读对 (无统计)    → 用来分离"统计记账"成本
 *     3 = 骨架 + DWT + 统计 + PA9 分频输出 → PA9 线路验证 (LA CH1)
 *
 * 接线: LA CH4 ← PA8 (拍输出, 5kHz) / LA CH1 ← PA9 (模式3 才输出)
 *       失败指示: 时钟初始化失败 → PA8 闪 |错误码| 次 (CLK_ERR_*)
 */
#include <stdint.h>
#include "regs.h"
#include "clock.h"

#ifndef ISR_MODE
#define ISR_MODE 1
#endif

/* ══════════ 输出脚 ══════════ */
#define TICK_PORT       0u          /* GPIOA */
#define TICK_BIT        8u          /* 拍输出: 每拍翻转, 5kHz 方波 */
#define UARTT_PORT      0u          /* GPIOA */
#define UARTT_BIT       9u          /* PA9 = USART1_TX (模式3 先当普通 GPIO 用) */

#define REG8(a)         (*(volatile uint8_t *)(a))
#define NVIC_IP(n)      REG8(0xE000E400UL + (n))

/* ══════════ 全局可观测 (供 pyocd 读) ══════════ */
volatile int      g_boot_status = 0;    /* clock_init 返回值 */
volatile uint32_t g_tick_count  = 0;    /* 拍计数 */
volatile uint32_t g_clock_hclk  = 0;    /* 从 RCC 反推的 AXI 频率 */
volatile uint32_t g_stage       = 0;    /* 执行进度: 看崩在哪一步 */
volatile uint32_t g_isr_mode    = ISR_MODE;

/* ── 阶段 1: 空拍开销 (CPU 周期) ── */
volatile uint32_t g_isr_cyc_last = 0;
volatile uint32_t g_isr_cyc_min  = 0xFFFFFFFFu;
volatile uint32_t g_isr_cyc_max  = 0;
volatile uint64_t g_isr_cyc_sum  = 0;
volatile uint32_t g_isr_n        = 0;

/* ── 阶段 1: 拍周期 (CPU 周期数, 固件侧抖动) ── */
volatile uint32_t g_per_cyc_last = 0;
volatile uint32_t g_per_cyc_min  = 0xFFFFFFFFu;
volatile uint32_t g_per_cyc_max  = 0;
volatile uint32_t g_per_prev     = 0;

/* ── 阶段 1: DWT 标定 ── */
volatile uint32_t g_dwt_overhead = 0;   /* 连续两次读 CYCCNT 的差值 */
volatile uint32_t g_cal_n1000    = 0;   /* 1000 次 nop 的周期数 */
volatile uint32_t g_cal_n2000    = 0;   /* 2000 次 nop 的周期数 */
volatile uint32_t g_pa9_div      = 0;

/* ══════════ GPIO 输出 ══════════ */
static void pin_out_init(uint32_t port, uint32_t bit)
{
    if (port == 0u) RCC_AHB4ENR |= (1u << 0);              /* GPIOAEN */

    uint32_t mod = GPIO_MODER(port);
    mod &= ~(3u << (bit * 2u));
    mod |=  (1u << (bit * 2u));                            /* 01 = 通用输出 */
    GPIO_MODER(port) = mod;

    GPIO_OTYPER(port)  &= ~(1u << bit);                    /* 推挽 */
    GPIO_PUPDR(port)   &= ~(3u << (bit * 2u));             /* 无上下拉 */

    uint32_t spd = GPIO_OSPEEDR(port);
    spd &= ~(3u << (bit * 2u));
    spd |=  (3u << (bit * 2u));                            /* 非常高 (边缘干净) */
    GPIO_OSPEEDR(port) = spd;
}

static inline void pin_set(uint32_t port, uint32_t bit, int hi)
{
    GPIO_BSRR(port) = hi ? (1u << bit) : (1u << (bit + 16u));
}

static inline void tick_pin_hi(void) { pin_set(TICK_PORT, TICK_BIT, 1); }
static inline void tick_pin_lo(void) { pin_set(TICK_PORT, TICK_BIT, 0); }

/* ══════════ 阶段 1-B: DWT 标定 (证明周期计数可信) ══════════
 * ① 读对开销: 连续两次读 CYCCNT 的差 (ISR 测量要减掉它)
 * ② 已知长度 nop 循环: 周期数/迭代 → 应是个小的稳定整数
 *    ★必须 noinline + volatile 计数: 否则编译器会把不同迭代数的循环
 *      优化成不同形状 (首版就是这样: n=1000 得 1602cyc, n=2000 得 60007cyc,
 *      差分算出 58 cyc/迭代这种荒谬值)
 * ③ ★真正权威的标定不在这里, 而是"拍周期 40000 cyc" 对照 LA 实测 100.0000μs
 *    —— 外部仪器独立确认了 CYCCNT 的计数频率 = 400.00MHz。比 nop 循环硬得多。 */
__attribute__((noinline))
static uint32_t calib_nop(volatile uint32_t n)
{
    uint32_t t0 = DWT_CYCCNT;
    for (volatile uint32_t i = 0; i < n; i++) { __asm__ volatile("nop"); }
    return DWT_CYCCNT - t0;
}

static void calibrate(void)
{
    /* ① 读对自身开销 */
    uint32_t a = DWT_CYCCNT, b = DWT_CYCCNT;
    g_dwt_overhead = b - a;

    /* ② 已知长度 nop 循环 (单点 + 周期数/迭代, 不做差分) */
    g_cal_n1000 = calib_nop(1000u);
}

/* ══════════ TIM2: 精确 100μs 拍 ══════════
 * TIMxCLK = 200MHz; PSC=0; ARR = 20000-1 → 20000/200MHz = 100.000μs */
static void tick_timer_init(void)
{
    RCC_APB1LENR |= (1u << 0);                 /* TIM2EN */

    TIM_CR1(TIM2_BASE) = 0;                    /* 先停 */
    TIM_PSC(TIM2_BASE) = 0;                    /* 不分频 */
    TIM_ARR(TIM2_BASE) = CLK_TICK_TIMCNT - 1u; /* 20000-1 */
    TIM_EGR(TIM2_BASE) = TIM_EGR_UG;           /* 装入影子寄存器 */
    TIM_SR(TIM2_BASE)  = 0;                    /* 清 UG 顺带产生的 UIF */

    NVIC_IP(IRQ_TIM2)  = 0;                    /* 最高抢占优先级 (硬拍抢占一切) */
    NVIC_ISER          = (1u << IRQ_TIM2);

    TIM_DIER(TIM2_BASE) = TIM_DIER_UIE;
    TIM_CR1(TIM2_BASE)  = TIM_CR1_CEN;         /* 启动 */
}

/* ══════════ 拍中断 ══════════
 * 这是将来引擎 ISR 的外壳 (阶段 2 起往里面加路由扫描)。
 * 无论 ISR_MODE 取值, 骨架部分都是: 查标志 → 清标志 → 翻脚 → 计数。 */
void TIM2_IRQHandler(void)
{
#if ISR_MODE >= 1
    uint32_t t0 = DWT_CYCCNT;
#endif

    if (TIM_SR(TIM2_BASE) & TIM_SR_UIF) {
        TIM_SR(TIM2_BASE) = ~TIM_SR_UIF;       /* TIM 的 SR 是 rc_w0: 写 0 清除 */
        g_stage = 7;                           /* ISR 真的在跑 (最高级别证据) */
        if (g_tick_count & 1u) tick_pin_hi();
        else                   tick_pin_lo();
        g_tick_count++;

#if ISR_MODE == 1 || ISR_MODE == 3
        {
            uint32_t t1 = DWT_CYCCNT;
            uint32_t d  = t1 - t0;
            g_isr_cyc_last = d;
            if (d < g_isr_cyc_min) g_isr_cyc_min = d;
            if (d > g_isr_cyc_max) g_isr_cyc_max = d;
            g_isr_cyc_sum += d;
            g_isr_n++;

            /* 拍周期 (CPU 周期): 相邻两次 ISR 入口之差 —— 固件侧抖动 */
            if (g_per_prev) {
                uint32_t p = t0 - g_per_prev;
                g_per_cyc_last = p;
                if (p < g_per_cyc_min) g_per_cyc_min = p;
                if (p > g_per_cyc_max) g_per_cyc_max = p;
            }
            g_per_prev = t0;
        }
#elif ISR_MODE == 2
        {
            uint32_t t1 = DWT_CYCCNT;          /* 只有读对, 不做统计记账 */
            g_isr_cyc_last = t1 - t0;
        }
#endif

#if ISR_MODE == 3
        /* 阶段 1-C: PA9 线路验证 —— 每 32 拍翻转一次
         * (拍 100μs → 3.2ms 半周期 ≈ 156Hz, 与 PA8 的 5kHz 明显可区分) */
        if (++g_pa9_div >= 32u) {
            g_pa9_div = 0;
            int hi = (GPIO_ODR(UARTT_PORT) & (1u << UARTT_BIT)) ? 0 : 1;
            pin_set(UARTT_PORT, UARTT_BIT, hi);
        }
#endif
    }
}

/* ══════════ 失败指示: 闪 N 次 (N = |err|) ══════════ */
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
        for (volatile uint32_t d = 0; d < 3000000u; d++) { }
    }
}

/* ══════════ SystemInit: startup 在 .data 初始化**之前**调用 ══════════ */
void SystemInit(void)
{
    SCB_CPACR |= (0xFu << 20);                 /* 使能 FPU (否则浮点触发 UsageFault) */
    __asm__ volatile("dsb; isb");
}

int main(void)
{
    pin_out_init(TICK_PORT, TICK_BIT);
    pin_out_init(UARTT_PORT, UARTT_BIT);       /* PA9 先作普通输出 */
    g_stage = 1;

    /* ① 时钟: HSE 25MHz → VOS0 → PLL1 → 400MHz */
    int err = clock_init();
    g_boot_status = err;
    g_stage = 2;
    if (err != CLK_OK) blink_error(err);       /* 不返回 */

    /* ② 从 RCC 反推实际频率 */
    g_clock_hclk = clock_get_hclk_hz();
    g_stage = 3;

    /* ③ DWT 周期计数 + 标定 */
    dwt_enable();
    calibrate();
    g_stage = 4;

    /* ④ 100μs 拍 + PA8 输出 */
    tick_timer_init();
    g_stage = 5;

    /* ⑤ 主循环: 只维护"活着"的证据。真正验证靠 LA —— 不靠自报。 */
    for (;;) {
        g_stage = 6;
        __asm__ volatile("wfi");
    }
}
