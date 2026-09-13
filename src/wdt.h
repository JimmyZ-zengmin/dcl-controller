/**
 * wdt.h — 独立看门狗 (IWDG1) + **喂狗契约** (2026-09-13)
 *
 * ═══════════════════════════════════════════════════════════════════════════
 * 为什么是 IWDG 而不是 WWDG:
 *   ① IWDG 走 **LSI**, 不依赖 APB/主时钟 ⇒ **时钟树配错它照样在数**(正是要保的失效模式);
 *   ② 一旦启动**无法停止** ⇒ 软件关不掉它(不会被"顺手关掉"这种改动绕过);
 *   ③ WWDG 走 APB、有窗口语义(喂太早也复位), 与本项目的**忙等**结构冲突,
 *      且 PCLK=100MHz 下超时上限只有几十 ms, 做不出 200ms 这一档。
 *
 * ★★ 喂狗契约 (本文件最重要的部分, 比寄存器操作重要):
 *   **喂狗点 = 100µs 拍中断(ISR)**, 且**不**把主循环纳入看门狗。
 *   依据(实测, 不是推理):
 *     · sector erase 会让**主循环**阻塞 ~1s (T26 实测 ~816ms);
 *     · 但交付版**擦除期间拍中断一拍不丢** (T26 的 A/B: 对照 8153 拍缺口 / 交付 0 拍,
 *       原因是 T26 把**向量表 + ISR 代码 + 表**都搬离了 flash)。
 *   ⇒ 若把主循环也拉进看门狗门, **每次持久化刷盘都会误复位** —— 一个在正常工况下误报的
 *     看门狗, 比没有看门狗更坏(它会毁掉每一次刷盘)。
 *   ⇒ 所以: 看门狗只保证 "**CPU + 定时器 + 拍 ISR 活着**";
 *     "主循环是否在推进"另外**作为可观测量**报出来(见 g_loop_hb + FAULT_LOOP_STALL),
 *     由上位机 `mgmt.py --health` 判 —— 记录与归因交给台账, 复位交给看门狗。
 *
 * ★ 它抓不到什么(必须写清, 否则就是"宣称>实现"):
 *   ① **逻辑锁死但拍照在跑** —— 例: Modbus ORE 未清导致接收自锁死那次, 拍照常、isr_cyc 正常,
 *      看门狗**不会**触发。这类只能靠故障台账(faultlog.h)。
 *   ② **输出安全电平** —— 复位只是"停止驱动"。真正的安全电平要靠复位后尽早显式驱动(见 main.c
 *      的 boot 安全段) + 硬件侧外部下拉。
 *   ③ **确定性退化**(周期变长但没卡死) —— 靠 isr_cyc / 预算判据。
 */
#ifndef DCL_WDT_H
#define DCL_WDT_H

#include <stdint.h>
#include "regs.h"

/* ★★ 同步等待预算 —— **按时间 (DWT 周期) 给, 不按"轮询次数"给**。
 *
 * 实测 (2026-09-13, 三次开机一致, 可复跑):
 *   · "等更新落" = 10093.8 / 10095 / 10098 µs。把预算 ×10 后**读数不变**
 *     ⇒ 这不是"跑满预算", 是**真耗时 ~10.1ms** (跑满会长成预算那个大小)。
 *   · 同一条轮询的**单次成本 ≈ 30 周期 (75ns @400MHz)** —— 实测 4e6 次迭代 = **302ms**。
 *     ⇒ 按"次数"给预算会随编译器/总线时序漂移 (第一版就是这么给的), 所以这里按时间给。
 *
 * 取 100ms 的依据 (三条都可核):
 *   ① 是实测 10.1ms 的 ~10 倍;
 *   ② 按**已验证**的"耗时 = 5 个预分频步长", PR≤6 ⇒ ≤40ms 仍在预算内
 *      (PR=7 ⇒ 5×16ms = 80ms 已在边缘 ⇒ **换档时要一起调** —— 写成条件, 不假装覆盖全部档位);
 *   ③ 远小于"默认档看门狗 512ms"(5 倍): 关闸期间没人喂狗, 而 IWDG 已按默认档在数
 *      ⇒ 等待本身不会把自己等复位。
 *
 * ★ RM0468 §50.4.4 说更新"最多 5 个 RC 40kHz 周期 (≈125µs)" —— 与实测 10.1ms 差 80 倍。
 *   预算按**实测**定, 不按手册标称定; 差异如实留档, 不用推测把它解释掉。
 * ★★ 必须同时记 `sync_expired` (跑满 = 其实什么也没等到):
 *   否则"等标志落"这个动作在两种情况下读数长得一模一样, 就是个**空判据**。
 *   实测有效: 对照 C2(旧顺序) = 跑满(1); 交付 = 真落(0)。 */
