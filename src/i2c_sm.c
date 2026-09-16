/* ═══════════ 拍内推进的 I2C 事务状态机 (GAP-6 / G6-1, 2026-09-16) ═══════════
 * 设计依据: docs/REF-program-contract.md §3.6（四条硬约束）与 §3.7（实施与验收）
 * 与 src/i2c_sm.h 的关系: 头文件写"为什么"，这里写"怎么做"。
 *
 * ★★ 纪律: 本文件的**唯一验收判据**是"与阻塞路径(i2c_bb)读数一致"
 *    —— 两条独立路径对上才算数（i2c_bb.h:8 原话）。
 *    所以本模块**不替换** i2c_bb，两者并存; 总线靠 g_i2c_active 门互斥。
 */
#include "i2c_sm.h"
#include "i2c_bb.h"    /* ★ G6-2: 总线独占门（住在资源处）—— 两条路走同一道门 */
#include <stddef.h>
#include "regs.h"
#include "itcm.h"      /* ★ tick 在拍 ISR 内 ⇒ 必须住 ITCM（构建期闸门会查调用树）*/

/* ── 时序参数 ────────────────────────────────────────────────────────────
 * 目标 400 kHz: 位周期 2.5 µs = tLOW 1.5 µs + tHIGH 1.0 µs。
 * 经验换算: 现有 dly(400 次 nop) 实测 ≈ 4.5 µs ⇒ ≈ 4.5 cyc/次循环。
 *   ★★ 这两个数是**估出来的**, 不是量出来的 —— 所以：
 *      ① 它们可被 -D 覆盖（调参用）;  ② **验收必须先看器件答不答**（AS5600 有 NAK 计数）。
 *      ③ 若上机发现 NAK/STUCK 升高 ⇒ 先怀疑这里太快（tLOW 不足）, 把 LO 调大。
 *      ④ 本项目铁律: "配置全对 ≠ 功能可用" —— 时序对不对只有器件的 ACK 说了算。 */
#ifndef I2C_SM_LO_ITERS
#define I2C_SM_LO_ITERS  133u          /* ≈1.5 µs */
#endif
#ifndef I2C_SM_HI_ITERS
#define I2C_SM_HI_ITERS   89u          /* ≈1.0 µs */
#endif
#define I2C_SM_SCL_WAIT  40u           /* 时钟延展等待上限（次） */

/* 相位号 */
#define PH_IDLE    0u
#define PH_START   1u
#define PH_TX      2u
#define PH_RESTART 3u
#define PH_RX      4u
#define PH_STOP    5u
#define PH_DONE    6u

/* 引脚（与 i2c_bb 同组：GPIOB PB10=SCL / PB11=SDA） */
static uint8_t s_port = 1u, s_scl = 10u, s_sda = 11u;

static uint8_t  s_phase = PH_IDLE;
static uint8_t  s_bit, s_byte, s_rx;
static uint8_t  s_addr, s_op, s_reg, s_len, s_idx, s_seq, s_rxpend;
static uint8_t  s_rbuf[I2C_SM_MAX_DATA], s_tbuf[I2C_SM_MAX_DATA];
static uint32_t s_status = I2C_SM_ST_IDLE;
static uint32_t s_pcnt, s_tcnt;
static uint32_t s_run;

volatile uint8_t  g_i2c_sm_active = 0u;    /* 1 = 状态机事务在飞（阻塞路径要等它）*/
volatile uint8_t  g_i2c_sm_release_pending = 0u;  /* ★ ISR 置位, 主循环放门（见 i2c_sm.h）*/
volatile uint32_t g_i2c_sm_req_n = 0u, g_i2c_sm_ok_n = 0u, g_i2c_sm_nak_n = 0u,
                  g_i2c_sm_stuck_n = 0u, g_i2c_sm_gate_n = 0u, g_i2c_sm_ticks_n = 0u,
                  g_i2c_sm_clk_cyc_max = 0u;

