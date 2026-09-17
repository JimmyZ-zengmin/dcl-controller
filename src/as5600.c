#include "as5600.h"
#include <stddef.h>
#include "i2c_bb.h"
#include "i2c_sm.h"      /* ★ 阶段 0：拍内发起/收结果 */
#include "itcm.h"        /* ★ 阶段 0：as5600_tick 在拍内 ⇒ 必须住 ITCM */
#include "engine.h"

static const i2c_bb_pin_t s_pins = { 1u, 10u, 11u };   /* GPIOB: SCL=PB10, SDA=PB11 */

volatile uint32_t g_as_raw_v = 0xFFFFFFFFu, g_as_deg_x1000 = 0xFFFFFFFFu,
                  g_as_status = 0xFFFFFFFFu, g_as_mag_ok = 0u,
                  g_as_ok_n = 0u, g_as_err_n = 0u, g_as_last_err = 0u;
volatile uint32_t g_as_scan_lo = 0xFFFFFFFFu, g_as_scan_hi = 0xFFFFFFFFu;

/* ★★★ 反馈率与"被总线挡掉"的计数（见 as5600.h）。默认 100 拍 = 10 ms ⇒ **既有行为不变**。 */
volatile uint32_t g_as_skip_bus_n    = 0u;
volatile uint32_t g_as_period_ticks  = 100u;

void as5600_set_period_ticks(uint32_t n)
{
    if (n < 2u)       { n = 2u; }          /* 下限：单次读就要 250 µs ⇒ 比这更密没有意义 */
    if (n > 100000u)  { n = 100000u; }     /* 上限：10 s */
    g_as_period_ticks = n;
}
uint32_t as5600_period_now(void) { return g_as_period_ticks; }

/* ★★★ 阶段 0：把"发起"搬进拍内（见 as5600.h 的说明）。★ **默认关** ⇒ 零行为变化。 */
volatile uint32_t g_as_sm_mode    = AS_SM_MODE_BLOCKING;
volatile uint32_t g_as_sm_busy_n  = 0u;   /* 发起被拒（别的请求在飞）*/
volatile uint32_t g_as_sm_nobase_n= 0u;   /* 还没拿到 base（没走过阻塞读）*/
volatile uint32_t g_as_sm_req     = 0u;   /* 在飞请求号；0 = 无 */
static uint8_t *s_as_base = NULL;         /* 从 as5600_poll 的入参记住 */

void as5600_set_sm_mode(uint32_t m)
{
    g_as_sm_mode = (m != 0u) ? AS_SM_MODE_TICK : AS_SM_MODE_BLOCKING;
    g_as_sm_req  = 0u;                    /* 切模式时把在飞的号作废（避免跨模式收错）*/
}
uint32_t as5600_sm_mode(void) { return g_as_sm_mode; }

/** @brief 把 raw 写进 `SENSOR[0]`（raw）与 `SENSOR[1]`（角度）。与 `as5600_poll` 同一算法。 */
static void as_write_shm(uint8_t *base, uint32_t raw)
{
    if (base == NULL) { return; }
    *(volatile float *)(base + OFF_SENSOR_MAP + AS5600_SENSOR_RAW * 4u) = (float)raw;
    *(volatile float *)(base + OFF_SENSOR_MAP + AS5600_SENSOR_DEG * 4u) =
        (float)((raw * 360000u) / 4096u) / 1000.0f;
}

/** @brief **拍 ISR** 内调用（在 `i2c_sm_tick()` **之后** —— 状态机本拍完成的话，这里就能收到）。 */
DCL_ITCM void as5600_tick(uint32_t tick_now)
{
    static uint32_t s_next = 0u;

    if (g_as_sm_mode != AS_SM_MODE_TICK) { return; }

    /* ── ① 收结果：归属核对住在 `i2c_sm_take_result()` 里（资源处）── */
    if (g_as_sm_req != 0u) {
        uint8_t b[2];
        if (i2c_sm_take_result(g_as_sm_req, b, 2u) >= 2u) {
            uint32_t raw = (((uint32_t)b[0] << 8) | b[1]) & 0x0FFFu;
            g_as_raw_v     = raw;
            g_as_deg_x1000 = (raw * 360000u) / 4096u;
            as_write_shm(s_as_base, raw);
            g_as_ok_n++;
            g_as_sm_req = 0u;
        } else if (i2c_sm_done_req() == g_as_sm_req) {
            /* 完成了但状态非 OK / 长度不对 ⇒ **丢弃并保留上次值**（写 0 会把"没读到"伪装成 0）*/
            g_as_err_n++;
            g_as_last_err = i2c_sm_done_status();
            g_as_sm_req = 0u;
        }
        /* 否则：还没完成 ⇒ 什么都不做，下一拍再看 */
        return;                          /* ★ 一次只推进一件事：在飞时不发起新的 */
    }

    /* ── ② 到点发起下一次 ── */
    if ((int32_t)(tick_now - s_next) >= 0) {
        s_next = tick_now + as5600_period_now();
        if (s_as_base == NULL) { g_as_sm_nobase_n++; return; }
        if (i2c_sm_request(AS5600_ADDR, I2C_SM_OP_READ, 0x0Cu, NULL, 2u) != 0u) {
            g_as_sm_req = g_i2c_sm_req_n;      /* ★ 受理后**立刻**记下自己那一号 */
        } else {
            g_as_sm_busy_n++;                  /* 别人在飞（如 dev_bind）⇒ 下一轮再试 */
        }
    }
}

void as5600_init(void)
{
    i2c_bb_select(&s_pins);
}

void as5600_poll(uint8_t *base)
{
    uint8_t b[2];
    s_as_base = base;      /* ★ 记住 base，供拍内模式（`as5600_tick`）写 SENSOR 用 */
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
