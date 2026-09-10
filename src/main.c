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
 *   g_pa9_enable     1=PA9 每 32 拍翻转 (CH1 线路验证; ★仅 PA9_MODE=0 时有效 ——
 *                      阶段 3.1 起 PA9 默认归 USART1_TX, 见下)
 *
 * ── 编译期开关 ──
 *   ISR_ITCM  0=中断外壳留在 FLASH  1=外壳也进 ITCM (默认)
 *   PA9_MODE  0=PA9 方波(阶段1线路验证) 1=PA9=USART1_TX (默认, 阶段 3.1 起)
 *   (扫描体两份实例**总是同时存在** —— 那是运行期 A/B 的前提)
 *
 * 接线: LA CH4 ← PA8 (拍输出 5kHz) / LA CH1 ← PA9
 *       (PA9_MODE=0 时是 3.2ms 方波; =1 时是 115200 的协议 UART 波形)
 *
 * ── 阶段 3.1 新增: 协议层 (transport 帧 + USART1) ──
 *   帧格式逐字沿用 esp32-core0; 能力位图只声明**真跑通了**的位 (见 transport.h)。
 *   UART 中断优先级 0x80 < 拍(0) —— 外设永远不许抢拍。
 */
#include <stdint.h>
#include <string.h>
#include "regs.h"
#include "clock.h"
#include "engine.h"
#include "transport.h"
#include "uart.h"

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

/* ── 阶段 3.1 协议层开关 ── */
#ifndef PA9_MODE
#define PA9_MODE 1           /* 0 = PA9 作 GPIO 方波 (阶段 1 线路验证, 保留可复跑)
                              * 1 = PA9 作 USART1_TX (协议; 默认)
                              * ★ 只能是编译期: 引脚复用不是运行期可切的状态,
                              *   做成运行期旋钮就会变成"宣称能做到其实做不到"。 */
#endif
#ifndef BOOT_BANNER
#define BOOT_BANNER 1        /* 上电主动发一帧版本响应 —— 让 LA **不需要 PC 接线**
                              * 就能拿到协议级外部证据 (PA9 上真实波形) */
#endif
#ifndef BANNER_PERIOD_MS
#define BANNER_PERIOD_MS 0   /* >0: 每 N 毫秒重播一次横幅 (LA 抓取用);
                              * 生产固件为 0 —— 持续占总线会干扰 PC 通信 */
#endif
#ifndef UART_SELFTEST
#define UART_SELFTEST 0      /* 1 = 上电做回环自检 (需 PA9↔PA10 短接);
                              * 证明 RX 通路 + CRC 校验 + 解析器真的工作 */
#endif
#ifndef DEPLOY_SELFTEST
#define DEPLOY_SELFTEST 0    /* 1 = 上电跑 deploy 自检 (9 例合法/非法载荷);
                              * 结果在 g_dst_case[] (1=符合预期 2=不符) 与 g_dst_done */
#endif
#ifndef UART_BAUD
#define UART_BAUD 115200u
#endif
#ifndef UART_PCLK2
#define UART_PCLK2 100000000u   /* APB2 = HCLK(200MHz)/2; 见 clock.h CLK_PCLK_HZ */
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

/* ── 阶段 3.2: deploy / 热重载 观测量 ──
 * ★ 必须声明在 ISR **之前** (ISR 里要用) —— 这几个量构成 deploy 的可失败判据:
 *   受理成功与被拒**分别**计数 (只数成功会让"全部被拒"看起来像"没部署过");
 *   applied_seq 与 reload_lat 证明"真的生效了", 而不是只"受理了"。 */
