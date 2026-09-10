/**
 * engine.h — DCL 引擎表结构与 SHM 布局 (H723 阶段 2)
 *
 * ★ 本文件是 esp32-core0 `components/core0/shared_mem.h` 的**同构子集**:
 *   容量宏、SHM 偏移、表条目结构、OP/SRC 码, 全部逐字节对齐。
 *   为什么: 迁移不变量 (MIGRATE-H723.md §1) —— 表布局与 SHM 偏移一改,
 *   S3 的 20 套回归、4 份审计记录、PC 侧按偏移读的脚本全部失效。
 *
 * 与 S3 的差异 (有意为之, 逐条说明):
 *   ① SHM 基址从"运行时 heap_caps 分配"变成**链接期 DTCM 静态区** (零等待无 cache)
 *   ② ISR 代码从 IRAM_ATTR 变成 .itcm_text 段
 *   ③ HMI 源 (SRC_HMI, 通信域写区) 本阶段未落地 — 通信域属阶段 4, 这里留位不实现
 */
#ifndef DCL_ENGINE_H
#define DCL_ENGINE_H

#include <stdint.h>
#include <stdbool.h>

/* ══════════ 容量 (与 esp32-core0 一致) ══════════ */
#define MAX_ROUTES    128
#define MAX_PARAMS    128
#define MAX_STATES    128
#define MAX_SENSORS   64
#define MAX_ACTUATORS 64
#define MAX_WIRES     128
#define MAX_LUT       256
#define MB_NREG       64    /* 通信域每区寄存器数 (阶段 2 只用于 SRC_HMI 的留位) */

/* ══════════ SHM 偏移 (逐字节对齐 esp32-core0 shared_mem.h) ══════════
 *
 * ★ 审计修正 (对照 esp32-core0 第二十七轮 OA20 同族):
 *   第一版这里**只**从 OFF_SENSOR_MAP(0x40) 开始定义, 0x00-0x3F 是一段
 *   "无名字、无断言"的 64 字节空洞。S3 的 OA20 正是这么来的 —— MB 区
 *   当时**一条断言都没有**, 于是控制块从 32B 扩到 40B 踩进 RX 缓冲无人发觉。
 *   现在把控制块区/计时区按 S3 的原偏移**显式命名并断言**, 空洞消失。
 *
 *   控制块区 (0x00-0x3F) 本阶段**只占位不实现** (deploy/热重载属阶段 3),
 *   但偏移先按不变量钉死 —— 偏移一改, S3 的 20 套回归与 14 章审计记录全失效。 */
/* ---- SHM 定址助手 (与 S3 同名同语义, 迁移期少一次心智换算) ----
 * ★ 一律带 base 参数: 本项目所有引擎函数都以 base 为唯一入口 (不用隐藏的 g_shm),
 *   这样"表在哪"永远由调用方显式给出, 便于将来第二块表区/自检。 */
#define SHM_U8(b, off)   (*(volatile uint8_t  *)((b) + (off)))
#define SHM_U16(b, off)  (*(volatile uint16_t *)((b) + (off)))
#define SHM_U32(b, off)  (*(volatile uint32_t *)((b) + (off)))
#define SHM_PTR(b, off)  (void *)((b) + (off))

#define OFF_CTRL_MAGIC       0x00   /* u32 */
#define OFF_CTRL_VERSION     0x04   /* u32 */
#define OFF_CTRL_HEARTBEAT   0x08   /* u32 */
#define OFF_CTRL_RELOAD      0x0C   /* u8  */
#define OFF_CTRL_ENGINE_RUN  0x0D   /* u8  */
#define OFF_CTRL_N_ROUTES    0x0E   /* u16 */
#define OFF_CTRL_N_PARAMS    0x10   /* u16 */
#define OFF_CTRL_N_STATES    0x12   /* u16 */
#define OFF_CTRL_PROG_MAGIC  0x14   /* u32 */
#define OFF_TIMING_SAMPLES      0x18   /* u32 */
#define OFF_TIMING_PERIOD_MIN   0x1C   /* u32 */
#define OFF_TIMING_PERIOD_MAX   0x20   /* u32 */
#define OFF_TIMING_EXEC_MIN     0x24   /* u32 */
#define OFF_TIMING_EXEC_MAX     0x28   /* u32 */
#define OFF_TIMING_LAST_PERIOD  0x2C   /* u32 */
#define OFF_TIMING_LAST_EXEC    0x30   /* u32 */
#define OFF_CTRL_GPIO_MASK      0x34   /* u32 */
#define OFF_CTRL_N_SEQ          0x38   /* u8  Sequencer 实例数 (阶段 5) */
/* 0x39 空闲 (1 字节); 0x3A-0x3F 已被下面的 deploy 生效确认字段占满 ——
 * ★ A8 修正: 旧注释写"0x38-0x3F 空闲, 阶段 5 Sequencer", 与事实不符
 *   (0x3A/0x3C/0x3E 已是 DEPLOY_SEQ/APPLIED_SEQ/APPLIED_LAT)。
 *   阶段 5 落地 Sequencer 时应使用 0x39 与下面的保留区, 不要假设 0x3A-0x3F 可用。 */

