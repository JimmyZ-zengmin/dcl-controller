/* ══════════════════════════════════════════════════════════════════════════
 * lut_seg.c —— `LUT` 段的解析 / 打包 / 原子生效（方案 A 的固件半边）
 * 设计说明与字节布局见 lut_seg.h。
 *
 * ★ 三条纪律（都来自本项目踩过的坑）:
 *   ① **解析失败必须响亮**：`LUT_SEG_BAD` 而不是"当作没段"——否则一个坏表会让程序
 *      带着**上一张表**跑，而现象是"逻辑不对"，矛头指向业务而不是装载路径。
 *      注意与 `LUT_SEG_NONE` 的区别：**没有段**（老包）是正常；**段坏了**是错误。
 *   ② **不半装载**：校验不过 ⇒ 连 pending 都不写（半张表比没有表更坏）。
 *   ③ **顺序敏感**：解析必须先从尾部跳过 `dev_bind` 段（它永远在最尾），再见 LUT 段。
 *      这一条写在这里是因为它**看不见**：谁改了段的追加顺序，解析就静默错位。
 * ══════════════════════════════════════════════════════════════════════════ */
#include "lut_seg.h"
#include "dev_bind.h"
#include "itcm.h"       /* DCL_ITCM：ISR 可达 ⇒ 必须住 ITCM */          /* DB_SEG_LEN / DB_MAGIC_VAL：尾部顺序约定的另一半 */
#include <string.h>

static float    s_lut_pending[MAX_LUT];
static uint32_t s_lut_have = 0u;

volatile uint32_t g_lut_seg_status = LUT_SEG_NONE;
volatile uint32_t g_lut_applies    = 0u;

/* 本地小端读写（不依赖 dev_bind.c 的内部符号；也不做非对齐指针转换） */
static uint32_t lu_rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void lu_wr32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu); p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu); p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

/** FNV-1a，**按 32 位字**（与 `db_crc` 同算法 ⇒ 装载路径可独立复核同一套纪律） */
static uint32_t lu_fnv_words(const uint8_t *p, uint32_t nbytes)
{
    uint32_t h = 2166136261u;
    for (uint32_t i = 0; i + 4u <= nbytes; i += 4u) {
        h = (h ^ lu_rd32(p + i)) * 16777619u;
    }
    return h;
}

uint32_t lut_seg_unpack(const uint8_t *payload, uint32_t len)
{
    uint32_t tail, n_used, fnv;
    const uint8_t *seg;

    g_lut_seg_status = LUT_SEG_NONE;
    if (payload == NULL) { return LUT_SEG_NONE; }

    /* ① 先从尾部跳过 dev_bind 段（**它永远在最尾** —— 见 lut_seg.h 的顺序约定） */
    tail = len;
    if (tail >= (uint32_t)DB_SEG_LEN &&
        lu_rd32(payload + tail - (uint32_t)DB_SEG_LEN) == DB_MAGIC_VAL) {
        tail -= (uint32_t)DB_SEG_LEN;
    }
    /* ② 再看 LUT 段 */
    if (tail < LUT_SEG_LEN) { return LUT_SEG_NONE; }        /* 老包 / 不带表 —— 正常 */
    seg = payload + tail - LUT_SEG_LEN;
    if (lu_rd32(seg) != LUT_SEG_MAGIC) { return LUT_SEG_NONE; }

    /* ③ 段在 ⇒ 从这一刻起**任何不符都判 BAD**（不许回退成"当作没段"） */
    if (lu_rd32(seg + 4) != LUT_SEG_LEN) { g_lut_seg_status = LUT_SEG_BAD; return LUT_SEG_BAD; }
    n_used = lu_rd32(seg + 8);
    if (n_used < 2u || n_used > (uint32_t)MAX_LUT) {
        g_lut_seg_status = LUT_SEG_BAD; return LUT_SEG_BAD;
    }
    fnv = lu_rd32(seg + 12);
    if (fnv != lu_fnv_words(seg + LUT_SEG_HDR, (uint32_t)MAX_LUT * 4u)) {
        g_lut_seg_status = LUT_SEG_BAD; return LUT_SEG_BAD;
    }
    /* ④ 表里的每个值必须有限（NaN/Inf 会让插值把下游污染成不可归因的形态） */
    for (uint32_t i = 0; i < (uint32_t)MAX_LUT; i++) {
        uint32_t bits = lu_rd32(seg + LUT_SEG_HDR + i * 4u);
        if ((bits & 0x7F800000u) == 0x7F800000u) {          /* 指数全 1 = ±Inf/NaN */
            g_lut_seg_status = LUT_SEG_BAD; return LUT_SEG_BAD;
        }
    }
    /* ⑤ 全部通过 ⇒ 暂存（**不碰 ACTIVE**；生效交给 lut_seg_apply 在 reload 临界区做） */
    for (uint32_t i = 0; i < (uint32_t)MAX_LUT; i++) {
        uint32_t bits = lu_rd32(seg + LUT_SEG_HDR + i * 4u);
        memcpy(&s_lut_pending[i], &bits, 4u);
    }
    s_lut_have = 1u;
    g_lut_seg_status = LUT_SEG_OK;
    return LUT_SEG_OK;
}

uint32_t lut_seg_pack(uint8_t *payload, uint32_t len, uint32_t cap,
                      const float *tbl, uint32_t n_used)
{
    uint8_t *seg;

    if (payload == NULL || tbl == NULL) { return len; }
    if (n_used < 2u || n_used > (uint32_t)MAX_LUT) { return len; }
    if (len + LUT_SEG_LEN > cap) { return len; }             /* 放不下 ⇒ 宁可不带，也不截断 */

    seg = payload + len;
    lu_wr32(seg + 0u, LUT_SEG_MAGIC);
    lu_wr32(seg + 4u, LUT_SEG_LEN);
    lu_wr32(seg + 8u, n_used);
    for (uint32_t i = 0; i < (uint32_t)MAX_LUT; i++) {
        uint32_t bits = 0u;
        float v = (i < n_used) ? tbl[i] : 0.0f;
        memcpy(&bits, &v, 4u);
        lu_wr32(seg + LUT_SEG_HDR + i * 4u, bits);
    }
    lu_wr32(seg + 12u, lu_fnv_words(seg + LUT_SEG_HDR, (uint32_t)MAX_LUT * 4u));
    return len + LUT_SEG_LEN;
}

/* ★★ `DCL_ITCM` 不是可选项：本函数从**拍 ISR 的 reload 临界区**调用
 *   ⇒ 若住在 flash，擦 flash 期间取指会被 stall（本项目已因此被看门狗复位过一次）。
 *   抓到它的是 `gate_isr_itcm.py`（ISR 调用树闸门）—— 它是**构建闸门**，不是我事后想起来的。 */
DCL_ITCM void lut_seg_apply(uint8_t *base)
{
    if (!s_lut_have) { return; }                            /* 热路径成本 = 一次标志读 */
    float *dst = (float *)(void *)(base + OFF_LUT_DATA);
    for (uint32_t i = 0; i < (uint32_t)MAX_LUT; i++) { dst[i] = s_lut_pending[i]; }
    s_lut_have = 0u;
    g_lut_applies++;
}

void lut_seg_reset(void)
{
    s_lut_have = 0u;
    g_lut_seg_status = LUT_SEG_NONE;
}

int lut_seg_pending(void)
{
    return (int)s_lut_have;
}
