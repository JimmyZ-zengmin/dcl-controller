/**
 * main.c — DCL 引擎 H723 平台 · 阶段 2
 *
 * 阶段 0: 时钟树 (HSE 25MHz → VOS0 → PLL1 → 400MHz) + LA 外部验证
 * 阶段 1: 100μs 拍 + 空拍骨架 + DWT 拍开销/抖动测量
 * 阶段 2 (本文件): ★ ITCM / DTCM 落位 + 路由扫描移植 + **同镜像 A/B 实验**
 *
 * ══════════════════════════════════════════════════════════════════
 * 阶段 2 要回答的问题 (来自 STAGE1-REPORT §2 的观察)
 * ══════════════════════════════════════════════════════════════════
 * 阶段 1 发现: 同一份 ISR, 删掉 10 条指令反而更慢 (98 vs 95 cyc) ——
 * flash 常驻代码的 WCET 被"对齐/取指"主导, ±10% 不可预测。
 * 于是提了个**假设**: 把热代码搬进 ITCM 就能治。
 *
 * 本文件把这个假设做成可测的实验。为了排除"换固件 = 换环境"的干扰,
 * 用 **单一镜像 + 运行期选择器** 完成 4 组对比:
 *
 *   ① 空拍骨架          (engine_gate=0)                 → 外壳成本
 *   ② 全表扫 · FLASH 版 (gate=1, sel=0, profile=0)      → flash 取指成本
 *   ③ 全表扫 · ITCM  版 (gate=1, sel=1, profile=0)      → ITCM 取指成本
 *   ④ 混合程序 · 两版   (profile=1)                     → 真实程序成本谱
 *
 * ── 运行期选择器 (由 pyocd 写, 见 tools/h723_stage2_read.py) ──
 *   g_engine_gate    0=跳过扫描(只跑骨架) 1=扫描
 *   g_engine_sel     0=扫描走 FLASH 版    1=扫描走 ITCM 版
 *   g_n_routes       本拍扫描条数 (1..128) —— 两点法测斜率用
 *   g_table_profile  0=全DIRECT 1=19原语轮转 2=全PID
 *   g_reinit         写 1 → 主循环重填表 (填表期间自动关 gate, 防撕裂)
 *   g_stat_reset     写 1 → ISR 清空统计 (切配置时用)
 *   g_pa9_enable     1=PA9 每 32 拍翻转 (CH1 线路验证)
 *
 * ── 编译期开关 ──
 *   ISR_ITCM  0=中断外壳留在 FLASH  1=外壳也进 ITCM (默认)
 *   (扫描体两份实例**总是同时存在** —— 那是运行期 A/B 的前提)
 *
 * 接线: LA CH4 ← PA8 (拍输出 5kHz) / LA CH1 ← PA9 (g_pa9_enable=1 时)
 */
#include <stdint.h>
#include "regs.h"
#include "clock.h"
#include "engine.h"

#ifndef ISR_ITCM
#define ISR_ITCM 1
#endif

/* ══════════ 启动默认配置 (bench 用, 编译期) ══════════
 * ★ 为什么需要它 (审计途中发现): 运行期选择器再方便, 也**只在 pyocd 会话内有效** ——
 *   实测 pyocd 断开后目标核心被 HALT, 且 `connect_mode=under-reset` 的每次连接
 *   都会**复位目标** (工位经验: 会话内写、会话内读才可靠)。
 *   所以"要用外部仪器(LA)测某个非默认配置"就必须把它**编进固件**。
 * 默认 (0/0/1) = 骨架态 (不开扫描门) —— 上电即安全, 不跑引擎。 */
#ifndef BOOT_PROFILE
#define BOOT_PROFILE 0
#endif
#ifndef BOOT_GATE
#define BOOT_GATE    0
#endif
#ifndef BOOT_SEL
#define BOOT_SEL     1
#endif
#ifndef BOOT_SCAN_MODE
#define BOOT_SCAN_MODE 0     /* 0 = 全表扫 / 1 = 分档调度 (阶段 3) */
#endif

/* ══════════ 输出脚 ══════════ */
#define TICK_PORT       0u
#define TICK_BIT        8u          /* PA8: 拍输出 */
#define UARTT_PORT      0u
#define UARTT_BIT       9u          /* PA9: 线路验证 (CH1) */

#define REG8(a)         (*(volatile uint8_t *)(a))
#define NVIC_IP(n)      REG8(0xE000E400UL + (n))

#if ISR_ITCM
#define ISR_PLACE       __attribute__((section(".itcm_text"), noinline, used))
#else
#define ISR_PLACE       __attribute__((noinline, used))
#endif

