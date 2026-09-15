#include "i2c_bb.h"
#include <stddef.h>
#include "regs.h"

/* ★★★ 两个掩码必须分开 —— 这是 2026-09-15 花了三轮才定位的坑:
 *   `MODER/PUPDR/OSPEEDR` 是**每引脚 2 位**; `OTYPER/ODR/IDR/BSRR` 是**每引脚 1 位**。
 *   第一版只写了一个 (1<<pin) 掩码, `GPIO_MODER |= it` 改的是**别的引脚**,
 *   PB10/PB11 从未切成输出 ⇒ 线一直靠外部上拉拉高 ⇒ I2C 的 START 条件根本不成立
 *   ⇒ 永远收不到 ACK, 而"线上有上拉"又让人以为是器件/接线问题。 */
static uint8_t s_port = 1u, s_scl = 10u, s_sda = 11u;

volatile uint32_t g_i2c_tx_n = 0u, g_i2c_ok_n = 0u, g_i2c_nak_n = 0u,
                  g_i2c_stuck_n = 0u, g_i2c_timeout_n = 0u;

static inline void dly(void)
{
    for (volatile uint32_t i = 0u; i < 400u; i++) { __asm__ volatile("nop"); }
}
static inline uint32_t bit_m(void)  { return (1u << s_scl) | (1u << s_sda); }
static inline uint32_t m2_m(void)   { return (3u << (s_scl * 2u)) | (3u << (s_sda * 2u)); }
static inline uint32_t out_m(void)  { return (1u << (s_scl * 2u)) | (1u << (s_sda * 2u)); }

static inline void scl_hi(void) { GPIO_BSRR(s_port) = (1u << s_scl); }
static inline void scl_lo(void) { GPIO_BSRR(s_port) = (1u << (s_scl + 16u)); }
static inline void sda_hi(void) { GPIO_BSRR(s_port) = (1u << s_sda); }
static inline void sda_lo(void) { GPIO_BSRR(s_port) = (1u << (s_sda + 16u)); }
static inline uint32_t sda_rd(void) { return (GPIO_IDR(s_port) >> s_sda) & 1u; }
static inline uint32_t scl_rd(void) { return (GPIO_IDR(s_port) >> s_scl) & 1u; }

static void mode_in(uint32_t pupd)      /* 0=浮空 1=上拉 2=下拉 */
{
    uint32_t m = m2_m();
    GPIO_MODER(s_port)   &= ~m;
    GPIO_PUPDR(s_port)   &= ~m;
    GPIO_PUPDR(s_port)   |=  (pupd << (s_scl * 2u)) | (pupd << (s_sda * 2u));
    GPIO_OTYPER(s_port)  |=  bit_m();                   /* 开漏 */
    GPIO_OSPEEDR(s_port) |=  m;
    __asm__ volatile("dsb" ::: "memory");
}

static void od_out(void)
{
    mode_in(1u);
    GPIO_MODER(s_port) |= out_m();                      /* ★ 2 位/引脚! */
    GPIO_ODR(s_port)   |= bit_m();                      /* 释放(高) */
    __asm__ volatile("dsb" ::: "memory");
}

void i2c_bb_select(const i2c_bb_pin_t *p)
{
    if (p == NULL) { return; }
    s_port = p->port; s_scl = p->scl; s_sda = p->sda;
    RCC_AHB4ENR |= (1u << s_port);
    od_out();
}

uint32_t i2c_bb_selftest(uint32_t *lo, uint32_t *hi)
{
    od_out();
    scl_lo(); sda_lo(); dly(); dly();
    uint32_t a = GPIO_IDR(s_port) & bit_m();
    scl_hi(); sda_hi(); dly(); dly();
    uint32_t b = GPIO_IDR(s_port) & bit_m();
    if (lo) { *lo = a; }
    if (hi) { *hi = b; }
    return (a == 0u && b == bit_m()) ? 1u : 0u;
}

