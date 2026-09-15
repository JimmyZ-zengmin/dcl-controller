#include "as5600.h"
#include <stddef.h>
#include "i2c_bb.h"
#include "engine.h"

static const i2c_bb_pin_t s_pins = { 1u, 10u, 11u };   /* GPIOB: SCL=PB10, SDA=PB11 */

volatile uint32_t g_as_raw_v = 0xFFFFFFFFu, g_as_deg_x1000 = 0xFFFFFFFFu,
                  g_as_status = 0xFFFFFFFFu, g_as_mag_ok = 0u,
                  g_as_ok_n = 0u, g_as_err_n = 0u, g_as_last_err = 0u;
volatile uint32_t g_as_scan_lo = 0xFFFFFFFFu, g_as_scan_hi = 0xFFFFFFFFu;

void as5600_init(void)
{
    i2c_bb_select(&s_pins);
}

void as5600_poll(uint8_t *base)
{
    uint8_t b[2];
    uint32_t e = i2c_bb_read(AS5600_ADDR, 0x0Cu, b, 2u);   /* RAW ANGLE: 0x0C(高4位在低半字节) 0x0D */
    if (e != 0u) {
        g_as_err_n++; g_as_last_err = e;
        return;                                            /* ★ 读失败时**保留上一次值**, 不写 0 ——
                                                            *   把"没读到"伪装成"角度=0"会让闭环跑飞 */
    }
    uint32_t raw = (((uint32_t)b[0] << 8) | b[1]) & 0x0FFFu;
    g_as_raw_v     = raw;
    g_as_deg_x1000 = (raw * 360000u) / 4096u;              /* 0..359999 = 0..359.999° */
    g_as_ok_n++;

    uint8_t st;
    if (i2c_bb_read(AS5600_ADDR, 0x0Bu, &st, 1u) == 0u) {  /* STATUS: MH(3)/ML(4)/MD(5) */
        g_as_status = st;
        g_as_mag_ok = ((st & (1u << 5)) != 0u) ? 1u : 0u;  /* MD=1 ⇒ 磁场在量程内 */
    }

    if (base != NULL) {
        *(volatile float *)(base + OFF_SENSOR_MAP + AS5600_SENSOR_RAW * 4u) = (float)raw;
        *(volatile float *)(base + OFF_SENSOR_MAP + AS5600_SENSOR_DEG * 4u) = (float)g_as_deg_x1000 / 1000.0f;
    }
}

void as5600_scan(uint32_t *ack, uint32_t *idrpd, uint32_t *raw, uint32_t *selftest2)
{
    static const uint8_t pairs[4][2] = { {10u, 11u}, {11u, 10u}, {6u, 7u}, {8u, 9u} };
    for (uint32_t k = 0u; k < 4u; k++) {
        i2c_bb_pin_t p = { 1u, pairs[k][0], pairs[k][1] };
        i2c_bb_select(&p);
        if (k == 0u) {
            uint32_t lo = 0u, hi = 0u;
            *selftest2 = i2c_bb_selftest(&lo, &hi);
            g_as_scan_lo = lo; g_as_scan_hi = hi;
        }
        if (ack)   { ack[k]   = i2c_bb_ping(AS5600_ADDR); }
        if (idrpd) { idrpd[k] = 0u; }                      /* 下拉 IDR 由 i2c_bb_selftest 覆盖, 此处留位 */
        if (raw) {
            raw[k] = 0xFFFFFFFFu;
            if (ack && ack[k]) {
                uint8_t b[2];
                if (i2c_bb_read(AS5600_ADDR, 0x0Cu, b, 2u) == 0u) {
                    raw[k] = (((uint32_t)b[0] << 8) | b[1]) & 0x0FFFu;
                }
            }
        }
    }
    i2c_bb_select(&s_pins);                                /* 回到正式接线 */
}
