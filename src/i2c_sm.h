#ifndef I2C_SM_H
#define I2C_SM_H
/* ═══════════ 拍内推进的 I2C 事务状态机 (GAP-6 / G6-1, 2026-09-16) ═══════════
 *
 * ## 为什么要有它（契约 §3.6 的落法，不是"另一个 I2C 实现"）
 * 现有 `i2c_bb.c` 是**阻塞位翻转**（一次 2 字节读 ≈ 250 µs），挂在**主循环**上。
 * 契约 §3.6 定案：外设的"多步操作"必须做成**拍内每拍推进一步的状态机**，理由是四条硬约束：
 *   ① 拍内推进（不忙等）② 标价 + 预算门
 *   ③ ★ **总线独占门**（与 `as5600` 同用一条总线 ⇒ 公理② 单写者）
 *   ④ ★ **就绪门**（跨拍 ⇒ 使用者不能假设"发请求即得值"）
 *
 * ★★ 本模块**不是**要取代 `i2c_bb`。`i2c_bb` 是**对照路径**，按本项目的纪律：
 *    "将来若换实现，那时有现成判据可对照（**两条独立路径对上才算数**）"（`i2c_bb.h:8` 原文）。
 *    G6-1 的验收判据就是这一条：**状态机读 AS5600 的 RAW ANGLE 必须与阻塞路径一致**。
 *
 * ## 时间账（公理④：每拍开销必须有界）
 * 拍 = 100 µs = 40000 cyc，执行预算 32000 cyc。
 * 本状态机按 400 kHz 设计（位周期 2.5 µs ≈ 1000 cyc）⇒ **一拍推进一个相位**
 * （一个相位 = START / RESTART / 一个字节(9 位) / STOP），一个字节 ≈ 9000 cyc ≈ 预算 28%。
 * ⇒ AS5600 一次 2 字节读 = **6+n 个相位 = 8 拍 ≈ 800 µs**（现状 250 µs）。
 *   用 2.4× 延迟换"**无阻塞 + 每拍有界**"——这正是契约 §3.7 写明的取舍。
 *
 * ## ★★ 判据（不用 DWT，故意如此）
 * DWT 会被调试器会话静默停掉（本项目铁律），所以**不拿 DWT 当判据**。
 * 判据用**可预测的总拍数**：一次读 n 字节 = `6 + n` 个相位 ⇒ **8 拍 (n=2)**。
 *   · 实测总拍数 == 6+n  ⇒ 证明"**每拍只推进一步**"（若一拍跑完整个事务, 这个数会≪6+n）
 *   · 相位计数 `phase_n` 与总拍数一致 ⇒ 同一个性质的两个独立观测量
 *   · `s_clk_cyc_max`（DWT）只作**旁证**, 其值为 0 时判为"时基未启用", **不得**当"很快"。
 */
#include <stdint.h>

/* 事务类型 */
#define I2C_SM_OP_PING   0u
#define I2C_SM_OP_READ   1u     /* [reg] → len 字节 */
#define I2C_SM_OP_WRITE  2u     /* [reg][data…] */

/* 状态码（对外可读，每个都能失败） */
#define I2C_SM_ST_IDLE      0u  /* 空闲，可以下新请求 */
#define I2C_SM_ST_BUSY      1u  /* 事务在飞（就绪门：done_seq 还没回） */
#define I2C_SM_ST_OK        2u  /* 完成且全 ACK */
#define I2C_SM_ST_NAK       3u  /* 从机没 ACK */
#define I2C_SM_ST_STUCK     4u  /* SCL 被别处拉低（时钟延展）超界 */
#define I2C_SM_ST_BADARG    5u  /* 参数非法（len 越界 / op 未知） */
#define I2C_SM_ST_GATE_BUSY 6u  /* ★ 被**总线独占门**拦下（阻塞路径正在用总线）*/

#define I2C_SM_MAX_DATA  8u     /* 单次事务最大数据字节 */

