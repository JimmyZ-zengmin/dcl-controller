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
#include "engine.h"    /* ★ `i2c_xact_reset()` 要用 OFF_I2C_XACT / IX_* / SHM_U32 —— 
                        *   本模块是 SHM 事务区的**定义者之一**, 这些符号都在这里。
                        *   ★ 缺这一行时 `i2c_xact_reset()` 根本编不过（本项目纪律:
                        *     "声明了却没人编过" 与 "实现了却没接线" 是同一族缺陷）。 */
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
/* ★★★ 欠释放**计数**（不是 bool）—— 2026-09-16 实测"泄漏 61 个引用"后改成计数。见 i2c_sm.h 的说明。 */
volatile uint16_t g_i2c_sm_release_pending = 0u;
volatile uint32_t g_i2c_ref_leak_n = 0u;   /* 主循环对账发现"空闲却仍被 SM 占"的次数（可读、有界）*/
volatile uint32_t g_i2c_sm_req_n = 0u, g_i2c_sm_ok_n = 0u, g_i2c_sm_nak_n = 0u,
                  g_i2c_sm_stuck_n = 0u, g_i2c_sm_gate_n = 0u, g_i2c_sm_ticks_n = 0u,
                  g_i2c_sm_clk_cyc_max = 0u;
/* ★★★ 事务归属令牌（2026-09-16，G6-4 落地时发现）—— **必须住在资源处**（本项目 §4.5 铁律）。
 *
 * ## 它挡的是什么（真缺陷，不是理论风险）
 * 本状态机只有**一个** `s_rbuf` / `s_status` / `s_len`，而现在有**两个使用者**：
 *   `i2c_shm_service()`（G6-3 SHM 事务区）与 `dev_bind_service()`（G6-4 设备绑定表）。
 * 完成判据一直是 `i2c_sm_status() == I2C_SM_ST_OK` —— 它只回答"**有没有人**跑完了一次全 ACK 的
 * 事务"，**不回答"跑完的是不是我要的那次"**。
 * ⇒ 于是存在这条静默错数据路径：
 *     ① 我发起 → ② 事务完成(OK, `s_rbuf` = 我的数据) → ③ 另一个使用者在我收尾前
 *     发了新事务并且也完成了(`s_rbuf` = **他的**数据) → ④ 我按 OK 判据收尾,
 *     把**他的**读数写进我的 `SENSOR[dst]`。
 *     全程无报错、计数器全绿、寄存器全对 —— 只有值悄悄是错的。
 * 判据：两步之间**别的使用者能不能插入**。能插入 ⇒ 就一定会发生（本项目"守护放错层"同族）。
 *
 * ## 修法 = 给事务发号，并广播"**哪一号**完成了"
 *   `i2c_sm_request()` 受理时 `g_i2c_sm_req_n++`（既有）⇒ 发起方记下自己那一号；
 *   完成时把该号快照到 `g_i2c_sm_done_req` ⇒ 发起方只需 `done_req == my_req` 才能收尾。
 *   别人的事务完成后 `done_req != my_req` ⇒ 发起方判为"我的那次被覆盖了"（丢了, 需重发），
 *   **绝不会把别人的值当成自己的**。
 * ★ 为什么放这里而不是在调用点各加一个判断: 调用点会越加越多（现在就两个, 将来三个）,
 *   而"守卫必须住在资源的定义处"正是为了"多一个调用者不会静默穿透"。 */
volatile uint32_t g_i2c_sm_done_req = I2C_SM_DONE_NONE;   /* 初始"无完成记录" */
/* ★★★ 完成记录（2026-09-16 第二轮审计后补）—— `{req, status, len}` 三者必须**同一刻快照**。
 *
 * 为什么单有 `done_req` 不够（真漏洞）:
 *   `s_status` 是**实时态** —— 一个**被拒**的请求（门忙 / BADARG）会覆写它。
 *   时序: 我的事务完成(OK) → 别人发请求被门拒(`s_status=GATE_BUSY`) → 我收尾
 *   ⇒ 令牌匹配、而 `s_status` 已不是 OK ⇒ 我**丢弃自己的好数据并计一次 err**。
 *   `s_len` 同理（下一个事务会覆写它）⇒ 取结果可能拷回**张冠李戴的长度**。
 * ⇒ 处方: **完成的一刻**把三元组拷进完成记录；**受理新请求时立刻失效**（`done_req=NONE`）。
 *   于是"取结果"不再依赖任何实时字段 —— 这叫**完成态与实时态分开**。 */
