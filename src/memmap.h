/* ══════════════════════════════════════════════════════════════════════════
 * memmap.h — 片上内存的**唯一地址地图**（2026-09-19 立）
 *
 * ## 它是什么
 * 公理⑤（放置公理）：**每块内存恰有一种所有权**（链接器 拥有 **异或** 固定地址 拥有），
 *   且**它的物理性质必须与使用它的代码对时间的假设一致**。
 * 本文件是"固定地址"那一半的**唯一真相源**（不变量 **M1 地址唯一**）：
 *   · 全仓**只允许本文件出现固定地址字面量**（`0x2400xxxx` / ITCM 向量表位）
 *   · 其它文件一律 `#include "memmap.h"` 取名字（判据 C0）
 *
 * ## 为什么必须有（血证）
 * 1. **AXI 地图曾在三处各写一份**（`blackbox.h` / `sd.c` / `prog_store.c`），其中一份过期
 *    ⇒ 有人照过期注释去 `0x24024000` 找空档放缓冲 ⇒ **落在黑匣子环里，每拍被覆写**
 *    （读回载荷头是 `DLBK`）。⇒ 地图只能有一份，且必须**可断言**。
 * 2. `0x24000400` 曾有**两个名字**（`BB_CFG` / `SD_CFG`），前者从未被引用 —— 死的重复名
 *    就是"下一个人用错"的种子。⇒ 不变量 **M5 归属唯一**：一个区一个 owner 一个名字。
 * 3. 链接器曾有一个**空的** `.axi_buf` 段，起点 = `0x24000000` = 诊断区起点
 *    ⇒ 谁往里放东西就静默盖住四个诊断区。⇒ 不变量 **M2 所有权互斥**：已删除该段，
 *    AXI **100% 固定地址拥有**（裁定见 `docs/PLAN-memmap-constitution.md` §2.1）。
 *
 * ## 两类所有权（本文件只描述第二类）
 *   链接器拥有 : SHM(`.dtcm_shm`) · `.itcm_text` · `.itcm_vectors` · `.data/.bss` · 栈
 *                —— 地址由链接器定，固件运行期"发现"它（`_shm_start` / `0x38` 应答）
 *   固定地址拥有: **本文件里的每一行**
 *
 * ## refs= 是**可执行的**归属声明
 *   每区的行尾带 `refs=<允许引用它的模块>`（`-` = 只允许 memmap.h 自己引用）。
 *   `tools/hardcode_audit.py` 机械执行它（不变量 **M5 归属唯一**）：
 *   任何模块引用了不在它 refs 里的区 ⇒ 审计红。
 *   ★ owner 与 refs 是两件事: owner 是**写者**（谁拥有它），refs 是**可触碰者**
 *     （例如黑匣子环 owner=blackbox 写、sd.c 只读它来落盘）。
 *
 * ## class 不是注释，是判据的输入
 *   HOT_CODE ∈ ITCM · HOT_DATA ∈ DTCM · DMA_BUF ∈ AXI · DIAG 可在只读窗口里被读到
 *   ⇒ `tools/mem_report.py` 按 class 检查"性质与用途是否匹配"（构建闸门第 7 道）
 * ══════════════════════════════════════════════════════════════════════════ */
#ifndef DCL_MEMMAP_H
#define DCL_MEMMAP_H

#include <stdint.h>

/* ── 三个片上内存域（器件事实，不随构建变） ───────────────────────────── */
#define ITCM_BASE       0x00000000u
#define ITCM_SIZE       0x00010000u        /* 64 KB  */
#define DTCM_BASE       0x20000000u
#define DTCM_SIZE       0x00020000u        /* 128 KB */
#define AXI_BASE        0x24000000u
#define AXI_SIZE        0x00050000u        /* 320 KB */

/* ══════════════════ ITCM：向量表钉在**顶部**（固定地址拥有）══════════════════
 * `.itcm_vectors` 是 NOLOAD 段，VMA = ITCM_BASE + ITCM_SIZE − 1KB（`size -A` 实测 0xFC00）。
 * HOT_CODE（`.itcm_text`）由链接器从 0x0 起排 —— 实测 13.3 KB / 64 KB = 22.4%。
 * ⇒ 这里只需把"顶部 1KB 属于向量表"钉住，剩下的由链接器拥有。 */
