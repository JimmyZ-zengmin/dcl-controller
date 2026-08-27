/**
 * STM32H723 外设寄存器定义
 *
 * 所有外设基地址 + 寄存器偏移 + bit 宏集中在此。
 * 按外设分区: RCC, GPIO, DMA, DMAMUX, ADC, TIM1, USART2, FDCAN1, NVIC, DWT, SCB, FLASH
 */
#ifndef DCL_REGISTERS_H
#define DCL_REGISTERS_H

#include <stdint.h>

/* ═══════════════════════════════════════════════════════════
 * RCC (Reset and Clock Control)
 * ═══════════════════════════════════════════════════════════ */
#define RCC_BASE      0x58024400UL
#define RCC_CR        (*(volatile uint32_t *)(RCC_BASE + 0x00))
#define RCC_CFGR      (*(volatile uint32_t *)(RCC_BASE + 0x10))
#define RCC_D1CFGR    (*(volatile uint32_t *)(RCC_BASE + 0x18))
#define RCC_D2CFGR    (*(volatile uint32_t *)(RCC_BASE + 0x1C))
#define RCC_PLLCKSELR (*(volatile uint32_t *)(RCC_BASE + 0x28))
#define RCC_PLLCFGR   (*(volatile uint32_t *)(RCC_BASE + 0x2C))
#define RCC_PLL1DIVR  (*(volatile uint32_t *)(RCC_BASE + 0x30))
#define RCC_APB2ENR   (*(volatile uint32_t *)(RCC_BASE + 0x0F0))
#define RCC_APB1HENR  (*(volatile uint32_t *)(RCC_BASE + 0xEC))
#define RCC_AHB1ENR   (*(volatile uint32_t *)(RCC_BASE + 0x0D8)) /* H723: 0xD8! */
#define RCC_AHB4ENR   (*(volatile uint32_t *)(RCC_BASE + 0x0E0))
#define RCC_D3CCIPR   (*(volatile uint32_t *)(RCC_BASE + 0x138))

#define PWR_BASE      0x58024800UL
#define PWR_CR1       (*(volatile uint32_t *)(PWR_BASE + 0x00))   /* H723: reserved/SVOS (unused for VOS) */
#define PWR_CR3       (*(volatile uint32_t *)(PWR_BASE + 0x0C))   /* H723 VOS: VOS[5:4] (00=VOS0/550M,01=VOS1/400M), VOSRDY[6] */

#define FLASH_BASE    0x52002000UL
#define FLASH_ACR     (*(volatile uint32_t *)(FLASH_BASE + 0x00))

/* ═══════════════════════════════════════════════════════════ * GPIO
 * ═══════════════════════════════════════════════════════════ */
#define GPIOE_BASE    0x58021000UL
#define GPIOE_MODER   (*(volatile uint32_t *)(GPIOE_BASE + 0x00))
#define GPIOE_OSPEEDR (*(volatile uint32_t *)(GPIOE_BASE + 0x08))
#define GPIOE_ODR     (*(volatile uint32_t *)(GPIOE_BASE + 0x14))
#define GPIOE_AFRL    (*(volatile uint32_t *)(GPIOE_BASE + 0x20))
#define GPIOE_AFRH    (*(volatile uint32_t *)(GPIOE_BASE + 0x24))

#define GPIOD_BASE    0x58020C00UL
#define GPIOD_MODER   (*(volatile uint32_t *)(GPIOD_BASE + 0x00))
#define GPIOD_AFRL    (*(volatile uint32_t *)(GPIOD_BASE + 0x20))

/* ═══════════════════════════════════════════════════════════
 * DMA2 + DMAMUX1  (STM32H7: 头4个是ISR/IFCR, Stream从0x10开始)
 * ═══════════════════════════════════════════════════════════ */
#define DMA2_BASE     0x40020400UL
#define DMA2_S0CR     (*(volatile uint32_t *)(DMA2_BASE + 0x10))
#define DMA2_S0NDTR   (*(volatile uint32_t *)(DMA2_BASE + 0x14))
#define DMA2_S0PAR    (*(volatile uint32_t *)(DMA2_BASE + 0x18))
#define DMA2_S0M0AR   (*(volatile uint32_t *)(DMA2_BASE + 0x1C))
#define DMA2_S0FCR    (*(volatile uint32_t *)(DMA2_BASE + 0x24))
#define DMA2_S1CR     (*(volatile uint32_t *)(DMA2_BASE + 0x28))
#define DMA2_S1NDTR   (*(volatile uint32_t *)(DMA2_BASE + 0x2C))
#define DMA2_S1PAR    (*(volatile uint32_t *)(DMA2_BASE + 0x30))
#define DMA2_S1M0AR   (*(volatile uint32_t *)(DMA2_BASE + 0x34))
#define DMA2_S1FCR    (*(volatile uint32_t *)(DMA2_BASE + 0x3C))
#define DMA2_S5CR     (*(volatile uint32_t *)(DMA2_BASE + 0x88))
#define DMA2_S5NDTR   (*(volatile uint32_t *)(DMA2_BASE + 0x8C))
#define DMA2_S5PAR    (*(volatile uint32_t *)(DMA2_BASE + 0x90))
#define DMA2_S5M0AR   (*(volatile uint32_t *)(DMA2_BASE + 0x94))
#define DMA2_S5FCR    (*(volatile uint32_t *)(DMA2_BASE + 0x9C))

