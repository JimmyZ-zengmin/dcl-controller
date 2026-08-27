/**
 * GPIOE 数字输出
 *
 * PE0-7:  通用输出 ( actuator_idx 32-39 → bit0-7 )
 * PE8:    通用输出
 * PE9:    AF1 → TIM1_CH1 (PWM)
 * PE10:   通用输出
 * PE11:   AF1 → TIM1_CH2 (PWM)
 * PE12:   通用输出
 * PE13:   AF1 → TIM1_CH3 (PWM)
 * PE14:   AF1 → TIM1_CH4 (ADC trigger)
 * PE15:   通用输出
 */
#include "gpioe.h"

void gpioe_init(void) {
    RCC_AHB4ENR |= (1 << 4);  /* GPIOEEN */
    __asm__ volatile("dsb");

    /* MODER: PE0-8,10,12,15=输出; PE9,11,13,14=AF */
    uint32_t moder = GPIOE_MODER;
    for (int i = 0; i <= 7; i++) { moder &= ~(3 << (i*2)); moder |= (1 << (i*2)); }
    moder &= ~(3 << 16); moder |= (1 << 16);  /* PE8 */
    moder &= ~(3 << 18); moder |= (2 << 18);  /* PE9: AF */
    moder &= ~(3 << 20); moder |= (1 << 20);  /* PE10 */
    moder &= ~(3 << 22); moder |= (2 << 22);  /* PE11: AF */
    moder &= ~(3 << 24); moder |= (1 << 24);  /* PE12 */
    moder &= ~(3 << 26); moder |= (2 << 26);  /* PE13: AF */
    moder &= ~(3 << 28); moder |= (2 << 28);  /* PE14: AF */
    moder &= ~(3 << 30); moder |= (1 << 30);  /* PE15 */
    GPIOE_MODER = moder;

    /* AFRH: PE9/11/13/14 → AF1 (TIM1) */
    uint32_t afrh = GPIOE_AFRH;
    afrh &= ~((0xF << 4) | (0xF << 12) | (0xF << 20) | (0xF << 28));
    afrh |=  (1 << 4) | (1 << 12) | (1 << 20) | (1 << 28);
    GPIOE_AFRH = afrh;

    GPIOE_OSPEEDR = 0xFFFFFFFF;
    GPIOE_ODR = 0x0000;
    SHADOW_GPIO = 0x0000;
}

void gpioe_write(uint32_t value) {
    SHADOW_GPIO = value;
    GPIOE_ODR = value;
}
