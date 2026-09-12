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
    s_rtc_base = base;
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

    /* ③ RTC 时钟源 = LSE (RTCSEL[3:2]=01) + RTCEN(bit15)
     * ★ 此处写完后**再写一遍** — 如果 LSE 在首次写时还没 ready ⇒ 写被忽略 ⇒
     *   幂等重写确保写进去 (LSERDY=1 后写一定生效)。 */
    RCC_BDCR_R = (RCC_BDCR_R & ~(3u << 8)) | (1u << 8) | (1u << 15);
    __asm__ volatile("dsb; isb" ::: "memory");
    RCC_BDCR_R = (RCC_BDCR_R & ~(3u << 8)) | (1u << 8) | (1u << 15);
    __asm__ volatile("dsb; isb" ::: "memory");

    /* ④ 检查 INITS(bit4): =1 说明日历已在跑 (上次上电初始化过, VDD 未断 ⇒ 保持) */
    if (RTC_ISR_R & (1u << 4)) { s_rtc_ready = 1; rtc_snapshot(); return; }

    /* ⑤ 全新初始化 (首次上电或备份域完全掉电) */
    RTC_WPR_R = 0xCAu;  RTC_WPR_R = 0x53u;               /* WPR 解锁 */
    RTC_ISR_R |= (1u << 7);                               /* INIT=1 进初始化模式 */
    { uint32_t g = 0; while (!(RTC_ISR_R & (1u << 6)) && ++g < 2000000u) { } } /* 等 INITF */
    /* PRER: PREDIV_A=31(32 分频) / PREDIV_S=1023(1024 分频) ⇒ SSR=1024Hz, 日历=1Hz */
    RTC_PRER_R = (127u << 16) | 255u;  /* PREDIV_A=127(7b,异步128分频) + PREDIV_S=255(15b,同步256分频)
                                          * ⇒ ck_spre = 32768/128/256 = 1Hz ✓, SSR = 32768/128 = 256Hz (~3.9ms 分辨率)
                                          * ★ 首版搞反: (1023<<16)|31 ⇒ PREDIV_A=127(截断), PREDIV_S=31
                                          *   ⇒ ck_spre = 256/32 = 8Hz ⇒ 日历 8 倍速走! */
    RTC_TR_R  = 0x00000000u;                             /* 00:00:00 (BCD) */
    RTC_DR_R  = 0x00000100u;                             /* 2000-01-01 (BCD: MU=1) */
    RTC_ISR_R &= ~(1u << 7);                              /* INIT=0 退出, 日历开始走 */
    RTC_WPR_R = 0xFFu;                                    /* WPR 锁回 */
    __asm__ volatile("dsb" ::: "memory");
    s_rtc_ready = 1;
    rtc_snapshot();
}

void rtc_snapshot(void)
{
    if (!s_rtc_ready) return;
    volatile uint8_t *b = s_rtc_base;
    *(volatile uint32_t *)((volatile uint8_t *)b + OFF_RTC_SSR) = RTC_SSR_R & 0xFFFFu;
    *(volatile uint32_t *)((volatile uint8_t *)b + OFF_RTC_TR)  = RTC_TR_R;
    *(volatile uint32_t *)((volatile uint8_t *)b + OFF_RTC_DR)  = RTC_DR_R;
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
