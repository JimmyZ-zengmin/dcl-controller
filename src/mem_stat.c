/* ══════════════════════════════════════════════════════════════════════════
 * mem_stat.c — 内存账本的**运行期**那一半（2026-09-19，内存宪法 D 期）
 *
 * ## 它回答什么问题
 * `tools/mem_report.py` 回答"构建期每区用了多少"；本文件回答"**运行期栈最深用到哪**"。
 * 两者合起来才叫"账本"：一个说静态占用，一个说动态水位。
 *
 * ## 做法（铺魔术字 + 找最深被改写处）
 *   ① 引导时：从 `_shm_end + 128`（越过 `shm_guard` 的 128 B 堤）向上，把整段
 *      **无名 DTCM 区**铺成 `PAINT` 直到**当前 SP 之下**（`SP - 0`，保守取整到 4）。
 *      —— 这段内存按定义是"没人用的栈余量"，铺它不影响任何数据。
 *   ② 之后（引导一次 + 主循环里限频）扫描：从铺的起点往上找**第一个不再是 PAINT 的字**
 *      ⇒ 那就是栈曾经到过的最低地址 ⇒ `used = _estack − 那个地址`。
 *   ③ 结果写进 **AXI 的 `MEM_STAT` 块**（`src/memmap.h`）：它在**只读协议窗口内**
 *      ⇒ 上位机用 `0x22` 就能读；而且 AXI **跨复位保持** ⇒ 复位后还能读到"复位前栈用到哪"。
 *
 * ## 为什么必须做（而不是"记个数字在文档里"）
 * 实测（`arm-none-eabi-size -A`）：DTCM 128 KB 里有主部分 56.2 KB，
 *   `_shm_end + 0x1200` 到 `_estack` 之间 **71.8 KB 无名字、无断言、无水位**。
 *   而 `_Min_Stack_Size` 只有 4 KB —— 意思是"**要溢出 71.8 KB 才碰得到哨兵**"，
 *   中间那 71.8 KB 到底用了多少，此前**没有人知道**。
 *   ⇒ 这是本项目的典型形态："没出事"和"还有多少余量"是两件事，后者必须可测量。
 *
 * ## 代价
 *   铺：仅在引导时写 ~18k 个字（≈ 数十 µs，一次性）。
 *   扫：从铺的起点向上走到第一个被改写的字 —— 最坏情况（栈很浅）要走满 71.8 KB
 *       ≈ 1.8 万字 ≈ 45 µs；**限频到 ~1 次/秒** ⇒ 占拍预算 0.0045%，可忽略。
 *
 * ## 判据（都能失败，见 tools/exp_fa_mem_account.py）
 *   F1 引导后 `MEM_STAT.magic == 'SMEM'` 且 `scans >= 1`（证明这段代码真的跑过）
 *   F2 `0 < stack_used < headroom`（水位既不是 0 也不是满 —— 两头都说明测量坏了）
 *   F3 ★ 变异构建 `-DDCL_STACK_PROBE_BYTES=16384`（在铺之后故意吃 16 KB 栈）
 *      ⇒ `stack_used` 必须**至少增加 ~16 KB**（证明水位真的在量栈，而不是个常数）
 *   F4 复位前后：`MEM_STAT` 仍在（AXI 保持）—— 与 `BOOT_REC` 同性质的取证价值
 * ══════════════════════════════════════════════════════════════════════════ */
#include "engine.h"
#include "memmap.h"
#include "lsym.h"

extern uint8_t _shm_start[];
extern uint8_t _shm_end[];
extern uint8_t _estack[];

#define PAINT_WORD      0xA5A5A5A5u
#define PAINT_GUARD_SKIP 0x80u      /* shm_guard 的 128 B: 别覆盖它（它是"表被踩"的第一现场）*/
#define SCAN_DIV         1024u      /* 主循环限频（主循环 ~0.1~0.4 ms ⇒ 约 0.1~0.4 s 一次）*/