static inline uint32_t bit1_m(void) { return (1u << s_scl) | (1u << s_sda); }
static inline uint32_t bit2_m(void) { return (3u << (s_scl * 2u)) | (3u << (s_sda * 2u)); }
static inline uint32_t out2_m(void) { return (1u << (s_scl * 2u)) | (1u << (s_sda * 2u)); }

static inline void scl_hi(void) { GPIO_BSRR(s_port) = (1u << s_scl); }
static inline void scl_lo(void) { GPIO_BSRR(s_port) = (1u << (s_scl + 16u)); }
static inline void sda_hi(void) { GPIO_BSRR(s_port) = (1u << s_sda); }
static inline void sda_lo(void) { GPIO_BSRR(s_port) = (1u << (s_sda + 16u)); }
static inline uint32_t sda_rd(void) { return (GPIO_IDR(s_port) >> s_sda) & 1u; }
static inline uint32_t scl_rd(void) { return (GPIO_IDR(s_port) >> s_scl) & 1u; }
static inline void ndly(uint32_t n) { for (volatile uint32_t i = 0u; i < n; i++) { __asm__ volatile("nop"); } }

/* 开漏输出形态。★ 两个掩码必须分开: MODER/PUPDR/OSPEEDR 每脚 2 位, OTYPER/ODR/IDR 每脚 1 位。
 *   （i2c_bb.c:5-9 记着这条: 混用会让引脚**从未切成输出**, 现象与"器件不应答"一模一样。） */
DCL_ITCM static void sm_od_init(void)
{
    uint32_t m = bit2_m();
    GPIO_MODER(s_port)   &= ~m;
    GPIO_PUPDR(s_port)   &= ~m;
    GPIO_PUPDR(s_port)   |= (1u << (s_scl * 2u)) | (1u << (s_sda * 2u));
    GPIO_OTYPER(s_port)  |= bit1_m();                 /* 开漏 */
    GPIO_OSPEEDR(s_port) |= m;
    GPIO_MODER(s_port)   |= out2_m();                 /* ★ 2 位/引脚 */
    GPIO_ODR(s_port)     |= bit1_m();                 /* 释放(高) */
    __asm__ volatile("dsb" ::: "memory");
}

/* 有界等 SCL 真的变高（从机可能做时钟延展）。返回 1 = 等到, 0 = 超界。 */
DCL_ITCM static uint32_t scl_wait(void)
{
    scl_hi();
    for (uint32_t i = 0u; i < I2C_SM_SCL_WAIT; i++) {
        if (scl_rd()) { return 1u; }
        ndly(8u);
    }
    return 0u;
}

/* 一个字节发完(含 ACK 位)之后, 决定下一个相位 */
DCL_ITCM static void tx_advance(void)
{
    if (s_op == I2C_SM_OP_PING) { s_phase = PH_STOP; return; }
    if (s_seq == 0u) { s_seq = 1u; s_byte = s_reg; s_phase = PH_TX; return; }
    if (s_seq == 1u) {
        if (s_op == I2C_SM_OP_WRITE) {
            if (s_len == 0u) { s_phase = PH_STOP; return; }
            s_seq = 2u; s_idx = 0u; s_byte = s_tbuf[0]; s_phase = PH_TX; return;
        }
        s_phase = PH_RESTART;            /* 读: 需要 RESTART 转读方向 */
        return;
    }
    if (s_rxpend) { s_rxpend = 0u; s_rx = 0u; s_phase = PH_RX; return; }
    s_idx++;
    if (s_idx >= s_len) { s_phase = PH_STOP; return; }
    s_byte = s_tbuf[s_idx];
    s_phase = PH_TX;
}

void i2c_sm_init(void)
{
    sm_od_init();
    s_phase = PH_IDLE;
    s_status = I2C_SM_ST_IDLE;
    s_run = 1u;
}

