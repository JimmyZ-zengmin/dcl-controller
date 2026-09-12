/* rtc.c — RTC 实时时钟 (P3: 事件时间戳, 2026-09-12)
 *
 * ★ RTC 在 D3 域 (VDD 供电, 不受系统复位/深睡眠影响)。
 *   日历一旦初始化, 后续 reset 只要在 VDD 范围内 ⇒ 时间保持。
 *   通过 RTC_ISR.INITS (bit4) 判断: =1 说明日历已在跑 (跳过重新初始化)。
 *
 * ★ 关键时序:
 *   ① PWR_CR1.DBP=1 (解除备份域写保护) 必须在所有 BDCR/RTC 写之前
 *   ② RCC_BDCR.LSEON + 等 LSERDY (LSE 起振 ~2s, 但首次冷启动只需 ~500ms)
 *   ③ RTCSEL=01(LSE) + RTCEN=1 — 只改这两字段, 不碰 LSEON/LSERDY
 *   ④ WPR 写保护解锁 (0xCA→0x53) 后才能写 ISR/PRER/TR/DR
 *
 * ★ 观测面: SSR/TR/DR 三个寄存器的**值**拷进 SHM (pyocd 直接读)。
 *   SSR 是 16 位向下计数器 (1024Hz), 差值 = 事件间精确时间。 */
#include "rtc.h"
#include "engine.h"
#include "regs.h"

#define RTC_R_BASE   0x58004000u
#define RCC_BDCR_A   0x58024470u
#define RTC_TR_R     REG32(RTC_R_BASE + 0x00)
#define RTC_DR_R     REG32(RTC_R_BASE + 0x04)
#define RTC_ISR_R    REG32(RTC_R_BASE + 0x0Cu)
#define RTC_PRER_R   REG32(RTC_R_BASE + 0x10u)
#define RTC_WPR_R    REG32(RTC_R_BASE + 0x24u)
#define RTC_SSR_R    REG32(RTC_R_BASE + 0x28u)
#define RCC_BDCR_R   REG32(RCC_BDCR_A)

static volatile uint8_t s_rtc_ready = 0;
static volatile uint8_t *s_rtc_base = 0;   /* SHM 基址 (rtc_init 传入) */

