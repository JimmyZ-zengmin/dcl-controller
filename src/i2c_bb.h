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

/* ══════════ ★★★ 总线独占门（契约 §3.6 约束③「总线独占门」/ §3.7 G6-2）══════════
 * ## 为什么必须有
 * I2C 是**一条共享总线**（此处挂 AS5600），而它有两个驱动者：
 *   · **阻塞路径** `i2c_bb_*` —— 跑在**主循环**，一次读 ≈ 250 µs
 *   · **状态机**   `i2c_sm`   —— 在**拍 ISR** 里推进，一次事务跨 ~8 拍（≈800 µs）
 * 两者的**时间窗必然交叠**（ISR 不会等主循环）⇒ 同一对引脚上出现**两个写者** ⇒ 违反公理②。
 * 本项目已因"一条线两个写者"栽过两次（`TIM3` 两个驱动者 → C1；SD 卡两个写者）。
 *
 * ## ★★ 为什么把门放在**总线模块**里，而不是各调用点自己判断
 * 调用点判断只能保护"我以为的那个调用点"——**再多一个调用者就静默穿透**。
 * 本项目的同族教训：`0x43` 那条链、`do_latch_init` 的无条件启动，都是"守卫放错了层"。
 * ⇒ 门必须住在**资源的定义处**（谁拥有引脚，谁守门）。
 *
 * ## 判据（能失败）
 * 占用期再 `acquire` 别的 owner ⇒ **必须失败**且 `g_i2c_bus_busy_n++`。
 * 可用 `0x39 op=22`（诊断族）人工制造占用来跑这条判据。
 */
#define I2C_OWNER_NONE     0u
#define I2C_OWNER_BLOCKING 1u   /* 阻塞路径 i2c_bb_* */
#define I2C_OWNER_SM       2u   /* 拍内状态机 i2c_sm */

uint32_t i2c_bus_acquire(uint8_t owner);   /* 1 = 拿到（同 owner 重入**加计数**也算拿到）; 0 = 被占 */
void     i2c_bus_release(uint8_t owner);   /* 非持有者调用 = 空操作（不会误放别人的） */
uint8_t  i2c_bus_owner(void);
uint16_t i2c_bus_refs(void);               /* ★ 引用计数: 正常空闲必须 == 0（泄漏判据）*/

#define I2C_BB_ERR_BUSY   0x10u            /* i2c_bb_* 被门拦下时的返回码（非 0 = 失败）*/
extern volatile uint32_t g_i2c_bus_busy_n; /* 被门拦下的次数 */
#endif