uint32_t i2c_sm_request(uint8_t addr7, uint8_t op, uint8_t reg, const uint8_t *tx, uint8_t n)
{
    if (!s_run) { return 0u; }
    if (s_phase != PH_IDLE && s_phase != PH_DONE) { return 0u; }   /* 上一个还在飞 */
    /* ★★★ 总线独占门（契约 §3.6 约束③ / §3.7 G6-2）——
     *   与阻塞路径(i2c_bb_*)走**同一道门**。占用中 ⇒ 明确拒绝 + 计数(能失败的判据)。
     *   ★ 判据怎么跑: `0x39 op=22 sub=0` 人工占住总线, 再发 `op=20 sub=0` ⇒
     *     必须被拒(本函数返回 0、status=GATE_BUSY、op=20 应答里的 gate_n +1)。 */
    if (!i2c_bus_acquire(I2C_OWNER_SM)) {
        g_i2c_sm_gate_n++;
        s_status = I2C_SM_ST_GATE_BUSY;
        s_phase  = PH_DONE;
        return 0u;
    }
    if (addr7 > 0x7Fu || op > I2C_SM_OP_WRITE) {
        s_status = I2C_SM_ST_BADARG; s_phase = PH_DONE; return 0u;
    }
    if (n > I2C_SM_MAX_DATA) { s_status = I2C_SM_ST_BADARG; s_phase = PH_DONE; return 0u; }
    if (op == I2C_SM_OP_WRITE && n > 0u && tx == NULL) {
        s_status = I2C_SM_ST_BADARG; s_phase = PH_DONE; return 0u;
    }

    s_addr = addr7; s_op = op; s_reg = reg; s_len = n; s_idx = 0u; s_seq = 0u;
    s_rxpend = 0u; s_byte = 0u; s_rx = 0u; s_bit = 8u;
    /* ★ 门已拿到（上面 acquire 过）⇒ 清掉上一次次留下的"待释放" —— 否则主循环
     *   会把**本次在飞的事务**的门放掉（延迟释放引入的新竞态, 必须在这里堵住）。 */
    g_i2c_sm_release_pending = 0u;
    if (op == I2C_SM_OP_WRITE) { for (uint32_t i = 0u; i < n; i++) { s_tbuf[i] = tx[i]; } }
    s_pcnt = 0u; s_tcnt = 0u;
    s_status = I2C_SM_ST_BUSY;
    g_i2c_sm_req_n++;
    g_i2c_sm_active = 1u;
    sm_od_init();                        /* 阻塞路径可能改过引脚形态 ⇒ 每次重设 */
    s_phase = PH_START;
    return 1u;
}

