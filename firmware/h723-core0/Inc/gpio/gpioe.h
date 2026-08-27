/**
 * GPIOE 接口: 数字输出 (PE0-15) + AF (PE9/11/13/14 = TIM1 CH1-4)
 */
#ifndef DCL_GPIOE_H
#define DCL_GPIOE_H

#include <stdint.h>
#include "../registers.h"
#include "../dtcm_layout.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 初始化 GPIOE:
 * - PE0-8, 10, 12, 15 = 通用输出
 * - PE9, 11, 13, 14 = AF1 (TIM1 PWM)
 * - OSPEEDR = 最高速度
 * - ODR = 0, SHADOW_GPIO = 0
 */
void gpioe_init(void);

/**
 * 写 GPIOE 数字输出 (32 路映射到 ACTUATOR_STATUS[32..63])
 * 写 SHADOW_GPIO + ODR
 */
void gpioe_write(uint32_t value);

#ifdef __cplusplus
}
#endif

#endif /* DCL_GPIOE_H */
