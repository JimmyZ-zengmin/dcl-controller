/*
 * adc.c — H723 ADC1 16bit 驱动 + AI 通道 (W5 外设域)
 * 见 adc.h 的移植说明。本文件的关键纪律: **所有位域/偏移抄官方头文件**, 不手推。
 */
#include "adc.h"
#include "engine.h"
#include "regs.h"
#include "clock.h"

#define ADC1  ADC1_BASE

static volatile uint32_t s_adc_ready = 0;

/* 把引脚置 analog 模式 (11) + 无上下拉。ADC 取样前必须做, 否则数字输入缓冲会引入
 * 漏电/振荡 (本项目纪律: "配置全对≠功能可用" —— 这里 MODER 不对就是"全对里的一处")。 */
void adc_analog_pin(uint32_t pin)
{
    uint32_t port = pin >> 4, bit = pin & 15u;
    RCC_AHB4ENR |= (1u << port);
    uint32_t m = GPIO_MODER(port);
    m &= ~(3u << (bit * 2u));
    m |=  (3u << (bit * 2u));          /* 11 = analog */
    GPIO_MODER(port) = m;
    uint32_t p = GPIO_PUPDR(port);
    p &= ~(3u << (bit * 2u));          /* analog 模式: 无上下拉 */
    GPIO_PUPDR(port) = p;
}

static void adc_delay(volatile uint32_t n) { while (n--) { } }

void adc_init(void)
{
    /* ① 时钟: per_ck = HSE(25MHz), adc_ker_ck = CLKP(per_ck)
     *   ★ 只用 HSE: 不依赖 PLL2/PLL3 (本项目未配置), 25MHz 稳在 16bit fADC 限内。 */
    RCC_D1CCIPR = (RCC_D1CCIPR & ~(3u << RCC_D1CCIPR_CKPERSEL_SHIFT))
                | (2u << RCC_D1CCIPR_CKPERSEL_SHIFT);       /* 10 = HSE */
    RCC_D3CCIPR = (RCC_D3CCIPR & ~(3u << RCC_D3CCIPR_ADCSEL_SHIFT))
                | (2u << RCC_D3CCIPR_ADCSEL_SHIFT);         /* 10 = CLKP */
    RCC_AHB1ENR |= RCC_AHB1ENR_ADC12EN;

    /* ② ADC 时钟模式: CKMODE=00 → 用 adc_ker_ck (异步), PRESC=0 → /1。
     *   ★ 反直觉但已核官方头文件: CKMODE=00 才是异步; 01/10/11 才是 AHB+/1,/2,/4。 */
    ADC_CCR = (0u << ADC_CCR_CKMODE_SHIFT) | (0u << ADC_CCR_PRESC_SHIFT);

    /* ③ 上电: 退出深睡眠 + 使能内部稳压器 (★ 这两位在 ADC_CR 上, 不是 ADC_CCR) */
    ADC_CR(ADC1) &= ~ADC_CR_DEEPPWD;
    ADC_CR(ADC1) |=  ADC_CR_ADVREGEN;
    adc_delay(200000u);                                   /* tADC 稳压器稳定 (~ms 级余量) */

    /* ④ 校准 (照 HAL 口径: 先禁能再校准; ADVREGEN 必须在) */
    ADC_CR(ADC1) |= ADC_CR_ADDIS;
    { uint32_t g = 0; while ((ADC_CR(ADC1) & ADC_CR_ADEN) && ++g < 2000000u) { } }
    ADC_CR(ADC1) |= ADC_CR_BOOST;                         /* fADC 较高时建议 BOOST=1 */
    ADC_CR(ADC1) |= ADC_CR_ADCAL;
    { uint32_t g = 0; while ((ADC_CR(ADC1) & ADC_CR_ADCAL) && ++g < 20000000u) { } }

    /* ⑤ 配置 (禁用态): 16bit / 单次 / 软件触发; 所有通道给最长采样时间
     *   (810.5 周期 @25MHz ≈ 32µs) —— 本自检用内部 40kΩ 上拉驱动, 高阻源必须长采样。 */
    ADC_CFGR(ADC1)  = 0u;                                 /* RES=000 → 16bit, CONT=0 */
    ADC_SMPR1(ADC1) = 0x3FFFFFFFu;                        /* SMP0..SMP9  = 810.5 周期 */
    ADC_SMPR2(ADC1) = 0x3FFFFFFFu;                        /* SMP10..SMP19= 810.5 周期 */

    /* ⑥ 使能, 等 ADRDY */
    ADC_CR(ADC1) |= ADC_CR_ADEN;
    { uint32_t g = 0; while (!(ADC_ISR(ADC1) & ADC_ISR_ADRDY) && ++g < 2000000u) { } }

    s_adc_ready = 1;
}

