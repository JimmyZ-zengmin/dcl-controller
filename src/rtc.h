/* rtc.h — RTC 实时时钟 (P3 遗留项: 事件时间戳, 2026-09-12)
 *
 * LSE 32.768kHz → RTC 日历 + SSR 亚秒计数器
 * 分频: PREDIV_A=31 (32 分频), PREDIV_S=1023 (1024 分频)
 *   ⇒ SSR 频率 = 32768/32/1024 = 1Hz? 不:
 *   SSR 的计数频率 = ck_apre = LSE/(PREDIV_A+1) = 32768/32 = 1024 Hz
 *   ⇒ SSR 分辨率 ≈ 1ms (从 PREDIV_S+1=1024 倒计到 0)
 *   日历频率 = ck_spre = 1024/1024 = 1Hz ✓
 *
 * 事件时间戳机制: 关键事件点调 event_log(code), 记录 (SSR, code) 到 SHM
 * 环形缓冲 (32 条)。pyocd 读 SHM 即可按时间排序查看事件序列。
 */
#ifndef DCL_RTC_H
#define DCL_RTC_H

#include <stdint.h>
/* 事件码 (按需扩充) */
#define EVT_BOOT         0x01u
#define EVT_ENGINE_START 0x02u
#define EVT_ENGINE_STOP  0x03u
#define EVT_DEPLOY       0x04u
#define EVT_PERSIST_SAVE 0x05u
#define EVT_ERROR        0x06u

#define EVT_RING_SZ      32u   /* 环形缓冲条数 (SHM 里 32×8B = 256B) */

void rtc_init(uint8_t *base);
void rtc_snapshot(void);
/* ★ 拍中断里周期调用: 自带降频, 让 SHM 的 SSR/TR/DR 镜像随真实时间走。
 *   (原来镜像只在上电时拍一次 ⇒ 黑匣子记它只会记到常数。) */
void rtc_latch(void);
uint32_t rtc_ssr(void);
void event_log(uint32_t code);

#endif /* DCL_RTC_H */