#define ITCM_VECTORS_BASE   (ITCM_BASE + ITCM_SIZE - 0x400u)
#define ITCM_VECTORS_SZ     0x400u
_Static_assert(ITCM_VECTORS_BASE + ITCM_VECTORS_SZ == ITCM_BASE + ITCM_SIZE,
               "memmap: 向量表必须**精确**贴住 ITCM 顶部（它是 VTOR 的落点）");
_Static_assert(ITCM_VECTORS_BASE == 0x0000FC00u,
               "memmap: 向量表落点变了 ⇒ .ld 与 tools/gate_isr_itcm.py 的假设要一起改");

/* ══════════════════ AXI SRAM 320 KB —— **100% 固定地址拥有** ══════════════════
 * 顺序即物理顺序（平铺，无缝）。class 见每行注释；owner = 唯一被允许引用它的模块。
 *
 *   offset        区              大小      class    owner
 *   0x00000  AXI_DIAG_MEM          512 B   DIAG     engine/mem（内存账本自述, 只读窗内）
 *   0x00200  AXI_SD_DIAG           256 B   DIAG     sd.c
 *   0x00300  AXI_BB_DIAG           256 B   DIAG     blackbox.c
 *   0x00400  AXI_SD_CFG            256 B   DIAG     sd.c      ← 历史别名 BB_CFG（已废弃）
 *   0x00500  AXI_BOOT_REC          256 B   DIAG     main.c
 *   0x00600  AXI_FREE0             512 B   FREE     —
 *   0x00800  AXI_PROG_PAY        7680 B   DMA_BUF  prog_store.c
 *   0x02600  AXI_FREE1             512 B   FREE     —
 *   0x02800  AXI_PROG_BLK          512 B   DMA_BUF  prog_store.c
 *   0x02A00  AXI_FREE2            1520 B   FREE     —
 *   0x02FF0  AXI_LATCH              272 B   DMA_BUF  do.c（含 0x2FFC 的自环源字）
 *   0x03100  AXI_LNODE              256 B   DMA_BUF  do.c（MDMA 链表节点, 32B 对齐）
 *   0x03200  AXI_FREE3             3584 B   FREE     —
 *   0x04000  AXI_BB_RING         240 KB   DMA_BUF  blackbox.c（960 槽 × 256 B）
 *   0x40000  AXI_SD_STAGE         64 KB   DMA_BUF  sd.c（落盘前冻结/暂存）
 *
 * ★ 这个域**没有链接器可用空间**（真实空隙只有 FREE0/1/2/3 里那几块零散的）。
 *   需要 AXI 大缓冲时的正确做法: ① 改本文件的地图 ② 重跑 `tools/mem_report.py`
 *   ③ 在 `docs/PLAN-memmap-constitution.md` 备案 —— **不要再找一个"看起来空"的地址**。 */
#define AXI_DIAG_MEM     0x24000000u    /*  DIAG  owner=engine.c（见 OFF_MEM_STAT）  refs=- */
#define AXI_DIAG_MEM_SZ     0x00000200u
#define AXI_MEM_STAT     0x24000100u    /*    区内的账本块（16 字, 只读窗内 ⇒ 协议可读）  refs=mem_stat.c,manifest.h */
#define AXI_SD_DIAG      0x24000200u    /*  DIAG  owner=sd.c  refs=sd.c,manifest.h */
#define AXI_SD_DIAG_SZ      0x00000100u
#define AXI_BB_DIAG      0x24000300u    /*  DIAG  owner=blackbox.c  refs=blackbox.c,manifest.h */
#define AXI_BB_DIAG_SZ      0x00000100u
#define AXI_SD_CFG       0x24000400u    /*  DIAG  owner=sd.c（历史名 BB_CFG 已废弃：从未被引用）  refs=sd.c,manifest.h */
#define AXI_SD_CFG_SZ       0x00000100u
#define AXI_BOOT_REC     0x24000500u    /*  DIAG  owner=main.c  refs=main.c,manifest.h */
#define AXI_BOOT_REC_SZ     0x00000100u
#define AXI_FREE0        0x24000600u    /*  FREE  512 B  refs=- */
#define AXI_FREE0_SZ        0x00000200u
#define AXI_PROG_PAY     0x24000800u    /*  DMA_BUF owner=prog_store.c（程序载荷缓冲 7680 B）  refs=prog_store.c */
#define AXI_PROG_PAY_SZ     0x00001E00u
#define AXI_FREE1        0x24002600u    /*  FREE  512 B  refs=- */
#define AXI_FREE1_SZ        0x00000200u
#define AXI_PROG_BLK     0x24002800u    /*  DMA_BUF owner=prog_store.c（单块缓冲 512 B）  refs=prog_store.c */
#define AXI_PROG_BLK_SZ     0x00000200u
#define AXI_FREE2        0x24002A00u    /*  FREE  1520 B  refs=- */
#define AXI_FREE2_SZ        0x000005F0u
#define AXI_LATCH        0x24002FF0u    /*  DMA_BUF owner=do.c（0x2FFC 自环源字 + 0x3000 快照）  refs=do.c */
#define AXI_LATCH_SZ        0x00000110u
#define AXI_LATCH_SNAP   0x24003000u    /*    区内: TIM2_CNT 快照落点  refs=do.c */
#define AXI_LNODE        0x24003100u    /*  DMA_BUF owner=do.c（MDMA 链表节点, nd[0..5]）  refs=do.c */
#define AXI_LNODE_SZ        0x00000100u
#define AXI_FREE3        0x24003200u    /*  FREE  3584 B  refs=- */
#define AXI_FREE3_SZ        0x00000E00u
#define AXI_BB_RING      0x24004000u    /*  DMA_BUF owner=blackbox.c（BB_SLOTS×BB_SLOT_SZ）  refs=blackbox.h,blackbox.c,sd.c */
#define AXI_BB_RING_SZ      0x0003C000u
#define AXI_SD_STAGE     0x24040000u    /*  DMA_BUF owner=sd.c  refs=sd.c */
#define AXI_SD_STAGE_SZ     0x00010000u