#ifndef WDT_SYNC_BUDGET_CYC
#define WDT_SYNC_BUDGET_CYC   40000000u   /* 100ms @400MHz (1 周期 = 2.5ns) */
#endif

/* LSI 标称 (RM0468: LSI1 ≈ 32 kHz)。★ 实际有 ±5~10% 偏差 ⇒
 *   这就是**超时不能贴太紧**的量化依据, 也是我们选 200ms 而非 50ms 的原因。 */
#define WDT_LSI_HZ        32000u

/* 超时 (ms)。可用 -DDCL_WDT_TIMEOUT_MS=... 覆盖 (A/B 用)。 */
#ifndef WDT_TIMEOUT_MS
#define WDT_TIMEOUT_MS    200u
#endif

/* 预分频档 → 分频系数 = 4 × 2^PR。选 PR=4 ⇒ 步长 = 4×16/32000 = **2ms/计数**。
 *   RLR 是 12 位 (≤4095) ⇒ 本档最大 ~8.19s (没给到 4095: 见下面的取值理由)。
 * ★ 可覆盖 (`-DWDT_PR=<0..7>`) —— 用途是验证"更新耗时 = 5 个预分频步长"这条推断:
 *   PR 4→6 时步长 2ms→8ms ⇒ 等待应从 ~10ms 变成 ~40ms。 */
#ifndef WDT_PR_VALUE
#define WDT_PR_VALUE      4u
#endif
#define WDT_STEP_MS       ((4u * (1u << WDT_PR_VALUE) * 1000u) / WDT_LSI_HZ)  /* = 2 (整数截断) */
#define WDT_RLR_VALUE     ((WDT_TIMEOUT_MS) / (WDT_STEP_MS) - 1u)

_Static_assert(WDT_RLR_VALUE <= 4095u, "wdt: RLR 超 12 位 (调大 WDT_PR_VALUE 或调小超时)");
_Static_assert(WDT_TIMEOUT_MS >= 20u, "wdt: 超时 <20ms 太贴 (LSI 偏差 + 擦除期间的调度延迟会误报)");

/* 实际生效的超时 (ms)。★ 供外部读走 —— "写一次就算"的配置必须能被读回核对。 */
static inline uint32_t wdt_timeout_ms(void)
{
    return (WDT_RLR_VALUE + 1u) * WDT_STEP_MS;
}

/* LSI 使能 (IWDG 的时钟源)。★ 上电 LSI 默认关 ⇒ 必须先开并等 LSIRDY。 */
static inline int wdt_lsi_on(void)
{
    RCC_CSR |= RCC_CSR_LSION;
    for (uint32_t i = 0; i < 100000u; i++) {          /* ★ 有界等待, 不无限阻塞 */
        if (RCC_CSR & RCC_CSR_LSIRDY) return 0;
    }
    return -1;
}

/* ★★ 喂狗门开关 (A/B 对照用): 1 = 交付 (关闸生效) / 0 = **改前行为** (不关闸)。
 *   为什么要 0 档: 本项目铁律 —— **结构性修复必须配一个"能失败的对照构建"**。
 *   只跑 1 档看到 rc=0 不构成证据: 你无法排除"这次只是运气好"(ISR 恰好没撞上窗口)。
 *   带 0 档时, 两份**同一套测量方法**打出的读数才是归因证据。 */