/* 释放 SCL 并等它真的变高 (别的设备可能拉低它 = 时钟延展)。
 * ★ 这是"总线卡死"的**有界**等待 —— 没它会变成死循环, 把一个可诊断的故障变成挂死。 */
static uint32_t scl_release(void)
{
    scl_hi();
    for (uint32_t i = 0u; i < 200u; i++) { if (scl_rd()) { return 0u; } dly(); }
    g_i2c_stuck_n++;
    return 1u;
}

static void bus_recover(void)      /* 9 个 SCL 脉冲 + STOP, 解掉"从机卡在半途" */
{
    sda_hi();
    for (uint32_t i = 0u; i < 9u; i++) { scl_hi(); dly(); scl_lo(); dly(); }
    sda_lo(); dly(); scl_hi(); dly(); sda_hi(); dly();
}
static void st_start(void) { sda_hi(); scl_hi(); dly(); sda_lo(); dly(); scl_lo(); dly(); }
static void st_stop(void)  { sda_lo(); dly(); scl_hi(); dly(); sda_hi(); dly(); }

/* 返回 1 = 收到 ACK */
static uint32_t wr(uint32_t b)
{
    for (uint32_t i = 0u; i < 8u; i++) {
        if (b & 0x80u) { sda_hi(); } else { sda_lo(); }
        b <<= 1;
        dly();
        if (scl_release()) { return 0u; }
        dly(); scl_lo(); dly();
    }
    sda_hi();                              /* 释放 SDA 收 ACK */
    dly();
    if (scl_release()) { return 0u; }
    dly();
    uint32_t ack = (sda_rd() == 0u) ? 1u : 0u;
    scl_lo(); dly();
    return ack;
}

static uint32_t rd(uint32_t ack)
{
    uint32_t v = 0u;
    sda_hi();
    for (uint32_t i = 0u; i < 8u; i++) {
        dly();
        if (scl_release()) { return 0u; }
        dly();
        v = (v << 1) | sda_rd();
        scl_lo();
    }
    if (ack) { sda_lo(); } else { sda_hi(); }
    dly();
    if (scl_release()) { return v; }
    dly(); scl_lo(); dly();
    sda_hi();
    return v;
}

uint32_t i2c_bb_ping(uint8_t addr7)
{
    od_out();
    bus_recover();
    st_start();
    uint32_t ack = wr((uint32_t)addr7 << 1);
    st_stop();
    return ack;
}

uint32_t i2c_bb_read(uint8_t addr7, uint8_t reg, uint8_t *buf, uint32_t n)
{
    g_i2c_tx_n++;
    od_out();
    st_start();
    if (!wr((uint32_t)addr7 << 1)) { g_i2c_nak_n++; st_stop(); return 1u; }
    if (!wr(reg))                  { g_i2c_nak_n++; st_stop(); return 2u; }
    st_start();
    if (!wr(((uint32_t)addr7 << 1) | 1u)) { g_i2c_nak_n++; st_stop(); return 3u; }
    for (uint32_t i = 0u; i < n; i++) {
        buf[i] = (uint8_t)rd((i + 1u < n) ? 1u : 0u);
    }
    st_stop();
    g_i2c_ok_n++;
    return 0u;
}

uint32_t i2c_bb_write(uint8_t addr7, uint8_t reg, const uint8_t *buf, uint32_t n)
{
    g_i2c_tx_n++;
    od_out();
    st_start();
    if (!wr((uint32_t)addr7 << 1)) { g_i2c_nak_n++; st_stop(); return 1u; }
    if (!wr(reg))                  { g_i2c_nak_n++; st_stop(); return 2u; }
    for (uint32_t i = 0u; i < n; i++) {
        if (!wr(buf[i])) { g_i2c_nak_n++; st_stop(); return 4u; }
    }
    st_stop();
    g_i2c_ok_n++;
    return 0u;
}
