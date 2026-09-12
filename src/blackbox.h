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
 *   [3]      ctrl   run<<24 | n_routes   —— ★ **只有这两个字段**
 *            ★ 踩坑 (2026-09-12): 这里曾写 `run<<24 | n_routes<<16 | seq_step`,
 *              而固件**从没写过** seq_step ⇒ PC 端照它解码, 于是 `routes` 列恒 0、
 *              `seq_step` 列显示的其实是 n_routes。实测 ctrl=0x01000080 + SHM 的
 *              N_ROUTES=128 对撞才暴露。**头文件里写下的字段必须真存在** ——
 *              最阴的不是错值, 是"这个字段根本不存在却解出了数"。

 *   [4..63]  60 个数据槽 —— ★★ **"哪一槽是哪一路通道"由通道映射表决定**。
 *
 * ★★ 通道映射 (2026-09-12): 记录里 4 字头 + 60 数据字 = 正好 64 字 = 256B。
 *   映射表 = 60 个 u32, 每项 `(seg << 16) | idx`:
 *       seg 0 = SENSOR (idx 0..63)   1 = WIRE (idx 0..127)
 *       2 = ACTUATOR (idx 0..63)     3 = 空槽 (恒 0)
 *   表随**日志头**写到卡上 (LBA0 的 h[16..75] + 魔数 h[76] + 校验 h[77]),
 *   PC 端按它标列名 —— 数据自带"这一列是哪一路通道"。
 *
 *   ★ 为什么不是"把记录放大到全部 256 通道": 缓冲条数 = 环字节 / 记录字节,
 *     丢包风险 = "一次 SD 卡内写抖动窗口内产出的条数 > 槽数"。记录 256B→1040B
 *     会让槽数 960→236 (÷4), 同时通道变多活跃度上升、产出也升 —— **双重恶化**:
 *     实测最坏卡顿 220ms, 变化率 15% 时 236 槽只够 157ms ⇒ 丢包立刻回来。
 *     **保持小记录才是鲁棒性来源**; 扩通道靠"选得准", 不靠"装得下"。
 *
 *   ★ 默认映射 = SENSOR[0..15] / WIRE[0..15] / ACTUATOR[0..15] (前 48 槽, 与旧布局
 *     **逐字节相同**) + 12 个"非 I/O 标量"槽: 绝对时间 TR/DR、通信域 5 个量、
 *     DO 影子位图、强制位图 4 字。⇒ 记录不只记"引擎的输入输出", 还能回答
 *     "这是几点 / 通信通不通 / 引脚上是什么电平 / 谁被强制过"。
 */
#ifndef DCL_BLACKBOX_H
#define DCL_BLACKBOX_H

#include <stdint.h>

#define BB_AXI_BASE     0x24004000u   /* AXI 黑匣子区起始 (避开低 16KB) */
#define BB_SLOT_SZ      256u          /* 每拍快照 256B */
#define BB_SLOTS        960u          /* ★ 512->768->960 槽 (2026-09-12)。
                                       *   丢包判据 = "落后量 > 槽数", 而卡顿实测来自
                                       *   **SD 卡自身块编程抖动**([60] 单批最长 68.4ms,
                                       *   30s 内 35 次 >20ms) ⇒ 唯一解法是加大吸收量。
                                       *   960 槽 = 96ms, 占 AXI 240KB, 冻结区 64KB,
                                       *   正好把 0x24004000..0x24050000 用满。 */
#define BB_TOTAL        (BB_SLOT_SZ * BB_SLOTS)  /* 128KB */
#define BB_MAGIC        0x42424B42u   /* "BBKB" (环首字, 旧) */
#define BBLOG_REC_MAGIC 0x4B424C44u   /* "DLBK" 每条记录的魔数 */
#define BB_SEQ_MAGIC    0x51455344u   /* "DSEQ" 序号魔数 */

/* ── 通道映射 ── */
#define BB_MAP_N        60u           /* 记录里的数据槽数 (64 字 - 4 字头) */
#define BB_MAP_SEG_SENSOR 0u
#define BB_MAP_SEG_WIRE   1u
#define BB_MAP_SEG_ACT    2u
#define BB_MAP_SEG_NONE   3u          /* 空槽 (恒 0) */
/* ★★ 以下四段 "非 I/O 量" (2026-09-12): 记录不只有输入输出, 还要能回答
 *   "这是什么时刻 / 通信通不通 / 引脚上的电平是什么 / 谁被强制过"。
 *   它们都指向现有的**权威位置** (RTC 镜像 / MbCtrl_t / DO 影子 / 强制位图),
 *   不新建状态、不复制 —— 避免"两份真值互相漂移"。 */
#define BB_MAP_SEG_TIME   4u          /* idx 0 = RTC_TR(时分秒 BCD), 1 = RTC_DR(年月日 BCD) */
#define BB_MAP_SEG_COMM   5u          /* idx 0=帧数 1=响应数 2=CRC错 3=异常 4=状态机首字 */
#define BB_MAP_SEG_DO     6u          /* idx 0 = DO 打包位图 (真正锁存到引脚的电平) */
#define BB_MAP_SEG_FORCE  7u          /* idx 0..3 = 强制位图 128bit 的 4 个字 */
#define BB_ME(seg, idx) (((uint32_t)(seg) << 16) | (uint32_t)(idx))

/* 卡上日志头里的映射区位置 (头块 512B = 128 字, h[0..15] 是原有字段) */
#define BB_MAP_HDR_OFF  16u           /* 映射表在日志头里的字偏移 */
#define BB_MAP_HDR_MAGIC 0x50414D42u  /* "BMAP" */
#define BB_MAP_HDR_SUM_OFF (BB_MAP_HDR_OFF + BB_MAP_N + 1u)  /* = 77 */

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

/* ★ 当前生效的通道映射 (60 项), 供 sd.c 写进日志头 ⇒ 数据自描述 */
const uint32_t *bb_map(void);
uint32_t bb_map_sum(const uint32_t *map);   /* 映射表校验和 (两端同一算法) */

#endif /* DCL_BLACKBOX_H */

