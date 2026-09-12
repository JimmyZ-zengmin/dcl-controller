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
 * 快照格式 (256B 对齐, MDMA word 传输 × 64):
 *   [0]      tick (u32)
 *   [1..16]  SENSOR[0..15] (f32 × 16)
 *   [17..32] WIRE[0..15] (f32 × 16)
 *   [33..48] ACTUATOR[0..15] (f32 × 16)
 *   [49]     run<<24 | n_routes<<16 | seq_step (u32)
 *   [50..63] 预留 (对齐 256B)
 */
#ifndef DCL_BLACKBOX_H
#define DCL_BLACKBOX_H

#include <stdint.h>

#define BB_AXI_BASE     0x24004000u   /* AXI 黑匣子区起始 (避开低 16KB) */
#define BB_SLOT_SZ      256u          /* 每拍快照 256B */
#define BB_SLOTS        512u          /* 512 槽 */
#define BB_TOTAL        (BB_SLOT_SZ * BB_SLOTS)  /* 128KB */
#define BB_MAGIC        0x42424B42u   /* "BBKB" */

#define EVT_BOOT         0x01u
#define EVT_ENGINE_START 0x02u
#define EVT_ENGINE_STOP  0x03u
#define EVT_DEPLOY       0x04u
#define EVT_ERROR        0x06u

void bb_init(uint8_t *shm_base);
void bb_kick(uint32_t tick);           /* 拍尾调: MDMA 发起 256B 快照搬运 */
uint32_t bb_write_idx(void);
uint32_t bb_tick_last(void);           /* 最近一次写入的 tick (诊断) */

#endif /* DCL_BLACKBOX_H */
