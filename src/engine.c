/**
 * engine.c — DCL 引擎 H723 移植层 (阶段 2)
 *
 * 本文件回答阶段 1 报告 §2 提的那个问题:
 *   "把 ISR 搬进 ITCM 到底是不是必要?" —— 阶段 1 观察到 flash 常驻代码的
 *   WCET 被"对齐/取指"主导 (±10% 不可预测), 于是**假设** ITCM 能治。
 *   阶段 2 把这个假设变成可测的 A/B。
 *
 * ★ 核心手法: **一份源码, 两份实例化**
 *   同一个宏 DEFINE_ENGINE_SCAN 生成两个函数:
 *       engine_scan_flash() → .text   (FLASH, 经 L1 I-cache)
 *       engine_scan_itcm()  → .itcm_text (ITCM 0x00000000, 零等待无 cache)
 *   两者**逐指令相同**, 唯一差异是所在内存区。同一个镜像、同一次上电、
 *   同一个调用点 —— 任何差异都只能归因于"取指路径", 排除了环境/时钟/编译器差异。
 *
 *   ★ 必须配 -fno-ipa-icf: GCC 的 IPA-ICF 会把"内容相同的函数"折叠成一个,
 *     若被折叠, 两份实例只剩一份, 实验作废。CMakeLists 已关掉 (见注释)。
 *
 * ★ 表数据全在 DTCM (链接段 .dtcm_shm, 0x20000000):
 *   零等待 + 无 cache + 不经过 AXI (HCLK = CPU/2, AXI 每次访问要 2 个 CPU 周期)。
 *   对照: 若把表放在 AXI SRAM, 每个 load/store 都要多花 1 个 CPU 周期, cache
 *   命中与否还会让成本随程序历史漂移 —— 确定性直接破产。
 */
#include <string.h>
#include "engine.h"
#include "primitives.h"

/* ITCM 段属性 (阶段 3 的分档调度也住在热路径上) */
#define ATTR_ITCM __attribute__((section(".itcm_text"), noinline))

/* ══════════════════════════════════════════════════════════════════
 * SHM: 静态 DTCM 区 (S3 是运行期 heap_caps 分配, 这里是链接期落位)
 * ══════════════════════════════════════════════════════════════════ */
__attribute__((section(".dtcm_shm"), aligned(32)))
uint8_t g_shm[SHM_SIZE];

/* 链接脚本提供的段边界 (用于断言实际落位与声明一致) */
extern uint8_t _shm_start[];
extern uint8_t _shm_end[];

/* ★ 把段边界导出成全局, 让**外部工具**(pyocd 读回来)做权威比对。
 * 为什么不用固件自报: 见 shm_layout_ok() 的注释 —— 自报会被编译器折叠。 */
volatile uint32_t g_shm_start_addr = 0;
volatile uint32_t g_shm_end_addr   = 0;

/* state_offset 非法时的兜底槽 (S3 core0_isr.c 同款) */
static StateEntry_t s_state_fallback;

/**
 * @brief 冷启动清零 —— S3 纪律: .dtcm_shm 是 NOLOAD, 上电内容不确定,
 *        由固件显式清零 (而不是"恰好为 0")。
 * ★ 单一入口 (S3 第二十六轮收口纪律): engine_fill_tables() 必须先调本函数,
 *   不允许各自 memset —— 否则"新增域登记到哪"会分裂成两个答案。
 */
void cold_start_reset(void)
{
    memset(g_shm, 0, sizeof(g_shm));
}

/* ══════════ 栈边界哨兵 (设防) ══════════
 * 在 _shm_end 之上铺 128 字节魔术字。栈从 DTCM 顶端向下生长, 一旦越过
 * _shm_end 就说明快踩到表了 —— 这是 SHM 与栈之间唯一的一道堤。
 * 位置选在 _shm_end 而不是 _estack 附近: 那是"表被踩"的**第一现场**。 */
#define SHM_GUARD_WORDS 32
#define SHM_GUARD_MAGIC 0xC0DEAD42u
static uint32_t *guard_words(void)
{
    return (uint32_t *)(uintptr_t)_shm_end;
}

/** @brief 铺哨兵 (必须在任何可能大量用栈的代码之前调用) */
void shm_guard_paint(void)
{
    uint32_t *p = guard_words();
    for (int i = 0; i < SHM_GUARD_WORDS; i++) p[i] = SHM_GUARD_MAGIC;
}

