/**
 * NVIC 接口: TIM1_UP 中断使能 (IRQn=25, ISER0 bit 25)
 */
#ifndef DCL_NVIC_H
#define DCL_NVIC_H

#include <stdint.h>
#include "../registers.h"

#ifdef __cplusplus
extern "C" {
#endif

#define TIM1_UP_IRQn  25

/** 使能 TIM1_UP 中断 */
void nvic_enable_tim1(void);

#ifdef __cplusplus
}
#endif

#endif /* DCL_NVIC_H */
