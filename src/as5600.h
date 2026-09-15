#ifndef AS5600_H
#define AS5600_H
/* ═══════════ AS5600 磁编码器 (I2C, 地址 0x36) (2026-09-15) ═══════════
 * 接线 (已实测连通): SCL→PB10(U9 pin44)  SDA→PB11(U9 pin42)
 *                    VCC→板 +5V (模块单电源脚标 5V, 板载 LDO 降 3.3V)
 *                    PGO **悬空**(接 GND 会进编程模式)  DIR→GND(顺时针角增)
 * ★ 磁铁必须与轴同轴, 间隙 1~2mm —— 实测 MH=1(磁场太强)/AGC≈7 说明当时太近。 */
#include <stdint.h>

#define AS5600_ADDR        0x36u
/* ★ SENSOR_MAP 槽位: 0/1 是"原 DHT22"槽, H723 本工程没有 DHT22 ⇒ 借用。
 *   (2=HIL 反馈, 3..6=DI, 8..=AI —— 见 di.h / hil.h / adc.h 各自的 _SENSOR_BASE。) */
#define AS5600_SENSOR_RAW  0u      /* 原始 12 位 0..4095 */
#define AS5600_SENSOR_DEG  1u      /* 角度 0..360.0     */

void     as5600_init(void);
void     as5600_poll(uint8_t *base);     /* 读一次角度 → SENSOR_MAP + 全局量 (主循环调用) */
/* 上电诊断: 扫 4 组候选引脚对 (初次接线定位用)。ack/idrpd/raw 各 4 项。 */
void     as5600_scan(uint32_t *ack, uint32_t *idrpd, uint32_t *raw, uint32_t *selftest2);

/* 观测量 (每个都能失败) */
extern volatile uint32_t g_as_raw_v, g_as_deg_x1000, g_as_status, g_as_mag_ok;
extern volatile uint32_t g_as_ok_n, g_as_err_n, g_as_last_err;
extern volatile uint32_t g_as_scan_lo, g_as_scan_hi;   /* as5600_scan 的自检两个读回值 */
#endif
