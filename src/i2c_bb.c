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

/* ══════════ 总线独占门（说明见 i2c_bb.h）══════════
 * ★★ **必须带引用计数** —— 这是实测踩出来的（G6-2 验收首跑 G2/G5 双 FAIL）：
 *   阻塞路径的 `i2c_bb_read()` 是 `acquire(BLOCKING) … release(BLOCKING)` 成对的,
 *   而 `as5600_poll` 每 10ms 就跑一次 ⇒ **它的一次读会把别人的占用顺手放掉**。
 *   现象: `0x39 op=22 sub=0` 占住后, 下一个 `op=22 sub=2` 读到的 owner 已经是 0,
 *   于是"占用期再申请必须被拒"这条判据**永远测不到**（门形同虚设）。
 *   ⇒ 单一 owner 只能表达"谁在用", 表达不了"**有几层在用**"。
 *   ★ 代价与缓解: 引用计数怕"释放不配对 ⇒ 永久占死"。本项目里 acquire/release 只出现在
 *     三个配对完整的包装函数 + 状态机的两处, 且诊断占用**有界自动释放** ⇒ 风险可控。
 *   ★ 观测量 `i2c_bus_refs()` 就是给"泄漏"准备的判据（正常空闲时必须 == 0）。 */
static volatile uint8_t  s_i2c_owner = I2C_OWNER_NONE;
static volatile uint16_t s_i2c_refs  = 0u;
volatile uint32_t g_i2c_bus_busy_n = 0u;

uint32_t i2c_bus_acquire(uint8_t owner)
{
    if (owner == I2C_OWNER_NONE) { return 0u; }
    uint8_t cur = s_i2c_owner;
    if (cur == I2C_OWNER_NONE) {
        s_i2c_owner = owner;
        s_i2c_refs  = 1u;
        __asm__ volatile("dsb" ::: "memory");
        return 1u;
    }
    if (cur == owner) {                    /* 同 owner 重入 ⇒ 只加计数 */
        if (s_i2c_refs < 0xFFFFu) { s_i2c_refs++; }
        return 1u;
    }
    g_i2c_bus_busy_n++;                    /* ★ 判据: 占用期再申请必须失败且计数 +1 */
    return 0u;
}

void i2c_bus_release(uint8_t owner)
{
    /* 只有持有者能释放 —— 否则"晚到的释放"会把别人的占用误放掉 */
    if (s_i2c_owner != owner) { return; }
    if (s_i2c_refs > 0u) { s_i2c_refs--; }
    if (s_i2c_refs == 0u) { s_i2c_owner = I2C_OWNER_NONE; }
    __asm__ volatile("dsb" ::: "memory");
}

uint8_t  i2c_bus_owner(void) { return s_i2c_owner; }
uint16_t i2c_bus_refs(void)  { return s_i2c_refs; }

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

static uint32_t ping_body(uint8_t addr7)
{
    od_out();
    bus_recover();
    st_start();
    uint32_t ack = wr((uint32_t)addr7 << 1);
    st_stop();
    return ack;
}

/* ★ 公开入口一律"**持门 → 干活 → 放门**"（三个入口同款）。
 *   门在**资源处**守着 ⇒ 后来再多一个调用者也不会静默穿透。
 *   拿不到门 ⇒ 返回 `I2C_BB_ERR_BUSY`（非 0 = 失败，与既有调用方的约定一致）。 */
uint32_t i2c_bb_ping(uint8_t addr7)
{
    if (!i2c_bus_acquire(I2C_OWNER_BLOCKING)) { return I2C_BB_ERR_BUSY; }
    uint32_t r = ping_body(addr7);
    i2c_bus_release(I2C_OWNER_BLOCKING);
    return r;
}

static uint32_t read_body(uint8_t addr7, uint8_t reg, uint8_t *buf, uint32_t n)
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

static uint32_t write_body(uint8_t addr7, uint8_t reg, const uint8_t *buf, uint32_t n)
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

/* ★ 读/写的公开入口 —— 与 ping 同款：持门 → 干活 → 放门。 */
uint32_t i2c_bb_read(uint8_t addr7, uint8_t reg, uint8_t *buf, uint32_t n)
{
    if (!i2c_bus_acquire(I2C_OWNER_BLOCKING)) { return I2C_BB_ERR_BUSY; }
    uint32_t r = read_body(addr7, reg, buf, n);
    i2c_bus_release(I2C_OWNER_BLOCKING);
    return r;
}

uint32_t i2c_bb_write(uint8_t addr7, uint8_t reg, const uint8_t *buf, uint32_t n)
{
    if (!i2c_bus_acquire(I2C_OWNER_BLOCKING)) { return I2C_BB_ERR_BUSY; }
    uint32_t r = write_body(addr7, reg, buf, n);
    i2c_bus_release(I2C_OWNER_BLOCKING);
    return r;
}
