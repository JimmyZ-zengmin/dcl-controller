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
#include "regs.h"
#include "engine.h"
#include "modbus.h"
#include "macro.h"
#include "lsym.h"
#include "primitives.h"
#include "faultlog.h"   /* 统一故障台账: cold_start_reset 是本域的登记入口 */

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

/* ★★ 审计发现 H 的观测面 —— 已随**定案②** 拆成两个量 (2026-09-12):
 *
 *   `g_safe_mask_nonzero` : GPIO_MASK 非 0 且**合法** (高 16 位为 0) 的次数。
 *     ★ 语义已变: GPIO_MASK 定案为 "GPIOE[15:0] 输出使能掩码" 之后, 它非 0 是
 *       **预期行为** (有域正常登记了输出位), 不再是"应恒为 0 的否定性证据"。
 *       它现在回答的是"**这个字段被用起来了没有**"。
 *
 *   `g_safe_mask_oob` : GPIO_MASK **高 16 位非 0** 的次数 = **语义违规**。
 *     ★ 为什么另立一个, 而不是并进上面那个: 一个计数只回答一个问题。
 *       "有域正常登记输出位" 与 "有人按旧的跨端口 u32[6] 设想写了个越界值" 是
 *       **两种病因** —— 混在一个数里就再也分不出来 (本项目对"空判据/混判据"的既有教训)。
 *     ★ 它才是真正"必须查的 bug"信号, 且**可失败**: P3 落地后正常使用不会让它动。
 *
 * 为什么观测面必须存在 (原论证保留): "不可达 / 从来没人写"是一个**否定性声称** ——
 * 按本项目纪律, 否定性声称同样需要**可被外部核对的证据**。 */
volatile uint32_t g_safe_mask_nonzero = 0;
volatile uint32_t g_safe_mask_oob     = 0;

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
    /* ★★ 审计发现 C: 就绪标志与布局版本**必须在这里写, 而不是在 main 里写一次**。
     *   理由: cold_start_reset 是"冷启动清零的单一入口" (本项目的既有纪律:
     *   新增域必须登记到该函数)。所有清零路径 —— main 上电 / 0x13 RESET /
     *   engine_fill_tables / deploy 装载 —— 都要经过它。
     *   若把写 MAGIC 放在 main 里: 0x13 RESET 之后 MAGIC 又变回 0, 于是
     *   "SHM 已就绪"的外部门就会在**一次普通 RESET 之后不再成立** ——
     *   而 PC 侧只会看到"读到 0", 分不清"没就绪"与"刚被 RESET 过"。
     *   ⇒ 写在单一入口 = 任何清零动作之后 SHM 都立刻回到"已就绪(空配置)"态。
     *   ★ HEARTBEAT 不在这里写: 它由 ISR 在 gate&&RUN 时递增, 清零即"复位心跳",
     *     语义正确 (RESET 后不应残留旧心跳值)。 */
    SHM_U32(g_shm, OFF_CTRL_MAGIC)   = CTRL_MAGIC;
    SHM_U32(g_shm, OFF_CTRL_VERSION) = SHM_LAYOUT_VERSION;
    /* ★ W4: 通信域配置重建。
     *   本函数的 memset 把 MB 控制块一起清了 ⇒ 必须显式重建默认配置, 否则
     *   一次 RESET 就让通信域变成"enabled=0 / slave_addr=0"的死状态 ——
     *   而 PC 侧只会看到 0x60 永远 NAK, 查不出是"没配置"还是"坏了"。
     *   (S3 不需要这一步是因为它的 cold_start_reset 不整段 memset; 差异已记在
     *    modbus.h 的 mb_config 说明里。) */
    mb_config(g_shm, MB_DEFAULT_ADDR, MB_DEFAULT_USE_UART);
    /* ★ W5: macro 域登记。
     *   本函数的 memset 已经把 macro 控制块清了 (run/len/loop_cnt 全 0 = 停),
     *   这里显式调 macro_reset 是**登记动作** —— 让"新域必须登记到单一入口"
     *   这条纪律在代码里可见, 并为将来可能出现的非零默认值留落点。
     *   ★ 语义后果必须说清: 因为 macro 字节码也驻 SHM, 所以**任何清零路径
     *     (上电 / 0x13 RESET / deploy 装载) 都会清掉已上传的程序** —— 这是
     *     macro.h 声明的 v0 边界 (RAM-only, 掉电/RESET 丢失), 不是缺陷。 */
    macro_reset(g_shm);
    /* ★ 统一故障台账登记 (2026-09-13)。
     *   memset 已把台账清 0, 这里显式写 magic —— 目的是让"漏登记"从
     *   "读出来是 0 分不清'干净'还是'没登记'" 变成 `fault_sane()==1` 的**可判**状态。
     *   ★ 台账必须活在**任何清零路径**之后仍然已登记: 上电 / 0x13 RESET /
     *     deploy 装载 / engine_fill_tables 都经过本函数 ⇒ 放在这里是唯一正确位置。 */
    fault_init(g_shm);
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
 *   ⇒ 这是一**类**问题 (所有 linker symbol 与 C 对象地址比较处), 已固化成
 *     `LSYM_ADDR()` 宏 (src/lsym.h) —— 本函数走它, 而不是就地手写 volatile 中转。
 *   ★ 权威比对仍交给**外部工具**: pyocd 读回 g_shm_start_addr/g_shm_end_addr
 *     与 nm 的符号地址比对 (固件自报永远可能被优化掉, 外部比对不会)。
 */
int shm_layout_ok(void)
{
    uint32_t s = (uint32_t)LSYM_ADDR(_shm_start);   /* ← 不可折叠的取址 */
    uint32_t e = (uint32_t)LSYM_ADDR(_shm_end);
    uint32_t p = (uint32_t)(uintptr_t)g_shm;

    /* 导出给外部工具做权威比对 (值本身由上面的不可折叠路径取得) */
    g_shm_start_addr = s;
    g_shm_end_addr   = e;

    if (p != s)                                        return 0;  /* 段首不吻合 */
    if (e != p + SHM_SIZE)                             return 0;  /* 段长不吻合 */
    if (p < 0x20000000u || p + SHM_SIZE > 0x20020000u) return 0;  /* 不在 DTCM 128KB */
    /* 反向检查: 哨兵区必须落在 SHM 之后而不是与 SHM 重叠 */
    if (e < p + SHM_SIZE)                              return 0;
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
        /* ★ 第二输入判据: **必须走 wire2_valid()** (与 S3 最终形态、与 deploy 校验
         *   共用同一判据)。旧写法只查 `wire2_idx < MAX_WIRES`, 会把"没接第二输入"
         *   (wire2_idx==0) 当成"第二输入是 wire[0]" → 静默读错值 (A3 实锤:
         *   ARITH(CONST 10, 无 WIRE2 标志, wire[0]=7) 输出 17.0, 应为 10.0)。 */ \
        float wb = wire2_valid(r->flags, r->wire2_idx) ? wm[r->wire2_idx] : 0.0f;  \
        if (!_finite_f(src)) src = 0.0f;                                       \
        if (!_finite_f(wb))  wb  = 0.0f;                                       \
        float res = prim_exec(r->op, src, p, s, wm, lu, wb, dt);               \
        if (r->dst_channel < MAX_WIRES) {                                      \
            /* ★★ W2.2 写端屏蔽: 被强制的 wire **不允许**被路由覆写。          \
             *   拍首已把它钉成 FORCE_VAL, 这里是同一拍内的另一半 —— 少了它,   \
             *   强制只在"路由不写该 wire"时有效, 一旦有路由写它, 值立刻被改。  \
             *   判据 (tools/h723_force.py 用例 2): 部署一条写 wire[3] 的路由,  \
             *   强制 wire[3]=7.5 → 跑 ≥3 拍后回读仍须 == 7.5 (写端被屏蔽)。   \
             * ★ mask 就地 volatile 读而不是从栈快照取: 栈快照是**拍首**的值,  \
             *   而 h_force 可能在本拍扫描**进行中被协议侧改掉** —— 用旧快照会  \
             *   让"刚刚置位的 force"晚一拍生效 (判据会看到 3 拍里第 1 拍漏过)。 */ \
            uint32_t dw = (uint32_t)r->dst_channel;                            \
            uint32_t msk = *(const volatile uint32_t *)                        \
                (base + OFF_FORCE_MASK + (dw >> 5) * 4u);                      \
            if (!(msk & (1u << (dw & 31u)))) wm[r->dst_channel] = res;         \
        }                                                                      \
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
    /* 死槽 = 相位 64..99 (路由的 6 位 phase 字段到不了那里) —— 在**两个**数组里:
     * off2[64..99] 与 cnt2[64..99]。
     * ★ H9 第二次修正: 判据的本体没变 (这 72 个槽必须恒 0), 变的只是它的边界来源 ——
     *   现在用 BUCKET_DIV2_PHASE_MAX (phase 字段上限) 而不是整个表长。 */
    const uint16_t *bkt = (const uint16_t *)(base + OFF_ROUTE_BUCKETS);
    const uint16_t *off2 = bkt + 20, *cnt2 = bkt + 120;
    uint32_t bad = 0;
    for (int p = BUCKET_DIV2_PHASE_MAX + 1; p < BUCKET_DIV2_PHASES; p++) {
        if (off2[p]) bad++;
        if (cnt2[p]) bad++;
    }
    return bad;
}

