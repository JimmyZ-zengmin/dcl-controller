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
#define I2C_SM_ST_STOLEN    7u  /* ★★ 结果被**后一个事务**覆盖（收尾前被别人抢先发起并完成）
                                 *   ⇒ **不得当成功用**（数据是别人的）, 由使用者重发。
                                 *   ★ 为什么要有这个独立状态码: 没有它, 那条静默错数据路径
                                 *     在协议面上**完全不可观测** —— 上位机只会看到一个"正常"的值。
                                 *   判据: 用 `i2c_sm_done_req()` 与自己的受理序号比对（见下）。 */

#define I2C_SM_MAX_DATA  8u     /* 单次事务最大数据字节 */

/* ── 生命周期 ── */
void     i2c_sm_init(void);      /* 绑定引脚(与 i2c_bb 同一组: GPIOB PB10/PB11) + 允许跑 */

/* ── 发起一次事务（非阻塞：只登记，不碰总线）──
 * 返回 1 = 已受理（之后靠 i2c_sm_tick() 推进）; 0 = 被拒（看 i2c_sm_status()）。 */
uint32_t i2c_sm_request(uint8_t addr7, uint8_t op, uint8_t reg,
                        const uint8_t *tx, uint8_t n);

/* ── ★ 每拍调用一次（拍 ISR 内）。推进**一个相位**，开销有界。── */
void     i2c_sm_tick(void);

/* ── 观测面 ──
 * ★★ 2026-09-16 修正（第二轮审计挖出的第二条漏洞）: `s_status` 是**实时态** ——
 *   一个**被拒**的请求（门忙 / BADARG）会**覆写**它。于是"令牌匹配"仍可能配上
 *   一个**被别人改过的状态** ⇒ 收尾方会**丢弃自己的好数据并计错**。
 *   ⇒ 引入 **完成记录** `{req, status, len}`：完成的一刻快照，**受理新请求时立即失效**
 *     （`done_req = I2C_SM_DONE_NONE`）。
 *   **取结果必须走完成记录**（`take_result` / `result`），**不要**再看 `i2c_sm_status()`。
 *   ★ 判据一般化: **"完成态"与"实时态"是两件事** —— 共用一份状态，就会被后续动作改掉。 */
#define I2C_SM_DONE_NONE  0xFFFFFFFFu   /* 完成记录为空（无结果可取）*/

uint32_t i2c_sm_status(void);              /* **实时态**（诊断用；**不要**用它判"我的结果好了没"）*/
uint32_t i2c_sm_phase(void);       /* 当前相位号（诊断）*/
uint32_t i2c_sm_phase_cnt(void);   /* 本次事务已推进的相位数 */
uint32_t i2c_sm_tick_cnt(void);    /* 本次事务已耗的拍数 */
/* 取"最近一次完成"的数据（要求完成记录存在且其状态为 OK）。返回实际字节数。
 * ★ 注意它不是"我的结果" —— 要判归属必须用下面的 take_result。 */
uint32_t i2c_sm_result(uint8_t *buf, uint32_t n);

/* ★★★ 事务归属令牌 + 完成记录（2026-09-16）。**每个使用者都必须用它收尾**。
 *
 * 反例（真缺陷，不是理论风险）：本状态机只有一个 `s_rbuf`/`s_status`/`s_len`，而使用者有
 * **两个**（`i2c_shm_service` 与 `dev_bind_service`）。两类静默错数据：
 *   ① 只查 `status == OK` ⇒ 别人的完成被当成自己的（数据串台）；
 *   ② 只查令牌 ⇒ 令牌匹配但**状态已被别人后来的被拒请求改掉** ⇒ 丢自己的好数据、还计错。
 * 处方（两者都要）:
 *   ③ `i2c_sm_request()` 返回 1 之后立刻记下 `my = g_i2c_sm_req_n`;
 *   ④ 收尾时 **①** 实时态非 BUSY、**②** `i2c_sm_done_req() == my`（否则结果已被覆盖 / 失效）;
 *   ⑤ 用 `i2c_sm_take_result(my, …)` 取数据（它同时校验完成记录的状态与长度）。
 * ★ 放在资源处（而不是在每个调用点各判一次）是本项目 §4.5 铁律：
 *   "守卫必须住在资源的定义处" —— 否则多一个调用者就静默穿透。 */
