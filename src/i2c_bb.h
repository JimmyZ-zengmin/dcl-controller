#ifndef I2C_BB_H
#define I2C_BB_H
/* ═══════════ 位操作 I2C 主机 (2026-09-15) ═══════════
 * 为什么不用硬件 I2C1/I2C2:
 *   ① 本工程原本**一行 I2C 代码都没有** → 从零开始。硬件 I2C 会一并引入
 *      `TIMINGR` 计算、`ANFOFF` 模拟滤波、H7 I2C 的一堆 errata 等**新的失败面**;
 *   ② 实测需求只有 **10ms 读一次 2 字节 ≈ 250µs** ⇒ 占主循环 2.5% ⇒ 不值得冒那个风险。
 *   ★ 将来若 CPU 预算吃紧再换硬件 I2C —— 那时有现成判据可对照 (两条独立路径对上才算数)。
 *
 * ★★ 纪律 (2026-09-15 踩坑换来的): 位操作代码**必须配"输出通路自检"** ——
 *   把线驱动到低再读 IDR, 期望读到 0。只验"有没有上拉/有没有 ACK"是不够的:
 *   我第一版把 1 位/引脚的掩码用去写 `MODER`(2 位/引脚), 引脚**从未切成输出**,
 *   现象与"器件不应答"一模一样, 白猜了三轮硬件。 */
#include <stdint.h>

typedef struct { uint8_t port, scl, sda; } i2c_bb_pin_t;

void     i2c_bb_select(const i2c_bb_pin_t *p);        /* 绑定引脚 + 使能端口时钟 */
uint32_t i2c_bb_selftest(uint32_t *lo, uint32_t *hi); /* 1 = 通过(能拉低/能释放) */
uint32_t i2c_bb_ping(uint8_t addr7);                  /* 1 = 地址有 ACK */
uint32_t i2c_bb_read(uint8_t addr7, uint8_t reg, uint8_t *buf, uint32_t n);   /* 0 = OK */
uint32_t i2c_bb_write(uint8_t addr7, uint8_t reg, const uint8_t *buf, uint32_t n);

/* 计数器: 每个都能失败 ⇒ 都可作判据 */
extern volatile uint32_t g_i2c_tx_n, g_i2c_ok_n, g_i2c_nak_n, g_i2c_stuck_n, g_i2c_timeout_n;
#endif