/* ══════════ 全局可观测 (供 pyocd 读) ══════════
 * ★ 坑位 (2026-09-10 实测两次):
 *   - 只有**静态初值**、固件内无人读也无人写的全局, 会被 `-fdata-sections`
 *     单独开段, 再被 `--gc-sections` 整段回收 → 从符号表消失, 外部读不到。
 *   - 例: 阶段 1 的 g_isr_mode; 阶段 2 的 g_isr_itcm (第一版只在声明处初始化)。
 *   - `__attribute__((used))` 只挡 GCC 层的丢弃, **挡不住链接器的 --gc-sections**。
 *   - 真正可靠的办法: 让变量在代码里被读或被写 (g_isr_itcm 已在 main 里赋值)。
 *   所以下面每个变量都保证被代码触碰过 —— 这是与外部调试器的接口契约。 */
#define OBS   volatile __attribute__((used))

OBS int      g_boot_status = 0;    /* clock_init 返回值, 0=OK */
OBS uint32_t g_stage       = 0;    /* 执行进度 / 运行证据 (7 = ISR 在跑) */
OBS uint32_t g_tick_count  = 0;
OBS uint32_t g_clock_hclk  = 0;
OBS uint32_t g_isr_itcm    = ISR_ITCM;

/* ── 落位自检 ── */
OBS uint32_t g_shm_ok      = 0;    /* SHM 是否真落在 .dtcm_shm / DTCM 域 */
OBS uint32_t g_shm_addr    = 0;    /* g_shm 实际地址 (应为 0x2000xxxx) */
OBS uint32_t g_scan_itcm_addr = 0; /* ITCM 版扫描函数地址 (应 < 0x10000) */
OBS uint32_t g_scan_flash_addr= 0; /* FLASH 版扫描函数地址 (应 ≥ 0x08000000) */

/* ── 运行期选择器 (pyocd 写) ── */
OBS uint32_t g_engine_gate    = 0;
OBS uint32_t g_engine_sel     = 0;
OBS uint32_t g_n_routes       = 128;
OBS uint32_t g_table_profile  = 0;
OBS uint32_t g_reinit         = 0;   /* 写 1 → 主循环重填表 */
OBS uint32_t g_stat_reset     = 0;   /* 写 1 → ISR 清统计 */
OBS uint32_t g_pa9_enable     = 0;
OBS uint32_t g_reinit_done    = 0;   /* 证据: 重填表真的发生了几次 */
OBS uint32_t g_table_ck       = 0;   /* ★整表校验和 (工具在 Python 里独立重算比对) */
OBS uint32_t g_bucket_ck      = 0;   /* ★桶表校验和 (同上, 覆盖 220×u16) */
OBS uint32_t g_active_routes  = 0;   /* 表内 ACTIVE 条数 (期望 = n_routes) */
OBS uint32_t g_guard_ok       = 0;   /* 栈哨兵: 1 = SHM 顶上的魔术字完好 */
OBS uint32_t g_guard_bad_off  = 0xFFFFFFFFu;  /* 被踩的第几个字 (诊断用) */

/* ── 阶段 3: 档桶调度 ── */
OBS uint32_t g_scan_mode      = 0;   /* 0 = 全表扫 (阶段 2 基线) / 1 = 分档调度 */
OBS uint32_t g_eng_routes_last = 0;  /* 本拍**实际执行**的路由条数 (正向证据) */
OBS uint64_t g_eng_routes_total = 0; /* 累计执行条数 (与外部预测求和比对) */
OBS uint32_t g_eng_ticks      = 0;   /* 参与累计的拍数 */
OBS uint32_t g_bucket_zero_slots = 0;/* 桶表 64..99 槽非零个数 (期望 0; H9 断言) */

/* ── 引擎扫描: 执行证据 ── */
OBS uint32_t g_eng_ck       = 0;     /* 最近一拍的扫描校验和 */
OBS uint32_t g_eng_sel_used = 0xFFFFFFFFu;  /* 最近一拍实际走的实例 (0/1) */
OBS uint32_t g_eng_n_used   = 0;     /* 最近一拍实际扫的条数 */

/* ── 引擎扫描: 统计 (CPU 周期) ── */
OBS uint32_t g_eng_cyc_last = 0;
OBS uint32_t g_eng_cyc_min  = 0xFFFFFFFFu;
OBS uint32_t g_eng_cyc_max  = 0;
OBS uint64_t g_eng_cyc_sum  = 0;
OBS uint32_t g_eng_n        = 0;
OBS uint32_t g_eng_div0     = 0;