volatile uint32_t g_i2c_sm_done_status = I2C_SM_ST_IDLE;
static uint8_t    s_done_len = 0u;

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
    /* ★★★ 参数校验必须在**拿门之前**（2026-09-16 上机实测抓到的真缺陷）。
     *   原实现是"先 `i2c_bus_acquire()`、再校验参数"，而三条"参数非法"分支
     *   `s_status = BADARG; s_phase = PH_DONE; return 0u;` —— **直接返回、不放门**
     *   ⇒ **引用计数泄漏**：owner 永久卡在 `I2C_OWNER_SM`，阻塞路径再也拿不到总线。
     *   症状（实测）: `0x39 op=22` 读回 `owner=2` **冻结不变**、`refs != 0`、
     *     而状态机早已 idle（`status=OK`、`phase=DONE`）——
     *     ⇒ 之后**所有**依赖阻塞路径的判据（AS5600 轮询、G6-2 的 G1/G4/G5、G6-3 的 S6）
     *       **一起变红**，而错误方向指向"总线被谁占了"，与真因（一次参数非法的请求）相隔很远。
     *   ★ 抓到它的是**专为这类泄漏准备的 `refs` 计数**（G6-2 的 `i2c_bus_refs()`，
     *     `0x39 op=22 +32`）—— 又一个"为可解释性加的观测，顺手抓住静默故障"的实例。
     *   ★ 一般化: **不要为一个你准备拒绝的请求去占用资源**；真要占，就必须保证每条
     *     出口都成对释放（"成对"这种事靠人记，迟早漏 —— 所以用结构性写法：先验后占）。 */
    if (addr7 > 0x7Fu || op > I2C_SM_OP_WRITE) {
        s_status = I2C_SM_ST_BADARG; s_phase = PH_DONE; return 0u;
    }
    if (n > I2C_SM_MAX_DATA) { s_status = I2C_SM_ST_BADARG; s_phase = PH_DONE; return 0u; }
    if (op == I2C_SM_OP_WRITE && n > 0u && tx == NULL) {
        s_status = I2C_SM_ST_BADARG; s_phase = PH_DONE; return 0u;
    }
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

    s_addr = addr7; s_op = op; s_reg = reg; s_len = n; s_idx = 0u; s_seq = 0u;
    s_rxpend = 0u; s_byte = 0u; s_rx = 0u; s_bit = 8u;
    /* ★★★ 这里**不能**清 `g_i2c_sm_release_pending`（2026-09-16 实测：清了会泄漏引用计数）。
     *   原意图是"别让主循环把本次在飞事务的门放掉"，但那样会**丢掉上一次欠的释放**：
     *   上一个事务若恰在 `i2c_sm_service()` **之后**、本次请求**之前**完成，它的释放就永远没人做。
     *   实测累积到 **refs=61**（`owner=2(SM)` 冻结、表为空、状态机 idle）⇒ **阻塞路径永久拿不到总线**
     *   —— 而症状表现为"G6-2/G6-3 一起变红"，离真因很远。
     *   ⇒ 改为**配对计数**：欠几次就在 `i2c_sm_service()` 里还几次。语义天然正确
     *     （每次 acquire 最终恰好对应一次 release），受理处**不需要**任何补偿动作。 */
    if (op == I2C_SM_OP_WRITE) { for (uint32_t i = 0u; i < n; i++) { s_tbuf[i] = tx[i]; } }
    s_pcnt = 0u; s_tcnt = 0u;
    s_status = I2C_SM_ST_BUSY;
    /* ★★ 受理即**失效完成记录** —— 从这一刻起"没有结果可取"，
     *   直到本次事务真的完成。这一条同时堵住"新事务读了一半数据就把 s_rbuf 改掉"的窗口。 */
    g_i2c_sm_done_req    = I2C_SM_DONE_NONE;
    g_i2c_sm_done_status = I2C_SM_ST_BUSY;
    s_done_len           = 0u;
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
        /* ★ 完成的一刻**同时**快照三元组 {序号, 状态, 长度} —— 收尾方据此判断
         *   "有没有结果 / 是不是我的 / 多长"。此后即使别人发起并被拒（覆写 `s_status`），
         *   这份记录也不受影响。 */
        g_i2c_sm_done_req    = g_i2c_sm_req_n;
        g_i2c_sm_done_status = s_status;
        s_done_len           = s_len;
        /* ★ 放门**不在这里做** —— `i2c_bus_release()` 在 flash 里, 而本函数跑在拍 ISR:
         *   直接调它会违反 ISR 调用树不变量（闸门已当场拦下 `i2c_bus_release@0x08008C04`）;
         *   而"加 DCL_ITCM"走不通（**ITCM 已 100% 占满**）⇒ 改成**记一笔欠释放**, 由主循环还。 */
        if (g_i2c_sm_release_pending < 0xFFFFu) { g_i2c_sm_release_pending++; }
        s_phase = PH_DONE;
        return;

    default:
        g_i2c_sm_active = 0u;
        /* ★ 完成记录（同上）—— default 也是"事务结束"的一条出口，必须同样快照。 */
        g_i2c_sm_done_req    = g_i2c_sm_req_n;
        g_i2c_sm_done_status = s_status;
        s_done_len           = s_len;
        /* ★ 放门**不在这里做**（同 PH_STOP）⇒ 记一笔欠释放, 主循环还。 */
        if (g_i2c_sm_release_pending < 0xFFFFu) { g_i2c_sm_release_pending++; }
        s_phase = PH_DONE;
        return;
    }
}