/** @brief 1 = 哨兵完好 (栈没踩到表) */
int shm_guard_ok(void)
{
    uint32_t *p = guard_words();
    for (int i = 0; i < SHM_GUARD_WORDS; i++) {
        if (p[i] != SHM_GUARD_MAGIC) return 0;
    }
    return 1;
}

/* ══════════ 路由表校验和 (独立可预测 → 可失败) ══════════ */
uint32_t engine_table_checksum(const uint8_t *base)
{
    /* ★ 对**整个 16 字节条目**逐字节做 FNV-1a, 而不是对拼好的字段做。
     *   第一版是"字段位拼装"版, 有两个毛病:
     *     ① `state_offset << 20` 与 `src_type << 16` 在 bit 20-23 **重叠** —— 
     *        (src_type, state_offset) 的不同组合可能撞同一个 v;
     *     ② **完全没覆盖 `period`** —— 也就是档位/相位分配不在校验范围内。
     *   逐字节版没有重叠, 且天然覆盖全部字段 (含 period), 因此也顺带覆盖了
     *   engine_build_buckets 的**归组重排** (字节序参与哈希)。 */
    const uint8_t *rt = base + OFF_ROUTE_TABLE;
    uint32_t h = 0x811C9DC5u;
    for (uint32_t i = 0; i < (uint32_t)MAX_ROUTES * 16u; i++) {
        h = (h ^ (uint32_t)rt[i]) * 16777619u;
    }
    return h;
}

uint32_t engine_active_routes(const uint8_t *base)
{
    const RouteEntry_t *rt = (const RouteEntry_t *)(base + OFF_ROUTE_TABLE);
    uint32_t n = 0;
    for (int i = 0; i < MAX_ROUTES; i++) {
        if (rt[i].flags & ROUTE_FLAG_ACTIVE) n++;
    }
    return n;
}

/** @brief 落位自检: 声明位置必须真的落在 .dtcm_shm 段内且在 DTCM 地址域
 *
 * ★ 踩坑记录 (2026-09-10, 本函数第一版是错的):
 *   第一版写的是 `if ((uintptr_t)g_shm != (uintptr_t)_shm_start) return 0;`
 *   —— 编译器把整个函数折叠成 `movs r0,#0; bx lr`, 恒返 0。
 *   原因: C 标准保证"不同对象地址不同", GCC 因此可以**在不比较数值的情况下**
 *   断定 `&g_shm != &_shm_start` 恒真 (已在 .tmp/t.c 里最小复现, GCC 7.3.1 -O2)。
 *   教训: **指针相等的自检会被编译器证伪并折叠** —— 自检必须让取值路径
 *   经过 volatile, 或者干脆交给外部工具算 (本项目采用后者作为权威口径,
 *   这里保留一个不能折叠的固件内版本作为第一道闸)。
 */
int shm_layout_ok(void)
{
    g_shm_start_addr = (uint32_t)(uintptr_t)_shm_start;
    g_shm_end_addr   = (uint32_t)(uintptr_t)_shm_end;

    uint32_t p = (uint32_t)(uintptr_t)g_shm;
    uint32_t s = g_shm_start_addr;   /* volatile 读: 编译器无法再"证明" p != s */
    uint32_t e = g_shm_end_addr;

    if (p != s)                                        return 0;  /* 段首不吻合 */
    if (e != p + SHM_SIZE)                             return 0;  /* 段长不吻合 */
    if (p < 0x20000000u || p + SHM_SIZE > 0x20020000u) return 0;  /* 不在 DTCM 128KB */
    return 1;
}

/* ══════════════════════════════════════════════════════════════════
 * 读源 (与 S3 core0_isr.c read_source 同构)
 * ══════════════════════════════════════════════════════════════════ */
AINLINE float read_source(uint8_t st, uint8_t si, const float *sm, const float *wm,
                          const uint8_t *base)
{
    switch (st) {
        case SRC_SENSOR: return sm[si & (MAX_SENSORS - 1)];
        case SRC_WIRE:   return wm[si & (MAX_WIRES - 1)];
        /* ★ 注意: CONST 源的常量值用 **src_index** 索引参数表, 不是 route.param_idx
         *   —— 这是 S3 第二十五轮踩过的坑 (脚本误用 param_idx → 所有路由都取
         *   param[0], LA 解出 00fa00fa...)。移植时刻意逐字保留同一语义。 */
        case SRC_CONST:  return ((const ParamEntry_t *)(base + OFF_PARAM_TABLE
                                    + ((si & (MAX_PARAMS - 1)) * 16)))->value_a;
        /* ★ 未实现源 —— **显式列出**而不是让它掉进 default 被"静默吞掉":
         *   静默返回 0 会让"引用了未实现源"伪装成"这条路由恒为 0"
         *   (同 S3 审计 M1 族: 静默恒假的语义错误最难发现)。
         *   阶段 4 落地前, deploy 校验必须在源头拒绝 src_type == SRC_HMI。 */
        case SRC_HMI:    return 0.0f;
        default:         return 0.0f;
    }
}