/* ── 整个 ISR: 统计 ── */
OBS uint32_t g_isr_cyc_last = 0;
OBS uint32_t g_isr_cyc_min  = 0xFFFFFFFFu;
OBS uint32_t g_isr_cyc_max  = 0;
OBS uint64_t g_isr_cyc_sum  = 0;
OBS uint32_t g_isr_n        = 0;

/* ── 拍周期 (相邻 ISR 入口 CYCCNT 差) ── */
OBS uint32_t g_per_cyc_last = 0;
OBS uint32_t g_per_cyc_min  = 0xFFFFFFFFu;
OBS uint32_t g_per_cyc_max  = 0;
OBS uint32_t g_per_prev     = 0;

/* ── DWT 标定 ── */
OBS uint32_t g_dwt_overhead = 0;
OBS uint32_t g_cal_n1000    = 0;
OBS uint32_t g_pa9_div      = 0;

/* ── L1 I-cache 状态 (H7 复位默认 **关闭**; 不开它 flash 版是被"冤枉"的) ──
 * 这个开关是为了把实验做完整: FLASH 版在 I-cache 关 / 开 两种状态下的成本,
 * 对照 ITCM 版 (ITCM 不经 cache, 应该完全不受影响)。 */
OBS uint32_t g_icache_req = 0;   /* 写 1 → 主循环使能 I-cache (单向, 不可逆) */
OBS uint32_t g_icache_on  = 0;   /* 实际状态 */
OBS uint32_t g_ccr_before = 0;
OBS uint32_t g_ccr_after  = 0;

/* ══════════ GPIO ══════════ */
static void pin_out_init(uint32_t port, uint32_t bit)
{
    if (port == 0u) RCC_AHB4ENR |= (1u << 0);

    uint32_t mod = GPIO_MODER(port);
    mod &= ~(3u << (bit * 2u));
    mod |=  (1u << (bit * 2u));
    GPIO_MODER(port) = mod;

    GPIO_OTYPER(port)  &= ~(1u << bit);
    GPIO_PUPDR(port)   &= ~(3u << (bit * 2u));

    uint32_t spd = GPIO_OSPEEDR(port);
    spd &= ~(3u << (bit * 2u));
    spd |=  (3u << (bit * 2u));
    GPIO_OSPEEDR(port) = spd;
}

static inline void pin_set(uint32_t port, uint32_t bit, int hi)
{
    GPIO_BSRR(port) = hi ? (1u << bit) : (1u << (bit + 16u));
}

/* ══════════ DWT 标定 (阶段 1 沿用) ══════════ */
__attribute__((noinline))
static uint32_t calib_nop(volatile uint32_t n)
{
    uint32_t t0 = DWT_CYCCNT;
    for (volatile uint32_t i = 0; i < n; i++) { __asm__ volatile("nop"); }
    return DWT_CYCCNT - t0;
}

static void calibrate(void)
{
    uint32_t a = DWT_CYCCNT, b = DWT_CYCCNT;
    g_dwt_overhead = b - a;
    g_cal_n1000 = calib_nop(1000u);
}

/* ══════════ TIM2: 精确 100μs 拍 (TIMxCLK 200MHz, ARR 20000-1) ══════════ */
static void tick_timer_init(void)
{
    RCC_APB1LENR |= (1u << 0);                 /* TIM2EN */

    TIM_CR1(TIM2_BASE) = 0;
    TIM_PSC(TIM2_BASE) = 0;
    TIM_ARR(TIM2_BASE) = CLK_TICK_TIMCNT - 1u;
    TIM_EGR(TIM2_BASE) = TIM_EGR_UG;
    TIM_SR(TIM2_BASE)  = 0;

    NVIC_IP(IRQ_TIM2)  = 0;                    /* 最高抢占优先级 */
    NVIC_ISER          = (1u << IRQ_TIM2);

    TIM_DIER(TIM2_BASE) = TIM_DIER_UIE;
    TIM_CR1(TIM2_BASE)  = TIM_CR1_CEN;
}

/* ══════════ L1 I-cache 使能 (实验用; 生产固件不用 —— 见下方说明) ══════════
 * ★ 为什么不默认开: cache 是**确定性**的敌人 —— 命中/未命中取决于程序历史,
 *   同一个循环在冷/热两种状态下执行时间不同。本项目的做法是"热代码进 ITCM,
 *   大缓冲留在 non-cacheable 的地址域", 而不是靠 cache 撞运气。
 *   这里的开关只为了把 A/B 实验做完整 (证伪"ITCM 只是碰巧比没开 cache 快")。
 * 序列按 ARM ARM: ICIALLU → DSB → ISB → 置 CCR.IC → DSB → ISB */