ATTR_ITCM /* ══════════════════ W2.2 — 拍首 Force 覆写 (独立入口) ══════════════════
 *
 * ★★ 为什么必须在 engine_tick **之外**单独存在一个入口:
 *    第一版把覆写写在 engine_tick() 里 —— 但 ISR 有**两条扫描路径**:
 *      · g_scan_mode=1 → engine_tick()      (分档调度)
 *      · g_scan_mode=0 → engine_scan_*(全表扫, 阶段 2 基线与 bench 用)
 *    覆写只在其中一条上, 于是"BOOT_SCAN_MODE=0 的板子上 force 完全不生效"。
 *    实测 2026-09-11: fmask=0x8 / fval=7.5 都写进去了, wire[3] 却恒为路由值 1.5
 *    —— 症状是"SHM 全对但效果为零", 与 A1 事故 (NVIC_ISER 位移溢出) 同族。
 *    ⇒ 凡"每拍都必须发生"的动作, 不能挂在某条分支里; 必须是扫描**之前**的
 *      统一前置步骤。这条同时修掉了"换 scan_mode 就静默失效"的隐患。
 *
 * 语义 (S3 core0_isr.c:196-215 同构):
 *   被强制的 wire 先钉成 FORCE_VAL, 然后才扫路由。
 *   ⇒ 下游路由读到的就是强制值 (而不是"上一拍的旧值 + 这一拍才被覆盖")。
 *
 * ★ 为什么值必须写 FORCE_VAL 而不是只写 WIRE_MAP (OA9 事故本身):
 *   拍首覆写是**每拍**做的。若只写 WIRE_MAP, 下一拍拍首会用 FORCE_VAL(0)
 *   把它抹掉 —— 强制值为 0 时看不出问题 (bug 与期望重合), 一旦强制非零值
 *   就立刻暴露。S3 旧版 13/13 全绿正是"测试全用 val=0.0"造成的判据盲区。
 *
 * ★ 只遍历置位位 (ctz): 全扫 128 固定要 ~2000 cyc = 预算 5%;
 *   强制 1 个 wire 不该和强制 128 个同样贵 —— 循环次数 = 置位数。
 *
 * ★ 放 ITCM: 这是每拍都要跑的代码, 且它必须在拍长内完成。
 */
ATTR_ITCM void engine_force_apply(uint8_t *base)
{
    uint32_t fm[FORCE_MASK_WORDS];
    fm[0] = *(const volatile uint32_t *)(base + OFF_FORCE_MASK + 0u);
    fm[1] = *(const volatile uint32_t *)(base + OFF_FORCE_MASK + 4u);
    fm[2] = *(const volatile uint32_t *)(base + OFF_FORCE_MASK + 8u);
    fm[3] = *(const volatile uint32_t *)(base + OFF_FORCE_MASK + 12u);
    if (!(fm[0] | fm[1] | fm[2] | fm[3])) return;   /* 无强制 = 快路径 (零成本) */

    volatile float *wm = (volatile float *)(void *)(base + OFF_WIRE_MAP);
    const volatile float *fv = (const volatile float *)(const void *)(base + OFF_FORCE_VAL);
    for (uint32_t w = 0; w < FORCE_MASK_WORDS; w++) {
        uint32_t m = fm[w];
        while (m) {
            uint32_t b = (uint32_t)__builtin_ctz(m);
            wm[w * 32u + b] = fv[w * 32u + b];
            m &= m - 1u;
        }
    }
}

ATTR_ITCM /* ══════════════════ W3 — 顺序域扫描段 (Sequencer v0) ══════════════════
 *
 * 位置语义: 由 TIM2_IRQHandler 在**路由扫描之后、计时统计之前**调用。
 *   · 之后: 本拍路由已跑完, seq 条件读到的 wire/SENSOR 是本拍的最新值
 *     (拍首 force 覆写 → 路由 → seq, 这个顺序让"步号 → 译码路由 → 输出"在一拍内闭合)
 *   · 之前: seq 的成本天然计入 isr_cyc (无需新统计), 且被拍长门覆盖
 *
 * ★★ 四条件 v0 (与 S3 core0_isr.c:288+ 逐字同):
 *   ① 条件转移: 步条目的条件源 (SENSOR/WIRE) > param.value_a
 *   ② 超时强推: flags.timeout_en 且 停留激活拍数 × dt ≥ param.value_b (秒)
 *   ③ 末步: flags.loop → 回第 1 步; 否则停末步 (完成态, 步号保持不归零)
 *   ④ 步号镜像: out_wire = step_cur + 1 (1.0 起步, 与"第几步"人眼一致)
 *
 * ★ 一次至多推进 1 步 (不是 while 循环): 否则"条件恒真 + loop"会让一个实例在
 *   单拍内绕完整圈 —— WCET 就随步数增长了, 且外部看不到中间步号。
 *   一拍一步 ⇒ 成本上界 = 实例数, 与前缀无关 (可预算)。
 *
 * ★ 非激活拍: (div,phase) 门在**读步条目之前**就跳过 —— 所以慢档实例在非激活拍
 *   的成本只有"一次取模 + 一次比较"。这是"8 实例全慢档均摊 <2%"的来源。
 *
 * ★ 写端屏蔽 (与路由 DEFINE_ENGINE_SCAN 的 dst 写同一判据, 就地 volatile 读):
 *   被 force 的 wire 不允许被 seq 改。缺这一半,"强制"只在"seq 不推进"时有效。
 */