/* ══════════════════════════════════════════════════════════════════
 * ★ 落位敏感实验的填充区 (AUDIT-H723-stage2.md H8)
 *   在 FLASH 版扫描体之前插入 SCAN_FLASH_PAD 字节 —— 只挪地址, 不改指令。
 *   配合链接脚本的 .scan_pad / .scan_flash 两个专区, 让"落位"成为唯一变量;
 *   ITCM 版 (VMA 固定 0x0) 不受影响, 作为"必须纹丝不动"的控制组。
 *   ★ 必须 KEEP/used: 否则 --gc-sections 会把这段"没人引用"的常量回收掉,
 *     落位实验会静默失效 (本项目的经典坑, 见 README 纪律 7)。
 * ══════════════════════════════════════════════════════════════════ */
#ifndef SCAN_FLASH_PAD
#define SCAN_FLASH_PAD 0
#endif

#if SCAN_FLASH_PAD > 0
__attribute__((used, section(".scan_pad")))
const uint8_t g_scan_pad[SCAN_FLASH_PAD] = { 0x5A };
#endif

/* ══════════════════════════════════════════════════════════════════
 * ★ 扫描体: 一份源码 → 两份实例 (FLASH / ITCM)
 * ══════════════════════════════════════════════════════════════════ */
#define DEFINE_ENGINE_SCAN(FN, ATTR)                                           \
ATTR uint32_t FN(uint8_t *base, uint32_t first, uint32_t count)                \
{                                                                              \
    uint32_t n = count;                                                        \
    if (first >= MAX_ROUTES) return 0;                                         \
    if (first + n > MAX_ROUTES) n = MAX_ROUTES - first;                        \
    const RouteEntry_t *rt = (const RouteEntry_t *)(base + OFF_ROUTE_TABLE);    \
    const ParamEntry_t *pm = (const ParamEntry_t *)(base + OFF_PARAM_TABLE);    \
    StateEntry_t       *st = (StateEntry_t       *)(base + OFF_STATE_TABLE);    \
    float *sm = (float *)(base + OFF_SENSOR_MAP);                              \
    float *wm = (float *)(base + OFF_WIRE_MAP);                                \
    float *ac = (float *)(base + OFF_ACTUATOR_STATUS);                         \
    float *lu = (float *)(base + OFF_LUT_DATA);                                \
    uint32_t acc = 0;                                                          \
                                                                               \
    for (uint32_t k = 0; k < n; k++) {                                         \
        const RouteEntry_t *r = &rt[first + k];                                 \
        /* 未激活的路由跳过 (S3 桶化只放 ACTIVE, 全表扫需显式判) */             \
        if (!(r->flags & ROUTE_FLAG_ACTIVE)) continue;                         \
        uint32_t dv = r->period & PERIOD_DIV_MASK;                             \
        float dt = (dv == PERIOD_DIV_IDX_MID) ? DT_MID                         \
                 : (dv == PERIOD_DIV_IDX_SLOW) ? DT_SLOW : DT_FAST;            \
        float src = read_source(r->src_type, r->src_index, sm, wm, base);      \
        const ParamEntry_t *p = &pm[r->param_idx & (MAX_PARAMS - 1)];          \
        StateEntry_t *s = (r->state_offset && r->state_offset < MAX_STATES)    \
                          ? &st[r->state_offset] : &s_state_fallback;          \
        float wb = (r->wire2_idx < MAX_WIRES) ? wm[r->wire2_idx] : 0.0f;       \
        if (!_finite_f(src)) src = 0.0f;                                       \
        if (!_finite_f(wb))  wb  = 0.0f;                                       \
        float res = prim_exec(r->op, src, p, s, wm, lu, wb, dt);               \
        if (r->dst_channel < MAX_WIRES) wm[r->dst_channel] = res;              \
        uint16_t ai = r->actuator_idx;                                         \
        if (ai && ai < MAX_ACTUATORS) ac[ai] = res;                            \
        /* 校验和: ① 防死代码消除 ② 给外部一个"真算过"的证据 */                \
        union { float f; uint32_t u; } cv; cv.f = res; acc ^= cv.u;            \
    }                                                                          \
    return acc;                                                                \
}