#ifndef WDT_FEED_GATE
#define WDT_FEED_GATE     1
#endif

/* ★★ "启动先于配置" 开关 (A/B 对照用): 1 = 交付 (先写 0xCCCC 启动) /
 *   0 = **改前行为** (先解锁写 PR/RLR, 最后才启动)。
 *   依据见下面的"结论一"—— 这一条才是 -2 的**已证实根因**, 所以它必须有 0 档。 */
#ifndef WDT_START_FIRST
#define WDT_START_FIRST   1
#endif

/* 启动顺序 (2026-09-13 第三次修正 —— 依据 = 厂商权威件 + **单变量对照实验**):
 *
 *     启动(KR=0xCCCC) → 关闸 → 解锁(KR=0x5555) → 写 PR/RLR → 等更新落 → 开闸
 *
 * ★ 调用时机: **必须在喂狗点已经跑起来之后** (本项目是 tick_timer_init 之后),
 *   否则启动那一刻没人喂 ⇒ 200ms 后自己复位一次。
 *
 * ── 结论一: **"启动"必须先写** (已由对照实验证实, 不是推理) ─────────────────
 *   机制: PR/RLR 的更新只有在看门狗**已启动**时才可能在 VDD 域完成。
 *   证据 (单变量对照, 同一套测量方法, 见 `-DDCL_WDT_START_FIRST=0`):
 *     · 改前顺序 (先配 PR/RLR, 最后才写 0xCCCC) ⇒ `IWDG_SR` 的 PVU/RVU **永不清零**
 *       ⇒ rc=-2, 且读回仍是默认值 (PR=0 / RLR=0xFFF);
 *     · 只把"启动"挪到最前、其余不动            ⇒ rc=0, 读回 PR=4 / RLR=99 / SR=0。
 *   旁证: ST 的 `HAL_IWDG_Init()` 也是**第一步就 `__HAL_IWDG_START`**, 而且整个 H7
 *   HAL 驱动**从不**显式开 LSI —— 原话 "Enable IWDG. LSI is turned on automatically"
 *   (stm32h7xx_hal_iwdg.c 第 187 行)。
 *   ⇒ 我们那个 `wdt_lsi_on()` (置 RCC_CSR.LSION 并等 LSIRDY) 对 IWDG 的时钟通路
 *     **是不够的** —— "启动"这一步才是把它接通的开关。LSI 标志齐全却什么都不同步,
 *     正是本次 -2 最初的误导来源。
 *
 * ── 结论二: 更新耗时实测 ≈ **10.1ms**, 与轮询预算无关 (可复跑) ─────────────
 *   三次开机: 10095µs / 10098µs / 10093.8µs。
 *   ★★ 为什么必须把"等到了"与"跑满了"分开: 预算从 4e5 提到 4e6 (×10) 后读数**不变**,
 *      才排除了"轮询跑满预算"这个解释 ⇒ 10.1ms 是真耗时 (见 WDT_SYNC_BUDGET)。
 *   ★ 与手册口径**不一致, 如实记下**: RM0468 §50.4.4 说更新"最多 5 个 RC 40kHz 周期
 *     (≈125µs) 完成" —— 实测慢 80 倍。**预算按实测定, 不按手册标称定。**
 *   ★★ 机制**已验证** (2026-09-13, 可复跑的预测-验证): 耗时 = **5 个预分频后的计数步长**
 *      (步长 = 4×2^PR / LSI)。两档都命中:
 *        PR=4 ⇒ 步长 2ms ⇒ 预测 10.00ms, 实测 **10.09ms** (吻合 0.9%);
 *        PR=6 ⇒ 步长 8ms ⇒ 预测 40.00ms, 实测 **40.01ms** (吻合 0.03%)。
 *      旁证: 关闸期间被拦下的喂狗次数 ×100µs 与 DWT 读数一致 (101↔10095µs, 400↔40011µs)。
 *      ⇒ **PR 越大, 初始化阻塞越久** (PR=7 ⇒ 5×16ms = 80ms) —— 换档必须同步调预算。
 *      ★ 这条是本项目"先逻辑推演、再实机验证"的一个正面样例: 推断给出**可被证伪的数
 *        (40ms)**, 实验给了 40.01ms —— 而不是"看起来挺合理"。
 *
 * ── 关闸 (g_wdt_kr_busy): **硬化措施, 不是本次的根因** (如实标注) ────────────
 *   RM0468 §50.3.6 原文: "…must first write 0x00005555 in IWDG_KR. A write access to
 *   this register with a different value **breaks the sequence** and register access is
 *   protected again. **This is the case of the reload operation (writing 0x0000AAAA).**"
 *   而喂狗用的正是 0xAAAA、喂狗点在**拍 ISR** 里 (每 100µs 一次) ⇒ 解锁与写 RLR 之间
 *   若被 ISR 插一次 0xAAAA, RLR 的写会被**重新保护**而静默丢弃 (只在读回时暴露为 -3)。
 *   ★ 但对照实验**没有**证明它是 -2 的成因: 不关闸的那次构建同样成功。
 *     ⇒ 不许把"相关的机制"说成"已证实的因果"。保留它的理由是它消除一个**真实竞态**:
 *       实测编程窗口 ~10ms ⇒ 期间拍 ISR 会来 ~101 次 (见 kr_blocked), 撞上那个
 *       几周期的缝隙是小概率但非零; 关闸把"靠运气"变成"由构造保证"。
 *
 * ★★ 每一"写"都必须**读回核对** (项目铁律): 每一步的读回都留在 `g_wdt_diag` 里, 供外部读走。
 *   返回码: 0=已启动且配置成功 / -1=LSI 没起来 /
 *           -2=PR|RLR 更新未落 (看门狗没启动就写配置 —— 见结论一) /
 *           -3=读回值与我写的不符 (寄存器访问被丢弃, 如解锁序列被 KR 的其它写打断)
 */
