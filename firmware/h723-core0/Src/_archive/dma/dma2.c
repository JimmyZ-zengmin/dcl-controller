/**
 * DMA2 配置 (双路)
 *
 * Stream 1: ADC1_DR → DTCM (ADC_RAW) — 输入侧 DMA 搬运
 *           (H723 Stream 0 保留, 不可用)
 * Stream 5: DTCM(SHADOW_GPIO) → GPIOE_ODR — 输出侧 DMA 搬运
 */
#include "dma2.h"

void dma2_s1_adc_init(void) {
    /* 使能时钟 */
    RCC_AHB1ENR |= (1 << 1);   /* DMA2EN */
    RCC_AHB1ENR |= (1 << 2);   /* DMAMUX1EN */
    __asm__ volatile("dsb; isb");
    for (volatile int i = 0; i < 8; i++) {};  /* 时钟稳定延迟 */

    /* DMAMUX1 Stream 1 → ADC1 (REQ_ID=9) */
    DMAMUX1_S1CR = DMAMUX_REQ_ADC1;

    /* 禁用 Stream 1, 等 EN 清除 */
    DMA2_S1CR = 0;
    __asm__ volatile("dsb");
    { uint32_t tout = TIMEOUT; while ((DMA2_S1CR & 1) && --tout) {} }

    /* 清错误标志 (Stream 1: LIFCR bit 11=TEIF, bit 10=HTIF, bit 9=TCIF, bit 8=DMEIF) */
    *(volatile uint32_t *)(DMA2_BASE + 0x08) = (1 << 11) | (1 << 10) | (1 << 9) | (1 << 8);

    DMA2_S1PAR  = (uint32_t)&ADC1_DR;
    DMA2_S1M0AR = DTCM_BASE + 0x00F0;             /* ADC_RAW (dtcm_layout.h) */
    DMA2_S1NDTR = 1;
    DMA2_S1FCR  = (1 << 2);                      /* DMDIS: 直通模式 */

    /* CR: PINC=0, CIRC=0, MSIZE=32, PSIZE=32, PL=最高, DIR=P2M */
    { uint32_t cr = (1 << 8) | (2 << 10) | (2 << 12) | (3 << 16);
      DMA2_S1CR = cr; }
    __asm__ volatile("dsb; isb");
    DMA2_S1CR |= 1;
    __asm__ volatile("dsb; isb");
}

void dma2_s5_gpio_init(void) {
    /* 使能时钟 */
    RCC_AHB1ENR |= (1 << 1);   /* DMA2EN */
    RCC_AHB1ENR |= (1 << 2);   /* DMAMUX1EN */
    __asm__ volatile("dsb; isb");
    for (volatile int i = 0; i < 8; i++) {}   /* 时钟稳定延迟 */

    /* DMAMUX1 Stream 5 → TIM1_UP (REQ_ID=15) */
    DMAMUX1_S5CR = DMAMUX_REQ_TIM1_UP;

    /* 禁用 Stream 5, 等 EN 清除 */
    DMA2_S5CR = 0;
    __asm__ volatile("dsb");
    { uint32_t tout = TIMEOUT; while ((DMA2_S5CR & 1) && --tout) {} }

    /* 清错误标志 (Stream 5: HIFCR bit 11=TEIF, bit 10=HTIF, bit 9=TCIF, bit 8=DMEIF) */
    *(volatile uint32_t *)(DMA2_BASE + 0x0C) = (1 << 11) | (1 << 10) | (1 << 9) | (1 << 8);

    /* EN=0 后才能写 M0AR */
    DMA2_S5PAR  = (uint32_t)&GPIOE_ODR;              /* 外设地址 */
    DMA2_S5M0AR = DTCM_BASE + 0x00E0;                 /* SHADOW_GPIO (0x200000E0) */
    DMA2_S5NDTR = 1;
    DMA2_S5FCR  = (1 << 2);                          /* DMDIS: 直通 */

    /* CR: DIR=M→P, CIRC, PSIZE=32, MSIZE=32, PL=最高 */
    { uint32_t cr = 0;
      cr |= (1 << 6);   /* DIR: M→P */
      cr |= (1 << 8);   /* CIRC: 循环 */
      cr |= (2 << 11);  /* PSIZE: 32-bit */
      cr |= (2 << 13);  /* MSIZE: 32-bit */
      cr |= (3 << 16);  /* PL: 最高优先级 */
      DMA2_S5CR = cr; }
    __asm__ volatile("dsb; isb");
    DMA2_S5CR |= 1;
    __asm__ volatile("dsb; isb");
}

void dma2_s5_disable(void) {
    DMA2_S5CR = 0;
    __asm__ volatile("dsb");
    { uint32_t tout = TIMEOUT; while ((DMA2_S5CR & 1) && --tout) {} }
}
