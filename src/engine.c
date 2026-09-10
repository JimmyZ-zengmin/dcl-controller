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
    const RouteEntry_t *rt = (const RouteEntry_t *)(base + OFF_ROUTE_TABLE);
    uint32_t h = 0x811C9DC5u;                        /* FNV-1a 偏移基数 */
    for (int i = 0; i < MAX_ROUTES; i++) {
        uint32_t v = (uint32_t)rt[i].op
                   | ((uint32_t)rt[i].flags        << 8)
                   | ((uint32_t)rt[i].src_type     << 16)
                   | ((uint32_t)rt[i].state_offset << 20);
        h = (h ^ v) * 16777619u;                     /* FNV-1a 素数 (u32 自然回绕) */
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
ATTR uint32_t FN(uint8_t *base, uint32_t n)                                    \
{                                                                              \
    if (n > MAX_ROUTES) n = MAX_ROUTES;                                        \
    const RouteEntry_t *rt = (const RouteEntry_t *)(base + OFF_ROUTE_TABLE);    \
    const ParamEntry_t *pm = (const ParamEntry_t *)(base + OFF_PARAM_TABLE);    \
    StateEntry_t       *st = (StateEntry_t       *)(base + OFF_STATE_TABLE);    \
    float *sm = (float *)(base + OFF_SENSOR_MAP);                              \
    float *wm = (float *)(base + OFF_WIRE_MAP);                                \
    float *ac = (float *)(base + OFF_ACTUATOR_STATUS);                         \
    float *lu = (float *)(base + OFF_LUT_DATA);                                \
    uint32_t acc = 0;                                                          \
                                                                               \
    for (uint32_t i = 0; i < n; i++) {                                         \
        const RouteEntry_t *r = &rt[i];                                        \
        /* 未激活的路由跳过 (S3 桶化只放 ACTIVE, 这里全表扫需显式判) */         \
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
 * 表填充 (测量用的确定性装载, 不是 deploy —— deploy 属阶段 3)
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
            case 1:  op = k_mixed_ops[i % 19]; break;
            case 2:  op = OP_PID;              break;
            default: op = OP_DIRECT;           break;
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
        rt[i].period       = PERIOD_DIV_IDX_FAST;          /* 全 div0 (本阶段不分档) */
    }
}
