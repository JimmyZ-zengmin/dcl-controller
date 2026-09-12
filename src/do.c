/*
 * do.c — DO 数字量输出面 (P3-A: ACTUATOR → GPIOE, CPU 直写)
 *
 * ★ 本组件的存在意义 (2026-09-12 P3-A): 用户最初要求"输入输出链一起搬"。
 *   输入链 (di/adc) 与 PWM 输出 (hil) 已进拍内, 但 16 路 DO 之前**根本不存在**:
 *   `OFF_CTRL_GPIO_MASK` 只做了定案与观测, `eng_outputs_safe` 对 GPIOE 只计数不清位。
 *   本组件把这条最后的"输出链"补上, 并让安全态第一次真正覆盖 GPIOE。
 *
 * 写法要点: **BSRR 单次 32 位写** (低 16 位=置 1, 高 16 位=清 0), 天然原子 ——
 *   不存在"读-改-写 ODR"的窗口, 也不需要临界区。GPIO_MASK 定案② 的越界检查
 *   (高 16 位非 0 ⇒ g_safe_mask_oob) 在 engine.c 的 eng_outputs_safe 里, 不在本处重复。
 */
#include "do.h"
#include "engine.h"
#include "regs.h"

/* ★ 硬件就绪门: 拍中断从阶段 8 就在跑, 而 GPIOE 时钟/引脚配置在 do_init (阶段 26)。
 *   没有这扇门, 阶段 8~26 之间每一拍都会去配未使能时钟的 GPIOE —— 与 adc.c 的
 *   s_adc_ready / hil.c 的 s_hil_ready 同一条纪律: 凡 ISR 可能在 init 之前调用的域,
 *   必须自带就绪门, 不依赖"调用顺序恰好排在我后面"。 */
static uint8_t *s_do_base = 0;
static uint32_t s_do_ready = 0;
volatile uint32_t g_do_poll_n = 0;   /* do_poll 实际执行过的拍数 (观测面, 须进 obs_anchor) */

/* PEi = ACTUATOR[i] > 0.5。返回本拍要写进 ODR 的 16 位值 (只含管辖位)。 */
static uint32_t do_pack(uint8_t *base, uint32_t mask)
{
    uint32_t bits = 0;
    for (uint32_t i = 0; i < DO_COUNT; i++) {
        if (!((mask >> i) & 1u)) continue;              /* 非管辖位: 不碰 */
        float v = *(volatile float *)(base + OFF_ACTUATOR_STATUS + i * 4u);
        if (v > 0.5f) bits |= (1u << i);
    }
    return bits;
}

void do_init(uint8_t *base)
{
    s_do_base = base;
    RCC_AHB4ENR |= (1u << DO_GPIO_PORT);                /* GPIOE 时钟 */
    /* PE0..15 推挽输出, 初始 0: MODER=01(输出), ODR 经 BSRR 清一遍 */
    GPIO_MODER(DO_GPIO_PORT) = 0x55555555u;
    GPIO_BSRR(DO_GPIO_PORT) = 0xFFFF0000u;              /* 高 16 位写 1 = 全部清 0 */
    __asm__ volatile("dsb" ::: "memory");
    s_do_ready = 1;
}

void do_poll(uint8_t *base, uint32_t tick_now)
{
    (void)tick_now;          /* 输出面每拍都做, 不需要相位 */
    if (!s_do_ready) return;
    uint32_t mask = SHM_U32(base, OFF_CTRL_GPIO_MASK) & 0xFFFFu;
    if (mask == 0u) return;      /* 没登记任何管辖位 ⇒ 整口不碰 (且省 40 cyc) */
    uint32_t bits = do_pack(base, mask);
    /* BSRR 一次写完成"置位 + 清零", 对 ODR 是原子覆盖; 非管辖位因为 bits 里
     * 相应为 0、mask 相应为 0 ⇒ 写的是 "清 0" —— 但我们**不该清非管辖位**!
     * ⇒ 所以只对管辖位下发: 置位 = bits, 清零 = (mask & ~bits)。 */
    GPIO_BSRR(DO_GPIO_PORT) = bits | ((mask & ~bits) << 16);
    g_do_poll_n++;
}

void do_outputs_safe(void)
{
    if (!s_do_ready) return;
    uint32_t mask = SHM_U32(s_do_base, OFF_CTRL_GPIO_MASK) & 0xFFFFu;
    /* 管辖位全部清 0 (BSRR 高 16 位写 1 = 清); 非管辖位不下发 */
    GPIO_BSRR(DO_GPIO_PORT) = (mask << 16);
}