ATTR_ITCM uint32_t engine_seq_tick(uint8_t *base, uint32_t tick)
{
    uint32_t n_seq = (uint32_t)*(const volatile uint8_t *)(base + OFF_CTRL_N_SEQ);
    if (n_seq == 0u) return 0u;
    if (n_seq > MAX_SEQ_INST) n_seq = MAX_SEQ_INST;

    const SeqStepEntry_t *stb = (const SeqStepEntry_t *)(const void *)(base + OFF_SEQ_TABLE);
    const ParamEntry_t   *pm  = (const ParamEntry_t *)(const void *)(base + OFF_PARAM_TABLE);
    const volatile float *sm  = (const volatile float *)(const void *)(base + OFF_SENSOR_MAP);
    volatile float       *wm  = (volatile float *)(void *)(base + OFF_WIRE_MAP);
    /* ★ 0x44 写表时用的就是 void* 直写 (无 staging), 所以这里必须按 volatile 读
     *   —— 否则编译器可把整条 seq 循环提到"从不改变"的假设下优化掉。 */
    volatile SeqCtrl_t *scw = (volatile SeqCtrl_t *)(void *)(base + OFF_SEQ_CTRL);

    uint32_t ph1 = tick % (uint32_t)BUCKET_DIV1_PHASES;      /* 10 */
    uint32_t ph2 = tick % (uint32_t)BUCKET_DIV2_PHASES;      /* 100 —— 原注写 64 是过期值, 见 engine.h:191 */
    uint32_t wrote = 0u;

    for (uint32_t i = 0; i < n_seq; i++) {
        uint16_t run      = scw[i].run;
        uint16_t cur      = scw[i].step_cur;
        uint16_t nsteps   = scw[i].n_steps;
        uint16_t sbase    = scw[i].step_base;
        uint16_t owire    = scw[i].out_wire;
        uint8_t  period   = scw[i].period;
        uint32_t sttick   = scw[i].step_tick;

        if (!(run & 1u)) continue;
        if (nsteps == 0u || cur >= nsteps) continue;         /* 完成态/空实例 */

        /* ---- (div, phase) 门: 与本拍的路由扫描同一套数 (相位对齐) ---- */
        uint8_t pd = (uint8_t)(period & PERIOD_DIV_MASK);
        uint8_t ph = (uint8_t)((period >> PERIOD_PHASE_SHIFT) & 0x3Fu);
        float dt;
        if (pd == PERIOD_DIV_IDX_FAST)      dt = 0.0001f;
        else if (pd == PERIOD_DIV_IDX_MID)  { if (ph1 != ph) continue; dt = 0.001f; }
        else if (pd == PERIOD_DIV_IDX_SLOW) { if (ph2 != ph) continue; dt = 0.01f; }
        else continue;                                       /* div=3 非法: 不收不推 */

        /* ---- 读本步条目 (越界保护: 表损坏时宁可不动, 不读别人的槽) ---- */
        uint32_t slot = (uint32_t)sbase + (uint32_t)cur;
        if (slot >= MAX_SEQ_STEPS) continue;
        SeqStepEntry_t e = stb[slot];   /* ★ 整体拷贝: 表对 ISR 只读, 但 0x44 会写 */

        uint32_t pidx = (uint32_t)e.param_idx;
        if (pidx >= MAX_PARAMS) continue;                    /* 越界: 不推进 (校验器本该拦) */
        const ParamEntry_t *pp = &pm[pidx];

        int adv = 0;
        /* ---- 条件转移 (阈值): 有限值才比 —— NaN 比较恒假, 会让引擎静默卡步 ---- */
        if (e.cond_type <= 1u) {
            float v;
            if (e.cond_type == 0u) {
                uint32_t ci = (uint32_t)e.cond_idx;
                if (ci >= MAX_SENSORS) continue;
                v = sm[ci];
            } else {
                uint32_t ci = (uint32_t)e.cond_idx;
                if (ci >= MAX_WIRES) continue;
                v = wm[ci];
            }
            union { float f; uint32_t u; } cv; cv.f = v;
            if (is_finite_bits(cv.u) && cv.f > pp->value_a) adv = 1;
        }
        /* ---- 超时强推 ---- */
        if (!adv && (e.flags & 2u)) {
            sttick++;
            scw[i].step_tick = sttick;
            if ((float)sttick * dt >= pp->value_b) adv = 1;
        }

        if (adv) {
            uint16_t ncur = (uint16_t)(cur + 1u);
            if (ncur >= nsteps)
                ncur = (e.flags & 1u) ? 0u : (uint16_t)(nsteps - 1u);   /* loop / 完成态 */
            scw[i].step_cur  = ncur;
            scw[i].step_tick = 0u;
            /* 步号镜像 (1.0 起步) —— 写端屏蔽: 被强制的 wire 不改 */
            if (owire < MAX_WIRES) {
                uint32_t msk = *(const volatile uint32_t *)
                    (base + OFF_FORCE_MASK + ((uint32_t)owire >> 5) * 4u);
                if (!(msk & (1u << ((uint32_t)owire & 31u)))) {
                    wm[owire] = (float)(ncur + 1u);
                    wrote++;
                }
            }
        }
    }
    return wrote;
}