OBS uint32_t g_deploy_ok       = 0;   /* 受理成功的 deploy 次数 */
OBS uint32_t g_deploy_nak      = 0;   /* 被拒次数 (校验 / 预算门拦下的) */
OBS uint32_t g_deploy_routes   = 0;   /* 最近一次实际写入 ACTIVE 的路由数 */
OBS uint32_t g_deploy_budget   = 0;   /* 最近一次预算 (cyc/拍, 对照实测占拍) */
OBS uint32_t g_deploy_seq      = 0;   /* 固件侧受理序号 (每次成功 deploy +1) */
OBS uint32_t g_applied_seq     = 0;   /* ★ ISR 已切换生效的序号 (== deploy_seq 即已生效) */
OBS uint32_t g_reload_count    = 0;   /* ISR 实际执行 ACTIVE 切换的次数 */
OBS uint32_t g_reload_cyc      = 0;   /* 最近一次热重载本身的耗时 (DWT, cyc) */
OBS uint32_t g_reload_lat      = 0;   /* 从置 RELOAD 到生效完成跨了几拍 */
OBS uint32_t g_deploy_set_tick = 0;   /* 置 RELOAD 时的拍号 (供 ISR 算延迟) */

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

        /* ---- 热重载 (阶段 3.2): STAGING → ACTIVE ----
         * ★ 位置在扫描**之前**: 本拍就用新表跑完, 不出现"已受理但这一拍还在用旧表"
         *   的中间态。切换过程对 ISR 是原子的 (单字节 RELOAD 标志进入这里)。
         * ★ 代价: 这一拍 ISR 会长出 memcpy 的成本 (实测见 g_reload_cyc)。
         *   只要总时长仍 < 拍长, 拍周期就完全不受影响 —— 只有 isr_cyc_max 会记下
         *   这个尖峰。这是"部署瞬间有一拍变长"的**已知且有界**代价, 不是抖动。 */
        if (SHM_U8(g_shm, OFF_CTRL_RELOAD)) {
            uint32_t tr0 = DWT_CYCCNT;
            engine_reload_active(g_shm);
            g_reload_cyc = DWT_CYCCNT - tr0;
            g_n_routes   = SHM_U16(g_shm, OFF_CTRL_N_ROUTES);   /* 扫描条数随新表走 */
            /* ★ 部署的程序**必须走分档调度**: 全表扫会把 div1/div2 路由也每拍跑一遍
             *   —— 等于静默忽略档位语义 (程序"能跑"但时序全错)。所以这里强制置 1,
             *   并让它留在 g_scan_mode 这个可观测量里, 而不是藏在暗处。 */
            g_scan_mode  = 1;
            g_applied_seq = SHM_U16(g_shm, OFF_CTRL_APPLIED_SEQ);
            g_reload_lat  = g_tick_count - g_deploy_set_tick;
            SHM_U16(g_shm, OFF_CTRL_APPLIED_LAT) = (uint16_t)g_reload_lat;
            SHM_U8(g_shm, OFF_CTRL_RELOAD) = 0;
            __asm__ volatile("dsb" ::: "memory");   /* ARM: dsb (S3 的 Xtensa `memw` 在 ARM 上不存在) */
            g_reload_count++;
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

/* ══════════════════════════════════════════════════════════════════
 * 阶段 3.1 — 协议层 (transport 帧 + USART1)
 * ══════════════════════════════════════════════════════════════════
 * 分工严格:
 *   USART1 IRQ  → 只把字节塞进环形缓冲 (uart.c)
 *   主循环      → proto_poll() 排空缓冲 → fp_feed 解析 → 分发命令
 * 这样 ISR 时长与"帧有多长"无关, 拍周期不受 PC 通信影响。
 */
static FrameParser_t  s_parser;
static uint8_t        s_txbuf[FRAME_TOTAL_MAX];
static volatile uint32_t s_selftest_active = 0;

OBS uint32_t g_uart_brr      = 0;    /* BRR 实测值 (100MHz/115200 → 0x3641) */
OBS uint32_t g_uart_rx_bytes = 0;
OBS uint32_t g_uart_tx_bytes = 0;
OBS uint32_t g_uart_ore      = 0;    /* 硬件溢出: 非 0 = 主循环排空太慢 (故障信号) */
OBS uint32_t g_uart_drop     = 0;    /* 环形缓冲写满丢弃 */
OBS uint32_t g_frame_ok      = 0;    /* CRC 通过的完整帧数 —— 解析链路的正向证据 */
OBS uint32_t g_frame_bad     = 0;    /* 超长 / CRC 不符 */
OBS uint32_t g_cmd_count     = 0;
OBS uint32_t g_cmd_last      = 0xFFFFFFFFu;
OBS uint32_t g_nak_count     = 0;
OBS uint32_t g_banner_count  = 0;
OBS uint32_t g_selftest_state  = 3;  /* 0=待跑 1=通过 2=失败 3=未启用 */
OBS uint32_t g_selftest_frames = 0;

/* 组帧: [0xC1][sts][len:2 LE][payload][crc:2 LE]; CRC 覆盖 [sts][len][payload] */
static void send_response(uint8_t sts, const uint8_t *p, uint32_t n)
{
    if (n > FRAME_PAYLOAD_MAX) n = FRAME_PAYLOAD_MAX;
    uint32_t pos = 0;
    s_txbuf[pos++] = FRAME_SYNC_MCU2PC;
    s_txbuf[pos++] = sts;
    s_txbuf[pos++] = (uint8_t)(n & 0xFFu);
    s_txbuf[pos++] = (uint8_t)((n >> 8) & 0xFFu);
    for (uint32_t i = 0; i < n; i++) s_txbuf[pos++] = p[i];
    uint16_t crc = crc16_ccitt(s_txbuf + 1, 2u + n);      /* ★ 不含 SYNC */
    s_txbuf[pos++] = (uint8_t)(crc & 0xFFu);
    s_txbuf[pos++] = (uint8_t)(crc >> 8);
    uart1_write(s_txbuf, pos);
    g_uart_tx_bytes += pos;
}

static void ack(const uint8_t *p, uint32_t n) { send_response(STS_ACK, p, n); }

static void nak(const char *m)
{
    uint32_t n = 0;
    while (m && m[n]) n++;
    g_nak_count++;
    send_response(STS_NAK, (const uint8_t *)m, n);
}

/* 版本/能力协商: 载荷 [fw:u16 LE][cap:u16 LE] —— 与 S3 逐字节同构 */
static void h_get_version(void)
{
    uint8_t r[4];
    uint16_t v = DCL_FW_VERSION_H723;
    uint16_t c = DCL_CAP_H723_IMPL;
    r[0] = (uint8_t)(v & 0xFFu); r[1] = (uint8_t)(v >> 8);
    r[2] = (uint8_t)(c & 0xFFu); r[3] = (uint8_t)(c >> 8);
    ack(r, 4);
}

/* 上电横幅: 主动发一帧版本响应。两个作用:
 *   ① LA 端**不需要 PC 接线**就能抓到真实协议波形 → 外部证据
 *   ② PC 端连上时能立刻看到"设备还活着" */
static void proto_banner(void) { h_get_version(); g_banner_count++; }

/* ══════════ 阶段 3.2 — deploy (0x10) 与引擎状态 (0x38) ══════════ */

static inline void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v); p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
static inline uint16_t get16(const uint8_t *p) { return (uint16_t)(p[0] | ((uint16_t)p[1] << 8)); }
static inline uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* 0x10 DEPLOY — 载荷: [nr:u16][np:u16][ns:u16][routes nr×16B][params np×16B][states ns×16B]
 *
 * 与 S3 的差异 (都是"把债还掉", 不是"另搞一套"):
 *  ① ACK **带载荷** [seq:u16][budget:u32] —— S3 的 ACK 是空的, 上位机只能知道
 *     "受理了"; 现在知道"受理了第几号、预算多少 cyc"。旧上位机读前两个字节之外
 *     的内容会被忽略, 所以是向后兼容的追加 (S3 自己就用这个惯例: 0x43 从 8B 扩到 11B)。
 *  ② **生效确认**: seq 会被 ISR 在真正切换完 ACTIVE 表时写进 APPLIED_SEQ,
 *     0x38 的尾部扩展会把它带回来 ⇒ "已生效"变成可观测, 不再是假设。
 *  ③ 校验里 SRC_HMI 直接拒 (H723 未实现该源, 放行 = 静默给恒 0 的假信号)。
 *  ④ 预算模型的除数 div2 用 **64** 而不是 100 (H9)。 */
