/*
 * hil.h — HIL 硬件在环组件 (W5 外设域) — H723 移植版
 *
 * 来源: esp32-core0/components/hil/hil.h (S3)。语义保留:
 *   1. 输出臂: 读 WIRE_MAP[HIL_U_WIRE] 的 u (引擎 PID 输出) → 输出 PWM
 *   2. 反馈眼: PWM 经外部回路回来 → ADC 采样 → SENSOR_MAP[HIL_FB_SENSOR]
 * 交互全经共享内存, 引擎本体零改动 (路由侧 SRC_SENSOR/SRC_WIRE 天然支持)。
 *
 * 移植差异:
 *   · S3 用 LEDC 硬件 PWM (GPIO5) → H723 **TIM3_CH1 硬件 PWM (PA6, AF2)**。
 *     PWM 由定时器硬件产生, 引擎被暂停时波形照走 (同一设计意图)。
 *   · 反馈 ADC: S3 GPIO4 (ADC1_CH3) → H723 **PA5 = ADC1_INP19** (16bit, 精度升级)。
 *   · 采样/更新由主循环 10ms 驱动 (S3 是 1kHz Core0 任务)。
 * ★ 回环验证 (需 1 根线): PA6(PWM) 直连 PA5(反馈)。无 RC 滤波时 ADC 采到的是方波,
 *   固件按多次平均估占空比; 有 1kΩ+1µF 串成 RC 更干净 (见 docs)。
 */
#ifndef DCL_HIL_H
#define DCL_HIL_H

#include <stdint.h>

#define HIL_PWM_PIN     6      /* PA6 = TIM3_CH1 (AF2), 端口编码 6 = PA6 */
#define HIL_FB_PIN      5      /* PA5 = ADC1_INP19 (adc.c 里配 analog) */
#define HIL_FB_SENSOR   2      /* 反馈 → SENSOR_MAP[2] (与 S3 同: 0/1 原 DHT22) */
#define HIL_U_WIRE      20     /* u 源 → WIRE_MAP[20] (引擎 PID 输出写这里, 与 S3 同) */
#define HIL_PWM_HZ      1000   /* PWM 频率 1kHz */
#define HIL_PWM_RES     1024   /* 分辨率 10bit (1024 级, 与 S3 LEDC 同) */
#define HIL_FB_AVG      16     /* 无 RC 时反馈取 16 次平均 (方波占空比估计) */

void hil_init(uint8_t *base);
void hil_tick(uint8_t *base, uint32_t tick_now);

#endif /* DCL_HIL_H */
