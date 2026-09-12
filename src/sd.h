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

/* 初始化结果 (诊断用) */
extern volatile uint32_t g_sd_init_stage;   /* 卡在识别的哪一步停住 */
extern volatile uint32_t g_sd_status;       /* 最近一次 SDMMC_STA 快照 */
extern volatile uint32_t g_sd_rca;          /* 卡的 RCA (CMD3 返回值) */
extern volatile uint32_t g_sd_blocks;       /* 写成功的块数 */

int  sd_init(void);                                  /* 0 = 成功 */
int  sd_write_block(uint32_t lba, const uint8_t *buf);  /* CMD24 单块写 */
int  sd_write_multi(uint32_t lba, const uint8_t *buf, uint32_t nblk);  /* CMD25 多块写 */
int  sd_read_block(uint32_t lba, uint8_t *buf);         /* CMD17 单块读 */
void sd_dump_blackbox(void);                         /* 把 AXI 黑匣子缓冲写进卡 */
void sd_set_perf(uint32_t elapsed_ticks);            /* 回填落盘耗时 (拍) 供诊断区 */

#endif /* DCL_SD_H */