uint16_t adc_read(uint32_t ch)
{
    if (!s_adc_ready) return 0xFFFFu;
    ADC_SQR1(ADC1) = (uint32_t)((ch & 0x1Fu) << 6);       /* L[3:0]=0 → 1 次转换; SQ1=ch */
    ADC_CR(ADC1) |= ADC_CR_ADSTART;
    uint32_t g = 0;
    while (!(ADC_ISR(ADC1) & ADC_ISR_EOC) && ++g < 5000000u) { }
    return (uint16_t)(ADC_DR(ADC1) & 0xFFFFu);            /* 读 DR 同时清 EOC */
}

/* ══════════ AI: 3 路模拟量 → SENSOR[8..10] (V) ══════════ */
static const uint32_t AI_PINS[AI_NCH] = { AI_PIN_PA0, AI_PIN_PA1, AI_PIN_PA4 };
static const uint32_t AI_CHS[AI_NCH]  = { AI_CH_PA0,  AI_CH_PA1,  AI_CH_PA4  };

static inline float ai_raw_to_volt(uint16_t raw)
{
    return (float)raw * 3.3f / 65535.0f;                  /* VDDA 假设 3.3V (见 adc.h) */
}

void ai_init(uint8_t *base)
{
    for (int i = 0; i < AI_NCH; i++) adc_analog_pin(AI_PINS[i]);
    for (int i = 0; i < AI_NCH; i++)
        *(volatile float *)(base + OFF_SENSOR_MAP + (uint32_t)(AI_SENSOR_BASE + i) * 4u) = 0.0f;
    __asm__ volatile("dsb" ::: "memory");
}

void ai_tick(uint8_t *base, uint32_t tick_now)
{
    static uint32_t last = 0;
    if ((uint32_t)(tick_now - last) < 100u) return;       /* 100 拍 = 10ms (同 S3 周期) */
    last = tick_now;
    for (int i = 0; i < AI_NCH; i++) {
        uint16_t raw = adc_read(AI_CHS[i]);
        *(volatile float *)(base + OFF_SENSOR_MAP + (uint32_t)(AI_SENSOR_BASE + i) * 4u) =
            ai_raw_to_volt(raw);
    }
    __asm__ volatile("dsb" ::: "memory");
}

void ai_selftest(uint8_t *base, uint32_t method, uint16_t *out)
{
    (void)base;
    for (int i = 0; i < AI_NCH; i++) {
        uint32_t pin = AI_PINS[i], port = pin >> 4, bit = pin & 15u;
        for (int k = 0; k < 2; k++) {                     /* k=0 上拉, k=1 下拉 */
            uint32_t m = GPIO_MODER(port);
            m &= ~(3u << (bit * 2u));
            if (method == 0u) m |= (3u << (bit * 2u));    /* method0: analog */
            GPIO_MODER(port) = m;                         /* method1: 保持 00=input */
            uint32_t p = GPIO_PUPDR(port);
            p &= ~(3u << (bit * 2u));
            p |= ((k == 0) ? 1u : 2u) << (bit * 2u);      /* 01=上拉 10=下拉 */
            GPIO_PUPDR(port) = p;
            adc_delay(200000u);                           /* 建立时间 (40kΩ 拉的 RC) */
            out[i * 2 + k] = adc_read(AI_CHS[i]);
        }
    }
    for (int i = 0; i < AI_NCH; i++) adc_analog_pin(AI_PINS[i]);   /* 恢复 */
}
