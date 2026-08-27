/**
 * ADC1 接口: 同步时钟模式 + DMA 搬运 + TIM1_TRGO 触发
 */
#ifndef DCL_ADC1_H
#define DCL_ADC1_H

#include <stdint.h>
#include "../registers.h"
#include "../dtcm_layout.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 初始化 ADC1:
 * - 同步时钟模式 (CKMODE=10)
 * - 12-bit 分辨率, 单通道 (CH17=VREFINT)
 * - DMA 循环 + TIM1_TRGO 硬件触发
 */
void adc1_init(void);

/**
 * 启动 ADC 转换 (ADSTART)
 */
void adc1_start(void);

#ifdef __cplusplus
}
#endif

#endif /* DCL_ADC1_H */