static void h_deploy(const uint8_t *p, uint32_t n)
{
    if (n < 6) { g_deploy_nak++; nak("short"); return; }
    uint16_t nr = get16(p), np = get16(p + 2), ns = get16(p + 4);
    if (nr > MAX_ROUTES || np > MAX_PARAMS || ns > MAX_STATES) {
        g_deploy_nak++; nak("counts exceed max"); return;
    }
    uint32_t need = 6u + ((uint32_t)nr + np + ns) * 16u;
    if (n < need) { g_deploy_nak++; nak("payload short"); return; }
    const uint8_t *d = p + 6;
    const uint8_t *pd = d + (size_t)nr * 16u;

    /* ① 参数有限性: NaN/Inf 会经 DIRECT/SCALE/积分直接传播成 NaN 输出。
     *    只查每字的高 8 位全 1 (即指数 0xFF) —— 不用浮点比较, 也不依赖 FPU 状态。 */
    for (uint16_t i = 0; i < (uint16_t)(np * 4u); i++) {
        if (((get32(pd + (size_t)i * 4u) >> 23) & 0xFFu) == 0xFFu) {
            g_deploy_nak++; nak("param not finite"); return;
        }
    }

    /* ② 逐条校验 + dst 唯一写者 (两条路由写同一个 wire = 结果取决于表序, 非确定性) */
    uint64_t dst_seen[2] = { 0, 0 };
    for (uint16_t i = 0; i < nr; i++) {
        RouteEntry_t r;
        memcpy(&r, d + (size_t)i * 16u, 16u);
        if (!(r.flags & ROUTE_FLAG_ACTIVE)) continue;
        const char *err = engine_route_validate(&r);
        if (err) { g_deploy_nak++; nak(err); return; }
        if (r.op == OP_LPF) {                       /* v0.2: LPF 参数是时间常数 τ 秒, 必须 > 0 */
            uint32_t tb = get32(pd + (size_t)r.param_idx * 16u);
            if ((tb & 0x7FFFFFFFu) == 0u) { g_deploy_nak++; nak("lpf tau must be >0"); return; }
        }
        uint64_t bit = 1ULL << (r.dst_channel & 63u);
        if (dst_seen[r.dst_channel >> 6] & bit) { g_deploy_nak++; nak("dst conflict"); return; }
        dst_seen[r.dst_channel >> 6] |= bit;
    }

    /* ③ 预算门: 条数限制 ≠ 成本限制 —— 128 条 PID 是 128 条, 成本却是 DIRECT 的 2.6 倍。
     *    Σ ceil((op_cost+src_cost)/div倍率) 必须 ≤ EXEC_DEPLOY_BUDGET,
     *    否则放行的程序会把拍吃掉 (阶段 2 审计真的踩到过一次 102.9% 超载)。 */
    uint32_t budget = engine_prog_budget(d, nr);
    g_deploy_budget = budget;
    if (budget > EXEC_DEPLOY_BUDGET) { g_deploy_nak++; nak("exec budget exceeded"); return; }

    /* ④ 装载 STAGING (不碰 ACTIVE) → 置 RELOAD 让 ISR 在下一拍原子切换 */
    uint16_t nw = engine_stage_program(g_shm, d, nr, np, ns);
    g_deploy_routes = nw;
    g_deploy_seq++;
    SHM_U16(g_shm, OFF_CTRL_DEPLOY_SEQ) = (uint16_t)g_deploy_seq;
    g_deploy_set_tick = g_tick_count;
    __asm__ volatile("dsb" ::: "memory");   /* ARM: dsb (S3 的 Xtensa `memw` 在 ARM 上不存在) */
    SHM_U8(g_shm, OFF_CTRL_RELOAD) = 1;             /* 单字节写 = 原子 */
    __asm__ volatile("dsb" ::: "memory");   /* ARM: dsb (S3 的 Xtensa `memw` 在 ARM 上不存在) */
    g_deploy_ok++;

    uint8_t r[6];
    r[0] = (uint8_t)(g_deploy_seq); r[1] = (uint8_t)(g_deploy_seq >> 8);
    put32(r + 2, budget);
    ack(r, 6);
}

