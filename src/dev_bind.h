#ifndef DEV_BIND_H
#define DEV_BIND_H
/* ═══════════ 具名设备绑定表（GAP-6 / G6-4, 2026-09-16）═══════════
 *
 * **契据：`docs/REF-program-contract.md` §3.8** —— 实现必须与它一致；不一致即缺陷。
 *
 * 一句话：上位机下发"每 N 拍读 `设备地址/寄存器/长度` → 写 `SENSOR[z]`"，由拍内状态机执行。
 * 于是 **"接一个新器件" = 上传一段配置**（不改固件、也不写代码）——这才是 §3.2-C 的原意。
 *
 * 形态照 `bb_map_bind()`（`blackbox.c:161-193`）：
 *   打包 u32 条目 / 一次性解析 / 非法兜底 / 范围校验 / **与 PC 共用的表校验和** / 有效槽计数。
 *   ★ 唯一差异：**本表上位机可写**（走现成的 `0x23 WRITE_BURST`，不加新命令码）。
 *
 * ★★ 硬件无关性：本模块**不知道**任何具体器件——AS5600 只是 `addr7=0x36,reg=0x0C,len=2` 一行数据。
 *   这是"程序面不得出现引脚号/地址"（§1.5 红线 1）在**配置面**的对应物：
 *   地址在**上位机下发的表**里，既不在程序里，也不在固件里。
 */
#include <stdint.h>

#define DB_DEV_EMPTY   0u   /* 空槽 */
#define DB_DEV_I2C_RD  1u   /* 通用 I2C 读（厂商专属、不保证跨平台）*/

/* 拒绝原因码（契据 §3.8.4；每个都能失败 ⇒ 都可作判据）*/
#define DB_RC_OK       0u
#define DB_RC_CRC      1u   /* crc 不符 —— **"半个表"最危险** */
#define DB_RC_SEQ      2u   /* req_seq 非单调（**回退/重放旧表** —— 见下）*/
#define DB_RC_DEV      3u   /* dev 非法 */
#define DB_RC_ADDR     4u   /* addr7 > 0x7F */
#define DB_RC_LEN      5u   /* len 为 0 或 > 2 */
#define DB_RC_DST      6u   /* dst > 15 */
#define DB_RC_NOSEQ    8u   /* req_seq == 0（尚未提交）*/
/* ★ 码 2 的完整语义（实现必须真的挡, 不能只留常量）:
 *   一张**旧表 + 旧序号**字段全合法、crc 也对 ⇒ 若只看合法性就会被接受, **把现场打回旧配置**,
 *   而且没有任何观测量表明"这是一张旧表"。⇒ 判据 = `(int32_t)(req_seq - done_seq) < 0`。
 *   ★ 被拒时**不回 `done_seq`**（它代表"当前已生效的最新号"）, 上位机按
 *     `reject != 0 && done_seq != req_seq` 判定并**终止等待**。 */

/* ── 生命周期 ──
 * ★ `dev_bind_reset` 必须由 `cold_start_reset()` 调用（本项目"新增域必须登记到单一入口"的纪律）——
 *   否则 0x13 RESET / deploy / reinit 的 memset 会把 magic 抹掉, 于是"区存在自证"会**撒谎**。 */
void dev_bind_reset(uint8_t *shm);     /* 清提交块 + 解除绑定（只在冷启动路径）*/
void dev_bind_init(uint8_t *shm);      /* 开机一次: 记下 shm 指针 */

/* ── 服务方（都在**主循环**调用；理由见契据 §3.8.6）──
 * `submit`: 校验并绑定（crc + 字段范围 + seq 单调）；不通过 ⇒ **保持上一次绑定**并写拒绝码。
 * `service`: 轮询一个槽（round-robin），走总线门 + 拍内状态机；完成即写 SENSOR[dst]。
 * ★ 两者都不在 ISR：它们会碰 flash（总线门在 i2c_bb.c）且是 10ms 级服务动作（§3.6.1）。*/
void dev_bind_submit(void);
void dev_bind_service(uint32_t tick);   /* ★ 形参与 .c 必须一致（拍号：速率闸用） */

/* ── 观测面（照输出面注册表的既有理由：要能区分"没登记"与"登记了没跑"）── */
uint32_t dev_bind_valid_count(void);   /* 登记数（= SHM 的 DB_N_VALID）*/
uint32_t dev_bind_ok_count(void);      /* 执行数（成功轮询次数）*/
uint32_t dev_bind_err_count(void);     /* 失败轮询次数（**器件/通信**故障）*/
uint32_t dev_bind_skip_count(void);    /* ★ 轮空次数（总线门忙 / 事务被别的使用者取走）
                                        *   ★ 与 err 分开是刻意的：err 必须只回答"器件/通信出了什么事"，
                                        *     把调度现象混进去，"err 在涨"这条判据就无法解释 ⇒ 判据作废。 */
uint32_t dev_bind_rej_count(void);     /* ★ 表被拒次数（**上传面**）—— 同一条理由：与"轮询失败"是两个问题 */

/* ── 计数器本体（`obs_anchor()` 必须读一遍, 否则被 --gc-sections 回收 —— 本项目已踩三次）── */
extern volatile uint32_t g_db_ok_n, g_db_err_n, g_db_last_err,
                         g_db_skip_n, g_db_rej_n;

#endif /* DEV_BIND_H */
