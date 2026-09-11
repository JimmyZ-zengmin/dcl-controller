/*
 * adc.h — H723 ADC1 (16bit) 驱动 + AI 模拟量输入通道 (W5 外设域)
 *
 * 来源: esp32-core0/components/ai/ai.h (S3)。语义保留: 3 路 AI → SENSOR[8..10],
 * 工程量单位 **V** (float)。移植差异 (H723 侧):
 *   · S3 用 ADC oneshot 12bit (ADC1_CH1/CH2/CH7) → H723 **ADC1 16bit** (升级)
 *   · 通道: PA0=ADC1_INP16 / PA1=ADC1_INP17 / PA4=ADC12_INP18 (外脚)
 *   ★★ 内部通道 (VREFINT/温度) **在 H723 上挂在 ADC3(12bit)**, ADC1/2 才是 16bit
 *      (RM0468 / 官方 LL 头文件明确 "available only on ADC3") ⇒ 16bit 通路**必须**
 *      用外部脚验证, 不能靠内部通道自证。这是与 S3(ESP32 单 ADC 12bit) 的结构差异。
 *   · 时钟: adc_ker_ck = CLKP(per_ck) = **HSE 25MHz** (RCC_D1CCIPR.CKPERSEL=HSE)。
 *     选它而不选 AHB 同步时钟: HCLK=200MHz 只能 ÷1/2/4 得 50MHz(超 16bit 的
 *     fADC 上限); 而 HSE 25MHz 稳稳在限内。**不手推位域 —— 全部抄官方头文件**。
 *   · VDDA 假设 3.3V (板载)。绝对精度取决于它; 本文档已注明 (要更准需用 VREFINT 标定,
 *     但 VREFINT 在 ADC3 上, 属后续项)。
 */
#ifndef DCL_ADC_H
#define DCL_ADC_H

#include <stdint.h>

/* AI 3 路 → SENSOR[8..10] (与 S3 的 AI_SENSOR_BASE 同口径) */
#define AI_NCH          3
#define AI_SENSOR_BASE  8

/* 引脚用"端口编码"(与 macro VM 同一约定): 0..15=PA, 16..31=PB … */
#define AI_PIN_PA0      0
#define AI_PIN_PA1      1
#define AI_PIN_PA4      4

/* ADC1 通道号 (取自 stm32h723xx.h / betaflight H723 表, 非手推) */
#define AI_CH_PA0       16u   /* ADC1_INP16 */
#define AI_CH_PA1       17u   /* ADC1_INP17 */
#define AI_CH_PA4       18u   /* ADC12_INP18 */
#define HIL_FB_CH_PA5   19u   /* ADC12_INP19 (HIL 反馈, 由 hil.c 使用) */

void     adc_init(void);                 /* 时钟 + 上电 + 校准 + 16bit 配置 (调一次) */
uint16_t adc_read(uint32_t ch);          /* 单次转换读原始码 (0..65535), 失败返回 0xFFFF */
void     adc_analog_pin(uint32_t pin);   /* 把某引脚置 analog 模式 (供 HIL 用) */

void ai_init(uint8_t *base);             /* 配 3 路引脚 analog + SENSOR 初值 */
void ai_tick(uint8_t *base, uint32_t tick_now);   /* 主循环: 每 10ms 采一轮 → SENSOR */

/* 自检 (零接线): 用 GPIO 内部上拉/下拉把 AI 引脚拉到已知电平, 逐通道读 ADC。
 *   method 0 = 引脚置 **analog** + 上/下拉;  1 = 置 **input** + 上/下拉。
 *   out = [ch0_pullup, ch0_pulldown, ch1_pullup, ch1_pulldown, ch2_...] (AI_NCH*2 个 u16)
 * ★ 目的: 在没有外部信号源时, 证明"ADC 通路真的在按引脚电压出数"。
 *   拉高应接近满量程, 拉低应接近 0; 若两者相同 ⇒ ADC 没采到这个脚 (配置错)。 */
void ai_selftest(uint8_t *base, uint32_t method, uint16_t *out);

#endif /* DCL_ADC_H */