/* 0x38 ENGINE_STATUS — **前 31 字节与 S3 逐字节同布局** (上位机脚本零改动),
 * 尾部追加 H723 扩展 6B (S3 的"尾部追加保前段兼容"惯例)。
 *
 * ★ 数据源: 本平台的统计量住在 DTCM 的 C 全局里 (g_*), 不在 SHM 计时区 ——
 *   所以这里**按需打包**, 而不是让 ISR 每拍去维护第二份 SHM 计时块。
 *   理由: 每拍多写 8~10 个 SHM 字段会给 ISR 加成本, 而 ISR 成本是阶段 1/2
 *   基线的一部分, 不该为一个"被轮询才需要"的视图付每拍的代价。 */
static void h_engine_status(void)
{
    uint8_t r[37];
    uint32_t pn = (g_per_cyc_min == 0xFFFFFFFFu) ? 0u : g_per_cyc_min;
    uint32_t en = (g_isr_cyc_min == 0xFFFFFFFFu) ? 0u : g_isr_cyc_min;
    uint16_t nr = (uint16_t)g_active_routes;
    put32(r + 0,  g_isr_n);      /* samples */
    put32(r + 4,  pn);           /* period_min */
    put32(r + 8,  g_per_cyc_max);/* period_max */
    put32(r + 12, en);           /* exec_min   */
    put32(r + 16, g_isr_cyc_max);/* exec_max   */
    r[20] = (uint8_t)(nr); r[21] = (uint8_t)(nr >> 8);
    r[22] = (uint8_t)g_engine_gate;
    put32(r + 23, g_shm_addr);   /* SHM 地址 (供上位机发现) */
    put32(r + 27, 0u);           /* overrun: H723 暂未实现超预算计数 (列未决) */
    /* ---- H723 尾部扩展: 部署生效确认 ---- */
    r[31] = (uint8_t)(g_deploy_seq);  r[32] = (uint8_t)(g_deploy_seq >> 8);
    r[33] = (uint8_t)(g_applied_seq); r[34] = (uint8_t)(g_applied_seq >> 8);
    r[35] = (uint8_t)(g_reload_lat);  r[36] = (uint8_t)(g_reload_lat >> 8);
    ack(r, 37);
}

