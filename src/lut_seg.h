/* ══════════════════════════════════════════════════════════════════════════
 * lut_seg.h —— `LUT` 表的**归属**：让表随程序走（2026-09-19，方案 A）
 *
 * ## 它治的是什么（E-Y 量出来的事实）
 * `0x23` 能把表写进 `OFF_LUT_DATA`，但 **`0x10 deploy` 不碰它、SD 程序包不带它、
 * `0x13 RESET` 清零它、`fill_tables` 会覆盖它** ⇒ 表住在**一块无主的易失内存**里。
 * 生产上不可用：换个程序表还在（或没了），上电后表是零（或斜坡）。
 *
 * ## 方案（用户 2026-09-19 授权我定，我选 A）
 * **表随 deploy 载荷走**：载荷**尾部追加一段** `LUTT`，与路由/参数**同一次 reload 生效**。
 *   · 与既有 `dev_bind` 段**同一套约定**（尾部段 + 魔术字 + 长度字 + 校验），不发明新机制
 *   · 老包（无段）⇒ `LUT_SEG_NONE` ⇒ **什么都不做**（与从前逐字节相同）
 *   · SD 程序包走同一条路：`prog_store` 落盘时在 **dev_bind 段之前**多一段 ⇒ 上电装载后**表回来**
 *
 * ## 段的字节布局（**从尾往前**解析，见下"两段的顺序约定"）
 *   [0..3]   u32  magic  `LUT_SEG_MAGIC` = 'LUTT'
 *   [4..7]   u32  seg_len = `LUT_SEG_LEN`（1040）
 *   [8..11]  u32  n_used   有效点数（2..MAX_LUT；插值至少需要 2 点）
 *   [12..15] u32  fnv      FNV-1a（**按 32 位字**，与 `db_crc` 同算法）over 下面 1024 B
 *   [16..1040) float[256]  表（未用到的点补 0）
 *
 * ## ★★ 两段的顺序约定（**必须记住**，否则解析错位）
 *   从尾往前：`[dev_bind 段]`（最后 48 B）→ `[LUT 段]`（其前 1040 B）→ 再往前是 route/param 区。
 *   ⇒ **后来新增的段一律加在更靠前的位置**。`dev_bind_pack` 在程序落盘时把它追加到**最尾**，
 *     所以宿主（dclc）只要把 LUT 段接在 body 之后即可，两边自然形成这个顺序。
 *   ★ 为什么不让 `dev_bind` 跟着挪：那会改动既有段约定，而**四个工具 + claims 闸门**都在解析它。
 *
 * ## 判据（离线 4 条 + 在板 4 条，见 tools/exp_fb_lut_deploy.py）
 *   FB3 段组合往返：{无段} {只有 LUT} {只有 DB} {LUT+DB} 四种都要能正确解析
 *   FB4 ★ 变异对照：magic 错一位 / fnv 错一位 / n_used 越界 ⇒ **必须判 BAD 且不半装载**
 *   FB5 在板：部署带表程序 ⇒ 读回 LUT 区**逐位等于**发出的表
 *   FB8 在板：上电从 SD 程序包装载 ⇒ **表回来**（这就是方案 A 的核心价值）
 * ══════════════════════════════════════════════════════════════════════════ */
#ifndef DCL_LUT_SEG_H
#define DCL_LUT_SEG_H

#include <stdint.h>
#include "engine.h"

#define LUT_SEG_MAGIC   0x5454554Cu                       /* 'LUTT'（LE 字节序 L,U,T,T）*/
#define LUT_SEG_HDR     16u
#define LUT_SEG_LEN     (LUT_SEG_HDR + (uint32_t)MAX_LUT * 4u)   /* = 1040 */

/* 段状态（与 dev_bind 的 DB_SEG_* 同款三态 —— 不新造一套语义） */
#define LUT_SEG_NONE    0u   /* 载荷里没有这一段（老包 / 不用表的程序）⇒ 不碰表 */
#define LUT_SEG_OK      1u   /* 段有效 ⇒ 表已进 pending，等 reload 原子生效 */
#define LUT_SEG_BAD     2u   /* 段在但校验不过 ⇒ **明确拒绝该段**（不半装载；程序仍照常装载）*/

/** @brief 从载荷尾部解析 LUT 段 ⇒ 校验通过则暂存到 pending（**不碰 ACTIVE 表**）
 *  @return LUT_SEG_NONE / OK / BAD（同时写 `g_lut_seg_status` 供外部读） */
uint32_t lut_seg_unpack(const uint8_t *payload, uint32_t len);

/** @brief 反向：把表打包成段附加到载荷尾部（供固件自检与测试用；宿主侧由 dclc 生成）
 *  @return 新的载荷长度（放不下 ⇒ 原样返回，宁可不带也不截断 —— 与 dev_bind_pack 同款）*/
uint32_t lut_seg_pack(uint8_t *payload, uint32_t len, uint32_t cap,
                      const float *tbl, uint32_t n_used);

/** @brief ★ 把 pending 表**原子**写入 `OFF_LUT_DATA`；**必须与 engine_reload_active 同一临界区**调用
 *  （只在有 pending 时动作 ⇒ 热路径成本 = 一次标志读）*/
void lut_seg_apply(uint8_t *base);

/** @brief 丢弃 pending（`0x13 RESET` 调用 —— pending 住 .bss，`cold_start_reset` 清不到它）*/
void lut_seg_reset(void);

/** @brief 是否有待生效的表（诊断用） */
int  lut_seg_pending(void);

extern volatile uint32_t g_lut_seg_status;   /* 最近一次 unpack 的结果 */
extern volatile uint32_t g_lut_applies;      /* 表真正写入 ACTIVE 的次数（正向证据）*/

#endif /* DCL_LUT_SEG_H */