/* FLASH 版 (经 L1 I-cache + AXI/flash 取指) —— 独占 .scan_flash 段, 便于落位实验 */
DEFINE_ENGINE_SCAN(engine_scan_flash, __attribute__((section(".scan_flash"), noinline)))

/* ITCM 版 (0x00000000, 零等待, 无 cache, 不经 AXI) */
DEFINE_ENGINE_SCAN(engine_scan_itcm,  __attribute__((section(".itcm_text"), noinline)))

/* ══════════════════════════════════════════════════════════════════
 * 阶段 3: 档桶调度 (S3 OA15 治本)
 *
 * 为什么需要它: 阶段 2 的"全表扫"实测 128 条混合程序要 39426~41166 cyc,
 * 占拍 98~104% —— 贴着上限, 一次无关改动的落位就能把它推过线。
 * 桶化后每拍只跑 [div0 全部] + [div1 本拍 phase] + [div2 本拍 phase]。
 * ══════════════════════════════════════════════════════════════════ */

uint32_t engine_build_buckets(uint8_t *base, uint32_t nr)
{
    if (nr > MAX_ROUTES) nr = MAX_ROUTES;
    RouteEntry_t *act = (RouteEntry_t *)(base + OFF_ROUTE_TABLE);
    RouteEntry_t *scr = (RouteEntry_t *)(base + OFF_ROUTE_STAGING);  /* 暂存 */
    uint16_t *bkt  = (uint16_t *)(base + OFF_ROUTE_BUCKETS);
    uint16_t *off1 = bkt, *cnt1 = bkt + BUCKET_DIV1_PHASES;
    uint16_t *off2 = bkt + 20, *cnt2 = bkt + 120;

    /* 快照源表 (重排是"读源写目"的同表操作, 必须先存) */
    for (uint32_t i = 0; i < nr; i++) scr[i] = act[i];

    /* ★ 幂等的关键: 桶表必须**先清零**。不清的话第二次调用会把计数叠加上去
     *   (S3 "脚本非幂等"那一族在固件里的对应形态)。 */
    for (int i = 0; i < ROUTE_BUCKET_U16; i++) bkt[i] = 0;

    /* ---- 第一遍: 统计各桶容量 ---- */
    uint16_t n0 = 0;
    for (uint32_t i = 0; i < nr; i++) {
        uint32_t dv = scr[i].period & PERIOD_DIV_MASK;
        if      (dv == PERIOD_DIV_IDX_FAST) n0++;
        else if (dv == PERIOD_DIV_IDX_MID)  cnt1[(scr[i].period >> PERIOD_PHASE_SHIFT)
                                                 % BUCKET_DIV1_PHASES]++;
        else if (dv == PERIOD_DIV_IDX_SLOW) cnt2[(scr[i].period >> PERIOD_PHASE_SHIFT)
                                                 % BUCKET_DIV2_PHASES]++;
    }

    /* ---- 计算桶起点 (off1[0] 兼作"div0 条数", 与 S3 同语义) ---- */
    uint16_t acc = n0;
    for (int p = 0; p < BUCKET_DIV1_PHASES; p++) { off1[p] = acc; acc += cnt1[p]; }
    uint16_t div1_total = (uint16_t)(acc - n0);
    for (int p = 0; p < BUCKET_DIV2_PHASES; p++) { off2[p] = acc; acc += cnt2[p]; }

    /* ---- 第二遍: 按桶序落位 (对源表按序扫描 → 桶内相对顺序不变 = 稳定排序,
     *      所以对已归组的表再跑一次是恒等变换 ⇒ 幂等) ---- */
    uint32_t w = 0;
    for (uint32_t i = 0; i < nr; i++) {                      /* div0 段 */
        if ((scr[i].period & PERIOD_DIV_MASK) == PERIOD_DIV_IDX_FAST) act[w++] = scr[i];
    }
    for (int p = 0; p < BUCKET_DIV1_PHASES; p++) {           /* div1 各 phase 桶 */
        for (uint32_t i = 0; i < nr; i++) {
            if ((scr[i].period & PERIOD_DIV_MASK) != PERIOD_DIV_IDX_MID) continue;
            if (((scr[i].period >> PERIOD_PHASE_SHIFT) % BUCKET_DIV1_PHASES) != (uint32_t)p) continue;
            act[w++] = scr[i];
        }
    }
    for (int p = 0; p < BUCKET_DIV2_PHASES; p++) {           /* div2 各 phase 桶 */
        for (uint32_t i = 0; i < nr; i++) {
            if ((scr[i].period & PERIOD_DIV_MASK) != PERIOD_DIV_IDX_SLOW) continue;
            if (((scr[i].period >> PERIOD_PHASE_SHIFT) % BUCKET_DIV2_PHASES) != (uint32_t)p) continue;
            act[w++] = scr[i];
        }
    }
    (void)div1_total;
    return w;   /* 应 == nr */
}