/* ══════════ deploy 自检 (阶段 3.2) ══════════
 * 为什么需要它: CH340 还没接线, 无法从 PC 侧验证 deploy。自检把**同一个 h_deploy**
 * 用合成载荷驱动一遍 —— 验证的是真代码路径, 不是复制一份逻辑出来单独测。
 * ★ 分工要说清楚: 自检**证不了**"帧能收对"(那是 transport 的事, 已单独验证);
 *   它证的是"载荷合法时能部署、载荷错误时**逐类**被正确拒绝"。
 * ★ 判据全部可失败: 每例都对比调用前后的 (ok, nak) 计数器 —— 只数成功会让
 *   "全部被拒"看起来像"没部署过"; 只数失败会让"全部放行"看起来像"很严格"。 */
#define DSELFTEST_CASES 9
OBS uint32_t g_dst_case[DSELFTEST_CASES];   /* 每例: 0=未跑 1=符合预期 2=不符 */
OBS uint32_t g_dst_done = 0;

static inline void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }

static void put_route(uint8_t *d, uint8_t op, uint8_t st, uint8_t si, uint8_t dch,
                      uint8_t div, uint16_t pidx, uint16_t soff, uint16_t w2, uint8_t flags)
{
    memset(d, 0, 16);
    d[0] = st;  d[1] = si;  d[2] = DST_WIRE;  d[3] = dch;
    d[4] = op;  d[5] = flags;
    put16(d + 6, pidx);  put16(d + 8, soff);  put16(d + 10, 0);  put16(d + 12, w2);
    d[14] = div;  d[15] = 0;
}

/* 构造"例 0 的合法载荷": NR 条三档 DIRECT + NP×4 个有限浮点参数。 */
static void ds_build_valid(uint8_t *buf, uint16_t NR, uint16_t NP, uint16_t NS)
{
    memset(buf, 0, 6u + ((size_t)NR + NP + NS) * 16u);
    put16(buf, NR); put16(buf + 2, NP); put16(buf + 4, NS);
    for (uint16_t i = 0; i < NR; i++)
        put_route(buf + 6 + (size_t)i * 16u, OP_DIRECT,
                  (uint8_t)(i % 3), (uint8_t)(i % 64), (uint8_t)(i % MAX_WIRES),
                  (uint8_t)(i % 3), (uint16_t)(i % NP), 0, 0, ROUTE_FLAG_ACTIVE);
    for (int i = 0; i < NP * 4; i++)
        put32(buf + 6 + (size_t)NR * 16u + (size_t)i * 4u, 0x3F800000u);   /* 1.0f */
}

