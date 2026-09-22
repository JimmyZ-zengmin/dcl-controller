/* blackbox.h — 飞行记录仪 (P3: 每拍 I/O 快照 → AXI 环形缓冲, 2026-09-12)
 *
 * 目标: 记录每一拍的输入(SENSOR)与输出(ACTUATOR/WIRE)快照,
 *       出问题(看门狗复位/HardFault)后 AXI SRAM 数据保持,
 *       新固件启动时读出分析 —— "飞行记录仪"。
 *
 * 存储: ★★★ 2026-09-16 **更正 —— 下面这段原文是过期的, 并因此误导过一次实现**:
 *
 *   原文（错）: "AXI SRAM 320KB @ 0x24000000 (**当前完全空闲**)。
 *                黑匣子区 = 0x24004000 起 **128KB** (避开低 16KB 预留)。
 *                **512 槽** × 256B/拍 = 128KB = 51.2ms 回溯窗口。"
 *   实情: `BB_SLOTS` 已从 512 涨到 **960**（见下方 BB_SLOTS 的注释）⇒ 环 = **240KB**,
 *         从 0x24004000 一直铺到 **0x24040000**, 加上冻结区 SD_STAGE(64KB) 正好占满 AXI。
 *   ★ 后果: 有人（我）照"128KB / 完全空闲"去 0x24024000 找空档放缓冲
 *     ⇒ **落在环里, 每拍被本模块覆写** ⇒ 载荷读回来是 `BBLOG_REC_MAGIC`。白查一整轮。
 *   ⇒ 教训: **"当前完全空闲"这种话在注释里活不过两周**。容量类断言要么写成 `_Static_assert`,
 *     要么集中成一张**唯一的 AXI 地图**（现在放在 `prog_store.c` 顶部, 含每段的起止与占用者）。
 *
 * ★★ AXI 真实地图: **唯一权威源是 `src/memmap.h`**（2026-09-19 收口，本文件不再复制）。
 *   本文件曾在这里维护过一份地图，且有**两个版本**（"128KB/完全空闲" → "已用满"）；
 *   有人照过期的那份去 0x24024000 找空档 ⇒ 落在环里每拍被覆写（读回 `DLBK`）。
 *   ⇒ 教训：容量/地址类事实**只能有一份，且必须能被断言**（见 memmap.h 的平铺断言）。
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
#include "memmap.h"

#define BB_AXI_BASE     AXI_BB_RING    /* 地址唯一源: src/memmap.h（原写死 0x24004000）*/
#define BB_SLOT_SZ      256u          /* 每拍快照 256B */
#define BB_SLOTS        960u          /* ★ 512->768->960 槽 (2026-09-12)。
                                       *   丢包判据 = "落后量 > 槽数", 而卡顿实测来自
                                       *   **SD 卡自身块编程抖动**([60] 单批最长 68.4ms,
                                       *   30s 内 35 次 >20ms) ⇒ 唯一解法是加大吸收量。
                                       *   960 槽 = 96ms **@逐拍全量**; 交付开的是"变化才记"
                                       *   (blackbox.c 的 change-triggered) ⇒ **实测跨度 ~380ms**
                                       *   (变化率 ~25%, 记录率 ~1000/s) ★ 跨度必须按
                                       *   **记录里的 tick 字段**算, 不能按"槽数×拍长"算。
                                       *   占 AXI 240KB, 冻结区 64KB, 正好用满。 */
#define BB_TOTAL        (BB_SLOT_SZ * BB_SLOTS)  /* = 240KB (原注释写 128KB, 已过期) */
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
/* ★ 8 = 故障台账 (faultlog.h, 2026-09-13)。idx 0 = 累计故障数, 1 = 末例分类码。
 *   为什么把它放进**记录流**: 落盘是"变化才记"⇒ 故障计数一变就落一条,
 *   于是"什么时候出的错"被**天然打了 tick 时间戳** —— 这是事后取证最想要的东西。
 *   完整的 24 类计数与首例现场在**日志头快照** (h[78..]) 里, 两者互补:
 *     记录流 = 时间轴(何时), 头快照 = 全景(哪些类、首例现场)。 */
#define BB_MAP_SEG_FAULT  8u
#define BB_ME(seg, idx) (((uint32_t)(seg) << 16) | (uint32_t)(idx))

/* 卡上日志头里的映射区位置 (头块 512B = 128 字, h[0..15] 是原有字段) */
#define BB_MAP_HDR_OFF  16u           /* 映射表在日志头里的字偏移 */
#define BB_MAP_HDR_MAGIC 0x50414D42u  /* "BMAP" */
#define BB_MAP_HDR_SUM_OFF (BB_MAP_HDR_OFF + BB_MAP_N + 1u)  /* = 77 */

/* ---- ★ 故障台账全景快照在**日志头**里的位置 (2026-09-13) ----
 * 放 h[78..111] (34 字): 这是日志头 512B 里**唯一既没被初始化、也不参与任何校验和**
 * 的一段 (头只用 h[0..15] 自身校验 + h[16..77] 映射表) ⇒
 *   ① 加它**不改变任何既有语义** ⇒ **不必升 LOG_VERSION**
 *      (升版本会让 sd_log_open 当新卡重建 ⇒ 从 LBA1 覆写, 毁掉卡上已有记录);
 *   ② 旧读端看不到它 (无害), 新读端按下方偏移解析。
 * 布局: [0]=magic [1]=total [2..25]=24 类计数 [26..29]=首例 [30..33]=末例
 * 刷新: sd_flt_snapshot() —— 由上位机显式触发 (SD_CFG[12]), 符合"别让固件自己猜窗口"。 */
#define BB_FLT_HDR_OFF   78u
#define BB_FLT_HDR_MAGIC 0x464C5431u   /* "FLT1" (按字节序读出来就是 FLT1) */
#define BB_FLT_HDR_WORDS 34u
_Static_assert(BB_FLT_HDR_OFF + BB_FLT_HDR_WORDS <= 128u,
               "BB: fault snapshot must fit in the 512B header block (128 words)");

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

/* ★★ 增量上传环 (DELTA_RING, 2026-09-22) 的冷启动登记入口 —— 见 engine.h 的长注释。
 *   ★ 必须在 `cold_start_reset()` 里被调用（本项目"新增域必须登记到单一入口"的纪律）。
 *   ★ 它只写**掩码默认值**（排除 AI 三路的采样噪声）；环体与游标交给那次 memset。 */
void delta_reset(uint8_t *shm_base);

/* ★ 当前生效的通道映射 (60 项), 供 sd.c 写进日志头 ⇒ 数据自描述 */
const uint32_t *bb_map(void);
uint32_t bb_map_sum(const uint32_t *map);   /* 映射表校验和 (两端同一算法) */

/* 把故障台账全景写进日志头的 h[BB_FLT_HDR_OFF..] (34 字)。
 * 调用点: sd_log_flush_header() (开日志时) + sd_flt_snapshot() (上位机触发刷新)。 */
void bb_flt_into_hdr(uint32_t *h);

#endif /* DCL_BLACKBOX_H */