uint32_t i2c_sm_status(void)    { return s_status; }

/* ★★★ 归属令牌 + 完成记录读取口（2026-09-16）。语义与用法见 `i2c_sm.h`。
 * 收尾方三步：① 实时态非 BUSY ② `done_req == my` ③ `take_result(my, …)`。 */
uint32_t i2c_sm_done_req(void)    { return g_i2c_sm_done_req; }
uint32_t i2c_sm_done_status(void) { return g_i2c_sm_done_status; }

/* 取**最近一次完成**的数据。★ 它不再看实时 `s_status`/`s_len` —— 那两样会被后续动作改掉
 *   （这正是第二轮审计挖出的漏洞：被拒的请求会覆写 `s_status`）。改用完成记录。 */
static uint32_t sm_copy_done(uint8_t *buf, uint32_t n)
{
    if (buf == NULL) { return 0u; }
    if (g_i2c_sm_done_req == I2C_SM_DONE_NONE) { return 0u; }      /* 无完成记录 */
    if (g_i2c_sm_done_status != I2C_SM_ST_OK) { return 0u; }       /* 完成了但没成功 */
    uint32_t c = (n < (uint32_t)s_done_len) ? n : (uint32_t)s_done_len;
    for (uint32_t i = 0u; i < c; i++) { buf[i] = s_rbuf[i]; }
    return c;
}

uint32_t i2c_sm_result(uint8_t *buf, uint32_t n)
{
    return sm_copy_done(buf, n);
}

uint32_t i2c_sm_take_result(uint32_t my_req, uint8_t *buf, uint32_t n)
{
    /* ★ 归属核对放在**资源处**（不是在各调用点）—— 多一个使用者也不会静默穿透。 */
    if (g_i2c_sm_done_req != my_req) { return 0u; }
    return sm_copy_done(buf, n);
}

/* ★★ I2C 事务区的冷启动登记（由 cold_start_reset 调用）—— 见 i2c_sm.h 的说明。
 *   ★ 只碰 SHM，不碰硬件：`cold_start_reset()` 在启动序列的**阶段③**（早于 `i2c_sm_init()`），
 *     此刻引脚/外设都还没配，任何硬件动作都不该在这里发生。 */
void i2c_xact_reset(uint8_t *shm)
{
    for (uint32_t i = 0u; i < OFF_I2C_XACT_SZ; i++) { shm[OFF_I2C_XACT + i] = 0u; }
    SHM_U32(shm, IX_MAGIC)  = IX_MAGIC_VAL;
    SHM_U32(shm, IX_STATUS) = I2C_SM_ST_IDLE;
    /* ★ 这里**不**清 `g_i2c_sm_release_pending`：欠的释放必须**还掉**，清掉就是泄漏
     *   （与 `i2c_sm_request` 里那处是同一个错，别再犯一次）。
     *   真正的对账交给主循环：空闲却仍被 SM 占 ⇒ `g_i2c_ref_leak_n` 增加并当圈归还。 */
    /* ★ 完成记录也要**作废**（新纪元: 上一轮的完成号/状态不该被当成"刚发生的事"）。
     *   `g_i2c_sm_req_n` 本身**不清**（它是累计量, 与 DTCM 统计同族: 清它反而会造出
     *   "序号回绕"的假象）；真正要紧的是把**完成记录**置空, 让所有收尾方判为"无结果"。 */
    g_i2c_sm_done_req    = I2C_SM_DONE_NONE;
    g_i2c_sm_done_status = I2C_SM_ST_IDLE;
    s_done_len           = 0u;
}

/* ★ G6-2: 延迟放门 —— **只能在主循环调用**（本函数会碰 flash 里的门, 不能进 ISR）。
 * ★ 把**欠的**释放**全部**还掉（不是"有就放一次"）—— 计数语义要求这样：欠几次还几次。
 *   ★ 为什么不能用 `if (...) { pending = 0; release(); }`：那会把"欠了 2 次"只还 1 次 ⇒ 泄漏。 */
void i2c_sm_service(void)
{
    while (g_i2c_sm_release_pending != 0u) {
        g_i2c_sm_release_pending--;
        i2c_bus_release(I2C_OWNER_SM);
    }
}uint32_t i2c_sm_phase(void)     { return s_phase; }
uint32_t i2c_sm_phase_cnt(void) { return s_pcnt; }
uint32_t i2c_sm_tick_cnt(void)  { return s_tcnt; }

/* ★ 旧的 `i2c_sm_result()` 实现（按实时 `s_status`/`s_len` 取数）已**删除**，不要恢复：
 *   它会在"我的事务完成后、别人被拒"这类时序下返回**错误的长度或直接返回 0**。
 *   现在 `i2c_sm_result()` 是完成记录的薄封装（见上方 `sm_copy_done`）。 */