#define DMAMUX1_BASE  0x40020800UL
#define DMAMUX1_S0CR  (*(volatile uint32_t *)(DMAMUX1_BASE + 0x00))
#define DMAMUX1_S1CR  (*(volatile uint32_t *)(DMAMUX1_BASE + 0x04))
#define DMAMUX1_S5CR  (*(volatile uint32_t *)(DMAMUX1_BASE + 0x14))

#define DMAMUX_REQ_TIM1_UP  15
#define DMAMUX_REQ_ADC1     9

/* ═══════════════════════════════════════════════════════════
 * ADC1 + ADC12 Common
 * ═══════════════════════════════════════════════════════════ */
#define ADC1_BASE     0x40022000UL
#define ADC1_ISR      (*(volatile uint32_t *)(ADC1_BASE + 0x00))
#define ADC1_CR       (*(volatile uint32_t *)(ADC1_BASE + 0x08))
#define ADC1_CFGR     (*(volatile uint32_t *)(ADC1_BASE + 0x0C))
#define ADC1_SMPR1    (*(volatile uint32_t *)(ADC1_BASE + 0x14))
#define ADC1_SMPR2    (*(volatile uint32_t *)(ADC1_BASE + 0x18))
#define ADC1_PCSEL    (*(volatile uint32_t *)(ADC1_BASE + 0x1C))
#define ADC1_SQR1     (*(volatile uint32_t *)(ADC1_BASE + 0x30))
#define ADC1_DR       (*(volatile uint32_t *)(ADC1_BASE + 0x40))

#define ADC12_COMMON  0x40022300UL
#define ADC12_CCR     (*(volatile uint32_t *)(ADC12_COMMON + 0x08))

/* ═══════════════════════════════════════════════════════════
 * TIM1 (高级定时器)
 * ═══════════════════════════════════════════════════════════ */
#define TIM1_BASE     0x40010000UL
#define TIM1_CR1      (*(volatile uint16_t *)(TIM1_BASE + 0x00))
#define TIM1_CR2      (*(volatile uint16_t *)(TIM1_BASE + 0x04))
#define TIM1_SMCR     (*(volatile uint16_t *)(TIM1_BASE + 0x08))
#define TIM1_DIER     (*(volatile uint16_t *)(TIM1_BASE + 0x0C))
#define TIM1_SR       (*(volatile uint16_t *)(TIM1_BASE + 0x10))
#define TIM1_CCMR1    (*(volatile uint16_t *)(TIM1_BASE + 0x18))
#define TIM1_CCMR2    (*(volatile uint16_t *)(TIM1_BASE + 0x1C))
#define TIM1_CCER     (*(volatile uint16_t *)(TIM1_BASE + 0x20))
#define TIM1_PSC      (*(volatile uint16_t *)(TIM1_BASE + 0x28))
#define TIM1_ARR      (*(volatile uint16_t *)(TIM1_BASE + 0x2C))
#define TIM1_CCR1     (*(volatile uint16_t *)(TIM1_BASE + 0x34))
#define TIM1_CCR2     (*(volatile uint16_t *)(TIM1_BASE + 0x38))
#define TIM1_CCR3     (*(volatile uint16_t *)(TIM1_BASE + 0x3C))
#define TIM1_CCR4     (*(volatile uint16_t *)(TIM1_BASE + 0x40))
#define TIM1_BDTR     (*(volatile uint16_t *)(TIM1_BASE + 0x44))
#define TIM1_DCR      (*(volatile uint16_t *)(TIM1_BASE + 0x48))
#define TIM1_DMAR     (*(volatile uint16_t *)(TIM1_BASE + 0x4C))

/* ═══════════════════════════════════════════════════════════
 * USART2 (APB1)
 * ═══════════════════════════════════════════════════════════ */
#define USART2_BASE   0x40004400UL
#define USART2_CR1    (*(volatile uint32_t *)(USART2_BASE + 0x00))
#define USART2_CR2    (*(volatile uint32_t *)(USART2_BASE + 0x04))
#define USART2_CR3    (*(volatile uint32_t *)(USART2_BASE + 0x08))
#define USART2_BRR    (*(volatile uint32_t *)(USART2_BASE + 0x0C))
#define USART2_ISR    (*(volatile uint32_t *)(USART2_BASE + 0x1C))
#define USART2_ICR    (*(volatile uint32_t *)(USART2_BASE + 0x20))
#define USART2_RDR    (*(volatile uint32_t *)(USART2_BASE + 0x24))
#define USART2_TDR    (*(volatile uint32_t *)(USART2_BASE + 0x28))
#define USART2_PRESC  (*(volatile uint32_t *)(USART2_BASE + 0x2C))

/* ═══════════════════════════════════════════════════════════
 * DMA2 Stream 2 (USART2_RX)
 * ═══════════════════════════════════════════════════════════ */
