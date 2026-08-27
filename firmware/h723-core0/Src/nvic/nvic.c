/**
 * NVIC: TIM1_UP 中断使能
 *
 * TIM1_UP_IRQn = 25 → NVIC_ISER0 bit 25 (不是 ISER1 bit 11!)
 */
#include "nvic.h"

void nvic_enable_tim1(void) {
    NVIC_ISER0 = (1u << TIM1_UP_IRQn);
    __asm__ volatile("dsb; isb");
}
