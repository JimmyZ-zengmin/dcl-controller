/*
 * di.h — DI 数字量输入组件 (W5 外设域) — H723 移植版
 *
 * 来源: esp32-core0/components/di/di.h (S3)。语义保留: 4 路数字输入, 内部上拉,
 * 连续 3 次一致才提交 (30ms 去抖 @10ms 采样), 结果写 SENSOR[3..6] (1.0/0.0)。
 *   · S3 用 FreeRTOS 任务 + vTaskDelay(10ms) → H723 **主循环 + tick 自节流** (裸机无 RTOS)。
 *   · 引脚: S3 用 GPIO1/6/7/9 (S3 板) → H723 **PC0/PC1/PC2/PC3** (改 DI_PIN_* 即可换脚)。
 *   · 电气: 内部上拉 → 开关接 GND (按下=0) 或悬空=1; 也可外部灌 3.3V/0V。
 * ★ 去抖语义逐字保留 (含"S3 记过的坑": 电平抖动时重新计数, 不是"连续 N 次"就提交)。
 */
#ifndef DCL_DI_H
#define DCL_DI_H

#include <stdint.h>

#define DI_COUNT        4
#define DI_SENSOR_BASE  3        /* → SENSOR_MAP[3..6] (与 S3 同). 槽 0/1 原 DHT22, 2=HIL 反馈 */
#define DI_DEBOUNCE     3        /* 连续一致次数 (@10ms/次 = 30ms) */

/* 引脚 (端口编码: 0..15=PA, 16..31=PB, 32..47=PC, 48..63=PD) */
#define DI_PIN_PC0      32
#define DI_PIN_PC1      33
#define DI_PIN_PC2      34
#define DI_PIN_PC3      35

void di_init(uint8_t *base);
void di_tick(uint8_t *base, uint32_t tick_now);

/* 自检 (零接线): 用内部上拉/下拉把 DI 引脚拉到已知电平各读一次。
 *   out[8]: [ch0_pullup, ch0_pulldown, ch1_...]。上拉应=1, 下拉应=0。
 *   若两者相同 ⇒ 该脚读不到 (短路/配置错), 判据能失败。 */
void di_selftest(uint8_t *out);

#endif /* DCL_DI_H */
