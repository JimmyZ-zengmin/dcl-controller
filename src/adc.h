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

/* 单次转换读原始码。返回 0 = 成功 (*out 有效), -1 = 超时/未就绪。
 * ★★ 为什么改成"状态码 + 出参"而不是"返回 0xFFFF 当错误哨兵":
 *   0xFFFF 是 **16bit 的合法满幅读数** —— 引脚接 3.3V 时本来就该读 0xFFFF。
 *   v0 版把 0xFFFF 当超时哨兵, 于是 hil.c 的 `if (r==0xFFFF) r=0;` 把**真实满幅**
 *   清成 0 ⇒ SENSOR[2] 在满幅时读回 0 (实测撞到, 花了很久定位)。
 *   ⇒ 教训: **错误哨兵绝不能落在合法值域内**。 */
int      adc_read(uint32_t ch, uint16_t *out);
void     adc_analog_pin(uint32_t pin);   /* 把某引脚置 analog 模式 (供 HIL 用) */

void ai_init(uint8_t *base);             /* 配 3 路引脚 analog + SENSOR 初值 */
void ai_tick(uint8_t *base, uint32_t tick_now);   /* 主循环: 每 10ms 采一轮 → SENSOR (对照档) */

/** @brief ★★ P2 拍内非阻塞 ADC 状态机: 由 ISR **每拍**调用一次。
 *
 *  ★ 为什么必须非阻塞 (本文件最重要的一个数字): `adc_read()` 单次转换 ≈ **259µs**
 *    (`SMP=810.5 周期 @ adc_ker_ck=3.125MHz`), 而拍长只有 100µs ⇒ 一次转换 = **2.6 拍**。
 *    在 ISR 里自旋等 EOC 会把拍周期直接撑成 ISR 时长。
 *  ★ 做法: 一拍启动, 若干拍后取结果。通道轮询 AI0→AI1→AI2→HIL_FB,
 *    一轮 16 拍 ⇒ AI 每 **1.6ms** 更新 (原 10ms);
 *    HIL 反馈累加 16 次 ⇒ **25.6ms** 出一个平均值 (原 10ms 窗口内阻塞 16 次, 功能等价)。
 *  ★ 与 adc_read 的关系: **共存**。adc_read 留给自检 (需要"立刻拿到本次结果");
 *    生产路径走本状态机。
 *  ★ 观测面 (缺了它们就等于静默失败):
 *      g_adc_sm_done    完成转换数 —— **正向证据**, 必须随运行单调增;
 *      g_adc_sm_timeout 超时数 —— **应恒 0**。两者成对读, 才能区分
 *                       "ADC 没坏" 与 "状态机根本没跑起来"。
 *  ★★ `long_call` = **正确性要求** (不是优化提示): ITCM 里的 ISR 到本函数 (flash)
 *     相距 128MB, 超出 Thumb `BL` 的 ±16MB ⇒ 必须走 BLX。实测: 靠链接器 veneer
 *     会整机卡进 Default_Handler (详见 main.c 里 s_io_in 那段证据链, 或 di.h)。 */
__attribute__((long_call)) void adc_poll(uint8_t *base, uint32_t tick_now);

extern volatile uint32_t g_adc_sm_done;
extern volatile uint32_t g_adc_sm_timeout;

/* 自检 (零接线): 用 GPIO 内部上拉/下拉把 AI 引脚拉到已知电平, 逐通道读 ADC。
 *   method 0 = 引脚置 **analog** + 上/下拉;  1 = 置 **input** + 上/下拉。
 *   out = [ch0_pullup, ch0_pulldown, ch1_pullup, ch1_pulldown, ch2_...] (AI_NCH*2 个 u16)
 * ★ 目的: 在没有外部信号源时, 证明"ADC 通路真的在按引脚电压出数"。
 *   拉高应接近满量程, 拉低应接近 0; 若两者相同 ⇒ ADC 没采到这个脚 (配置错)。 */
void ai_selftest(uint8_t *base, uint32_t method, uint16_t *out);

#endif /* DCL_ADC_H */