#define OFF_SENSOR_MAP       0x0040   /* 64 × f32 */
#define OFF_ACTUATOR_STATUS  0x0140   /* 64 × f32 */
#define OFF_WIRE_MAP         0x0240   /* 128 × f32 */
#define OFF_LUT_DATA         0x0440   /* 256 × f32 */
#define OFF_ROUTE_TABLE      0x0840   /* 128 × 16B (ISR 只读) */
#define OFF_ROUTE_STAGING    0x1040   /* 128 × 16B (PC 写, ISR 热重载 memcpy) */
#define OFF_PARAM_TABLE      0x1840   /* 128 × 16B */
#define OFF_PARAM_STAGING    0x2040   /* 128 × 16B */
#define OFF_STATE_TABLE      0x2840   /* 128 × 16B */
#define OFF_STATE_STAGING    0x3040   /* 128 × 16B */
/* ---- 档桶索引区 (阶段 3, S3 OA15 治本; 起址与 S3 一致) ----
 * 路由表按 (div, phase) 归组: div0 全部在前, 其后 div1 (phase 0..9 各成桶),
 * 再其后 div2 (phase 0..63 各成桶)。桶表记录每桶的 [起始, 条数], 使 ISR 每拍
 * 只扫本拍激活的桶而非全表:
 *   [off1[10]][cnt1[10]][off2[100]][cnt2[100]] = 220 × u16 = 440B
 *   div0 段 = [0, off1[0]) —— off1[0] 即 div0 条数 (每拍全跑);
 *   div1 段 = [off1[m10], off1[m10]+cnt1[m10])      (m10  = tick%10)
 *   div2 段 = [off2[m100], off2[m100]+cnt2[m100])   (m100 = tick%64)
 *
 * ★★ 移植期发现并修正了 S3 的一个**宣称≠实现**缺陷 (详见 AUDIT-H723-stage2.md H9):
 *   S3 把 div2 相位声明为 **100 个**(桶表 100 槽, 注释写 "phase 0-99", 归组用
 *   `ph = q2 % 100`), 但 `period` 的 phase 字段只有 **6 位**
 *   (PERIOD_PHASE_MASK = 0x3F)。deploy 侧 `(uint8_t)(dv | (ph << 2))` 会把 ph≥64
 *   的高位直接截掉 → 实际存下的是 `ph & 0x3F`:
 *       · 槽 64..99 **恒空** (ISR 侧 `ph = (period>>2) & 0x3F` 只可能 0..63)
 *       · q2 = 0 与 q2 = 64 落到**同一个相位** → div2 > 64 条时相位重叠,
 *         每拍最坏 div2 条数 ≈ n/64 而非预算模型假设的 n/100 (低估最坏拍成本)
 *   `tools/verify_capacity.py` 的"慢档 PID×128 满表"正好压在这个点上,
 *   但该用例只看 emax 是否超预算(量级差太远), 所以从未暴露。
 *
 *   H723 的修法: **让宣称等于实现** —— div2 相位严格取 64 个
 *   (BUCKET_DIV2_PHASES = 64, 与 6 位字段一一对应)。
 *   桶表仍按 S3 的 220 × u16 保留 100 槽, 以维持 SHM 偏移不变, 但
 *   **槽 64..99 恒为 0 是被断言的** (由 engine_bucket_checksum 与外部独立预测比对证明)。
 *   副产品: 未来 deploy 预算模型对 div2 的摊薄系数必须**除以 64, 而不是 100**
 *   (div2 每条每拍成本从 ceil(cost/100) 升到 ceil(cost/64), +56%)。 */
#define ROUTE_BUCKET_U16       220
#define OFF_ROUTE_BUCKETS      0x4480
#define OFF_ROUTE_BUCKETS_ST   0x4638
#define OFF_ROUTE_BUCKETS_END  0x47F0
#define BUCKET_DIV1_PHASES     10
#define BUCKET_DIV2_PHASES     64   /* ★ 与 period 的 6 位 phase 字段严格一致 */