uint32_t engine_bucket_checksum(const uint8_t *base)
{
    const uint16_t *bkt = (const uint16_t *)(base + OFF_ROUTE_BUCKETS);
    uint32_t h = 0x811C9DC5u;
    for (int i = 0; i < ROUTE_BUCKET_U16; i++) {
        h = (h ^ (uint32_t)bkt[i]) * 16777619u;
    }
    return h;
}

uint32_t engine_bucket_dead_slots(const uint8_t *base)
{
    /* 槽 64..99 在**两个**数组里: off2[64..99] 与 cnt2[64..99] */
    const uint16_t *bkt = (const uint16_t *)(base + OFF_ROUTE_BUCKETS);
    const uint16_t *off2 = bkt + 20, *cnt2 = bkt + 120;
    uint32_t bad = 0;
    for (int p = BUCKET_DIV2_PHASES; p < 100; p++) {
        if (off2[p]) bad++;
        if (cnt2[p]) bad++;
    }
    return bad;
}

ATTR_ITCM uint32_t engine_tick(uint8_t *base, uint32_t tick, engine_scan_fn impl,
                               uint32_t *nrun_out)
{
    const uint16_t *bkt  = (const uint16_t *)(base + OFF_ROUTE_BUCKETS);
    const uint16_t *off1 = bkt, *cnt1 = bkt + BUCKET_DIV1_PHASES;
    const uint16_t *off2 = bkt + 20, *cnt2 = bkt + 120;

    uint32_t ph1 = tick % (uint32_t)BUCKET_DIV1_PHASES;
    uint32_t ph2 = tick % (uint32_t)BUCKET_DIV2_PHASES;
    uint32_t nrun = 0, ck = 0;

    uint32_t n0 = off1[0];                       /* div0 段: [0, n0) 每拍全跑 */
    if (n0) { ck ^= impl(base, 0, n0); nrun += n0; }

    uint32_t b1 = off1[ph1], c1 = cnt1[ph1];     /* div1: 本拍 phase 桶 */
    if (c1) { ck ^= impl(base, b1, c1); nrun += c1; }

    uint32_t b2 = off2[ph2], c2 = cnt2[ph2];     /* div2: 本拍 phase 桶 */
    if (c2) { ck ^= impl(base, b2, c2); nrun += c2; }

    if (nrun_out) *nrun_out = nrun;
    return ck;
}

/* ══════════════════════════════════════════════════════════════════
 * 表填充 (测量用的确定性装载, 不是 deploy —— deploy 属阶段 3.2)
 * ══════════════════════════════════════════════════════════════════ */
static const uint8_t k_mixed_ops[19] = {
    OP_DIRECT, OP_CMP, OP_CLAMP, OP_SCALE, OP_AND, OP_OR, OP_NOT, OP_MUX,
    OP_LUT, OP_LPF, OP_PID, OP_HYST, OP_RATE, OP_DEADBAND, OP_EDGE, OP_CNT,
    OP_TIMER, OP_ARITH, OP_SR
};

