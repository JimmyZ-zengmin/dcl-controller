/*
 * hil.c — HIL 硬件在环 (W5 外设域)。见 hil.h 说明 (语义保留 S3)。
 */
#include "hil.h"
#include "adc.h"
#include "engine.h"
#include "regs.h"
#include "clock.h"

#define TIM3 TIM3_BASE_ADDR

static inline float hil_read_f(uint8_t *base, uint32_t off)
{
    return *(volatile float *)(base + off);
}
static inline void hil_write_f(uint8_t *base, uint32_t off, float v)
{
    *(volatile float *)(base + off) = v;
}

void hil_init(uint8_t *base)
{
    (void)base;
    /* ① PWM 脚 PA6 → AF2 (TIM3_CH1) */
    RCC_AHB4ENR |= (1u << 0);
    uint32_t bit = HIL_PWM_PIN & 15u;                 /* PA6 → bit6, 端口 A */
    uint32_t m = GPIO_MODER(0);
    m &= ~(3u << (bit * 2u));
    m |=  (2u << (bit * 2u));                         /* 10 = AF */
    GPIO_MODER(0) = m;
    uint32_t afr = GPIO_AFRL(0);                      /* PA0..PA7 在 AFRL */
    afr &= ~(0xFu << (bit * 4u));
    afr |=  (2u << (bit * 4u));                       /* AF2 = TIM3 */
    GPIO_AFRL(0) = afr;
    uint32_t pup = GPIO_PUPDR(0);
    pup &= ~(3u << (bit * 2u));                       /* AF 无上下拉 */
    GPIO_PUPDR(0) = pup;
    GPIO_OSPEEDR(0) |= (3u << (bit * 2u));            /* 高速档 (1kHz 方波) */

    /* ② 反馈脚 PA5 → ADC analog (ADC1_INP19) */
    adc_analog_pin(HIL_FB_PIN);

    /* ③ TIM3: PSC 使计数 1MHz, ARR 使周期 1kHz, PWM 模式 1 */
    RCC_APB1LENR |= RCC_APB1LENR_TIM3EN;
    TIM_PSC(TIM3)  = (uint32_t)(CLK_TIMXCLK_HZ / 1000000u) - 1u;   /* → 1MHz */
    TIM_ARR(TIM3)  = (uint32_t)(1000000u / HIL_PWM_HZ) - 1u;       /* → 1kHz */
    TIM_CCR1(TIM3) = 0u;
    TIM_CCMR1(TIM3) = TIM_CCMR1_OC1PE | (6u << TIM_CCMR1_OC1M_SHIFT);  /* PWM1 */
    TIM_CCER(TIM3)  = TIM_CCER_CC1E;
    TIM_CR1(TIM3)   = TIM_CR1_ARPE | TIM_CR1_CEN;
    TIM_EGR(TIM3)   = TIM_EGR_UG;                     /* 立即装载 PSC/ARR */

    __asm__ volatile("dsb" ::: "memory");
}

void hil_tick(uint8_t *base, uint32_t tick_now)
{
    static uint32_t last = 0;
    if ((uint32_t)(tick_now - last) < 100u) return;   /* 100 拍 = 10ms */
    last = tick_now;

    /* 输出臂: WIRE[HIL_U_WIRE] → 占空比 (钳到 [0, RES]) */
    float u = hil_read_f(base, OFF_WIRE_MAP + (uint32_t)HIL_U_WIRE * 4u);
    if (!(u > 0.0f)) u = 0.0f;
    if (u > (float)HIL_PWM_RES) u = (float)HIL_PWM_RES;
    uint32_t arr1 = TIM_ARR(TIM3) + 1u;
    TIM_CCR1(TIM3) = (uint32_t)((u / (float)HIL_PWM_RES) * (float)arr1);

    /* 反馈眼: ADC 多次平均 (无 RC 时采到方波 → 平均≈占空比×VDDA) → SENSOR[2] (V) */
    uint32_t acc = 0;
    for (int i = 0; i < HIL_FB_AVG; i++) {
        uint16_t r = adc_read(HIL_FB_CH_PA5);
        if (r == 0xFFFFu) { r = 0u; }
        acc += r;
    }
    float v = (float)(acc / (uint32_t)HIL_FB_AVG) * 3.3f / 65535.0f;
    hil_write_f(base, OFF_SENSOR_MAP + (uint32_t)HIL_FB_SENSOR * 4u, v);
    __asm__ volatile("dsb" ::: "memory");
}