typedef struct __attribute__((packed, aligned(4))) {
    uint32_t rc;      /* 返回码 */
    uint32_t csr;     /* RCC_CSR (LSI 状态) */
    uint32_t pr, rlr, sr;   /* 失败时(或完成后)的读回 */
    /* ★ 分步快照 (2026-09-13): 为了分辨"哪一步没生效、动手前它是什么状态" ——
     *   一次读数就能裁决, 不必再猜。 */
    uint32_t sr0, pr0, rlr0;   /* **动手前** (写任何东西之前) */
    uint32_t sr1;              /* 写完 KR=0x5555 (解锁) 之后立刻 */
    uint32_t sr2;              /* 写完 PR/RLR 之后立刻 */
    /* ★ 第二版新增 (顺序改对 + 加关闸之后, 要能证明"这次真的不一样") */
    uint32_t sr_start;         /* 写完 KR=0xCCCC (启动) 之后立刻 */
    uint32_t wait_cyc;         /* 等 PVU/RVU/WVU 落花了多少 **CPU 周期** (400MHz ⇒ 2.5ns/周期) */
    uint32_t kr_blocked;       /* 关闸期间 ISR **想喂狗被拦下**的次数 (>0 ⇒ 闸门确实在工作) */
    uint32_t kr_busy_snap;     /* 开闸前那一刻的 g_wdt_kr_busy (应为 1, 之后被清 0) */
    uint32_t sync_ok;          /* ★ 1 = 同步**成功**(PVU/RVU/WVU 真落了) / 0 = 跑满预算啥也没等到
                                *   ★ 极性说明 (2026-09-13, 回应审计 P3): 原字段名 `sync_expired`
                                *     且 1 = "跑满预算", 读的人容易把 1 当成"成功" —— 反了。
                                *     这类"1 到底代表好还是坏"必须由**字段名说清**, 不靠注释。 */
} WdtDiag_t;
_Static_assert(sizeof(WdtDiag_t) == 60u,
               "WdtDiag_t must be 15 words (PC reads it as raw u32 from WDT_STAT)");

