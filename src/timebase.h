#ifndef TIMEBASE_H
#define TIMEBASE_H
/* ═══════════ 生产时基 (2026-09-16) ═══════════
 *
 * ## 为什么要有这个模块
 * 本工程的拍周期/执行周期统计、以及 `flash.c` 的**超时判据**，原本都读 `DWT_CYCCNT`。
 * 而 **DWT 是调试单元**：
 *   · 调试器会话收尾时会**主动清 `DEMCR.TRCENA`** ⇒ CYCCNT 冻结 —— **这是设计行为，不是 bug**：
 *       - pyOCD 维护者（issue #1540）："on disconnect, `DEMCR.TRCENA` is written to 0 … including DWT"
 *       - SEGGER KB：J-Link 同款，理由是"免得 WFI/WFE 省电时留下时钟"
 *   · 且核处于 Debug state（halt）时 CYCCNT **本来就暂停**（ARMv7-M ARM 明文）。
 * ⇒ 把**出货固件的时基**押在它身上，等于让"能不能量准"被一个**外部工具**控制。
 *
 * ★★ 最严重的后果不是"量不准"，是**功能**：
 *   `flash.c` 的 `fl_wait_qw()` 用 `(fl_cyccnt() - t0) > timeout` 判超时。
 *   DWT 一冻，这个差值**永远不推进** ⇒ 超时永不触发 ⇒ 紧跟其后的"有界喂狗"
 *   （只在预算内喂）**退化成无限喂狗** ⇒ **擦除卡死时主循环永不返回、而看门狗被喂着**。
 *   ⇒ 调试器关掉调试单元，能把"flash 超时"变成"永久挂死"。
 *
 * ## 做法
 * 用一个**普通硬件定时器**（TIM5，32 位，自由运行）当生产时基；DWT 降级为**第二条独立路径**
 * （"两条路径读数一致才算数" —— 本项目自己的纪律）。ISR 每拍同时读两者：
 *   · 时基在走 / DWT 不动  ⇒ `g_dwt_dead_n++`  ← **DWT 死活的判据，成本几乎为零**
 *   · 时基不动 / DWT 在走  ⇒ `g_tb_dead_n++`
 *
 * ## 分辨率（诚实交代）
 * | 时基 | 频率 | 分辨率 |
 * |---|---|---|
 * | `DWT_CYCCNT`（对照档） | 400 MHz (CPU) | 2.5 ns |
 * | **`TIM5`（交付档）** | 200 MHz (TIMxCLK) | **5 ns** |
 * 拍周期 ≈ 100 µs ⇒ 相对分辨率 0.005%，够用。但**判据的阈值单位随档而变**：
 * 例如基线的"±64 cyc"在 DWT 档 = ±160 ns、在 TIM5 档 = ±32 tick —— **同一个物理容差**。
 * ⇒ 凡写死"cyc"数字的判据，换档时必须用 `TB_US()/TB_MS()` 或按 `TB_HZ` 换算，**不许照抄**。
 *   （这正是本项目"一个常量两个语义 ⇒ 静默失效"族的又一入口，`FL_ERASE_TIMEOUT_CYC`
 *    原先把 `400000000` 写死在常量里，就是一类。）
 */
#include <stdint.h>
#include "clock.h"
#include "regs.h"

#define TB_KIND_DWT   0u
#define TB_KIND_TIM5  1u

/* ★ A/B 开关：1 档(TIM5)=交付；0 档(DWT)=**改前行为对照**。
 *   ★ CMakeLists 已 `set(... CACHE ...)` 并传 `-D`（不再是有名无实的"可覆盖"）。 */
#ifndef DCL_TIMEBASE
#define DCL_TIMEBASE  TB_KIND_TIM5
#endif

#if DCL_TIMEBASE == TB_KIND_DWT
#  define TB_HZ    (CLK_SYSCLK_HZ)      /* 400 MHz */
#  define TB_NAME  "DWT_CYCCNT"
#else
#  define TB_HZ    (CLK_TIMXCLK_HZ)     /* 200 MHz */
#  define TB_NAME  "TIM5"
#endif

/* 时间 → 时基计数（u32 内；8 s @400MHz = 3.2e9 < 4.29e9 ✓） */
#define TB_US(u)   ((uint32_t)((TB_HZ / 1000000UL) * (u)))
#define TB_MS(ms)  ((uint32_t)((TB_HZ / 1000UL)    * (ms)))

void tb_init(void);

/* ★★ 执行预算: **80 µs**（拍长 100 µs 的 80%）。★ 用**时间**表达, **不写死频率** ——
 *   换时基自动跟随: 0 档(DWT@400MHz) = 32000 cyc; 1 档(TIM5@200MHz) = 16000 tick。
 *   ★ 教训来源: `flash.c` 原来把 `400000000` 写死在超时常量里 ⇒ 换时基必漏。
 *     `main.c` 里有 `_Static_assert` 校验"两档表达同一个物理预算"。 */
#define EXEC_BUDGET_TB   TB_US(80u)

extern volatile uint32_t g_tb_dead_n;      /* 时基不推进而 DWT 在推进的次数 */
extern volatile uint32_t g_dwt_dead_n;     /* ★ DWT 不推进而时基在推进的次数（调试器停的）*/
extern volatile uint32_t g_tb_cyc_last;    /* 最近一次测得的"每拍时基增量"*/

/* ★ 产线时基读数 —— ISR 内可用（纯寄存器读，无函数调用） */
static inline uint32_t tb_cyc(void)
{
#if DCL_TIMEBASE == TB_KIND_DWT
    return DWT_CYCCNT;
#else
    return TIM_CNT(TIM5_BASE);
#endif
}

#endif /* TIMEBASE_H */