/* ---- 未落地保留区 (显式命名, 不留"无名字的空洞") ----
 * ★ 审计 H3/A7 指出: 下面两段既没有名字也没有断言, 将来往这里放新域不会报错,
 *   "吃掉"邻区字节也不会有任何提示 (与 S3 的 OA20 同族 —— 那一族的本质就是
 *   "无人设防的区域迟早出事")。现在把空洞显式命名, 并把尺寸**钉成常量断言**:
 *   谁把邻区改大/改小, 这里立刻编译失败。 */
#define OFF_RSVD_DSL_DOMAIN     0x3840   /* S3 的 macro/force/seq 域, H723 未落地 */
#define OFF_RSVD_DSL_DOMAIN_SZ  (OFF_ROUTE_BUCKETS - OFF_RSVD_DSL_DOMAIN)   /* 0xC40 */
#define OFF_RSVD_EXEC_DOMAIN    0x47F0   /* S3 的 exec/force 域, H723 未落地 */
#define OFF_RSVD_EXEC_DOMAIN_SZ (OFF_MB_SET - OFF_RSVD_EXEC_DOMAIN)         /* 0x2B0 */
#define OFF_MB_TAIL             0x4B20   /* MB_SET 之后到 SHM 末尾, 备用 */
#define OFF_MB_TAIL_SZ          (SHM_SIZE - OFF_MB_TAIL)                    /* 0x34E0 */

/* ★★ A7 修正: 原为 0x4AC0, 与 S3 的 0x4AA0 差 32 字节 —— 而本头文件开头写着
 *   "全部逐字节对齐 esp32-core0", 目标是"S3 的 20 套回归脚本零改动"。
 *   S3 的 tools/verify_hmi.py (MB_SET_OFF = 0x4AA0) 与 tools/dclc.py 都按 0x4AA0
 *   读写: 偏移差 32 字节不会报错, 只会**静默读写到错误地址** (写进去也读不出来)。
 *   这是全表 125 个常量里唯一被改动的一个值 —— 已改回。 */
#define OFF_MB_SET           0x4AA0   /* 写区 64 WORD (SRC_HMI 源, 阶段 4 落地) */
#define SHM_SIZE             0x8000   /* 32KB (S3 为 64KB; H723 DTCM 128KB 充裕) */

/* ══════════ 路由条目 (16B packed) —— 与 S3 逐字节相同 ══════════ */
typedef struct __attribute__((packed, aligned(4))) {
    uint8_t  src_type;
    uint8_t  src_index;
    uint8_t  dst_type;
    uint8_t  dst_channel;
    uint8_t  op;
    uint8_t  flags;
    uint16_t param_idx;
    uint16_t state_offset;
    uint16_t actuator_idx;
    uint16_t wire2_idx;
    uint8_t  period;   /* offset 14: div_idx(2bit) + phase(6bit) */
    uint8_t  reserved; /* offset 15: S3 里这是编译器的**尾部填充字节** (15 个字段
                        * + aligned(4) → sizeof 补齐到 16)。这里显式命名, 使
                        * 逐字节校验和**不依赖填充内容** —— 否则任何"逐字段赋值"
                        * 的改动都会让填充变脏, 校验和随之漂移 (实测被这一步绊到过)。 */
} RouteEntry_t;

_Static_assert(sizeof(RouteEntry_t) == 16, "RouteEntry_t must be 16 bytes");
_Static_assert(_Alignof(RouteEntry_t) == 4, "RouteEntry_t alignment must be 4");

/* ---- 参数条目 (16B) ---- */
typedef struct __attribute__((packed, aligned(4))) {
    float value_a;
    float value_b;
    float value_c;
    float value_d;
} ParamEntry_t;

_Static_assert(sizeof(ParamEntry_t) == 16, "ParamEntry_t must be 16 bytes");

/* ---- 状态条目 (16B) ---- */
typedef struct __attribute__((packed, aligned(4))) {
    float state_a;
    float state_b;
    float state_c;
    float state_d;
} StateEntry_t;

_Static_assert(sizeof(StateEntry_t) == 16, "StateEntry_t must be 16 bytes");

/* ---- period 字段位定义 ---- */
#define PERIOD_DIV_IDX_FAST  0   /* 1×: 每 100μs */
#define PERIOD_DIV_IDX_MID   1   /* 10×: 每 1ms */
#define PERIOD_DIV_IDX_SLOW  2   /* ★ H9 修正后是 **64×** (6.4ms), 不是 100× ——
                                  *   `period` 的 phase 字段只有 6 位, 相位数只能是 64。
                                  *   S3 声称 100 却只有 64 个可达相位 (见 AUDIT §12)。 */
#define PERIOD_DIV_MASK      0x03
#define PERIOD_PHASE_SHIFT   2

