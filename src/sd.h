/* sd.h — microSD (SDMMC1, 4-bit) 最小驱动 — 2026-09-12
 *
 * 接线 (板商原理图): PC8=D0 PC9=D1 PC10=D2 PC11=D3 PC12=CK PD2=CMD, AF12
 * 时钟源: PLL1Q = 100MHz (RCC_D1CCIPR.SDMMCSEL=0)
 * 用途: 把黑匣子/AXI 缓冲数据写到卡的原始扇区 (不建 FAT, 便于 PC 端直读物理扇区)
 */
#ifndef DCL_SD_H
#define DCL_SD_H

#include <stdint.h>

#define SD_BLK_SZ   512u

/* ══════════ S3 (2026-09-15): SD 分区 —— 日志上限 + 卡尾程序区 ══════════
 * 卡布局 (LBA):
 *      LBA0                = 日志头
 *      LBA1 .. g_sd_log_end = 日志数据区 (环形回卷, **上限受此约束**)
 *      g_sd_prog_a .. +15   = 程序副本 A
 *      g_sd_prog_b .. +15   = 程序副本 B
 * ⇒ **日志从此碰不到程序区**。区间由**容量**算出 (不是硬编码), 保证换卡自洽。
 * ★ 这组量必须能被外部读走 —— 否则"日志有没有被关进笼子"这件事不可观测。 */
#define SD_PROG_BLOCKS        16u            /* 每程序副本的块数: 1 头 + 15 数据 = 8KB */
#define SD_PROG_NEED_BLOCKS   (2u * SD_PROG_BLOCKS)
/* ★ 单一来源: sd.c 用这里的定义, 不许在 .c 里再写一份。 */

extern volatile uint32_t g_sd_log_end;      /* 日志可用的最后 LBA (0 = 未划区, 用满整卡) */
extern volatile uint32_t g_sd_prog_a;       /* 程序副本 A 起始 LBA */
extern volatile uint32_t g_sd_prog_b;       /* 程序副本 B 起始 LBA */
extern volatile uint32_t g_sd_part_ok;      /* 1 = 本卡已划出程序区 */
extern volatile uint32_t g_sd_log_shrink;   /* 1 = 本次上电缩过日志上限 (触发了钳位) */

/* 初始化结果 (诊断用) */
extern volatile uint32_t g_sd_init_stage;   /* 卡在识别的哪一步停住 */
extern volatile uint32_t g_sd_status;       /* 最近一次 SDMMC_STA 快照 */
extern volatile uint32_t g_sd_rca;          /* 卡的 RCA (CMD3 返回值) */
extern volatile uint32_t g_sd_blocks;       /* 写成功的块数 */

int  sd_init(void);                                  /* 0 = 成功 */
int  sd_write_block(uint32_t lba, const uint8_t *buf);  /* CMD24 单块写 */
int  sd_write_multi(uint32_t lba, const uint8_t *buf, uint32_t nblk);  /* CMD25 多块写 */
int  sd_read_block(uint32_t lba, uint8_t *buf);         /* CMD17 单块读 */
void sd_dump_blackbox(void);                         /* 落盘 = 写 + 回读校验 */
void sd_dump_write(void);                            /* 只写 (供吞吐计时) */
void sd_set_perf(uint32_t write_ticks, uint32_t verify_ticks, uint32_t verify_res);

/* ── 每拍连续落盘日志 (专用裸介质, 环形回卷) ── */
int  sd_log_open(void);              /* 读/建头部块, 0 = 成功 */
int  sd_flt_snapshot(void);          /* 把故障台账全景刷进日志头 (上位机 SD_CFG[12] 触发) */
void sd_log_poll(void);              /* 主循环调: 成批冻结 + 追加(回卷)落盘 */
int  sd_reopen_log(void);            /* 上位机触发: 重新初始化 SD + 开日志 (卡插晚了) */
void sd_log_diag_gap(uint32_t gap_ticks, uint32_t inpoll_ticks, uint32_t slow_cnt);
uint32_t sd_cfg_take(uint32_t idx);  /* 取走一次性配置字并清零 */

#endif /* DCL_SD_H */
