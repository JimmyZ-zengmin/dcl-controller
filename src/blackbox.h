/* blackbox.h — 飞行记录仪 (P3: 每拍 I/O 快照 → AXI 环形缓冲, 2026-09-12)
 *
 * 目标: 记录每一拍的输入(SENSOR)与输出(ACTUATOR/WIRE)快照,
 *       出问题(看门狗复位/HardFault)后 AXI SRAM 数据保持,
 *       新固件启动时读出分析 —— "飞行记录仪"。
 *
 * 存储: AXI SRAM 320KB @ 0x24000000 (当前完全空闲)。
 *   黑匣子区 = 0x24004000 起 128KB (避开低 16KB 预留)。
 *   512 槽 × 256B/拍 = 128KB = 51.2ms 回溯窗口。
 *
 * 搬运: MDMA ch1 软触发 (拍尾, 数据就绪后), 源=SHM 紧凑区(DTCM,经SBUS),
 *   目的=AXI 环形缓冲。每拍 CPU ~50cyc (更新 CDAR + EN + SWRQ)。
 *
 * 快照格式 (256B 对齐, MDMA word 传输 × 64) —— **每一条自带"是哪一拍"的标注**:
 *   [0]      magic  "DLBK" (0x4B424C44) —— 让 PC 端不靠对齐也能逐条解析
 *   [1]      tick   —— ★ 这一排数据是哪一拍的 (引擎拍号, 10kHz)
 *   [2]      seq    —— 全局记录序号 (单调递增, 回卷后可凭它找最新)
 *   [3]      ctrl   run<<24 | n_routes<<16 | seq_step
 *   [4..19]  SENSOR[0..15] (f32 × 16)
 *   [20..35] WIRE[0..15] (f32 × 16)
 *   [36..51] ACTUATOR[0..15] (f32 × 16)
 *   [52..63] 预留 (对齐 256B)
 */
#ifndef DCL_BLACKBOX_H
#define DCL_BLACKBOX_H

#include <stdint.h>

#define BB_AXI_BASE     0x24004000u   /* AXI 黑匣子区起始 (避开低 16KB) */
#define BB_SLOT_SZ      256u          /* 每拍快照 256B */
#define BB_SLOTS        512u          /* 512 槽 */
#define BB_TOTAL        (BB_SLOT_SZ * BB_SLOTS)  /* 128KB */
#define BB_MAGIC        0x42424B42u   /* "BBKB" (环首字, 旧) */
#define BBLOG_REC_MAGIC 0x4B424C44u   /* "DLBK" 每条记录的魔数 */
#define BB_SEQ_MAGIC    0x51455344u   /* "DSEQ" 序号魔数 */

#define EVT_BOOT         0x01u
#define EVT_ENGINE_START 0x02u
#define EVT_ENGINE_STOP  0x03u
#define EVT_DEPLOY       0x04u
#define EVT_ERROR        0x06u

void bb_init(uint8_t *shm_base);
void bb_kick(uint32_t tick);           /* 拍尾调: MDMA 发起 256B 快照搬运 */
uint32_t bb_write_idx(void);
uint32_t bb_slots_produced(void);   /* 已产出的快照条数 (单调) */
uint32_t bb_tick_last(void);           /* 最近一次写入的 tick (诊断) */

#endif /* DCL_BLACKBOX_H */