/* ══════════ deploy 预算模型 (阶段 3.2) ══════════
 * 条数限制 ≠ 成本限制: 128 条 PID 是 128 条, 但成本是 DIRECT 的 2.6 倍。
 * 门的作用是保证**任何可部署程序**每拍执行都在确定性预算内。
 *
 * ★ 除数: div1 摊 ÷10 / **div2 摊 ÷64** (S3 用 100 是 H9 缺陷的一部分, 不许抄)。
 * ★ 预算来源: `k_op_cost_itcm[]` 全部是**本平台实测值**
 *   (tools/h723_op_sweep.py 两点法, 19/19 原语, 表校验和逐 op 与 Python 预测吻合)。
 *   S3 那张表是 240MHz 上的数 (DIRECT=234), 照抄就是"宣称≠实现"。 */
#define OP_COST_DIV0         1
#define OP_COST_DIV1         10
#define OP_COST_DIV2         64   /* ★ 不是 100 (H9) */
#define SRC_COST_FALLBACK    20   /* 未实测源类型的保守兜底 (cycles/条) */

/* 部署门: 引擎扫描每拍 ≤ 此值。拍长 40000 cyc (100μs @400MHz)。
 * 余下 14000 cyc (35μs) 留给: 骨架 ISR (~45) + 热重载那一拍 (~3000) +
 * 通信域/顺序域/传感域 (阶段 4) + 安全余量。
 * 依据: S3 的同名门是 16000/24000 = 67%; 这里 26000/40000 = 65%, 口径一致。 */
#define EXEC_DEPLOY_BUDGET   26000

/* 本平台实测的**最贵原语**成本 (PID, 见 engine.c 的 k_op_cost_itcm)。
 * 改原语表/新增更重的原语时必须同步更新 —— 它参与下面那条绊线断言。 */
#define OP_COST_MAX_MEASURED   140   /* = 实测最贵原语 PID (2026-09-10 重测) */

/* ══════════════ 绊线断言: 预算门当前"具不具约束力" ══════════════
 * ★★ 这是一个**故意的反向断言**, 语义要说清楚:
 *   128 条上限 × 最贵原语 145 cyc = 18560 cyc = 拍长的 **46%** —— 也就是说
 *   **在 MAX_ROUTES=128 的前提下, 任何合法程序都不可能把拍吃满**, 预算门
 *   当前**永远不会触发**。它是一条"未来的门"。
 *   为什么仍然保留它: ① 引擎成本模型必须在扩容前就位 (S3 的 OA12→OA22 就是
 *   成本模型漏维度反复返工); ② DTCM 能放 ~2000 条路由 —— **一旦扩容, 这个门
 *   立刻变成真门**, 到那时它就必须被实测验证"真的拦得住超载"。
 *   ⇒ 这条断言的作用是**在扩容/加重量级原语的那一刻失败**, 逼人回来重新评估:
 *     届时必须做一次超载实验 (构造 >门 的程序, 确认 NAK + 确认拍没被拉长),
 *     而不是相信一个从没被触发过的判据。
 *   断言通过 = "门还不具约束力, 无需动作"; 断言失败 = "门现在是真的了, 去验证它"。*/
_Static_assert((uint32_t)MAX_ROUTES * OP_COST_MAX_MEASURED <= EXEC_DEPLOY_BUDGET,
               "★ 预算门开始具约束力: 必须实测验证它能拦住超载 (见本断言上方注释)");

/* ══════════ H723 扩展: deploy 生效确认字段 (S3 的 0x39-0x3F 当时空闲) ══════════
 * ★ 这是对 S3 那笔语义债的偿还点: S3 的 "ACK = 已受理 ≠ 已生效" —— 上位机收到 ACK
 *   之后无法知道配置**什么时候**开始生效, 只能假设"大概很快"。
 *   H723 让"已生效"变成一个**可观测的量**: 每次受理 deploy 递增一个序号并回给 PC,
 *   ISR 真正切换完 ACTIVE 表后把这个序号写进 APPLIED_SEQ;
 *   PC 轮询 APPLIED_SEQ == 自己的序号 ⇒ 生效**被证明**, 而不是被假设。 */
#define OFF_CTRL_DEPLOY_SEQ   0x3A   /* u16: 固件受理的部署序号 (每次 deploy 递增) */
#define OFF_CTRL_APPLIED_SEQ  0x3C   /* u16: ISR 已切换生效的序号 (= DEPLOY_SEQ 即已生效) */
#define OFF_CTRL_APPLIED_LAT  0x3E   /* u16: 从置 RELOAD 到 ACTIVE 切换完成, 跨了几拍 */
#define PERIOD_PHASE_MASK    0x3F