static void scb_enable_icache(void)
{
    g_ccr_before = SCB_CCR;
    SCB_ICIALLU = 0;
    __asm__ volatile("dsb; isb" ::: "memory");
    SCB_CCR |= SCB_CCR_IC;
    __asm__ volatile("dsb; isb" ::: "memory");
    SCB_ICIALLU = 0;
    __asm__ volatile("dsb; isb" ::: "memory");
    g_ccr_after = SCB_CCR;
}

/* ══════════ 清统计 ══════════ */
static inline void stats_reset(void)
{
    g_eng_cyc_last = 0;
    g_eng_cyc_min  = 0xFFFFFFFFu;
    g_eng_cyc_max  = 0;
    g_eng_cyc_sum  = 0;
    g_eng_n        = 0;
    g_eng_div0     = 0;

    g_isr_cyc_last = 0;
    g_isr_cyc_min  = 0xFFFFFFFFu;
    g_isr_cyc_max  = 0;
    g_isr_cyc_sum  = 0;
    g_isr_n        = 0;

    g_eng_routes_last  = 0;
    g_eng_routes_total = 0;
    g_eng_ticks        = 0;

    /* 拍周期也复位 (★ 保留 g_per_prev —— 它保证复位后第一个样本仍然有效) */
    g_per_cyc_last = 0;
    g_per_cyc_min  = 0xFFFFFFFFu;
    g_per_cyc_max  = 0;
}

/* ══════════ 拍中断 ══════════ */
ISR_PLACE void TIM2_IRQHandler(void)
{
    uint32_t t0 = DWT_CYCCNT;

    if (TIM_SR(TIM2_BASE) & TIM_SR_UIF) {
        TIM_SR(TIM2_BASE) = ~TIM_SR_UIF;
        g_stage = 7;

        if (g_stat_reset) {            /* 切配置用 (不清拍周期统计, 保连续性) */
            g_stat_reset = 0;
            stats_reset();
        }

        if (g_tick_count & 1u) pin_set(TICK_PORT, TICK_BIT, 1);
        else                   pin_set(TICK_PORT, TICK_BIT, 0);
        g_tick_count++;

        if (g_pa9_enable && ++g_pa9_div >= 32u) {
            g_pa9_div = 0;
            pin_set(UARTT_PORT, UARTT_BIT,
                    (GPIO_ODR(UARTT_PORT) & (1u << UARTT_BIT)) ? 0 : 1);
        }

        /* ---- 引擎扫描 (ta..tb 只包住扫描体本身) ---- */
        if (g_engine_gate) {
            uint32_t ta = DWT_CYCCNT;
            uint32_t sel = g_engine_sel;
            uint32_t ck;
            uint32_t nrun = 0;
            if (g_scan_mode) {
                /* 阶段 3: 分档调度 —— 本拍只跑 [div0]+[div1 本拍桶]+[div2 本拍桶] */
                ck = engine_tick(g_shm, g_tick_count, sel ? engine_scan_itcm
                                                          : engine_scan_flash, &nrun);
            } else {
                /* 阶段 2 基线: 全表扫 (n = g_n_routes) */
                uint32_t n = g_n_routes;
                if (n > MAX_ROUTES) n = MAX_ROUTES;
                ck = sel ? engine_scan_itcm(g_shm, 0, n)
                         : engine_scan_flash(g_shm, 0, n);
                nrun = n;
            }
            uint32_t tb = DWT_CYCCNT;

            g_eng_sel_used = sel;
            g_eng_n_used   = g_n_routes;
            g_eng_ck       = ck;
            g_eng_routes_last = nrun;
            g_eng_routes_total += nrun;
            g_eng_ticks++;

            uint32_t d = tb - ta;
            g_eng_cyc_last = d;
            if (d < g_eng_cyc_min) g_eng_cyc_min = d;
            if (d > g_eng_cyc_max) g_eng_cyc_max = d;
            g_eng_cyc_sum += d;
            g_eng_n++;
            if (d == 0u) g_eng_div0++;      /* 防御: "零成本"一定是测量坏了 */
        }

        uint32_t t1 = DWT_CYCCNT;
        uint32_t di = t1 - t0;
        g_isr_cyc_last = di;
        if (di < g_isr_cyc_min) g_isr_cyc_min = di;
        if (di > g_isr_cyc_max) g_isr_cyc_max = di;
        g_isr_cyc_sum += di;
        g_isr_n++;

        if (g_per_prev) {
            uint32_t p = t0 - g_per_prev;
            g_per_cyc_last = p;
            if (p < g_per_cyc_min) g_per_cyc_min = p;
            if (p > g_per_cyc_max) g_per_cyc_max = p;
        }
        g_per_prev = t0;
    }
}

