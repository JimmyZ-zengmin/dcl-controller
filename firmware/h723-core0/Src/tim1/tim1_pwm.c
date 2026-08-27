/**
 * TIM1 初始化: 100us 周期 + 4 路 PWM + TRGO
 *
 * CH1 (PE9_AF1):  PWM mode 1 — heater/cooler 模拟输出
 * CH2 (PE11_AF1): PWM mode 1
 * CH3 (PE13_AF1): PWM mode 1
 * CH4 (PE14_AF1): PWM mode 2 — OC4REF 上升沿 → TRGO → ADC 触发
 *
 * 关键: TIM1 是高级定时器, BDTR.MOE 必须为 1
 */
#include "tim1_pwm.h"

void tim1_init(void) {
    RCC_APB2ENR |= (1 << 0);  /* TIM1EN */

    TIM1_CR1 = 0;             /* stop */
    __asm__ volatile("dsb");

    /* CR2: MMS=100 → OC4REF 作为 TRGO */
    TIM1_CR2 = (4 << 4);

    TIM1_PSC = 0;
    TIM1_ARR = 11999;         /* 100us @ 120MHz (480MHz CPU, APB2=120MHz) */

    /* CCMR1: CH1=OC1M(110)=PWM1, OC1PE=1; CH2=OC2M(110)=PWM1, OC2PE=1 */
    TIM1_CCMR1 = (6 << 4) | (1 << 3) | (6 << 12) | (1 << 11);

    /* CCMR2: CH3=OC3M(110)=PWM1, OC3PE=1; CH4=OC4M(111)=PWM2, OC4PE=1
     * CH4 用 PWM2: OC4REF 上升沿在 CCR4, 下降沿在 ARR, 触发 ADC */
    TIM1_CCMR2 = (6 << 4) | (1 << 3) | (7 << 12) | (1 << 11);

    /* CCER: 使能 CH1/2/3/4 输出, active high */
    TIM1_CCER = (1 << 0) | (1 << 4) | (1 << 8) | (1 << 12);

    /* BDTR: MOE=1 (高级定时器必须) */
    TIM1_BDTR = (1 << 15);

    /* DIER: UIE=1 (更新中断) + UDE=1 (DMA请求, bit8) */
    TIM1_DIER = (1 << 0) | (1 << 8);

    /* 初始化 CCR1/2/3 = 0, CCR4 = 11799 (触发点 @98.3%) */
    TIM1_CCR1 = 0;
    TIM1_CCR2 = 0;
    TIM1_CCR3 = 0;
    TIM1_CCR4 = 11799;

    /* 启动: CEN=1 + ARPE=1 */
    TIM1_CR1 = (1 << 0) | (1 << 7);
}

void tim1_set_pwm(uint8_t ch, float duty_pct) {
    if (duty_pct < 0.0f) duty_pct = 0.0f;
    if (duty_pct > 100.0f) duty_pct = 100.0f;
    uint16_t ccr = (uint16_t)(duty_pct * 119.99f);

    switch (ch) {
    case 1: TIM1_CCR1 = ccr; break;
    case 2: TIM1_CCR2 = ccr; break;
    case 3: TIM1_CCR3 = ccr; break;
    }
}
