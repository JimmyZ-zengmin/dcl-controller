/**
 * ADC1 配置: 同步时钟 + VREFINT 通道 + TIM1_TRGO 触发
 *
 * 解决两个核心问题:
 *   1. ADRDY=0: 同步模式 (CKMODE=10) 不依赖 per_ck
 *   2. ITCM→AHB4 阻断: DMA 绕过 CPU 直访
 */
#include "adc1.h"

void adc1_init(void) {
    /* 使能 ADC 时钟 */
    RCC_AHB1ENR |= (1 << 5);  /* ADC12EN */
    __asm__ volatile("dsb");

    /* 时钟源: 同步 AHB/2 = 136MHz */
    ADC12_CCR = (2 << 16) | (1 << 22);  /* CKMODE=10, VREFEN */

    /* 强制禁用之前残留 */
    if (ADC1_CR & 1) {
        ADC1_CR |= (1 << 1);
        { uint32_t t = TIMEOUT; while ((ADC1_CR & 1) && --t) {} }
    }

    /* 退出深度掉电 */
    ADC1_CR &= ~(1 << 29);
    { uint32_t t = TIMEOUT; while ((ADC1_CR & (1 << 29)) && --t) {} }

    /* 电压调节器 */
    ADC1_CR |= (1 << 28);
    { uint32_t t = TIMEOUT; while (!(ADC1_ISR & (1 << 12)) && --t) {} }

    /* 校准 */
    ADC1_CR |= (1 << 31);
    { uint32_t t = TIMEOUT; while ((ADC1_CR & (1 << 31)) && --t) {} }

    /* CFGR: 12-bit + EXTSEL=TIM1_TRGO + EXTEN=rising + DMAEN */
    ADC1_CFGR = 0x00002289;
    ADC1_PCSEL = (1 << 17);
    ADC1_SMPR1 |= (7 << 21);
    ADC1_SQR1 = (17 << 6);

    /* 清 ADRDY → ADEN → 等 ADRDY */
    ADC1_ISR = 1;
    ADC1_CR |= (1 << 0);
    { uint32_t t = TIMEOUT; while (!(ADC1_ISR & 1) && --t) {} }
}

void adc1_start(void) {
    ADC1_CR |= (1 << 2);  /* ADSTART */
}