/* ── 平铺断言（不变量 **M2 所有权互斥**：相邻区**精确相接**，最后一块贴住域尾）──
 * ★ 用 `==` 而不是 `<=`：`<=` 会放过"某区被改小、留下一条无人区"这件事
 *   （SHM 那边踩过同款的坑，见 engine.h 的布局断言注释）。 */
_Static_assert(AXI_DIAG_MEM  + AXI_DIAG_MEM_SZ  == AXI_SD_DIAG,      "memmap: AXI 区表不连续");
_Static_assert(AXI_SD_DIAG   + AXI_SD_DIAG_SZ   == AXI_BB_DIAG,      "memmap: AXI 区表不连续");
_Static_assert(AXI_BB_DIAG   + AXI_BB_DIAG_SZ   == AXI_SD_CFG,       "memmap: AXI 区表不连续");
_Static_assert(AXI_SD_CFG    + AXI_SD_CFG_SZ    == AXI_BOOT_REC,     "memmap: AXI 区表不连续");
_Static_assert(AXI_BOOT_REC  + AXI_BOOT_REC_SZ  == AXI_FREE0,        "memmap: AXI 区表不连续");
_Static_assert(AXI_FREE0     + AXI_FREE0_SZ     == AXI_PROG_PAY,     "memmap: AXI 区表不连续");
_Static_assert(AXI_PROG_PAY  + AXI_PROG_PAY_SZ  == AXI_FREE1,        "memmap: AXI 区表不连续");
_Static_assert(AXI_FREE1     + AXI_FREE1_SZ     == AXI_PROG_BLK,     "memmap: AXI 区表不连续");
_Static_assert(AXI_PROG_BLK  + AXI_PROG_BLK_SZ  == AXI_FREE2,        "memmap: AXI 区表不连续");
_Static_assert(AXI_FREE2     + AXI_FREE2_SZ     == AXI_LATCH,        "memmap: AXI 区表不连续");
_Static_assert(AXI_LATCH     + AXI_LATCH_SZ     == AXI_LNODE,        "memmap: AXI 区表不连续");
_Static_assert(AXI_LNODE     + AXI_LNODE_SZ     == AXI_FREE3,        "memmap: AXI 区表不连续");
_Static_assert(AXI_FREE3     + AXI_FREE3_SZ     == AXI_BB_RING,      "memmap: AXI 区表不连续");
_Static_assert(AXI_BB_RING   + AXI_BB_RING_SZ   == AXI_SD_STAGE,     "memmap: AXI 区表不连续");
_Static_assert(AXI_SD_STAGE  + AXI_SD_STAGE_SZ  == AXI_BASE + AXI_SIZE,
               "memmap: AXI 区表必须**精确铺满** 320 KB（留缝 = 留无人区）");
_Static_assert(AXI_DIAG_MEM == AXI_BASE, "memmap: AXI 区表必须从域首开始");