/* ---- flags 位定义 ---- */
#define ROUTE_FLAG_ACTIVE   0x01
#define ROUTE_FLAG_WIRE2    0x02

/* ---- 原语操作码 (与 S3 完全一致) ---- */
#define OP_DIRECT   0x00
#define OP_CMP      0x01
#define OP_HYST     0x02
#define OP_CLAMP    0x03
#define OP_LPF      0x04
#define OP_PID      0x05
#define OP_RATE     0x06
#define OP_DEADBAND 0x07
#define OP_MUX      0x08
#define OP_EDGE     0x09
#define OP_LUT      0x0A
#define OP_CNT      0x0B
#define OP_TIMER    0x0C
#define OP_ARITH    0x0D
#define OP_SCALE    0x0E
#define OP_AND      0x0F
#define OP_OR       0x10
#define OP_NOT      0x11
#define OP_SR       0x12
/* ★ 上界语义必须一眼可辨 (原版 OP_MAX=0x12 恰好**等于**最大值 OP_SR, 而代码里
 *   是按"含"用 `op <= OP_MAX` —— 极易被后来人写成 `< OP_MAX` 而静默漏掉 SR)。
 *   两个名字并存: OP_MAX 含上界, OP_MAX_EXCL 排他上界。新增原语时**两个都要改**,
 *   下面的 _Static_assert 会强制这一点。 */
#define OP_MAX      0x12   /* 最高有效操作码 (含) */
#define OP_MAX_EXCL 0x13   /* 排他上界 = OP_MAX+1 (数组尺寸/数量统计用这个) */
_Static_assert(OP_MAX + 1u == OP_MAX_EXCL, "OP_MAX and OP_MAX_EXCL must be adjacent");

#define OP_ARITH_ADD 0
#define OP_ARITH_SUB 1
#define OP_ARITH_MUL 2
#define OP_ARITH_DIV 3
#define OP_ARITH_MAX 4
#define OP_ARITH_MIN 5
#define OP_SR_SET_DOM    0
#define OP_SR_RESET_DOM  1

/**
 * @brief 该原语是否需要"第二输入" (从 wire 数组的 wire2_idx 取)
 *
 * ★★ 为什么要有这个函数 (A3 / S3 的 M1 族, 2026-09-10):
 *   第二输入有两条落地路径 —— flags 里的 ROUTE_FLAG_WIRE2 显式标志, 与
 *   wire2_idx 非 0 的隐式约定。**只查后者会出事**: `wire2_idx == 0` 既是
 *   "没接第二输入"的默认值, 又是合法索引 wire[0] 本身, 二者无法区分 →
 *   引擎会静默读 wire[0] 当第二输入 (S3 上花了 M1→F2→N-A→OA1 **四轮**才修掉)。
 *   审计实测 (H723): ARITH(CONST 10, wb=wire[0]=7) 输出 17.0, 而语义应为 10.0。
 *   ⇒ ISR 与 deploy 校验**必须共用这一个判据**。
 *
 * ★ AND/OR 是布尔双输入; ARITH 的右操作数是 wb; SR 的复位输入是 wb;
 *   CNT 的减计数/复位输入是 wb。S3 的 route_validate 只拦了 AND/OR,
 *   ARITH/SR/CNT 三类漏了 —— 这里补齐 (S3 侧也值得回写)。
 */
static inline int op_needs_wire2(uint8_t op)
{
    return (op == OP_AND || op == OP_OR || op == OP_ARITH || op == OP_SR || op == OP_CNT);
}

/**
 * @brief 第二输入判据 (与 S3 最终形态逐字一致) —— ISR 与校验必须共用
 * @param flags      RouteEntry_t.flags
 * @param wire2_idx  RouteEntry_t.wire2_idx
 * @return 1 = 第二输入有效, 可以读 wire[wire2_idx]; 0 = 无第二输入, 用 0.0f
 */
static inline int wire2_valid(uint8_t flags, uint16_t wire2_idx)
{
    return ((flags & ROUTE_FLAG_WIRE2) || wire2_idx) && (wire2_idx < MAX_WIRES);
}

/**
 * @brief 该原语是否**有状态** (需要 state 槽)
 *
 * ★ 有状态原语若不挂状态槽, ISR 会把 `&s_state_fallback` 传进去 —— 多个无槽路由
 *   会**共用同一个兜底槽**, 互相污染 (S3 T22 实证)。deploy 侧必须拒绝
 *   "有状态原语 + state_offset==0" 的载荷 (见 engine_route_validate)。
 *   (原在 engine.c 里, 移到此处以供表填充与校验共用同一份清单 —— 两处各写一份
 *    正是"改一处忘另一处"的温床。)
 */