#define DMA2_S2CR     (*(volatile uint32_t *)(DMA2_BASE + 0x30))
#define DMA2_S2NDTR   (*(volatile uint32_t *)(DMA2_BASE + 0x34))
#define DMA2_S2PAR    (*(volatile uint32_t *)(DMA2_BASE + 0x38))
#define DMA2_S2M0AR   (*(volatile uint32_t *)(DMA2_BASE + 0x3C))
#define DMA2_S2FCR    (*(volatile uint32_t *)(DMA2_BASE + 0x44))

#define DMAMUX1_S2CR  (*(volatile uint32_t *)(DMAMUX1_BASE + 0x08))
#define DMAMUX_REQ_USART2_RX  35   /* H723 RM0468 DMAMUX Table */

#define RCC_APB1LENR  (*(volatile uint32_t *)(RCC_BASE + 0x0E8))  /* USART2, SPI2, etc. */

/* ═══════════════════════════════════════════════════════════
 * FDCAN1
 * ═══════════════════════════════════════════════════════════ */
#define FDCAN1_BASE       0x4000AC00UL
#define FDCAN1_IR         (*(volatile uint32_t *)(FDCAN1_BASE + 0x00))
#define FDCAN1_CREL       (*(volatile uint32_t *)(FDCAN1_BASE + 0x04))
#define FDCAN1_ENDN       (*(volatile uint32_t *)(FDCAN1_BASE + 0x08))
#define FDCAN1_DBTP       (*(volatile uint32_t *)(FDCAN1_BASE + 0x0C))
#define FDCAN1_TEST       (*(volatile uint32_t *)(FDCAN1_BASE + 0x10))
#define FDCAN1_RWD        (*(volatile uint32_t *)(FDCAN1_BASE + 0x14))
#define FDCAN1_CCCR       (*(volatile uint32_t *)(FDCAN1_BASE + 0x18))
#define FDCAN1_NBTP       (*(volatile uint32_t *)(FDCAN1_BASE + 0x1C))
#define FDCAN1_TSCC       (*(volatile uint32_t *)(FDCAN1_BASE + 0x20))
#define FDCAN1_IRQ        (*(volatile uint32_t *)(FDCAN1_BASE + 0x50))
#define FDCAN1_IE         (*(volatile uint32_t *)(FDCAN1_BASE + 0x54))
#define FDCAN1_ILS        (*(volatile uint32_t *)(FDCAN1_BASE + 0x58))
#define FDCAN1_ILE        (*(volatile uint32_t *)(FDCAN1_BASE + 0x5C))
#define FDCAN1_RXF0C      (*(volatile uint32_t *)(FDCAN1_BASE + 0xA0))
#define FDCAN1_RXF0S      (*(volatile uint32_t *)(FDCAN1_BASE + 0xA4))
#define FDCAN1_RXF0A      (*(volatile uint32_t *)(FDCAN1_BASE + 0xA8))
#define FDCAN1_TXBC       (*(volatile uint32_t *)(FDCAN1_BASE + 0xC0))
#define FDCAN1_TXFQS      (*(volatile uint32_t *)(FDCAN1_BASE + 0xC4))
#define FDCAN1_TXBAR      (*(volatile uint32_t *)(FDCAN1_BASE + 0xCC))
#define FDCAN1_MSGRAM     0x4000B400UL
#define FDCAN1_RX_FIFO0_OFFSET 0
#define FDCAN1_TX_FIFO_OFFSET  0x300

#define FDCAN_CCCR_INIT   (1<<0)
#define FDCAN_CCCR_CCE    (1<<1)

/* ═══════════════════════════════════════════════════════════ * NVIC
 * ═══════════════════════════════════════════════════════════ */
#define NVIC_BASE     0xE000E100UL
#define NVIC_ISER0    (*(volatile uint32_t *)(NVIC_BASE + 0x00))
#define NVIC_ICER0    (*(volatile uint32_t *)(NVIC_BASE + 0x80))
#define NVIC_ISPR0    (*(volatile uint32_t *)(NVIC_BASE + 0x100))
#define NVIC_ICPR0    (*(volatile uint32_t *)(NVIC_BASE + 0x180))

/* ═══════════════════════════════════════════════════════════
 * DWT + SCB
 * ═══════════════════════════════════════════════════════════ */
#define DWT_BASE      0xE0001000UL
#define DWT_CTRL      (*(volatile uint32_t *)(DWT_BASE + 0x00))
#define DWT_CYCCNT    (*(volatile uint32_t *)(DWT_BASE + 0x04))

#define SCB_BASE      0xE000ED00UL
#define SCB_CPACR     (*(volatile uint32_t *)(SCB_BASE + 0x088))
#define SCB_VTOR      (*(volatile uint32_t *)(SCB_BASE + 0x008UL))
#define SCB_CFSR      (*(volatile uint32_t *)(SCB_BASE + 0x028))

#define DEMCR         (*(volatile uint32_t *)0xE000EDFCUL)

/* 常用超时 */
#ifndef TIMEOUT
#define TIMEOUT 8000000
#endif

#endif /* DCL_REGISTERS_H */