extern WdtDiag_t g_wdt_diag;      /* 定义在 main.c (放全局便于 0x22 按名字读) */

/* ★★ 关闸 (喂狗门) —— 定义在 main.c。
 *   1 = wdt_start() 正在编程 PR/RLR, 此刻**任何**对 KR 的写都会打断解锁序列
 *       (RM0468 §50.3.6, **包括喂狗用的 0xAAAA**) ⇒ 拍 ISR 必须闭嘴。
 *   为什么不用 `__disable_irq()` 一把关: 那会把**拍本身**也停掉 —— 违反本项目
 *   铁律 0 (观测/配置不得改变被测对象)。这里只让 ISR 少做一件它在**这个瞬间本就不
 *   该做**的事 (没有看门狗可喂时去写它的 KR 本来就无意义), 拍照走、其余工作照做。
 *   ★ 计数器同样重要: 它让"闸门有没有真的拦住东西"变成**可读回的量**,
 *     而不是一句"我加了闸"。 */
extern volatile uint32_t g_wdt_kr_busy;      /* 1 = 正在编程, ISR 禁止写 KR */
extern volatile uint32_t g_wdt_kr_blocked;   /* ISR 被拦下的次数 (单调) */

static inline int wdt_start(void)
{
    g_wdt_diag.rc = 0;
    /* ★ 动手前先照一张快照 —— 分辨"我改之前它是什么状态"与"哪一步没生效"。 */
    g_wdt_diag.sr0  = IWDG_SR;
    g_wdt_diag.pr0  = IWDG_PR;
    g_wdt_diag.rlr0 = IWDG_RLR;
    if (wdt_lsi_on() != 0) {
        g_wdt_diag.csr = RCC_CSR;
        g_wdt_diag.rc  = (uint32_t)-1;
        return -1;
    }

    /* ① 启动 (RM0468 §50.3.1)。★ 此后计数器**已经在数**了: 默认档 PR=0/RLR=0xFFF
     *   ⇒ ≈512ms。下面的编程实测约 10ms(见结论二), 但要清楚"手上有一个截止时刻"。
     * ★★ 这一步的顺序是本次 -2 的**根因所在** —— 见文件头"结论一"。 */
#if WDT_START_FIRST
    IWDG_KR = IWDG_KEY_START;
#endif
    g_wdt_diag.sr_start = IWDG_SR;

    /* ② 关闸 —— **必须在解锁之前** (见文件头"关闸")。 */
    g_wdt_kr_busy = 1u;

    IWDG_KR = IWDG_KEY_UNLOCK;                        /* 允许写 PR/RLR */
    g_wdt_diag.sr1 = IWDG_SR;                         /* ★ 解锁后立刻 (看 KR 是否被接受) */
    IWDG_PR  = WDT_PR_VALUE;
    IWDG_RLR = WDT_RLR_VALUE;
    g_wdt_diag.sr2 = IWDG_SR;                         /* ★ 写完 PR/RLR 之后立刻 */

    /* ③ 等三个"更新中"标志落下 —— 写进去才算生效 (同"写过了就算"的静默失败族)。
     *   实测 ~10ms (不是 ~125µs); 用 DWT 记下真实耗时, 并单独记"是不是跑满了预算"。 */
    {
        uint32_t c0 = DWT_CYCCNT;
        uint32_t dl = c0 + WDT_SYNC_BUDGET_CYC;   /* ★ **周期死线**, 不是次数上限 */
        uint32_t exp = 0u;
        while (IWDG_SR & IWDG_SR_UPDATE_Msk) {
            /* ★ 有符号比较 ⇒ 天然处理 CYCCNT 回绕 (32 位计数器约 10.7s 一圈) */
            if ((int32_t)(DWT_CYCCNT - dl) >= 0) { exp = 1u; break; }
        }
        g_wdt_diag.wait_cyc     = DWT_CYCCNT - c0;
        /* ★★ "跑满预算"与"标志真落"必须分开记 —— 否则"等标志落"可能是个**空动作**:
         *   两种情况下 wait_cyc 都会是一个大数, 而只有"跑满"说明我们其实什么都没等到。
         *   (项目"空判据"教训: 一个永远成立/永远不成立的动作看起来像在做事。)
         *   ★ 极性 = **1 表示成功** (sync_ok): 原写法 1 = 跑满, 读的人容易当成功 (审计 P3)。
         *   实测有效: 旧顺序的对照 = 0(跑满); 交付档 = 1(真落)。 */
        g_wdt_diag.sync_ok = (exp == 0u) ? 1u : 0u;
    }
    g_wdt_diag.csr = RCC_CSR;
    g_wdt_diag.pr  = IWDG_PR;
    g_wdt_diag.rlr = IWDG_RLR;
    g_wdt_diag.sr  = IWDG_SR;
    g_wdt_diag.kr_busy_snap = g_wdt_kr_busy;
    g_wdt_diag.kr_blocked   = g_wdt_kr_blocked;

#if !WDT_START_FIRST
    /* ★ **对照路径 (改前行为)**: 配置都写完了才启动。
     *   实测 ⇒ PVU/RVU/WVU 永不清零 ⇒ 走下面 -2 分支, 读回仍是默认 0/0xFFF。
     *   这条分支存在的唯一目的是"判据能失败": 用同一套测量方法打出坏的那一半。 */
    IWDG_KR = IWDG_KEY_START;
#endif

    /* ④ 开闸 —— **无论成败都要开**。理由: 上面已经把 IWDG 启起来了, 关闸期间没人喂狗;
     *   若失败时不开闸, 板子会在默认档 (~512ms) 后复位, 然后**永远复位循环**。
     *   ★ 失败路径的正确行为是: 照常喂狗 (于是默认档的看门狗**仍在保护系统**),
     *     同时由 main.c 记一条 FAULT_WDT_INIT_FAIL —— "配置没落地"这件事自己浮出来,
     *     而不是变成"看门狗没救场"这种查不到原因的现场。 */
    g_wdt_kr_busy = 0u;

    if (IWDG_SR & IWDG_SR_UPDATE_Msk) { g_wdt_diag.rc = (uint32_t)-2; return -2; }
    if (g_wdt_diag.pr != WDT_PR_VALUE || g_wdt_diag.rlr != WDT_RLR_VALUE) {
        g_wdt_diag.rc = (uint32_t)-3; return -3;      /* ★ 读回不符 = 写被丢弃 */
    }
    return 0;
}