static inline int op_is_stateful_h(uint8_t op)
{
    return (op == OP_LPF || op == OP_PID || op == OP_HYST || op == OP_RATE ||
            op == OP_DEADBAND || op == OP_EDGE || op == OP_CNT || op == OP_TIMER ||
            op == OP_SR);
}

/* ---- 源类型 ---- */
#define SRC_SENSOR  0
#define SRC_WIRE    1
#define SRC_CONST   2
#define SRC_HMI     3   /* 通信域设定区 — 阶段 4 落地 (本阶段 read_source 返回 0) */

/* ---- 目标类型 ---- */
#define DST_WIRE    2

/* ---- dt 感知 (秒) —— 与 S3 primitives.h 同口径 ---- */
#define DT_FAST  0.0001f   /* div0: 100μs */
#define DT_MID   0.001f    /* div1: 1ms  */
#define DT_SLOW  0.01f     /* div2: 10ms */

/* ══════════ 编译期布局断言 (S3 A4 纪律: 任何区域不得重叠) ══════════
 * ★★ 覆盖范围必须**无缝**, 而且相邻区**必须精确相接** —— 所以下面用 `==` 而不是
 *   `<=` (第一版用 `<=`, 于是"某区被悄悄改大/改小、留下看不见的空隙"不会报错;
 *   审计 H3 就是这么发现 [0x3840,0x4480) 与 [0x47F0,0x4AA0) 两段"无人区"的)。
 *   用 `==` 之后: 任何尺寸改动只要不与邻区严丝合缝, 编译直接失败。 */
_Static_assert(OFF_CTRL_N_SEQ    + 8   == OFF_SENSOR_MAP,      "SHM ctrl block must end exactly at 0x40");
_Static_assert(OFF_SENSOR_MAP      + MAX_SENSORS   * 4 == OFF_ACTUATOR_STATUS, "SHM SENSOR_MAP must abut next region");
_Static_assert(OFF_ACTUATOR_STATUS + MAX_ACTUATORS * 4 == OFF_WIRE_MAP,        "SHM ACTUATOR_STATUS must abut next region");
_Static_assert(OFF_WIRE_MAP        + MAX_WIRES     * 4 == OFF_LUT_DATA,        "SHM WIRE_MAP must abut next region");
_Static_assert(OFF_LUT_DATA        + MAX_LUT       * 4 == OFF_ROUTE_TABLE,     "SHM LUT_DATA must abut next region");
_Static_assert(OFF_ROUTE_TABLE     + MAX_ROUTES    * 16 == OFF_ROUTE_STAGING,  "SHM ROUTE_TABLE must abut next region");
_Static_assert(OFF_ROUTE_STAGING   + MAX_ROUTES    * 16 == OFF_PARAM_TABLE,    "SHM ROUTE_STAGING must abut next region");
_Static_assert(OFF_PARAM_TABLE     + MAX_PARAMS    * 16 == OFF_PARAM_STAGING,  "SHM PARAM_TABLE must abut next region");
_Static_assert(OFF_PARAM_STAGING   + MAX_PARAMS    * 16 == OFF_STATE_TABLE,    "SHM PARAM_STAGING must abut next region");
_Static_assert(OFF_STATE_TABLE     + MAX_STATES    * 16 == OFF_STATE_STAGING,  "SHM STATE_TABLE must abut next region");
/* 状态 staging 之后是**保留区** (0x3840-0x447F), 所以这里只能断言"不相交" */
_Static_assert(OFF_STATE_STAGING   + MAX_STATES    * 16 <= OFF_RSVD_DSL_DOMAIN,  "SHM STATE_STAGING overruns reserved hole");
_Static_assert(OFF_RSVD_DSL_DOMAIN + OFF_RSVD_DSL_DOMAIN_SZ == OFF_ROUTE_BUCKETS, "SHM DSL reserved-hole size mismatch");
_Static_assert(OFF_ROUTE_BUCKETS   + ROUTE_BUCKET_U16 * 2 == OFF_ROUTE_BUCKETS_ST,  "SHM ROUTE_BUCKETS must abut staging");
_Static_assert(OFF_ROUTE_BUCKETS_ST + ROUTE_BUCKET_U16 * 2 == OFF_ROUTE_BUCKETS_END, "SHM ROUTE_BUCKETS_ST must abut end");
_Static_assert(OFF_ROUTE_BUCKETS_END == OFF_RSVD_EXEC_DOMAIN,                  "SHM EXEC reserved-hole start mismatch");
_Static_assert(OFF_RSVD_EXEC_DOMAIN + OFF_RSVD_EXEC_DOMAIN_SZ == OFF_MB_SET,   "SHM EXEC reserved-hole size mismatch");
_Static_assert(OFF_MB_SET          + MB_NREG       * 2 == OFF_MB_TAIL,         "SHM MB_SET must abut MB tail");
_Static_assert(OFF_MB_TAIL        + OFF_MB_TAIL_SZ == SHM_SIZE,                "SHM MB tail must end exactly at SHM_SIZE");
/* ★ 保留区尺寸钉成常量: 邻区一改, 这三条立刻失败 (它们就是"无人区"的哨兵) */
_Static_assert(OFF_RSVD_DSL_DOMAIN_SZ  == 0xC40u, "DSL hole size changed from 0xC40 - did you resize a neighbour?");
_Static_assert(OFF_RSVD_EXEC_DOMAIN_SZ == 0x2B0u, "EXEC hole size changed from 0x2B0 - did you resize a neighbour?");
_Static_assert(OFF_MB_TAIL_SZ          == 0x34E0u, "MB tail hole size changed from 0x34E0");
/* 反向断言: 控制块区的每个字段都必须落在区内 (防止上面某个宏被改大而不自知) */
_Static_assert(OFF_CTRL_MAGIC + 4 <= OFF_CTRL_N_SEQ + 8, "SHM ctrl field overruns 0x40");
_Static_assert(OFF_TIMING_LAST_EXEC + 4 <= OFF_CTRL_GPIO_MASK + 4, "SHM timing region overlaps GPIO_MASK");
/* deploy 生效字段必须落在 0x3A-0x3F 且不越界到 SENSOR_MAP */
_Static_assert(OFF_CTRL_APPLIED_LAT + 2 == OFF_SENSOR_MAP, "SHM deploy seq fields must exactly fill 0x3A-0x3F");

