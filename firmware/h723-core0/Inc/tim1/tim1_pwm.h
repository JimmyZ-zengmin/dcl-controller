/**
 * TIM1 接口: 100us 周期 + 4 路 PWM + TRGO (ADC 触发)
 */
#ifndef DCL_TIM1_PWM_H
#define DCL_TIM1_PWM_H

#include <stdint.h>
#include "../registers.h"
#include "../dtcm_layout.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 初始化 TIM1:
 * - PSC=0, ARR=13599 → 100us @136MHz
 * - CH1/2/3: PWM mode 1 (output compare)
 * - CH4: PWM mode 2 → OC4REF 上升沿作为 TRGO 触发 ADC
 * - BDTR: MOE=1 (高级定时器必须)
 */
void tim1_init(void);

/**
 * 更新 PWM 占空比 (0-100%)
 * actuator_idx: 1=CH1, 2=CH2, 3=CH3
 */
void tim1_set_pwm(uint8_t ch, float duty_pct);

#ifdef __cplusplus
}
#endif

#endif /* DCL_TIM1_PWM_H */