void rtc_init(uint8_t *base)
{
    uint32_t tr0, dr0, isr0, bdcr0;
    volatile uint32_t *dg = (volatile uint32_t *)(base + OFF_RTC_DIAG);
    s_rtc_base = base;

    /* ⓪ ★ 先读一眼"马上要被我们覆盖掉的东西" —— 这是"日历到底有没有在走"的直接证据。
     *   如果 tr0 是个像样的值而我们又把 TR 写成 0, 那就是我们自己把时间清了。
     *   bdcr0 = **我们动任何寄存器之前**的 BDCR: 上一次上电留下的 RTCEN/LSEON 还在不在,
     *   一句话判死"备份域是否跨复位保留"。 */
    tr0   = RTC_TR_R;
    dr0   = RTC_DR_R;
    bdcr0 = RCC_BDCR_R;

    /* ① DBP=1: 解除备份域写保护 (PWR_CR1 bit8) */
    PWR_CR1 |= (1u << 8);
    __asm__ volatile("dsb" ::: "memory");

    /* ② LSE 使能 + 等 LSERDY
     * ★ LSE 晶振起振时间: 冷启动几百 ms ~ 2s。首版超时 2000000 循环 (~几 ms)
     *   远远不够 ⇒ 超时后 LSE 还没 ready ⇒ 后续写 RTCSEL/RTCEN 被硬件忽略
     *   (实测 BDCR 读回 RTCSEL=0 RTCEN=0 ⇒ RTC 不走, SSR 恒 0)。
     *   修法: 超时加大到 500000000 (~2 秒 @400MHz), 或者下面幂等重写兜底。 */
    RCC_BDCR_R |= (1u << 0);                              /* LSEON */
    { uint32_t g = 0; while (!(RCC_BDCR_R & (1u << 1)) && ++g < 500000000u) { } }

    /* ③ RTC 时钟源 = LSE (RTCSEL=01, 位域见官方头 [9:8]) + RTCEN(bit15)
     * ★★ 只在 RTCEN=0 时才写 RTCSEL (2026-09-12)。
     *   原实现**每次上电都无条件重写整个 BDCR (含 RTCSEL)**, 而上电时 RTCEN 很可能
     *   已经是 1 —— RM0433 规定 RTCSEL 只能在 RTCEN=0 时写。这一笔轻则被忽略,
     *   重则把备份域搞掉。
     *   ★ 位域已逐条对过官方 stm32h723xx.h: LSEON=0 LSERDY=1 RTCSEL=[9:8]
     *     RTCEN=15 BDRST=VSWRST=16 (代码从不碰它)。
     *   ★ 实测指纹: `TR 精确等于上电时长` (109s/109.3s 一次; 环 dump 64.8s/63s)
     *     ⇒ 日历每次上电都被重建, "绝对时间"退化成"上电后几秒"。 */
    if (!(RCC_BDCR_R & (1u << 15))) {
        RCC_BDCR_R = (RCC_BDCR_R & ~(3u << 8)) | (1u << 8) | (1u << 15);
        __asm__ volatile("dsb; isb" ::: "memory");
        if (!(RCC_BDCR_R & (1u << 15))) {      /* 写被忽略 (LSE 还没 ready) ⇒ 幂等重写 */
            RCC_BDCR_R = (RCC_BDCR_R & ~(3u << 8)) | (1u << 8) | (1u << 15);
            __asm__ volatile("dsb; isb" ::: "memory");
        }
    }

    /* ④ 判定"日历是否已经在跑" —— ★★ **不用 INITS**。
     *   ★ 实测现场 (2026-09-12, 本文件写的 RTC 诊断区 OFF_RTC_DIAG):
     *       上电时 BDCR = 0x00008103  (LSEON=1 | LSERDY=1 | RTCSEL=01 | **RTCEN=1**)
     *       RTC_ISR     = 0x00000027  (**RSF=1** ⇒ 影子寄存器每秒都在更新, 日历在走)
     *       而被清之前的 TR = 0x52 (BCD 52 秒) —— **日历里本来有值, 是这行代码清的**
     *       INITS(bit4) = **0**
     *     ⇒ 备份域跨复位保住了、日历一直在走, 但 **INITS 读回 0**。
     *       **INITS 在本次实测里不是可用的"已在跑"判据**, 照它判就把时间清零了。
     *   ★ 改用**直接证据**: RTCEN=1 且 RTCSEL=01 ⇒ 时钟源在 + RTC 使能 ⇒ 不初始化;
     *     再加一条 TR 的 BCD 合法性 (防"RTCEN 被置了但从没初始化过"的垃圾值)。 */
    isr0 = RTC_ISR_R;
    dg[0] = bdcr0;                            /* 我们动手之前的 BDCR */
    dg[1] = isr0;                             /* 判据现场的 ISR 原文 */
    dg[2] = tr0;                              /* ★ 被清之前的 TR (日历到底有没有在走) */
    {   uint32_t bd = RCC_BDCR_R;
        uint32_t rtc_armed = ((bd >> 15) & 1u) && (((bd >> 8) & 3u) == 1u);
        uint32_t tr_sane = (((tr0 >> 16) & 0xFu) <= 9u) && (((tr0 >> 20) & 0x3u) <= 2u)
                        && (((tr0 >> 8) & 0xFu) <= 9u) && (((tr0 >> 12) & 0x7u) <= 5u)
                        && ((tr0 & 0xFu) <= 9u) && (((tr0 >> 4) & 0x7u) <= 5u);
        dg[3] = (rtc_armed && tr_sane) ? 1u : 2u;   /* 1 = 复用旧日历 / 2 = 全新初始化 */
        if (dg[3] == 1u) { s_rtc_ready = 1; rtc_snapshot(); return; }
    }

    /* ⑤ 全新初始化 (首次上电或备份域完全掉电) */
    RTC_WPR_R = 0xCAu;  RTC_WPR_R = 0x53u;               /* WPR 解锁 */
    RTC_ISR_R |= (1u << 7);                               /* INIT=1 进初始化模式 */
    { uint32_t g = 0; while (!(RTC_ISR_R & (1u << 6)) && ++g < 2000000u) { } } /* 等 INITF */
    /* PRER: ck_spre = 32768/(127+1)/(255+1) = 1Hz ✓, SSR = 32768/128 = 256Hz */
    RTC_PRER_R = (127u << 16) | 255u;
    /* ★★ 不无条件清零 (2026-09-12)。
     *   原实现每次都写 TR=0/DR=0x00000100 ⇒ **顺便把日历里已有的时间抹掉了**,
     *   哪怕它本来带着一个有效值。现在: 只有当 DR 还是"从未设过的哨兵"(0 或本默认值)
     *   才用默认; 否则**连 TR 一起保留** ⇒ 跨复位不回到 00:00:00。
     *   ★ 诚实的边界: 没有备份电池时, 掉电期间时间不走, 复位后从原值继续
     *     (即"冻结"而非"归零") —— 比归零好得多, 但也不是真实绝对时间。 */
    if (dr0 == 0u || dr0 == 0x00000100u) {
        RTC_DR_R = 0x00000100u;                          /* 2000-01-01 (BCD, 未设哨兵) */
        RTC_TR_R = 0x00000000u;
    } else {
        RTC_DR_R = dr0;                                  /* 先日期后时间 (写 DR 可能触发锁存) */
        RTC_TR_R = tr0;
    }
    RTC_ISR_R &= ~(1u << 7);                              /* INIT=0 退出, 日历开始走 */
    RTC_WPR_R = 0xFFu;                                    /* WPR 锁回 */
    __asm__ volatile("dsb" ::: "memory");
    s_rtc_ready = 1;
    rtc_snapshot();
}