/** 喂狗 (重载计数)。★ 只能从**拍 ISR** 调用 —— 见文件头的"喂狗契约"。 */
static inline void wdt_feed(void)
{
    IWDG_KR = IWDG_KEY_FEED;
}

/* ══════════ ★★★ 运行期改超时 —— 为"落盘窗口"而加 (2026-09-13) ══════════
 * 为什么需要它 (实测依据, 不要再从零查一遍):
 *   擦 flash 期间, 拍 ISR 里的 `wdt_feed()`(写 IWDG_KR) **无法完成** ⇒ ISR 卡住不返回
 *   ⇒ 喂狗停 ⇒ 200ms 后 IWDG 复位 ⇒ "保存配置"变成"重启机器", 且配置从未落盘。
 *   见 docs/audit/H723-PERSIST-WDT-DEFECT.md §11/§12.2b (L1 实测: 去掉 DCL_WDT 后
 *   同一份固件落盘**完全成功** — writes=1 / nr=128 / 0.97s / 未复位)。
 *   ⇒ 形状确定为: **擦除前把窗口放大到 > 最长擦除, 擦完立刻恢复**。
 *
 * ★ 窗口取 8000ms 的**唯一理由**: 必须 ≥ `FL_ERASE_TIMEOUT_CYC`(flash.c 的擦除超时预算 8s)
 *   —— 项目已有同款教训("清空窗口必须 ≥ 该操作**自己的**超时预算, 否则 3s 的擦除会在
 *   2.5s 处被停滞自愈复位")。正常擦除端到端仅 0.97s, 8s 是给**失败路径**留的。
 *   ⇒ 这三个量是**同一件事的三种单位**, 改一个必须改另外两个:
 *      PERSIST_BLOCK_TICKS(main.c, 8s) · FL_ERASE_TIMEOUT_CYC(flash.c, 8s) · 本窗口
 *
 * ★ 实现严格照抄 `wdt_start()` 的解锁序列 (RM0468 §50.3.6: 写 PR/RLR 前必须先写 0x5555),
 *   并**必须关闸** `g_wdt_kr_busy` —— 否则 ISR 每 100µs 的喂狗会撞进"解锁→写 RLR"之间,
 *   把 RLR 的写**重新保护而静默丢弃**(只在读回时暴露), 这个 ~10ms 的窗口见 wdt.h 文件头。
 *
 * @param ms  目标超时(ms), 内部向上取整到 RLR 步长; 0 视为 1。
 * @return    之前的超时(ms) —— **调用者拿它恢复**; 0 = 失败(配置未改, 调用者不要恢复)。
 */