/* 区内小结构必须落在区内（防止"改了偏移没改尺寸"） */
_Static_assert(AXI_MEM_STAT  >= AXI_DIAG_MEM  && AXI_MEM_STAT  + 64u <= AXI_DIAG_MEM + AXI_DIAG_MEM_SZ,
               "memmap: MEM_STAT 越出所属区");
_Static_assert(AXI_LATCH_SNAP >= AXI_LATCH    && AXI_LATCH_SNAP + 16u <= AXI_LATCH + AXI_LATCH_SZ,
               "memmap: LATCH_SNAP 越出所属区");
_Static_assert(AXI_LATCH_SNAP - 4u >= AXI_LATCH,
               "memmap: do.c 的自环源字 (LATCH_SNAP-4) 越出所属区");

/* ── 只读协议窗口（engine.c 的 ENG_AXIDIAGF/FE）─────────────────────────
 * 窗口 = 前 6 个区（直到 BOOT_REC 尾）。★ 它**必须**盖住 MEM_STAT，否则账本读不到；
 * 又**必须**不盖住 PROG_PAY / BB_RING（那是用户数据与飞行记录）。 */
#define AXI_DIAG_WIN_BASE   AXI_BASE
#define AXI_DIAG_WIN_END    (AXI_BOOT_REC + AXI_BOOT_REC_SZ)
_Static_assert(AXI_DIAG_WIN_BASE <= AXI_MEM_STAT && AXI_MEM_STAT + 64u <= AXI_DIAG_WIN_END,
               "memmap: 只读窗口必须盖住 MEM_STAT（否则内存账本协议读不到）");
_Static_assert(AXI_DIAG_WIN_END <= AXI_PROG_PAY,
               "memmap: 只读窗口不得盖住程序载荷缓冲（用户数据）");

/* ══════════════════ DTCM：链接器拥有，这里只登记"无名区"的名字 ══════════════════
 * 实测（`size -A`）: .data 140 B + .bss 20.0 KB + SHM 32 KB + `._user_heap_stack` 4.5 KB
 *   = 56.2 KB 有主；`_shm_end + 0x1200` 到 `_estack` 之间 **71.8 KB 无名字、无断言、无水位**。
 * ⇒ 本文件给它一个名字（class=STACK），并由 `mem_report.py`（构建期）与
 *   `mem_stat`（运行期水位）两侧盯着它。 */
#define DTCM_HEAPSTACK_SZ   0x1200u       /* = _Min_Heap_Size(0x200) + _Min_Stack_Size(0x1000)
                                           * ★ 这两个值住在 ld/STM32H723ZG_FLASH.ld；
                                           *   mem_report.py 会**跨文件核对**（改了这边不改那边 ⇒ 闸门红）*/
#define DTCM_STACK_MIN_SZ   0x2000u       /* 栈余量下限（8 KB）：低于它 ⇒ 构建期告警 */

/* ── 内存账本块（AXI_MEM_STAT）的字段（运行期由 mem_stat.c 维护） ────────────
 * 放在 AXI 而不是 SHM 的两个理由: ① 它在只读协议窗口内 ⇒ 协议可读 ② AXI 内容**跨复位保持**
 *   ⇒ 复位之后还能读到"复位前栈用到了哪"（排障价值与 BOOT_REC 同级）。 */
#define MEM_STAT_MAGIC      0x4D454D53u   /* 'SMEM' */
#define OFF_MEM_MAGIC       0u
#define OFF_MEM_STACK_LOW   4u            /* 观测到的最深栈地址（越低说明用得越多）*/
#define OFF_MEM_STACK_USED  8u            /* _estack − stack_low */
#define OFF_MEM_HEADROOM    12u           /* 可铺区总大小（= headroom_total）*/
#define OFF_MEM_SHM_START   16u
#define OFF_MEM_SHM_END     20u
#define OFF_MEM_SHM_SIZE    24u
#define OFF_MEM_LAYOUT_OK   28u           /* shm_layout_ok() 的结果（不变量 M3 HOT_DATA）*/
#define OFF_MEM_GUARD_OK    32u           /* shm_guard_ok() 的结果（SHM 与栈之间那道堤）*/
#define OFF_MEM_PAINT_LO    36u           /* 本次铺魔术字的起点 */
#define OFF_MEM_SCANS       40u           /* 扫描次数（证明这段代码真的跑过）*/
#define OFF_MEM_STAT_SZ     64u

#endif /* DCL_MEMMAP_H */
