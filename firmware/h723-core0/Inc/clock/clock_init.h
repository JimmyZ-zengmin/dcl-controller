/**
 * 时钟初始化接口
 *
 * SystemInit() 544MHz VCO + Flash等待周期 + 向量表重定位
 */
#ifndef DCL_CLOCK_INIT_H
#define DCL_CLOCK_INIT_H

#include <stdint.h>
#include "../registers.h"
#include "../dtcm_layout.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 配置系统时钟到 544MHz (VCOSEL=0)
 * - PLL: HSI/4 × 34 = 544MHz VCO
 * - SYSCLK = 544MHz, AHB = 272MHz, APB2 = 136MHz
 * - Flash: 3WS + 4 cycles
 */
void SystemInit(void);

#ifdef __cplusplus
}
#endif

#endif /* DCL_CLOCK_INIT_H */