ATTR_ITCM uint32_t engine_tick(uint8_t *base, uint32_t tick, engine_scan_fn impl,
                               uint32_t *nrun_out)
{    const uint16_t *bkt  = (const uint16_t *)(base + OFF_ROUTE_BUCKETS);
    const uint16_t *off1 = bkt, *cnt1 = bkt + BUCKET_DIV1_PHASES;
    const uint16_t *off2 = bkt + 20, *cnt2 = bkt + 120;
    /* ★ 档级触发统计 (与 S3 同址 0x3854): 每档按"本拍执行的路由条次"累加 —— 见 engine.h。
     *   成本: 每拍最多 3 次 DTCM 读-改-写 (且只在桶非空时), 相对 40000 cyc 的拍长可忽略。 */
    uint32_t *ts = (uint32_t *)(void *)(base + OFF_TICK_STATS);

    uint32_t ph1 = tick % (uint32_t)BUCKET_DIV1_PHASES;
    uint32_t ph2 = tick % (uint32_t)BUCKET_DIV2_PHASES;
    uint32_t nrun = 0, ck = 0;

    uint32_t n0 = off1[0];                       /* div0 段: [0, n0) 每拍全跑 */
    if (n0) { ck ^= impl(base, 0, n0); nrun += n0; ts[0] += n0; }

    uint32_t b1 = off1[ph1], c1 = cnt1[ph1];     /* div1: 本拍 phase 桶 */
    if (c1) { ck ^= impl(base, b1, c1); nrun += c1; ts[1] += c1; }

    uint32_t b2 = off2[ph2], c2 = cnt2[ph2];     /* div2: 本拍 phase 桶 */
    if (c2) { ck ^= impl(base, b2, c2); nrun += c2; ts[2] += c2; }

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
    /* ★★ W1/W2 补漏 (实测 2026-09-11): 填表**不得改变运行态**。
     *
     * 缺口: cold_start_reset() 是"单一入口整段 memset" (S3 第二十六轮纪律), 它会把
     *   OFF_CTRL_ENGINE_RUN 一起清 0。上电路径没问题 (main 在 fill **之后**才置 1),
     *   但**运行期 reinit 路径** (主循环 g_reinit=1 → fill) 清完就没人置回来了 ⇒
     *   ISR 的双门 (gate && ENGINE_RUN) 恒不成立 ⇒ 引擎**永久停摆**,
     *   而所有"配置类"量 (表校验和/桶校验和/条数) 全部正常。
     *   ⇒ 症状与 A1 事故同族: "写过了就算" 的量全绿, 功能整体不可用。
     *
     * 为什么在 fill 里保存/恢复而不是改 cold_start_reset:
     *   ① cold_start_reset 的"整段清零"是**故意的** (NOLOAD 段上电内容不确定),
     *      让它按字段挑着清会给后来人一个"哪些字段不清"的清单要维护 —— 正是
     *      S3 第二十六轮收口要消灭的东西。
     *   ② "填表 = 只动配置域, 不动控制域" 才是 fill_tables 的正确语义, 这条
     *      边界本来就该在这里显式表达, 而不是靠调用方记得补一句。
     *   (换程序时**该不该**停引擎是调用方的事 —— deploy 与 reinit 各有各的语义,
     *    所以这里只负责"不擅自改变它"。) */
    uint8_t run_state = SHM_U8(base, OFF_CTRL_ENGINE_RUN);

    /* ★ 先走单一入口清零 (不允许这里自带 memset —— 见 cold_start_reset 注释)。
     *   约定: base 必须是 g_shm 本身 (表的唯一归属地是 .dtcm_shm)。
     *   若将来真需要第二块表区, 必须回 cold_start_reset 登记, 而不是绕过它。 */
    cold_start_reset();

    SHM_U8(base, OFF_CTRL_ENGINE_RUN) = run_state;   /* ★ 运行态原样恢复 */

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
            default:
                /* ★ profile >= 100 = 「单一原语」模式: op = profile - 100
                 *   用途: 逐原语测成本, 为 deploy 的预算模型建立**本平台自己的**
                 *   cost[] 表。
                 *   ★ 为什么不照抄 S3 的 op_cost[]: 那是 240MHz + ESP32 架构上的实测值
                 *     (DIRECT = 234 cyc/条), 而 H723 是 400MHz + ITCM (56 cyc/条)。
                 *     照抄过来就是"宣称≠实现"—— 预算门会用错的数去把关。 */
                op = (profile >= 100 && (profile - 100) <= OP_MAX)
                     ? (uint8_t)(profile - 100) : OP_DIRECT;
                break;
        }
        /* ---- 档位/相位分配 (★ 必须与工具里的 Python 预测逐字对应) ----
         * profile 3/4 = 三档混合: div = i % 3, 约 1/3 落在 div0/1/2;
         *   div1 phase = (i/3) % BUCKET_DIV1_PHASES
         *   div2 phase = (i/3) % BUCKET_DIV2_PHASES   (**100**, 见 engine.h:191) 
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
        /* ★ 双输入原语必须**同时**置 ROUTE_FLAG_WIRE2 —— 否则 ISR 的 wire2_valid()
         *   在 wire2_idx==0 时会判"无第二输入", 与下面赋的 wire2_idx 语义打架。
         *   (A3 的另一半: 光修 ISR 判据不够, 填表侧也要说清楚"我确实接了第二输入"。) */
        rt[i].flags        = (uint8_t)(ROUTE_FLAG_ACTIVE |
                                       (op_needs_wire2(op) ? ROUTE_FLAG_WIRE2 : 0u));
        rt[i].param_idx    = (uint16_t)(i % MAX_PARAMS);
        /* 有状态原语才挂 state 槽 (无状态原语挂 0 = 兜底槽, 与 S3 同语义)
         * ★ H11: 槽号必须**避开 0** —— 0 是"无槽"哨兵。旧写法 `i % MAX_STATES`
         *   在 i=0 时恰好得 0, 于是有状态原语会落到 s_state_fallback 兜底槽,
         *   与 S3 route_validate 的"有状态原语必须挂状态槽"不一致。
         *   (当前 profile 下第 0 条恰是 OP_DIRECT 所以没暴露, 属"靠巧合没事"。) */
        rt[i].state_offset = (uint16_t)(op_is_stateful_h(op) ? ((i % (MAX_STATES - 1)) + 1) : 0);
        rt[i].actuator_idx = 0;
        rt[i].wire2_idx    = (uint16_t)(op_needs_wire2(op) ? ((i + 7) % MAX_WIRES) : 0);
        rt[i].period       = (uint8_t)(dv | (uint8_t)(ph << PERIOD_PHASE_SHIFT));
        rt[i].reserved     = 0;
    }

    /* ★ 归组重排 + 生成桶索引 (幂等)。
     *   非三档 profile 也照跑 —— 结果是恒等变换(全 div0), 但保证桶表一定被建立,
     *   不会出现"某档忘了建桶 → 每拍空扫"的静默失效。 */
    engine_build_buckets(base, MAX_ROUTES);

    /* ★★ W2 补漏: 把 "装了几条" 落到 SHM —— 这条**原来缺失**。
     *   缺口后果 (实测 2026-09-11): profile 装载后 SHM 的 N_ROUTES 恒为 0, 而
     *   引擎实际在扫 128 条 (靠 C 全局 g_n_routes)。两处"条数"不一致导致:
     *     ① h_start_w1 的 F11 预算兜底被**静默跳过** (nr==0 → if(nr) 不成立)
     *        —— 也就是说 "毒药表必须拒 START" 这条 W1 判据**, 在 profile 装载路径下
     *        从未真正被走到过**。它在 deploy 路径下有效, 所以 S3/阶段 3 的测试
     *        全绿 —— 又一处"判据看起来在、其实没生效"。
     *     ② PC 侧按 N_ROUTES 判断"程序装了几条"会恒读 0。
     *   ⇒ 修法: 装载路径与 deploy 路径**都**维护这个量 (它们是同一个量的两个来源,
     *     不允许只有一个写)。 */
    SHM_U16(base, OFF_CTRL_N_ROUTES) = (uint16_t)MAX_ROUTES;
    SHM_U16(base, OFF_CTRL_N_PARAMS) = (uint16_t)MAX_PARAMS;
    SHM_U16(base, OFF_CTRL_N_STATES) = (uint16_t)MAX_STATES;
}

/* ══════════════════ W2: Force 辅助 (engine.h 声明) ══════════════════
 * ★ force_clear 的三处调用点 (与 S3 main.c:374/565/661 同语义):
 *     deploy(0x10) / RESET(0x13) / SEQ_DEPLOY(0x44)
 *   理由: force 是**调试态**, 不是组态。新程序 = 新语义, 旧的强制点位指向的
 *   wire 可能根本不存在于新程序里 —— 留着它会在下一拍把无关 wire 钉住,
 *   且 PC 完全看不到 (MASK 位还在, 但程序换了)。
 * ★ 清 VAL 残留是 S3 审计建议: 只清 MASK 的话, 下次"置位"前若忘了写 VAL
 *   (或写入失败), 会复用上一次的陈旧值 —— 一个只在特定时序下出现的幽灵。 */
void eng_force_clear(uint8_t *base)
{
    volatile uint32_t *fm = (volatile uint32_t *)(void *)(base + OFF_FORCE_MASK);
    volatile float    *fv = (volatile float    *)(void *)(base + OFF_FORCE_VAL);
    for (uint32_t i = 0; i < (uint32_t)FORCE_MASK_WORDS; i++) fm[i] = 0u;
    for (uint32_t i = 0; i < (uint32_t)MAX_WIRES; i++)       fv[i] = 0.0f;
    __asm__ volatile("dsb" ::: "memory");
}

/* ══════════════════════════════════════════════════════════════════
 * 阶段 3.2 — deploy 路径 (成本模型 → 校验 → staging → 热重载)
 * ══════════════════════════════════════════════════════════════════ */

/* ★★ 本平台实测的单条原语成本 (cycles/条, ITCM, 全表扫, 两点法斜率)
 * 来源: tools/h723_op_sweep.py — 19/19 原语数据可信, 每个原语的**表校验和都与
 * Python 独立预测逐位吻合** (证明"填进去的确实是这个原语, 不是在测别的东西"),
 * 两点法 (n=128 与 n=64) 除掉函数调用与 DWT 探针的常数项。
 * 实测 2026-09-10, 原始数据 build/op_cost.json。
 *
 * ★ 为什么不照抄 S3 的 op_cost[]: 那是 240MHz/ESP32 上的数, 这里逐步对照 ——
 *     DIRECT 234 → **50** (4.7× 快)      PID 337 → **140** (2.4× 快)
 *   加速比**不是常数**: PID 相对更贵 (浮点除法/饱和在 M7 上受益不如简单拷贝)。
 *   若把 S3 的表按 DIRECT 的 4.7× 比例整体缩放, PID 会被低估 ~1.9 倍 ——
 *   预算门就会放行会超载的程序。这是"平台换了数就要重测"的实证。
 *
 * ★★ 本表在 2026-09-10 晚**重测过一次** (数据 build/op_cost_after_fix.json):
 *   A3 修复把第二输入判据从"只查 wire2_idx"改成"查显式标志 || 非零索引"后,
 *   常见路径省掉一次 DTCM 加载与有限性检查 → 全部原语都变便宜约 10%
 *   (DIRECT 56→50, 全表 ITCM 7225→6476 cyc)。**成本表必须跟着代码走**:
 *   代码一动就要重测, 否则预算模型用的是"上一版代码"的数。 */