uint32_t i2c_sm_done_req(void);    /* 刚完成的事务的受理序号；无记录时 = I2C_SM_DONE_NONE */
uint32_t i2c_sm_done_status(void); /* **完成时**的状态快照（OK/NAK/STUCK），不受后续动作影响 */
uint32_t i2c_sm_take_result(uint32_t my_req, uint8_t *buf, uint32_t n);
                                   /* 仅当 `done_req == my_req` 且完成状态为 OK 时拷回数据 */

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
extern volatile uint16_t g_i2c_sm_release_pending;
void i2c_sm_service(void);   /* ★ 主循环每次循环调用: 把**欠的**释放都还掉 */

/* ★★★ 它为什么是"欠释放**计数**"而不是一个 bool（2026-09-16 上机抓到泄漏 61 个引用后改）：
 *   延迟释放的记账必须是**配对计数**。原实现是 bool，且 `i2c_sm_request()` 在受理新请求时
 *   把它**清 0**（本意：别把刚拿到的那次占用放掉）。但若上一个事务恰好在
 *   `i2c_sm_service()` **之后**、本次请求**之前**完成，那次"欠的释放"就被**丢掉**
 *   ⇒ `i2c_bus_refs()` 每次泄漏 1 ⇒ 累积到 61 后**阻塞路径永久拿不到总线**
 *   （实测症状：`owner=2(SM)` 冻结、`refs=61`，而状态机早已 idle、表也是空的）。
 *   ★ 改法：**计数**——完成一次 +1，主循环每圈把欠的**全部**还掉（`while`）。
 *     配对计数的语义天然正确：每次acquire最终恰好对应一次 release。
 *   ★ 观测量 `0x39 op=22 +32` = `refs`（空闲必须 0）+ 主循环的引用计数对账
 *     （`g_i2c_ref_leak_n`）—— "泄漏"这件事**既不静默也不永久**。 */
extern volatile uint32_t g_i2c_ref_leak_n;   /* 主循环对账发现"空闲却仍被 SM 占"的次数 */

/* ★★ I2C 事务区的**冷启动登记** —— 必须由 `cold_start_reset()` 调用。
 *   理由（本项目"新增域必须登记到单一入口"的纪律，与 mb_config / macro_reset / fault_init 同款）：
 *   `cold_start_reset` 是 SHM 整段清零的**唯一**入口（上电 / 0x13 RESET / reinit 都经过它）。
 *   ★ 2026-09-16 自查发现的真缺陷: 第一版只在开机把 IX_MAGIC 写一次 ⇒
 *     **一次普通的 `0x13 RESET` 之后 magic 就变 0** ⇒ 上位机读不到它, 会以为"该区不存在"
 *     —— 一个会**撒谎**的"区存在自证"。登记到这里之后, 任何清零路径都会立刻恢复它。 */
void i2c_xact_reset(uint8_t *shm);

/* 计数器：每个都能失败 ⇒ 都可作判据 */
extern volatile uint32_t g_i2c_sm_req_n;      /* 受理的请求数 */
extern volatile uint32_t g_i2c_sm_ok_n;       /* 全 ACK 完成数 */
extern volatile uint32_t g_i2c_sm_nak_n;      /* 被 NAK 次数 */
extern volatile uint32_t g_i2c_sm_stuck_n;    /* SCL 卡死次数 */
extern volatile uint32_t g_i2c_sm_gate_n;     /* ★ 被总线独占门拒绝的次数（判据③）*/
extern volatile uint32_t g_i2c_sm_ticks_n;    /* 累计推进的拍数 */
extern volatile uint32_t g_i2c_sm_clk_cyc_max;/* 单相位最大 DWT 周期（旁证；0=时基未启用）*/

#endif /* I2C_SM_H */
