/**
 * DMA2 接口: Stream1 (ADC→DTCM) + Stream5 (SHADOW_GPIO→GPIOE_ODR)
 *
 * H723: Stream 0 保留, ADC 用 Stream 1
 */
#ifndef DCL_DMA2_H
#define DCL_DMA2_H

#include <stdint.h>
#include "../registers.h"
#include "../dtcm_layout.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 初始化 DMA2 Stream1: ADC1_DR → DTCM+0x00F0 (ADC_RAW)
 * H723 Stream 0 保留, 必须用 Stream 1
 */
void dma2_s1_adc_init(void);

/**
 * 初始化 DMA2 Stream5: SHADOW_GPIO → GPIOE_ODR (TIM1_UP 触发, 循环模式)
 */
void dma2_s5_gpio_init(void);

/** 禁用 DMA2 Stream5 */
void dma2_s5_disable(void);

#ifdef __cplusplus
}
#endif

#endif /* DCL_DMA2_H */