/* ══════════ 引擎扫描 (两份实例: FLASH 与 ITCM, 见 engine.c) ══════════
 * @param base   SHM 基址 (DTCM 内)
 * @param first  起始路由下标 (档桶调度会传桶起点)
 * @param count  扫描条数 (0 起)
 * @return       校验和 (证明"真的算过" —— 防死代码消除 + 提供运行证据)
 */
typedef uint32_t (*engine_scan_fn)(uint8_t *base, uint32_t first, uint32_t count);

extern uint32_t engine_scan_flash(uint8_t *base, uint32_t first, uint32_t count);
extern uint32_t engine_scan_itcm (uint8_t *base, uint32_t first, uint32_t count);

/** @brief 按 profile 填充参数表/状态表/路由表 (冷启动与重配置共用)
 *  profile: 0 = 全 DIRECT (对照 S3 的 234 cyc 基线)
 *           1 = 19 原语轮转 (混合程序, 真实成本谱)
 *           2 = 全 PID (最重档, 探预算上界)
 *           3 = **三档混合** · 全 DIRECT  (div 0/1/2 各约占 1/3)
 *           4 = **三档混合** · 19 原语轮转 */
void engine_fill_tables(uint8_t *base, int profile);

/* ══════════ 阶段 3: 档桶调度 (S3 OA15 治本语义) ══════════ */

/** @brief 按 (div, phase) 归组重排路由表 + 生成桶索引
 *  ★ 幂等: 对已归组的表再跑一次结果不变 (对源表按序扫描再落桶 = 稳定排序)。
 *  ★ 用 OFF_ROUTE_STAGING 当暂存 (它就是为"重排/热重载"预留的 2KB)。
 *  @return 归组后的条数 (应 == nr) */
uint32_t engine_build_buckets(uint8_t *base, uint32_t nr);

/** @brief 桶表校验和 (FNV-1a over 220 × u16) —— 供外部独立预测比对 */
uint32_t engine_bucket_checksum(const uint8_t *base);

/** @brief H9 断言: 桶表里"不可达槽" (div2 相位 64..99) 的非零个数, 期望恒为 0
 *  —— 这是"div2 相位数 = 6 位字段能表达的 64 个"这一修正的**可失败判据**:
 *     若哪天有人把相位数改回 100 而不改字段宽度, 这里立刻不为 0。 */
uint32_t engine_bucket_dead_slots(const uint8_t *base);

/** @brief ★ 分档调度: 本拍只跑
 *         [div0 全部] + [div1 桶 tick%10] + [div2 桶 tick%100]
 *  @param tick      拍号 (调用方保证单调递增)
 *  @param impl      用哪份实例 (FLASH / ITCM)
 *  @param nrun_out  可选: 本拍**实际执行**的路由条数 (正向证据: 与外部预测比对)
 *  @return          三段校验和的异或
 */