static void deploy_selftest(void)
{
    static uint8_t buf[6 + (MAX_ROUTES + 8 + 8) * 16u];    /* 2310 B, 静态区不占栈 */
    const uint16_t NR = MAX_ROUTES, NP = 8, NS = 8;
    const uint32_t len = 6u + ((uint32_t)NR + NP + NS) * 16u;
    uint32_t ok0, nak0;

    for (int i = 0; i < DSELFTEST_CASES; i++) g_dst_case[i] = 0;

    /* ---- 例 0: 合法三档程序 → 必须受理 ---- */
    ds_build_valid(buf, NR, NP, NS);
    ok0 = g_deploy_ok; nak0 = g_deploy_nak;
    h_deploy(buf, len);
    g_dst_case[0] = (g_deploy_ok == ok0 + 1u && g_deploy_nak == nak0) ? 1u : 2u;

    /* ---- 例 1: dst 冲突 (路由1 的 dst_channel 改成与路由0 相同) ----
     * 两条路由写同一个 wire → 结果取决于表序 = 非确定性, 必须下载期拒绝 */
    ds_build_valid(buf, NR, NP, NS);
    buf[6u + 16u + 3u] = buf[6u + 3u];
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, len);
    g_dst_case[1] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 2: SRC_HMI → 必须拒 (H723 未实现该源, 放行 = 静默给恒 0 的假信号) ---- */
    ds_build_valid(buf, NR, NP, NS);
    buf[6u + 0u] = (uint8_t)SRC_HMI;
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, len);
    g_dst_case[2] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 3: PID(stateful) 挂 0 号 state 槽 (= "无槽") → 必须拒 (ISR 会传 NULL) ---- */
    ds_build_valid(buf, NR, NP, NS);
    buf[6u + 4u] = (uint8_t)OP_PID;          /* op */
    put16(buf + 6u + 8u, 0u);                /* state_offset = 0 */
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, len);
    g_dst_case[3] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 4: div=3 (掩码外) → 必须拒 ---- */
    ds_build_valid(buf, NR, NP, NS);
    buf[6u + 14u] = 3u;
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, len);
    g_dst_case[4] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 5: 非法 op (0x1F) → 必须拒 ---- */
    ds_build_valid(buf, NR, NP, NS);
    buf[6u + 4u] = 0x1Fu;
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, len);
    g_dst_case[5] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 6: 参数非有限 (指数全 1) → 必须拒 (NaN 会经运算传播成坏输出) ---- */
    ds_build_valid(buf, NR, NP, NS);
    put32(buf + 6u + (size_t)NR * 16u, 0x7F800000u);   /* +Inf */
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, len);
    g_dst_case[6] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 7: 载荷长度不足 (声称 128 条却只给 4 字节) → 必须拒 ---- */
    ds_build_valid(buf, NR, NP, NS);
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, 10u);
    g_dst_case[7] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 8: 全 NONACTIVE → 受理, 但写入 **0 条**
     *   (边界: "空程序"是合法的 —— 是停止运行的手段, 不该被当成错误) ---- */
    ds_build_valid(buf, NR, 0u, 0u);
    for (uint16_t i = 0; i < NR; i++) buf[6u + (size_t)i * 16u + 5u] = 0u;   /* flags 清 ACTIVE */
    ok0 = g_deploy_ok; nak0 = g_deploy_nak;
    h_deploy(buf, 6u + (uint32_t)NR * 16u);
    g_dst_case[8] = (g_deploy_ok == ok0 + 1u && g_deploy_nak == nak0 && g_deploy_routes == 0u)
                    ? 1u : 2u;

    g_dst_done = 1;
}

static void proto_dispatch(uint8_t cmd, const uint8_t *p, uint32_t n)
{
    g_cmd_count++;
    g_cmd_last = cmd;
    switch (cmd) {
        case CMD_GET_VERSION:   h_get_version(); break;
        case CMD_DEPLOY:        h_deploy(p, n); break;
        case CMD_ENGINE_STATUS: h_engine_status(); break;
        /* ★ 未实现的命令**显式拒绝**(NAK 带原因), 而不是静默丢弃或假装成功。
         *   静默丢弃的后果是 PC 端只能看到 TIMEOUT —— 分不清"固件挂了"还是
         *   "这命令没实现", 正是 S3 审计里 N2 记录过的那类缺陷。 */
        default: nak("bad cmd"); break;
    }
}