/* ★★ 让 SHM 镜像**活着** (2026-09-12) —— 这是"绝对时间进记录"的前置条件。
 *
 * 问题: 原来 `rtc_snapshot()` 只被 rtc_init() 调过两次 (第 56/73 行), 也就是说
 *   SHM 里的 SSR/TR/DR 是**上电瞬间的快照**, 之后一个字都不变 —— 典型的
 *   "写一次就算"的量。黑匣子若直接记它, 每条记录都会是同一个常数,
 *   **绝对时间等于没记**。实测指纹: 隔 5 秒读两次, TR 一模一样。
 * ⇒ 从拍中断里周期调用 rtc_latch() (自带降频), 镜像随真实时间走。
 *
 * ★ 影子寄存器读协议 (RM0433): TR/DR 在秒边界会**撕裂** —— 读 TR 之后、读 DR
 *   之前若跨过秒边界, 两者就不属于同一时刻 (最坏在 23:59:59→00:00:00 差一天)。
 *   正确姿势: 读 TR → 读 DR → 再读 TR; 两次 TR 相同才算一致, 否则重来。
 *   本函数按此实现 (最多试 2 次; 第二分辨率下撕裂概率极低, 试不中也不会更差)。 */
#define RTC_LATCH_DIV 64u           /* 10kHz/64 = 156Hz ⇒ 秒级分辨率远远够, 成本可忽略 */

static uint32_t s_latch_div = 0;

void rtc_latch(void)
{
    if (!s_rtc_ready) return;
    if ((s_latch_div++ & (RTC_LATCH_DIV - 1u)) != 0u) return;   /* 降频 (绝大多数拍只花几拍) */
    rtc_snapshot();
}

void rtc_snapshot(void)
{
    uint32_t tr1, tr2, dr, i;
    if (!s_rtc_ready) return;
    volatile uint8_t *b = s_rtc_base;
    for (i = 0; i < 2u; i++) {
        tr1 = RTC_TR_R;
        dr  = RTC_DR_R;                 /* 先 TR 后 DR: 若撕裂, 差一天的那个是 DR */
        tr2 = RTC_TR_R;
        if (tr1 == tr2) break;          /* 两次 TR 一致 ⇒ 没跨秒边界 */
        dr = RTC_DR_R;                  /* 跨了 ⇒ 重读日期, 再用 tr2 */
    }
    *(volatile uint32_t *)((volatile uint8_t *)b + OFF_RTC_SSR) = RTC_SSR_R & 0xFFFFu;
    *(volatile uint32_t *)((volatile uint8_t *)b + OFF_RTC_TR)  = tr2;
    *(volatile uint32_t *)((volatile uint8_t *)b + OFF_RTC_DR)  = dr;
}

uint32_t rtc_ssr(void)
{
    if (!s_rtc_ready) return 0;
    return RTC_SSR_R & 0xFFFFu;
}

void event_log(uint32_t code)
{
    if (!s_rtc_ready) return;
    uint32_t ssr = RTC_SSR_R & 0xFFFFu;
    uint32_t head = *(volatile uint32_t *)(s_rtc_base + OFF_EVT_HEAD);
    volatile uint32_t *slot = (volatile uint32_t *)(s_rtc_base + OFF_EVT_BUF
                                                   + (head & (EVT_RING_SZ - 1u)) * 8u);
    slot[0] = ssr;
    slot[1] = code;
    *(volatile uint32_t *)(s_rtc_base + OFF_EVT_HEAD) = head + 1;
}