uint32_t engine_tick(uint8_t *base, uint32_t tick, engine_scan_fn impl,
                     uint32_t *nrun_out);

/** @brief SHM 静态区 (定义在 engine.c, 链接段 .dtcm_shm / DTCM 0x20000000) */
extern uint8_t g_shm[SHM_SIZE];

/** @brief 冷启动清零 (.dtcm_shm 是 NOLOAD, 上电内容不确定 → 必须显式清)
 *  ★ 单一入口纪律 (S3 第二十六轮收口): "新增任何域必须在此登记"。
 *    本实现直接整段 memset(SHM_SIZE), 所以天然完整 —— 但**新域若放在 SHM 之外**
 *    必须回到这里显式登记。engine_fill_tables() 也走这个入口, 不允许自带 memset。 */
void cold_start_reset(void);

/** @brief 落位自检: 1 = 声明位置与 .dtcm_shm 段首吻合且落在 DTCM 域内 */
int shm_layout_ok(void);

/** @brief 路由表校验和 (FNV-1a, 覆盖 op/flags/src_type/state_offset)
 *  ★ 审计修正 (对照 S3 第二十七轮 OA23 "判据恒真"):
 *    第一版用 `路由[0].op` 当"表换成功了吗"的哨兵 —— 但 profile 0(全DIRECT) 与
 *    profile 1(19原语轮转, 首元素恰好也是 DIRECT) 的 route[0].op **都是 0**,
 *    哨兵在混合程序组上恒等 → 判据**不具备可失败性**。
 *    改成整表校验和后, 工具可以在 Python 里**独立重算**期望值再比对 ——
 *    这同时给出"表内容正确"的正向证据 (不只是"表变了")。 */
uint32_t engine_table_checksum(const uint8_t *base);

/** @brief 表内 ACTIVE 路由条数 (期望 = 装了几条就是几条) */
uint32_t engine_active_routes(const uint8_t *base);

/* ══════════════════ 阶段 3.2 — deploy 路径 ══════════════════ */

/** @brief 单条路由合法性校验。返回 NULL = 合法, 否则返回**可直接回给 PC 的原因文本**。
 *  原则 (与 S3 一致): 错误配置在**下载时显式失败**, 而不是等到运行时静默越界。
 *  H723 适配: 去掉了 S3 的 force/GPIO 安全掩码判据 (本阶段没有这两个域),
 *  但**保留了 SRC_HMI 的拦截** —— 引擎侧 read_source 对 SRC_HMI 是留位返回 0,
 *  若放行就会得到"恒 0 的假信号"而不是报错 (H5 未决项的正面处理)。*/
const char *engine_route_validate(const RouteEntry_t *r);

/** @brief 计算程序每拍均摊执行成本 (Σ ceil((op_cost+src_cost)/div倍率)), 过滤 ACTIVE。
 *  @param payload deploy 载荷起点 (**注意不是整个帧**, 是 [nr][np][ns] 之后的第一个路由)
 *  @param nr      载荷里的路由条数 */
uint32_t engine_prog_budget(const uint8_t *payload, uint16_t nr);

/** @brief 本平台实测的单条原语成本 (cycles/条, ITCM, 全表扫)。
 *  @param op 操作码; 越界返回保守值。 */
uint16_t engine_op_cost(uint8_t op);

/** @brief 热重载: STAGING → ACTIVE (路由 + 桶表 + 参数 + 状态)。
 *  ★ 必须在**关掉扫描**的前提下调用, 否则 ISR 可能扫到半张表。
 *  调用方负责把 SHM 控制块的 RELOAD 标志清掉。 */
void engine_reload_active(uint8_t *base);

/** @brief deploy 的 STAGING 装载 + 归组重排 + 桶表生成 (校验**之前**不要调)。
 *  @param payload [nr][np][ns] 之后的第一个路由
 *  @param nr/np/ns 条数 (调用方已校验 ≤ MAX_*)
 *  @return 实际写入 ACTIVE 的路由条数 (非 ACTIVE 的不写 → 与 nr 可能不同) */
uint16_t engine_stage_program(uint8_t *base, const uint8_t *payload,
                              uint16_t nr, uint16_t np, uint16_t ns);

/** @brief 栈边界哨兵: 在 _shm_end 之上铺 128 字节魔术字, 被踩返回 0
 *  ★ 设防缺口修正: SHM 与栈之间原本**没有任何保护**, 栈溢出会静默踩表
 *    (同 OA20 族: "无人设防的区域迟早出事")。 */
void shm_guard_paint(void);
int  shm_guard_ok(void);

#endif /* DCL_ENGINE_H */