static const uint16_t k_op_cost_itcm[0x13] = {
    /* DIRECT CMP HYST CLAMP  LPF PID RATE DBN MUX EDGE LUT CNT TMR ARITH SCL AND OR NOT SR */
        56,  76,  79,  74,   97, 145,  68, 76, 70,  87, 89, 83, 80,  74, 67, 77, 78, 71, 85,
    /* ★★ 本次回填的由来 (2026-09-11 一次性审计, 轴 4「成本表受控对照」):
     *   审计先把**复核路径**修通了 —— `tools/h723_op_sweep.py` 的链是
     *   `reset → 注入 → 读` 而**整程没有 `go`**, 于是核一直停在 halt:
     *     · `main()` 从未执行 ⇒ 表根本没被填过;
     *     · 读到的是上一次会话的遗留 DTCM 值 ⇒ 19 个原语量到**完全相同**的 7292,
     *       cyc/条 = 0.00, 工具报 `0 / 19 原语数据可信`。
     *   (工具**拒绝认证**是对的 —— 比"报个 0"好得多。)
     *   修好时序 (补三段式: reset→go→sleep(BOOT)→halt→注入→go→sleep→halt→读) 后
     *   复测得 `19 / 19 原语数据可信`, 各原语值互不相同且量级合理 (PID 占拍 42%)。
     *
     *   ★ 新旧表之差 = **每条恒定 +5~6 cyc** (19 个原语的差值全部是 5 或 6,
     *     不是"某些原语变慢了") ⇒ 这是**扫描循环多了一项常量开销**的指纹。
     *     而本表上方的历史注释记载: 之前 A3 优化曾让"全部原语便宜约 10%
     *     (DIRECT 56→50)" —— **现测回到 56**, 即那条优化的效果在当前代码上不再成立。
     *   ★ 诚实标注: **造成这个常量项的具体改动尚未隔离** (本轮审计只做到"确认差异是
     *     常量项 + 回填正确值")。隔离方法: 用 `-D` 开关逐项排除 W2 Force / W3 顺序域 /
     *     W4 通信域 / W5 外设域 在**扫描宏内**加的东西, 用同一套两点法对照。
     *   ⇒ 在那之前, 本表取**当前实测值** —— 预算门是"安全"方向 (偏大 = 偏保守),
     *     且"表必须等于当前代码的实测"是项目铁律 2。 */
};

/* 源类型附加成本: 本平台 4 种源都是"取址 + 一次掩码", 实测差异落在噪声内 (<2 cyc),
 * 故为 0。S3 给 SRC_HMI 记了 +60 (它要 volatile u16 读 + 整数转浮点 + 浮点除),
 * 但 H723 上 SRC_HMI **根本不该被放行** —— 引擎侧它是留位返回 0, 放行等于静默
 * 送一个恒 0 的假信号。所以这里不给成本, 而是在 engine_route_validate 里直接拒。 */
static const uint16_t k_src_cost[4] = { 0, 0, 0, 0 };

uint16_t engine_op_cost(uint8_t op)
{
    /* 越界/未知 → 取实测最贵值的 2 倍作保守兜底: 宁可拦错, 不可放错 */
    return (op <= OP_MAX) ? k_op_cost_itcm[op] : (uint16_t)(k_op_cost_itcm[OP_PID] * 2u);
}

uint32_t engine_prog_budget(const uint8_t *payload, uint16_t nr)
{
    uint32_t per = 0;
    for (uint16_t i = 0; i < nr; i++) {
        RouteEntry_t r;
        memcpy(&r, payload + (size_t)i * 16u, 16u);
        if (!(r.flags & ROUTE_FLAG_ACTIVE)) continue;
        uint8_t dv = r.period & PERIOD_DIV_MASK;
        uint32_t mult = (dv == PERIOD_DIV_IDX_MID)  ? OP_COST_DIV1
                      : (dv == PERIOD_DIV_IDX_SLOW) ? OP_COST_DIV2 : OP_COST_DIV0;
        uint32_t s = (r.src_type < 4u) ? k_src_cost[r.src_type] : SRC_COST_FALLBACK;
        /* 向上取整: 慢档每条每拍至少也要摊 1 cyc (不能因为除法取整把成本算没了) */
        per += ((uint32_t)engine_op_cost(r.op) + s + mult - 1u) / mult;
    }
    return per;
}

/* ★ op_is_stateful_h() 已移到 engine.h —— 表填充与 deploy 校验必须共用同一份清单,
 *   两处各写一份就是"改一处忘另一处"的温床。 */

const char *engine_route_validate(const RouteEntry_t *r)
{
    if (r->param_idx    >= MAX_PARAMS) return "param_idx out of range";
    if (r->state_offset >= MAX_STATES) return "state_offset out of range";
    if (r->state_offset == 0 && op_is_stateful_h(r->op))
        return "stateful op needs state_offset";
    if (r->dst_channel  >= MAX_WIRES)  return "dst_channel out of range";
    if (r->wire2_idx    >= MAX_WIRES)  return "wire2_idx out of range";
    /* ★★ R1 子项 ④ (2026-09-11 迁移保真度审查): actuator_idx 的边界。
     *   范本在这里有两条校验 (`actuator_idx >= 32` 拒绝 / 受保护引脚拒绝), 迁移时按
     *   "本平台没有 GPIO 执行器"整段略掉了 —— 于是越界索引会被**配置接受、物理无输出**:
     *   ISR 侧是 `if (ai && ai < MAX_ACTUATORS) ac[ai] = res;` ⇒ >=64 **静默丢弃**。
     *   ★ 为什么**不能照搬范本的 32**: 范本是**单端口 u32 位图**(位 = 引脚)所以上界 32;
     *     H723 **没有 GPIO 执行器面**, `actuator_idx` 的语义是
     *     **SHM 浮点槽索引** (`ACTUATOR_STATUS[0..63]`, 见 engine.h 的字段说明),
     *     上界是 `MAX_ACTUATORS` = 64。照搬 32 会把**合法的 32..63 槽一起误杀**。
     *   ★ `0` 的含义是"本路由不驱动执行器" (与 ISR 的 `if (ai && ...)` 一致)
     *     ⇒ 只拦 `>= MAX_ACTUATORS`, 不拦 0。
     *   ★ 判据 (tools/h723_r1_actuator.py): 64 → NAK "actuator_idx out of range";
     *     63 → ACK (阳性对照, 证明这条不是"一律拒绝")。 */
    if (r->actuator_idx >= MAX_ACTUATORS) return "actuator_idx out of range";
    if ((r->period & PERIOD_DIV_MASK) > PERIOD_DIV_IDX_SLOW) return "bad div";

    switch (r->src_type) {
        case SRC_SENSOR: if (r->src_index >= MAX_SENSORS) return "src_index(sensor) out of range"; break;
        case SRC_WIRE:   if (r->src_index >= MAX_WIRES)   return "src_index(wire) out of range";   break;
        case SRC_CONST:  if (r->src_index >= MAX_PARAMS)  return "src_index(const) out of range";  break;
        /* ★★ H5 未决项的正面处理: 引擎侧 read_source 对 SRC_HMI 是**留位返回 0**。
         *   若放行, 上位机会得到一个"恒 0 的合法信号"—— 程序能跑、不报错、结果全错。
         *   显式拒绝才是正确行为: 未实现的能力必须在**下载期**失败, 而不是运行时静默。 */
        case SRC_HMI:    return "SRC_HMI not implemented on H723";
        default:         return "bad src_type";
    }
    if (r->dst_type != DST_WIRE) return "bad dst_type";

    switch (r->op) {
        case OP_DIRECT: case OP_CMP: case OP_HYST: case OP_CLAMP: case OP_LPF:
        case OP_PID: case OP_RATE: case OP_DEADBAND: case OP_MUX: case OP_EDGE:
        case OP_LUT: case OP_CNT: case OP_TIMER: case OP_ARITH: case OP_SCALE:
        case OP_AND: case OP_OR: case OP_NOT: case OP_SR: break;
        default: return "bad op";
    }
    /* 双输入原语 (AND/OR/ARITH/SR/CNT): 第二输入必须有效, 否则 wb 恒 0 →
       恒假/恒真、src+0、永不复位、只加不减 等**静默语义错误** (S3 M1 实证)。
       ★ A3: 原实现只拦 AND/OR, 漏了同样消费 wb 的 ARITH/SR/CNT (S3 也漏了)。
       ★ 判据统一走 wire2_valid() —— 与 ISR 用的是同一个函数, 不可能再"两处不一致"。 */
    if (op_needs_wire2(r->op) && !wire2_valid(r->flags, r->wire2_idx))
        return "this op needs wire2 source (set ROUTE_FLAG_WIRE2 or wire2_idx!=0)";
    return NULL;
}