/* ── 生命周期 ── */
void     i2c_sm_init(void);      /* 绑定引脚(与 i2c_bb 同一组: GPIOB PB10/PB11) + 允许跑 */

/* ── 发起一次事务（非阻塞：只登记，不碰总线）──
 * 返回 1 = 已受理（之后靠 i2c_sm_tick() 推进）; 0 = 被拒（看 i2c_sm_status()）。 */
uint32_t i2c_sm_request(uint8_t addr7, uint8_t op, uint8_t reg,
                        const uint8_t *tx, uint8_t n);

/* ── ★ 每拍调用一次（拍 ISR 内）。推进**一个相位**，开销有界。── */
void     i2c_sm_tick(void);

/* ── 观测面 ── */
uint32_t i2c_sm_status(void);
uint32_t i2c_sm_phase(void);       /* 当前相位号（诊断）*/
uint32_t i2c_sm_phase_cnt(void);   /* 本次事务已推进的相位数 */
uint32_t i2c_sm_tick_cnt(void);    /* 本次事务已耗的拍数 */
uint32_t i2c_sm_result(uint8_t *buf, uint32_t n);   /* 拷回接收数据, 返回实际字节数 */

/* ★★ 总线独占门住在 **`i2c_bb`**（= 资源的定义处），本模块只是它的**使用者** —— 见 `i2c_bb.h` 的门 API。
 *   ★ 为什么改到这里: 早先那版把"阻塞路径在忙"的标志放在**主循环**、把判断放在**状态机**
 *     ⇒ 属于"守卫放错了层": 只要再多一个调用者就静默穿透。
 *     本项目的同族教训: `do_latch_init` 的无条件启动 / `0x43` 那条链 —— 守卫必须住在资源处。 */
extern volatile uint8_t g_i2c_sm_active;   /* **只作观测**: 1 = 状态机事务在飞（不再充当门）*/

/* ★★★ 放门必须由**主循环**做，不能由 ISR 做（2026-09-16，闸门当场拦下的）：
 *   状态机在**拍 ISR** 里收尾, 而总线门的 `i2c_bus_release()` 住在 **flash**（i2c_bb.c）
 *   ⇒ 放进 ISR 就违反 ISR 调用树不变量（擦 flash 期间取指被 stall ⇒ 喂狗停 ⇒ 复位）。
 *   ★ 而"给它加 DCL_ITCM"这条路**走不通**: 实测 **ITCM 已 100% 占满**（64KB/64KB）。
 *   ⇒ 于是改成**延迟释放**: ISR 只置 `g_i2c_sm_release_pending`, 主循环调 `i2c_sm_service()` 放门。
 *   ★ 代价（已权衡并接受）: 事务结束后总线最多再被持 ~1 圈主循环（**~0.37ms**）。
 *     影响面很小 —— 同 owner 再申请**照样成功**(acquire 允许同 owner 重入),
 *     只有阻塞路径会被多跳一次（而它本来 10ms 才轮一次）。 */
extern volatile uint8_t g_i2c_sm_release_pending;
void i2c_sm_service(void);   /* ★ 主循环每次循环调用: 需要时放门 */

/* 计数器：每个都能失败 ⇒ 都可作判据 */
extern volatile uint32_t g_i2c_sm_req_n;      /* 受理的请求数 */
extern volatile uint32_t g_i2c_sm_ok_n;       /* 全 ACK 完成数 */
extern volatile uint32_t g_i2c_sm_nak_n;      /* 被 NAK 次数 */
extern volatile uint32_t g_i2c_sm_stuck_n;    /* SCL 卡死次数 */
extern volatile uint32_t g_i2c_sm_gate_n;     /* ★ 被总线独占门拒绝的次数（判据③）*/
extern volatile uint32_t g_i2c_sm_ticks_n;    /* 累计推进的拍数 */
extern volatile uint32_t g_i2c_sm_clk_cyc_max;/* 单相位最大 DWT 周期（旁证；0=时基未启用）*/

#endif /* I2C_SM_H */