static inline uint32_t wdt_set_timeout_ms(uint32_t ms)
{
    if (wdt_lsi_on() != 0) return 0u;          /* 与 wdt_start 同一前提: LSI 得在跑 */

    uint32_t old_rlr = IWDG_RLR & 0xFFFu;      /* 旧值 (供恢复) */
    uint32_t old_ms  = (old_rlr + 1u) * WDT_STEP_MS;

    if (ms == 0u) ms = 1u;
    uint32_t rlr = (ms + WDT_STEP_MS - 1u) / WDT_STEP_MS;   /* 向上取整 */
    if (rlr == 0u)    rlr = 1u;
    if (rlr > 0x1000u) rlr = 0x1000u;          /* RLR 是 12 位: 值域 0..0xFFF, 步数 1..0x1000 */
    rlr -= 1u;

    g_wdt_kr_busy = 1u;                        /* ★ 关闸: 编程期间 ISR 不得写 KR */
    IWDG_KR  = IWDG_KEY_UNLOCK;
    IWDG_PR  = WDT_PR_VALUE;                   /* PR 不动 (2ms/步) ⇒ 只改 RLR 就够 */
    IWDG_RLR = rlr;

    /* 等 RVU/PVU 落下 —— "写过了" ≠ "写进去了" (照抄 wdt_start ③ 的口径) */
    {
        uint32_t c0 = DWT_CYCCNT;
        uint32_t dl = c0 + WDT_SYNC_BUDGET_CYC;
        while (IWDG_SR & IWDG_SR_UPDATE_Msk) {
            if ((int32_t)(DWT_CYCCNT - dl) >= 0) break;   /* 有符号比较 ⇒ 天然处理回绕 */
        }
    }
    g_wdt_kr_busy = 0u;                        /* ★ 无论成败都开闸 —— 否则永久停喂 */

    /* 读回核对: 写被丢弃时**如实返回失败**, 不让调用者按"改成功"去恢复 */
    if ((IWDG_RLR & 0xFFFu) != rlr) return 0u;
    return old_ms;
}

/** 读回实际生效的配置 (供外部核对"到底写进去了什么") */
static inline void wdt_readback(uint32_t *pr, uint32_t *rlr, uint32_t *sr)
{
    *pr = IWDG_PR; *rlr = IWDG_RLR; *sr = IWDG_SR;
}

#endif /* DCL_WDT_H */