/* ══════════ 失败指示 ══════════ */
static void blink_error(int err)
{
    if (err < 0) err = -err;
    if (err == 0) err = 1;
    for (;;) {
        for (int i = 0; i < err; i++) {
            pin_set(TICK_PORT, TICK_BIT, 1);
            for (volatile uint32_t d = 0; d < 400000u; d++) { }
            pin_set(TICK_PORT, TICK_BIT, 0);
            for (volatile uint32_t d = 0; d < 400000u; d++) { }
        }
        for (volatile uint32_t d = 0; d < 3000000u; d++) { }
    }
}

void SystemInit(void)
{
    SCB_CPACR |= (0xFu << 20);                 /* FPU */
    __asm__ volatile("dsb; isb");
}

int main(void)
{
    pin_out_init(TICK_PORT, TICK_BIT);
    pin_out_init(UARTT_PORT, UARTT_BIT);
    g_isr_itcm = ISR_ITCM;      /* ★ 在代码里写一次, 否则会被 --gc-sections 回收 */
    g_stage = 1;

    /* ① 时钟 */
    int err = clock_init();
    g_boot_status = err;
    g_stage = 2;
    if (err != CLK_OK) blink_error(err);

    g_clock_hclk = clock_get_hclk_hz();
    g_stage = 3;

    /* ② 落位自检 (宣称=实现: "表在 DTCM" 必须可验证) */
    g_shm_addr         = (uint32_t)(uintptr_t)g_shm;
    g_shm_ok           = (uint32_t)shm_layout_ok();
    g_scan_itcm_addr   = (uint32_t)(uintptr_t)&engine_scan_itcm;
    g_scan_flash_addr  = (uint32_t)(uintptr_t)&engine_scan_flash;
    g_stage = 4;

    /* ③ 表装载: 冷启动清零(单一入口) → 铺栈哨兵 → 填 profile 0
     *    ★ 哨兵必须铺在"第一次大量用栈"之前 —— 否则铺的时候已经踩过一遍了 */
    cold_start_reset();
    shm_guard_paint();
    engine_fill_tables(g_shm, BOOT_PROFILE);
    g_table_ck      = engine_table_checksum(g_shm);
    g_bucket_ck     = engine_bucket_checksum(g_shm);
    g_bucket_zero_slots = engine_bucket_dead_slots(g_shm);
    g_active_routes = engine_active_routes(g_shm);
    g_guard_ok      = (uint32_t)shm_guard_ok();
    g_table_profile = BOOT_PROFILE;   /* 让工具看到"当前配置", 而不是"假定配置" */
    g_engine_sel    = BOOT_SEL;
    g_scan_mode     = BOOT_SCAN_MODE;
    g_n_routes      = MAX_ROUTES;
    g_engine_gate   = BOOT_GATE;
    g_stage = 5;

    /* ④ DWT 标定 */
    dwt_enable();
    calibrate();
    g_stage = 6;

    /* ⑤ 100μs 拍 */
    tick_timer_init();
    g_stage = 8;

    for (;;) {
        /* L1 I-cache 使能 (实验用, 单向) */
        if (g_icache_req && !g_icache_on) {
            g_icache_req = 0;
            scb_enable_icache();
            g_icache_on = 1;
        }
        /* 重填表: 先关扫描门 (防 ISR 扫到半张表), 填完恢复 */
        if (g_reinit) {
            g_reinit = 0;
            uint32_t sv = g_engine_gate;
            g_engine_gate = 0;
            engine_fill_tables(g_shm, (int)g_table_profile);
            g_table_ck      = engine_table_checksum(g_shm);
            g_bucket_ck     = engine_bucket_checksum(g_shm);
            g_bucket_zero_slots = engine_bucket_dead_slots(g_shm);
            g_active_routes = engine_active_routes(g_shm);
            g_engine_gate = sv;
            g_reinit_done++;
        }
        /* 栈哨兵周期巡检 (廉价: 32 个字, 主循环有 100μs 一次的机会) */
        g_guard_ok = (uint32_t)shm_guard_ok();
        g_stage = 9;
        __asm__ volatile("wfi");
    }
}