/* ★★ 每拍调用一次。**一个相位**一步 —— 这是"每拍有界"的全部含义。 */
DCL_ITCM void i2c_sm_tick(void)
{
    if (!s_run) { return; }
    if (s_phase == PH_IDLE || s_phase == PH_DONE) { return; }

    s_tcnt++;
    g_i2c_sm_ticks_n++;
    s_pcnt++;

    switch (s_phase) {
    case PH_START:
    case PH_RESTART:
        sda_hi(); scl_hi(); ndly(I2C_SM_LO_ITERS);
        sda_lo(); ndly(I2C_SM_LO_ITERS);
        scl_lo(); ndly(I2C_SM_HI_ITERS);
        if (s_phase == PH_RESTART) {
            s_seq = 2u; s_byte = (uint8_t)((s_addr << 1) | 1u); s_rxpend = 1u;
        } else {
            s_seq = 0u; s_byte = (uint8_t)(s_addr << 1); s_rxpend = 0u;
        }
        s_bit = 8u;
        s_phase = PH_TX;
        return;

    case PH_TX: {
        for (uint32_t i = 0u; i < 8u; i++) {
            if (s_byte & 0x80u) { sda_hi(); } else { sda_lo(); }
            s_byte = (uint8_t)(s_byte << 1);
            ndly(I2C_SM_LO_ITERS);
            if (!scl_wait()) { g_i2c_sm_stuck_n++; s_status = I2C_SM_ST_STUCK; s_phase = PH_STOP; return; }
            ndly(I2C_SM_HI_ITERS);
            scl_lo();
        }
        sda_hi();                                     /* 释放 SDA 收 ACK */
        ndly(I2C_SM_LO_ITERS);
        if (!scl_wait()) { g_i2c_sm_stuck_n++; s_status = I2C_SM_ST_STUCK; s_phase = PH_STOP; return; }
        ndly(I2C_SM_HI_ITERS);
        uint32_t ack = (sda_rd() == 0u) ? 1u : 0u;
        scl_lo(); ndly(I2C_SM_HI_ITERS);
        if (!ack) { g_i2c_sm_nak_n++; s_status = I2C_SM_ST_NAK; s_phase = PH_STOP; return; }
        tx_advance();
        return;
    }

    case PH_RX: {
        s_rx = 0u;
        sda_hi();                                     /* 释放 SDA 让从机驱动 */
        for (uint32_t i = 0u; i < 8u; i++) {
            ndly(I2C_SM_LO_ITERS);
            if (!scl_wait()) { g_i2c_sm_stuck_n++; s_status = I2C_SM_ST_STUCK; s_phase = PH_STOP; return; }
            ndly(I2C_SM_HI_ITERS);
            s_rx = (uint8_t)((s_rx << 1) | (uint8_t)sda_rd());
            scl_lo();
        }
        s_rbuf[s_idx] = s_rx;
        uint32_t more = (s_idx + 1u < s_len) ? 1u : 0u;
        if (more) { sda_lo(); } else { sda_hi(); }     /* ACK / NACK */
        ndly(I2C_SM_LO_ITERS);
        if (!scl_wait()) { g_i2c_sm_stuck_n++; s_status = I2C_SM_ST_STUCK; s_phase = PH_STOP; return; }
        ndly(I2C_SM_HI_ITERS);
        scl_lo(); ndly(I2C_SM_HI_ITERS);
        if (more) { s_idx++; s_phase = PH_RX; } else { s_phase = PH_STOP; }
        return;
    }

    case PH_STOP:
        sda_lo(); ndly(I2C_SM_LO_ITERS);
        scl_hi(); ndly(I2C_SM_HI_ITERS);
        sda_hi(); ndly(I2C_SM_HI_ITERS);
        if (s_status == I2C_SM_ST_BUSY) { s_status = I2C_SM_ST_OK; g_i2c_sm_ok_n++; }
        g_i2c_sm_active = 0u;
        /* ★ 放门**不在这里做** —— `i2c_bus_release()` 在 flash 里, 而本函数跑在拍 ISR:
         *   直接调它会违反 ISR 调用树不变量（闸门已当场拦下 `i2c_bus_release@0x08008C04`）;
         *   而"加 DCL_ITCM"走不通（**ITCM 已 100% 占满**）⇒ 改成置标志, 由主循环放门。 */
        g_i2c_sm_release_pending = 1u;
        s_phase = PH_DONE;
        return;

    default:
        g_i2c_sm_active = 0u;
        /* ★ 放门**不在这里做** —— `i2c_bus_release()` 在 flash 里, 而本函数跑在拍 ISR:
         *   直接调它会违反 ISR 调用树不变量（闸门已当场拦下 `i2c_bus_release@0x08008C04`）;
         *   而"加 DCL_ITCM"走不通（**ITCM 已 100% 占满**）⇒ 改成置标志, 由主循环放门。 */
        g_i2c_sm_release_pending = 1u;
        s_phase = PH_DONE;
        return;
    }
}

uint32_t i2c_sm_status(void)    { return s_status; }

/* ★ G6-2: 延迟放门 —— **只能在主循环调用**（本函数会碰 flash 里的门, 不能进 ISR）。 */
void i2c_sm_service(void)
{
    if (g_i2c_sm_release_pending != 0u) {
        g_i2c_sm_release_pending = 0u;
        i2c_bus_release(I2C_OWNER_SM);
    }
}uint32_t i2c_sm_phase(void)     { return s_phase; }
uint32_t i2c_sm_phase_cnt(void) { return s_pcnt; }
uint32_t i2c_sm_tick_cnt(void)  { return s_tcnt; }

uint32_t i2c_sm_result(uint8_t *buf, uint32_t n)
{
    if (buf == NULL || s_status != I2C_SM_ST_OK) { return 0u; }
    uint32_t c = (n < s_len) ? n : s_len;
    for (uint32_t i = 0u; i < c; i++) { buf[i] = s_rbuf[i]; }
    return c;
}