static uint32_t s_paint_lo = 0u;    /* 铺的起点（= _shm_end + 128）*/
static uint32_t s_paint_hi = 0u;    /* 铺的终点（= 铺那一刻的 SP）*/
static uint32_t s_poll_n   = 0u;    /* 扫描次数（写进账本，证明确实跑过）*/
static uint32_t s_poll_tick = 0u;   /* 限频计数器 */

/** @brief 把无名 DTCM 区铺成魔术字（引导时调一次；幂等） */
void mem_stat_paint(void)
{
    volatile uint32_t probe = 0u;                  /* 取它的地址当 SP 的保守估计 */
    uint32_t sp = (uint32_t)(uintptr_t)&probe;
    uint32_t lo = ((uint32_t)(uintptr_t)LSYM_ADDR(_shm_end) + PAINT_GUARD_SKIP + 3u) & ~3u;
    uint32_t hi = sp & ~3u;                        /* 只铺到 SP 之下 ⇒ 不碰活栈 */

    s_paint_lo = lo;
    s_paint_hi = hi;
    if (hi <= lo) return;                          /* 余量为 0（不该发生）⇒ 什么都不做 */

    for (uint32_t a = lo; a < hi; a += 4u) *(volatile uint32_t *)(uintptr_t)a = PAINT_WORD;
}

/** @brief 扫描一次并把账本写进 AXI 的 MEM_STAT 块（幂等、可重复调用） */
void mem_stat_scan(void)
{
    volatile uint32_t *m = (volatile uint32_t *)AXI_MEM_STAT;
    uint32_t estack = (uint32_t)(uintptr_t)LSYM_ADDR(_estack);
    uint32_t low;

    if (s_paint_lo == 0u) {                        /* 没铺过（不该发生）⇒ 不写假数据 */
        m[OFF_MEM_SCANS / 4u] = 0xFFFFFFFFu;       /* ★ 显式标"测不了"，不是 0 */
        return;
    }
    low = s_paint_hi;                              /* 默认：栈没有比"铺的那一刻"更深 */
    if (s_paint_hi > s_paint_lo) {
        for (uint32_t a = s_paint_lo; a < s_paint_hi; a += 4u) {
            if (*(volatile uint32_t *)(uintptr_t)a != PAINT_WORD) { low = a; break; }
        }
    }
    s_poll_n++;
    m[OFF_MEM_MAGIC      / 4u] = MEM_STAT_MAGIC;
    m[OFF_MEM_STACK_LOW  / 4u] = low;
    m[OFF_MEM_STACK_USED / 4u] = (estack > low) ? (estack - low) : 0u;
    m[OFF_MEM_HEADROOM   / 4u] = (s_paint_hi > s_paint_lo) ? (estack - s_paint_lo) : 0u;
    m[OFF_MEM_SHM_START  / 4u] = (uint32_t)(uintptr_t)LSYM_ADDR(_shm_start);
    m[OFF_MEM_SHM_END    / 4u] = (uint32_t)(uintptr_t)LSYM_ADDR(_shm_end);
    m[OFF_MEM_SHM_SIZE   / 4u] = (uint32_t)SHM_SIZE;
    m[OFF_MEM_LAYOUT_OK  / 4u] = (uint32_t)shm_layout_ok();
    m[OFF_MEM_GUARD_OK   / 4u] = (uint32_t)shm_guard_ok();
    m[OFF_MEM_PAINT_LO   / 4u] = s_paint_lo;
    m[OFF_MEM_SCANS      / 4u] = s_poll_n;
}

/** @brief 主循环限频调用（内部按 SCAN_DIV 分频；未铺过 ⇒ 什么都不做） */
void mem_stat_scan_poll(void)
{
    if (s_paint_lo == 0u) return;
    if (++s_poll_tick < SCAN_DIV) return;
    s_poll_tick = 0u;
    mem_stat_scan();
}