void engine_fill_tables(uint8_t *base, int profile)
{
    /* ★ 先走单一入口清零 (不允许这里自带 memset —— 见 cold_start_reset 注释)。
     *   约定: base 必须是 g_shm 本身 (表的唯一归属地是 .dtcm_shm)。
     *   若将来真需要第二块表区, 必须回 cold_start_reset 登记, 而不是绕过它。 */
    cold_start_reset();

    float *sm = (float *)(base + OFF_SENSOR_MAP);
    float *wm = (float *)(base + OFF_WIRE_MAP);
    float *lu = (float *)(base + OFF_LUT_DATA);
    ParamEntry_t *pm = (ParamEntry_t *)(base + OFF_PARAM_TABLE);
    RouteEntry_t *rt = (RouteEntry_t *)(base + OFF_ROUTE_TABLE);

    /* 传感器/线网初值: 小幅度确定性序列 (避免 NaN 与极端值) */
    for (int i = 0; i < MAX_SENSORS; i++) sm[i] = (float)(i % 21) * 0.5f;
    for (int i = 0; i < MAX_WIRES;   i++) wm[i] = (float)(i % 13) * 0.25f;
    for (int i = 0; i < MAX_LUT;     i++) lu[i] = (float)(i & 0x3F) * 0.1f;

    /* 参数: 每个 op 会用到的字段都给合理值 */
    for (int i = 0; i < MAX_PARAMS; i++) {
        pm[i].value_a = 0.002f + (float)(i % 7) * 0.001f;  /* LPF τ / CLAMP lo / 阈值 */
        pm[i].value_b = 5.0f   + (float)(i % 5);           /* CLAMP hi / 第二操作数 */
        pm[i].value_c = 0.01f;                             /* PID Kd */
        pm[i].value_d = 50.0f;                             /* PID sp */
    }

    /* 路由 */
    for (int i = 0; i < MAX_ROUTES; i++) {
        uint8_t op;
        switch (profile) {
            case 1:  op = k_mixed_ops[i % 19]; break;   /* 19 原语轮转 */
            case 2:  op = OP_PID;              break;   /* 最重档 */
            case 4:  op = k_mixed_ops[i % 19]; break;   /* 三档混合 · 原语轮转 */
            default: op = OP_DIRECT;           break;   /* 0 与 3 */
        }
        /* ---- 档位/相位分配 (★ 必须与工具里的 Python 预测逐字对应) ----
         * profile 3/4 = 三档混合: div = i % 3, 约 1/3 落在 div0/1/2;
         *   div1 phase = (i/3) % BUCKET_DIV1_PHASES
         *   div2 phase = (i/3) % BUCKET_DIV2_PHASES   (64, 不是 100 —— 见 engine.h 的 H9)
         * 其它 profile = 全 div0 (与阶段 2 的表保持一致, 便于对照)。 */
        uint8_t dv = PERIOD_DIV_IDX_FAST, ph = 0;
        if (profile == 3 || profile == 4) {
            uint32_t g = (uint32_t)i / 3u;
            uint32_t m = (uint32_t)i % 3u;
            if (m == 1)      { dv = PERIOD_DIV_IDX_MID;  ph = (uint8_t)(g % BUCKET_DIV1_PHASES); }
            else if (m == 2) { dv = PERIOD_DIV_IDX_SLOW; ph = (uint8_t)(g % BUCKET_DIV2_PHASES); }
        }
        rt[i].src_type     = (uint8_t)((i % 3 == 0) ? SRC_SENSOR
                                     : (i % 3 == 1) ? SRC_WIRE : SRC_CONST);
        rt[i].src_index    = (uint8_t)(i % 64);
        rt[i].dst_type     = DST_WIRE;
        rt[i].dst_channel  = (uint8_t)(i % MAX_WIRES);
        rt[i].op           = op;
        rt[i].flags        = ROUTE_FLAG_ACTIVE;
        rt[i].param_idx    = (uint16_t)(i % MAX_PARAMS);
        /* 有状态原语才挂 state 槽 (无状态原语挂 0 = 兜底槽, 与 S3 同语义) */
        rt[i].state_offset = (uint16_t)((op == OP_LPF || op == OP_PID || op == OP_HYST ||
                                         op == OP_RATE || op == OP_DEADBAND || op == OP_EDGE ||
                                         op == OP_CNT || op == OP_TIMER || op == OP_SR)
                                        ? (i % MAX_STATES) : 0);
        rt[i].actuator_idx = 0;
        rt[i].wire2_idx    = (uint16_t)((op == OP_AND || op == OP_OR || op == OP_ARITH ||
                                         op == OP_SR  || op == OP_CNT)
                                        ? ((i + 7) % MAX_WIRES) : 0);
        rt[i].period       = (uint8_t)(dv | (uint8_t)(ph << PERIOD_PHASE_SHIFT));
        rt[i].reserved     = 0;
    }

    /* ★ 归组重排 + 生成桶索引 (幂等)。
     *   非三档 profile 也照跑 —— 结果是恒等变换(全 div0), 但保证桶表一定被建立,
     *   不会出现"某档忘了建桶 → 每拍空扫"的静默失效。 */
    engine_build_buckets(base, MAX_ROUTES);
}