/* 主循环调用: 排空串口 → 喂解析器 → 完整帧则分发 */
static void proto_poll(void)
{
    uint8_t b;
    while (uart1_rx_pop(&b)) {
        g_uart_rx_bytes++;
        int r = fp_feed(&s_parser, b);
        if (r == 1) {
            g_frame_ok++;
            /* 自检期只计数**不分发** —— 否则回环收到的请求会被再次应答,
             * TX→RX 再 TX… 变成回声风暴 */
            if (s_selftest_active) { g_selftest_frames++; continue; }
            proto_dispatch(s_parser.cmd, s_parser.payload, s_parser.payload_len);
        } else if (r < 0) {
            g_frame_bad++;
        }
    }
    g_uart_ore  = uart1_ore_count();
    g_uart_drop = uart1_drop_count();
}

/* 回环自检 (需 PA9↔PA10 短接): 发一帧 PC→MCU 请求, 看它能不能从 RX 回来并被
 * CRC 校验通过。这是**唯一能证明 RX 通路 + CRC + 解析器真的工作**的内部手段;
 * 配合 LA 抓 TX 波形, 形成"外部确证发出去的字节 + 内部确证收回来的字节"闭环。 */
static void proto_selftest(void)
{
    uint8_t req[6];
    req[0] = FRAME_SYNC_PC2MCU;
    req[1] = CMD_GET_VERSION;
    req[2] = 0; req[3] = 0;                        /* 无载荷 */
    uint16_t crc = crc16_ccitt(req + 1, 3);
    req[4] = (uint8_t)(crc & 0xFFu);
    req[5] = (uint8_t)(crc >> 8);

    s_selftest_active = 1;
    uart1_write(req, 6);
    uint32_t t0 = g_tick_count;                    /* 6B@115200 ≈ 0.52ms → 3ms 上限足够 */
    while ((g_tick_count - t0) < 30u) {
        proto_poll();
        if (g_selftest_frames) break;
    }
    s_selftest_active = 0;
    g_selftest_state = g_selftest_frames ? 1u : 2u;
}

/* ══════════ 观测变量锚定 —— 结构性防"被回收" ══════════
 * ★ 已踩过**三次**的同一个坑: 只被静态初始化、代码里无人读也无人写的全局,
 *   会被 -fdata-sections + --gc-sections 整段回收 → 从符号表消失 → 外部读不到。
 *     ① 阶段 2: g_isr_itcm     (第一版只在声明处初始化)
 *     ② 阶段 3.1: g_selftest_state (UART_SELFTEST=0 时无人写)
 *     ③ 阶段 3.1: g_banner_count   (BOOT_BANNER=0 && 周期=0 时无人写)
 *   逐个人工绕不可持续 —— 这里做**一次统一锚定**: 把每个观测变量读一遍。
 *   读操作是 volatile 的, 链接器看得见引用, 于是一个都不会被回收。
 *   ★ 纪律: **新增观测变量必须在这里加一行**, 否则它可能悄悄从符号表消失。 */