uint16_t engine_stage_program(uint8_t *base, const uint8_t *payload,
                              uint16_t nr, uint16_t np, uint16_t ns)
{
    uint8_t  *dst  = base + OFF_ROUTE_STAGING;
    uint16_t *bkt  = (uint16_t *)(void *)(base + OFF_ROUTE_BUCKETS_ST);
    uint16_t *off1 = bkt,      *cnt1 = bkt + 10;
    uint16_t *off2 = bkt + 20, *cnt2 = bkt + 120;

    /* 桶表先清零: 保证 64..99 槽恒 0 (H9 的判据), 且重填不同程序时不残留旧桶 */
    memset(bkt, 0, (size_t)ROUTE_BUCKET_U16 * 2u);
    memset(dst, 0, (size_t)MAX_ROUTES * 16u);

    uint16_t n0 = 0, n1 = 0, n2 = 0;
    for (uint16_t i = 0; i < nr; i++) {
        RouteEntry_t r;
        memcpy(&r, payload + (size_t)i * 16u, 16u);
        if (!(r.flags & ROUTE_FLAG_ACTIVE)) continue;
        uint8_t dv = r.period & PERIOD_DIV_MASK;
        if      (dv == PERIOD_DIV_IDX_FAST) n0++;
        else if (dv == PERIOD_DIV_IDX_MID)  n1++;
        else if (dv == PERIOD_DIV_IDX_SLOW) n2++;
    }

    {   /* 数每相位桶条数: 档内到达序 % 相位数 (与 S3 同语义: "同档错相") */
        uint16_t s1 = 0, s2 = 0;
        for (uint16_t i = 0; i < nr; i++) {
            RouteEntry_t r;
            memcpy(&r, payload + (size_t)i * 16u, 16u);
            if (!(r.flags & ROUTE_FLAG_ACTIVE)) continue;
            uint8_t dv = r.period & PERIOD_DIV_MASK;
            if      (dv == PERIOD_DIV_IDX_MID)  cnt1[s1++ % BUCKET_DIV1_PHASES]++;
            else if (dv == PERIOD_DIV_IDX_SLOW) cnt2[s2++ % BUCKET_DIV2_PHASES]++;
        }
    }

    /* 前缀和定桶起点: div0 段在最前 (每拍全跑), 故 off1[0] 从 n0 开始 */
    uint16_t acc = n0;
    for (int p = 0; p < BUCKET_DIV1_PHASES; p++) { off1[p] = acc; acc = (uint16_t)(acc + cnt1[p]); }
    uint16_t acc2 = (uint16_t)(n0 + n1);
    for (int p = 0; p < BUCKET_DIV2_PHASES; p++) { off2[p] = acc2; acc2 = (uint16_t)(acc2 + cnt2[p]); }

    uint16_t cur1[BUCKET_DIV1_PHASES], cur2[BUCKET_DIV2_PHASES];
    for (int p = 0; p < BUCKET_DIV1_PHASES; p++) cur1[p] = off1[p];
    for (int p = 0; p < BUCKET_DIV2_PHASES; p++) cur2[p] = off2[p];

    uint16_t cur0 = 0, q1 = 0, q2 = 0;
    for (uint16_t i = 0; i < nr; i++) {
        RouteEntry_t r;
        memcpy(&r, payload + (size_t)i * 16u, 16u);
        if (!(r.flags & ROUTE_FLAG_ACTIVE)) continue;
        uint8_t dv = r.period & PERIOD_DIV_MASK, ph = 0;
        uint16_t slot = 0;
        if      (dv == PERIOD_DIV_IDX_FAST) { slot = cur0++; }
        else if (dv == PERIOD_DIV_IDX_MID)  { ph = (uint8_t)(q1++ % BUCKET_DIV1_PHASES); slot = cur1[ph]++; }
        else                                { ph = (uint8_t)(q2++ % BUCKET_DIV2_PHASES); slot = cur2[ph]++; }
        r.period   = (uint8_t)(dv | (uint8_t)(ph << PERIOD_PHASE_SHIFT));
        r.reserved = 0;                       /* 显式写 0: 填充字节不参与语义, 但参与逐字节校验和 */
        memcpy(dst + (size_t)slot * 16u, &r, 16u);
    }

    if (np) memcpy(base + OFF_PARAM_STAGING, payload + (size_t)nr * 16u,        (size_t)np * 16u);
    if (ns) memcpy(base + OFF_STATE_STAGING, payload + (size_t)(nr + np) * 16u, (size_t)ns * 16u);

    uint16_t nw = (uint16_t)(n0 + n1 + n2);
    SHM_U16(base, OFF_CTRL_N_ROUTES) = nw;
    SHM_U16(base, OFF_CTRL_N_PARAMS) = np;
    SHM_U16(base, OFF_CTRL_N_STATES) = ns;
    return nw;
}

/* 字拷贝 / 字清零 —— **刻意不用 memcpy/memset**
 *
 * ★★ 这是被测出来的教训 (2026-09-10, deploy 自检第一版):
 *   第一版直接调 memcpy/memset (newlib nano), 实测热重载 **71241 cyc = 178 μs**,
 *   比 100 μs 的拍还长 → 那一次拍周期被拉到 196 μs, 而且下次定时器事件已经到期,
 *   返回后立刻又进一次 ISR (实测拍周期 min 掉到 1360 cyc = 3.4 μs)。
 *   折算下来 memcpy 约 **14.8 cyc/字节** —— 原因是它按字节拷 + 从 flash 取指
 *   (I-cache 复位默认关闭)。
 *   换成 ITCM 里的字拷贝循环后, 同样的数据量只要 ~1/30 的时间 (见 g_reload_cyc)。
 *   ⇒ 结论: **热路径上的内存搬运不能交给 libc**, 尤其是"默认关 cache"的 H7。 */
static inline void itcm_wcopy(uint32_t *d, const uint32_t *s, uint32_t words)
{
    volatile uint32_t *dv = d;
    const volatile uint32_t *sv = s;
    for (uint32_t i = 0; i < words; i++) dv[i] = sv[i];
}

static inline void itcm_wzero(uint32_t *d, uint32_t words)
{
    volatile uint32_t *dv = d;
    for (uint32_t i = 0; i < words; i++) dv[i] = 0u;
}

/* ★ 放 ITCM: 这是"部署瞬间"最热的一段代码, 且它的时长直接决定那一拍会不会超拍长 */
__attribute__((section(".itcm_text"), noinline, used))
void engine_reload_active(uint8_t *base)
{
    uint16_t nr = SHM_U16(base, OFF_CTRL_N_ROUTES);
    uint16_t np = SHM_U16(base, OFF_CTRL_N_PARAMS);
    uint16_t ns = SHM_U16(base, OFF_CTRL_N_STATES);
    if (nr > MAX_ROUTES) nr = MAX_ROUTES;
    if (np > MAX_PARAMS) np = MAX_PARAMS;
    if (ns > MAX_STATES) ns = MAX_STATES;

    /* 顺序: 路由 → 桶 → 参数 → 状态。桶必须与路由**同一拍**切换, 否则 ISR
     * 会用旧桶扫新表 → 前后半张表错乱 (S3 OA15 的连带缺陷)。 */
    if (nr) itcm_wcopy((uint32_t *)(void *)(base + OFF_ROUTE_TABLE),
                       (const uint32_t *)(const void *)(base + OFF_ROUTE_STAGING),
                       (uint32_t)nr * 4u);
    itcm_wcopy((uint32_t *)(void *)(base + OFF_ROUTE_BUCKETS),
               (const uint32_t *)(const void *)(base + OFF_ROUTE_BUCKETS_ST),
               (uint32_t)ROUTE_BUCKET_U16 / 2u);
    if (np) itcm_wcopy((uint32_t *)(void *)(base + OFF_PARAM_TABLE),
                       (const uint32_t *)(const void *)(base + OFF_PARAM_STAGING),
                       (uint32_t)np * 4u);
    /* M2: 状态表先全清再拷 — 新程序绝不继承旧程序的运行状态
     * (饱和积分残留 → 上电/换程序瞬间的满功率冲击) */
    itcm_wzero((uint32_t *)(void *)(base + OFF_STATE_TABLE), (uint32_t)MAX_STATES * 4u);
    if (ns) itcm_wcopy((uint32_t *)(void *)(base + OFF_STATE_TABLE),
                       (const uint32_t *)(const void *)(base + OFF_STATE_STAGING),
                       (uint32_t)ns * 4u);

    SHM_U32(base, OFF_CTRL_PROG_MAGIC) = 0x44434C31u;   /* 'DCL1' */
    /* ★ 生效确认: 把"已切换"这件事写成可观测的量 (S3 的 ACK≠已生效 语义债) */
    SHM_U16(base, OFF_CTRL_APPLIED_SEQ) = SHM_U16(base, OFF_CTRL_DEPLOY_SEQ);
}

