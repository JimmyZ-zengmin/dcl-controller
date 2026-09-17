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

/* ★★★ 2026-09-17：反馈率是**闭环带宽的上限**，而它此前是硬编码 `100` 拍（10 ms = 100 Hz）。
 *   实测依据：`g_as_ok_n` 的增长速率 = **实际**更新率（不用黑匣子就能量）。
 *   · **周期做成可配**：`op=19 sub=20 arg=拍数`（默认 100 不变 ⇒ **既有行为逐位不变**）
 *   · 同时记 **`g_as_skip_bus_n`**：因 `i2c_bus_owner()==SM` **连调用都不发起**的次数
 *     —— 这条以前**不可观测**，而它直接决定"闭环拿到的是不是新鲜反馈"。
 *   ★ 单次读 ≈250 µs ⇒ **I2C 物理上限 ~4 kHz**；提高周期时占用率同比上升（100 拍 ⇒ 2.5%，10 拍 ⇒ 25%）。 */
extern volatile uint32_t g_as_skip_bus_n, g_as_period_ticks;
void     as5600_set_period_ticks(uint32_t n);   /* 钳到 [2, 100000]；复位即回默认 100 */
uint32_t as5600_period_now(void);
extern volatile uint32_t g_as_scan_lo, g_as_scan_hi;   /* as5600_scan 的自检两个读回值 */
#endif