static void obs_anchor(void)
{
    volatile uint32_t sink = 0;
    sink ^= (uint32_t)g_boot_status;      sink ^= (uint32_t)g_stage;
    sink ^= g_tick_count;                 sink ^= g_clock_hclk;
    sink ^= g_isr_itcm;                   sink ^= g_shm_ok;
    sink ^= g_shm_addr;                   sink ^= g_scan_itcm_addr;
    sink ^= g_scan_flash_addr;            sink ^= g_reinit_done;
    sink ^= g_table_ck;                   sink ^= g_active_routes;
    sink ^= g_guard_ok;                   sink ^= g_guard_bad_off;
    sink ^= g_bucket_ck;                  sink ^= g_bucket_zero_slots;
    sink ^= g_engine_gate;                sink ^= g_engine_sel;
    sink ^= g_n_routes;                   sink ^= g_table_profile;
    sink ^= g_reinit;                     sink ^= g_stat_reset;
    sink ^= g_pa9_enable;                 sink ^= g_pa9_div;
    sink ^= g_eng_ck;                     sink ^= g_eng_sel_used;
    sink ^= g_eng_n_used;                 sink ^= g_eng_cyc_last;
    sink ^= g_eng_cyc_min;                sink ^= g_eng_cyc_max;
    sink ^= (uint32_t)g_eng_cyc_sum;      sink ^= g_eng_n;
    sink ^= g_eng_div0;                   sink ^= g_eng_routes_last;
    sink ^= (uint32_t)g_eng_routes_total; sink ^= g_eng_ticks;
    sink ^= g_isr_cyc_last;               sink ^= g_isr_cyc_min;
    sink ^= g_isr_cyc_max;                sink ^= (uint32_t)g_isr_cyc_sum;
    sink ^= g_isr_n;                      sink ^= g_per_cyc_last;
    sink ^= g_per_cyc_min;                sink ^= g_per_cyc_max;
    sink ^= g_per_prev;                   sink ^= g_dwt_overhead;
    sink ^= g_cal_n1000;                  sink ^= g_icache_req;
    sink ^= g_icache_on;                  sink ^= g_ccr_before;
    sink ^= g_ccr_after;                  sink ^= g_scan_mode;
    sink ^= g_uart_brr;                   sink ^= g_uart_rx_bytes;
    sink ^= g_uart_tx_bytes;              sink ^= g_uart_ore;
    sink ^= g_uart_drop;                  sink ^= g_frame_ok;
    sink ^= g_frame_bad;                  sink ^= g_cmd_count;
    sink ^= g_cmd_last;                   sink ^= g_nak_count;
    sink ^= g_banner_count;               sink ^= g_selftest_state;
    sink ^= g_selftest_frames;
    sink ^= g_deploy_ok;                  sink ^= g_deploy_nak;
    sink ^= g_deploy_routes;              sink ^= g_deploy_budget;
    sink ^= g_deploy_seq;                 sink ^= g_applied_seq;
    sink ^= g_reload_count;               sink ^= g_reload_cyc;
    sink ^= g_reload_lat;                 sink ^= g_deploy_set_tick;
    sink ^= g_dst_case[0];                sink ^= g_dst_case[DSELFTEST_CASES - 1];
    sink ^= g_dst_done;
    (void)sink;                            /* 只要求"被引用", 不要求有意义的和 */
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
#if PA9_MODE == 0
    pin_out_init(UARTT_PORT, UARTT_BIT);
#endif
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

    /* ⑥ 协议层 (阶段 3.1): USART1 + 帧解析
     *    ★ 放在拍之后: 横幅/自检要能观察到 g_tick_count 在走 (证明拍没被串口拖死) */
    fp_init(&s_parser);
    uart1_init(UART_PCLK2, UART_BAUD);
    g_uart_brr = uart1_brr();
    g_stage = 10;
#if BOOT_BANNER
    proto_banner();
#endif
#if UART_SELFTEST
    proto_selftest();          /* 内部会置 1(通过) / 2(失败) */
#else
    /* ★ 显式写一次: 只有静态初值、代码里无人读也无人写的全局会被 --gc-sections
     *   整段回收 (实测: 第一版 g_selftest_state 从符号表消失了 —— 与阶段 2 的
     *   g_isr_itcm 同款陷阱, 属"本项目已知族谱"里的一个)。 */
    g_selftest_state = 3;      /* 未启用 */
#endif
    /* 阶段 3.2: deploy 自检 (用合成载荷驱动**同一个** h_deploy 代码路径) */
#if DEPLOY_SELFTEST
    deploy_selftest();
#else
    g_dst_done = 3;            /* 未启用 (同样必须显式写一次, 否则会被回收) */
#endif
    g_stage = 11;

    /* ⑦ 统一锚定全部观测变量 (防 --gc-sections 回收; 见 obs_anchor 注释) */
    obs_anchor();

    for (;;) {
        /* 协议轮询: 排空串口 → 解析 → 分发 (命令执行在主循环, 不在中断里) */
        proto_poll();
#if BANNER_PERIOD_MS > 0
        {
            static uint32_t last = 0;
            if ((g_tick_count - last) >= (BANNER_PERIOD_MS * 10u)) {   /* 100μs/拍 */
                last = g_tick_count;
                proto_banner();
            }
        }
#endif
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