/* ══════════════════ W1: SHM 读写命令的地址守卫 ══════════════════
 * 结构与 S3 `main.c:83-140` 同构, 但地址表**必须重写** —— 见 engine.h 的说明。 */

/* ── H723 外设可见区白名单 ──
 * 只放行"读了有意义、写了不会砖"的区。★ 三个禁区必须显式排除:
 *   RCC(0x58024400) / PWR(0x58024800) / FLASH(0x52002000) */
#define ENG_SHMF  (0x40000000u)   /* APB1 起始 (TIM2 0x40000000 / USART2 0x40004400...) */
#define ENG_SHMFE (0x40025000u)   /* APB2 结束 (USART1 0x40011000 / SPI1 0x40013000) */
#define ENG_GPIOF (0x58020000u)   /* GPIOA 起始 */
#define ENG_GPIOFE (0x58022000u)  /* GPIOK 结束 (0x58020000 + 0x400*11) */

/* ══════════ ★★ AXI 诊断窗 —— **只读**扩展 (2026-09-13) ══════════
 * 起因 (实测): `SD_DIAG(0x24000200)` / `BB_DIAG(0x24000300)` / `SD_CFG(0x24000400)` /
 *   `BOOT_AXI(0x24000500)` 全在 **AXI**, 而协议的读路径只放行 SHM / APB 外设 / GPIO
 *   ⇒ **这些诊断区只能用调试器(halt + pyocd)看** —— 这本身就违反"非侵入式交互"(铁律 0):
 *     观测不得改变被测对象, 而调试器会停引擎。
 *   ★ 而且"看门狗复位原因"正好在 AXI (BOOT_AXI[2]=RCC_RSR / [3]=RCC_BDCR) ——
 *     ⇒ **这条只读窗是看门狗功能的前置**: 没有它, 复位之后只能靠调试器读原因。
 * ★★ 为什么必须做成"只读"而不是直接放宽 eng_valid_range:
 *   写路径 (0x21 WRITE / 0x23 WRITE_BURST) 复用的是 range 守卫 —— 一并放宽就等于
 *   **一条协议帧能改 SD_CFG(调试钩子) 或踩坏黑匣子 AXI 环**。读只读, 写不碰。
 *   窗口上界 0x24000600 = BOOT_AXI(0x24000500)+24B 之后留余量;
 *   ★ 此窗内含 SD_CFG —— 允许读(看得到钩子现值), 但写仍被 eng_valid_addr 拒。 */
#define ENG_AXIDIAGF  0x24000000u
#define ENG_AXIDIAGFE 0x24000600u

/* RCC / PWR / FLASH 各自 1KB 的禁区, valid_addr 里逐个排除 */
#define ENG_IS_FORBIDDEN(a) \
    (((a) >= RCC_BASE   && (a) < RCC_BASE   + 0x400u) || \
     ((a) >= PWR_BASE   && (a) < PWR_BASE   + 0x400u) || \
     ((a) >= FLASH_BASE && (a) < FLASH_BASE + 0x400u))

int eng_valid_addr(uint32_t a)
{
    /* 非对齐访问在 M7 上会触发 UsageFault (S3 的 LX7 是静默错位数据) —— 两种都该拒,
     * 但 H723 上不拒的后果更硬: 直接 HardFault 把协议打挂。 */
    if (a & 3u) return 0;
    if (a >= (uint32_t)(uintptr_t)g_shm && a < (uint32_t)(uintptr_t)g_shm + SHM_SIZE) return 1;
    if (ENG_IS_FORBIDDEN(a)) return 0;
    if (a >= ENG_SHMF  && a < ENG_SHMFE)  return 1;   /* APB1/APB2 外设 */
    if (a >= ENG_GPIOF && a < ENG_GPIOFE) return 1;   /* GPIOA..K */
    return 0;
}

int eng_valid_range(uint32_t a, uint32_t bytes)
{
    if (bytes == 0) return 0;
    if (a & 3u) return 0;      /* burst 基址须 4 对齐 (步进 4B) */
    uint32_t e = a + bytes;    /* ★ 溢出检查: a+bytes 回绕会让下面的比较全部通过 */
    if (e < a) return 0;
    if (a >= (uint32_t)(uintptr_t)g_shm && e <= (uint32_t)(uintptr_t)g_shm + SHM_SIZE) return 1;
    /* 外设区: 起止都落在同区, 且区间**不与禁区相交**。
     * ★ 不能用"大区包住禁区就算过"—— 那样 [RCC-4, RCC+4) 这种跨禁区的 burst 会被放行,
     *   而这正是最危险的一种越界 (读会出错值, 写会改时钟)。 */
    if (a >= ENG_SHMF && e <= ENG_SHMFE) {
        if (e <= RCC_BASE   || a >= RCC_BASE   + 0x400u) {
        if (e <= PWR_BASE   || a >= PWR_BASE   + 0x400u) {
        if (e <= FLASH_BASE || a >= FLASH_BASE + 0x400u) {
            return 1;
        }}}
        return 0;
    }
    if (a >= ENG_GPIOF && e <= ENG_GPIOFE) return 1;
    return 0;
}

/* ---- ★ 只读版守卫: 在 range 之上再放行 AXI 诊断窗 (见上面 ENG_AXIDIAGF 的注释) ----
 * 只给 **0x20/0x22 (读)** 用; 0x21/0x23 (写) 仍走 eng_valid_addr/eng_valid_range。 */
int eng_valid_rrange(uint32_t a, uint32_t bytes)
{
    if (eng_valid_range(a, bytes)) return 1;
    if (bytes == 0u) return 0;
    if (a & 3u) return 0;
    {   uint32_t e = a + bytes;
        if (e < a) return 0;                     /* 回绕 */
        if (a >= ENG_AXIDIAGF && e <= ENG_AXIDIAGFE) return 1;
    }
    return 0;
}

int eng_valid_raddr(uint32_t a) { return eng_valid_rrange(a, 4u); }

/* SHM 内哪些区是 float 数据区 (接受 NaN/Inf 会让 LUT/MUX/EDGE 的 float→int
 * 转换触发 C 未定义行为 → 静默越界读, S3 AUDIT P1b)。外设寄存器无浮点语义, 不拦。 */
int eng_shm_off_is_float(uint32_t off)
{
    if (off >= OFF_SENSOR_MAP      && off < OFF_ACTUATOR_STATUS) return 1;
    if (off >= OFF_ACTUATOR_STATUS && off < OFF_WIRE_MAP)        return 1;
    if (off >= OFF_WIRE_MAP        && off < OFF_LUT_DATA)        return 1;
    if (off >= OFF_LUT_DATA        && off < OFF_ROUTE_TABLE)     return 1;
    if (off >= OFF_PARAM_TABLE     && off < OFF_PARAM_STAGING)   return 1;
    if (off >= OFF_PARAM_STAGING   && off < OFF_STATE_TABLE)     return 1;
    if (off >= OFF_STATE_TABLE     && off < OFF_STATE_STAGING)   return 1;
    /* ★ W2.1: FORCE_VAL 是浮点区 —— 强制值必须过有限性闸。
     *   若放行 NaN/Inf, 拍首覆写会把坏值直接钉进 WIRE_MAP, 且**每拍重钉**
     *   (与一次性写入不同, 它无法被后续计算自愈)。 */
    if (off >= OFF_FORCE_VAL       && off < OFF_RSVD_EXEC_TAIL)  return 1;
    /* FORCE_MASK 是位图, 不是浮点 (放行任意 32 位模式) —— 刻意不列入。
     * ★ 状态 staging 到路由桶之间是保留洞 (未定义的域) —— 不列入 float 区,
     *   写进去既不拦 NaN 也不报错, 与 S3 的"洞外一律放行"行为一致。 */
    return 0;
}

/* float 位模式有限性: 定义已移到 engine.h (0x21 写守卫 / 0x24 强制守卫 / ISR
 * 三处必须共用同一判据, 各写一份就是"改一处忘一处"的温床)。 */

int eng_write_allowed(uint32_t a, uint32_t v)
{
    uint32_t base = (uint32_t)(uintptr_t)g_shm;
    if (a < base || (a & 3u)) return 1;          /* SHM 外/非对齐: valid_addr 已挡 */
    uint32_t o = a - base;
    if (o >= SHM_SIZE) return 1;
    return eng_shm_off_is_float(o) ? is_finite_bits(v) : 1;
}

/* ══════════ 物理输出面注册表 (见 engine.h 的说明) ══════════
 * 一个极小的函数指针数组: 各域 init 后把自己的"安全态"挂上来。
 * ★ 为什么不用"在 eng_outputs_safe 里直接调 hil_outputs_safe()":
 *   那样每次新增输出面都要回来改 core 文件 ⇒ 又变成"改一处忘一处"。
 *   注册表把"新增域必须登记"变成**调用方的一次显式动作**, 而登记数可被外部读走。
 * ★ 容量 4: 当前只有 HIL 一个面 (GPIO 执行器面在 H723 尚未实现)。满了就静默丢弃
 *   —— 不静默: 由 `g_safe_surfaces_reg` 与 0x38 byte38 暴露实际登记数, 判据可查。 */
static void (*s_out_safe[ENG_MAX_OUT_SURFACES])(void);
static uint32_t s_out_safe_n = 0;

/* 观测: 上一次安全态**实际执行**的面数 (与登记数对照 ⇒ 能区分"没登记"与"登记了没跑") */
volatile uint32_t g_safe_surfaces_ran = 0;

void eng_register_output_surface(void (*fn)(void))
{
    if (!fn || s_out_safe_n >= (uint32_t)ENG_MAX_OUT_SURFACES) return;
    s_out_safe[s_out_safe_n++] = fn;
}

uint32_t eng_output_surface_count(void) { return s_out_safe_n; }

/* 输出安全态: 停机 ≠ 输出保持最后一拍 —— 工业语义"停机 = 进安全态"。
 * ★ H723 没有 GPIO_OUT_W1TC (S3 的"只清不置"), 等价物 = BSRR 高 16 位。
 *   掩码外的引脚完全不受影响 (这正是 S3 用 W1TC 的用意: 别碰没被引擎管的脚)。 */
/* ★★ 2026-09-13: 加 ATTR_ITCM —— 闸门证明它"从 ISR 可达却落在 FLASH"(0x08004930)。
 *   它经**函数指针注册表**被调用 (eng_register_output_surface), 而函数指针调用
 *   **静态解析不出目标** ⇒ 正是 itcm.h 说的"必须显式保证的那一类"。 */
ATTR_ITCM void eng_outputs_safe(void)
{
    uint32_t mask = SHM_U32(g_shm, OFF_CTRL_GPIO_MASK);
    /* ★★ 审计发现 H (2026-09-11) 的处置 —— 这里原本是:
     *      for (p = 0..11) { m = (mask >> (p*2)) & 0xFFFF; if (m) GPIO_BSRR(p) = m<<16; }
     *   两个问题:
     *     ① **位映射语义错**: 每 port 只从 mask 取 **2 位**, 而每个 GPIO port 有
     *        **16 个引脚**。要清哪些引脚应该是 16 位/port, 现在等于说"每个端口只有
     *        2 个可寻址引脚" —— 对不上。
     *     ② 根因是 **u32 装不下**: 掩码需要覆盖 GPIOA..GPIOK 共 11 port × 16 pin
     *        = **176 位**。S3 是单端口 u32 (位=引脚), 直搬到 H723 就不成立了。
     *   ★★ 2026-09-16 更正: **已经不再"不可达"** —— `src/step.c:46` 会写
     *      `OFF_CTRL_GPIO_MASK = STEP_DO_MASK (0x0F00)`（步进初始化时登记 PE8~PE11）。
     *      原写"没有任何代码写它(恒 0)"在写下时是实情, 现已过期。
     *      ⇒ 本段循环在步进启用后**会**执行; 要判"它到底跑没跑过"必须看实测计数,
     *        不能沿用这句旧注释（否则又是一个"空判据"式的假结论）。
     *   ★ 处置原则 (本项目"宣称必须等于实现"): **宁可不做, 不做错的**。
     *     保留一个已知错误的位映射, 比什么都不做更危险 —— 一旦将来有人往
     *     GPIO_MASK 写值, 它会**去清错误的引脚** (而 P1-2 的本意恰恰是"停机时
     *     输出归零", 清错引脚 = 把安全功能变成事故源)。
     *   ⇒ 改为: 只做**观测**, 不执行清位 —— 直到语义定案。
     *
     * ★★ 定案 (2026-09-12): **② 单端口, 端口 = GPIOE** (`mask < (1u<<16)`)。
     *   完整理由见 engine.h 该字段说明 (GPIOE 未被使用 / 与"DMA2 一次搬一个 ODR"
     *   天然同构 / 零 SHM 偏移代价)。
     *   ⇒ 判据随之**分裂成两个**, 见下方与 `g_safe_mask_oob` 的说明:
     *       合法非 0 = 有人正常用 (定案后是预期行为, 不再是缺陷信号)
     *       高 16 位非 0 = 越界写入 (才是真正必须查的 bug)
     *   ★ 真正的清位动作**仍留到 P3** (输出面接上 DMA 锁存链时) —— 本处仍不执行:
     *     现在没有任何东西**登记**为 GPIOE 的输出面, 提前清位等于宣称一个尚不存在的
     *     作用域 (与上面"宁可不做, 不做错的"是同一条纪律)。 */
    if (mask) {
        if (mask & 0xFFFF0000u) g_safe_mask_oob++;      /* 越界: 语义违规 (独立计数) */
        else                    g_safe_mask_nonzero++;  /* 合法使用 (定案后为预期) */
    }
    /* 执行器状态数组归零 (S3: memset ACTUATOR_STATUS) —— 这一半是**有效**的 */
    volatile uint32_t *act = (volatile uint32_t *)(void *)(g_shm + OFF_ACTUATOR_STATUS);
    for (uint32_t i = 0; i < (uint32_t)MAX_ACTUATORS; i++) act[i] = 0u;
    /* ★★ 覆盖**全部已注册的物理输出面** (审计 #1 的结构性对策, 见 engine.h)。
     *   放在 ACTUATOR 数组归零**之后**: 数组是内部镜像, 物理面才是对外效果 ——
     *   "停机后执行器不再动作"这句话的判据必须在物理面上量, 不是在镜像上量。
     *   (顺序不影响正确性, 但影响可读性: 先内部后外部, 一眼看出覆盖顺序。) */
    for (uint32_t i = 0; i < s_out_safe_n; i++) s_out_safe[i]();
    g_safe_surfaces_ran = s_out_safe_n;
    __asm__ volatile("dsb" ::: "memory");
}
