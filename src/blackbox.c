/* blackbox.c — 飞行记录仪 (P3: 每拍 I/O 快照 → AXI 环形缓冲)
 *
 * 数据流: SHM 紧凑区(DTCM, CPU 拷贝 3 段 64B) → MDMA ch1 → AXI 环形缓冲
 *
 * MDMA ch1 配置 (与 DO 锁存链的 ch0 同架构, 独立通道):
 *   源 = SHM+OFF_BB_SNAP (DTCM, 经 SBUS=TSEL bit16)
 *   目的 = AXI 环形缓冲 (每拍 CPU 更新 CDAR 到下一个槽)
 *   BNDT = 256 字节, PSIZE/MSIZE=word(32bit) + SINC/DINC=按 word 递增
 *   TRGM = BUFFER(00), 每次软触发(SWRQ)搬完 BNDT 后 EN 自动清
 *
 * 故障后访问: AXI SRAM 在系统复位(watchdog/HardFault)后内容保持
 *   ⇒ 新固件启动时读 BB_BASE 处的 magic 判断是否有未读数据。
 *   BB_MAGIC 位置 = BB_AXI_BASE (每槽 [0] 的 tick 位置 = 槽起始)。 */
#include "blackbox.h"
#include "engine.h"
#include "regs.h"

/* ── MDMA ch1 寄存器 (ch_n 基址 = MDMA_BASE + 0x40×(n+1); ch1 = +0x80) ── */
#define BB_M        0x52000080u   /* MDMA ch1 寄存器组基址 (ch0=+0x40, 间距 0x40) */
#define BB_M_CISR   (BB_M + 0x00u)
#define BB_M_CIFCR  (BB_M + 0x04u)
#define BB_M_CESR   (BB_M + 0x08u)
#define BB_M_CCR    (BB_M + 0x0Cu)
#define BB_M_CTCR   (BB_M + 0x10u)
#define BB_M_CBNDTR (BB_M + 0x14u)
#define BB_M_CSAR   (BB_M + 0x18u)
#define BB_M_CDAR   (BB_M + 0x1Cu)
#define BB_M_CTBR   (BB_M + 0x28u)

/* MDMA CCR: EN=bit0, SWRQ=bit16 (权威: stm32h723xx.h MDMA_CCR_EN_Pos=0 / _SWRQ_Pos=16) */
#define MDMA_CCR_EN     1u
#define MDMA_CCR_SWRQ   (1u << 16)

/* ── CTCR 位域 (全部逐条核过 stm32h723xx.h 的 *_Pos/_Msk) ──
 *   SINC[1:0]  = bits[1:0]     DINC[1:0] = bits[3:2]     (2 = 按 size 递增)
 *   SSIZE[1:0] = bits[5:4]     DSIZE[1:0] = bits[7:6]    (2 = word)
 *   TLEN[6:0]  = bits[24:18]   ★ 单位是**字节数-1**, 只有 7 位 ⇒ **上限 128 字节**
 *   TRGM[1:0]  = bits[29:28]   ★ 不是在 [17:16] (旧注释写错)
 *   SWRM       = bit30
 * ----------------------------------------------------------------
 * ★★ 实测 (2026-09-12) 定位到的真因:
 *  ① **TRGM 从未设置** ⇒ TRGM=00。查 ST HAL: `MDMA_BUFFER_TRANSFER = 0`
 *     是"每请求搬一个 buffer"(=TLEN+1 字节), 所以 00 本身不违规;
 *  ② **但 TLEN 我们写的 255<<18 只有低 7 位有效** (mask 0x7F) ⇒ 实际 TLEN=127
 *     ⇒ 每次请求只搬 **128 字节**, 而一个槽是 256 字节 ⇒ 槽永远填不满一半;
 *  ③ 要"一次请求搬完 256B", 正确模式是 **TRGM=01 = MDMA_BLOCK_TRANSFER**
 *     (每个请求搬一整个 block = CBNDTR.BNDT 字节)。
 * ⇒ 本版: TRGM=01 + BNDT=256 + word 尺寸 + SINC/DINC 按 size 递增。
 */
#define BB_CTCR_SWRM      (1u << 30)   /* 软件请求模式 */
#define BB_CTCR_TRGM_BLK  (1u << 28)   /* TRGM=01: 每请求搬一个 block (=BNDT 字节) */
#define BB_CTCR_TLEN(n)   (((n) - 1u) << 18)  /* n 字节, n ≤ 128 (7 位字段) */
#define BB_CTCR_DINC_W    (2u << 2)    /* 目的按 size 递增 */
#define BB_CTCR_SINC_W    2u           /* 源按 size 递增 */
#define BB_CTCR_DSIZE_W   (2u << 6)    /* 目的数据尺寸 = word */
#define BB_CTCR_SSIZE_W   (2u << 4)    /* 源数据尺寸   = word */
/* CTBR: SBUS(bit16)=源走 DTCM/TCM 端口; DBUS(bit17)=目的走 DTCM/TCM 端口 */
#define BB_CTBR_SBUS    (1u << 16)

/* ★★ MDMA 自观测区 (pyocd **读不了** 0x5200_xxxx 外设区 —— 实测连 SDMMC1 的
 *   固定版本寄存器 IPVR 都读回 0 ⇒ 那是读失败不是真值)。所以 MDMA 的状态
 *   只能由**固件自己**读出来放进 SRAM, 再让 pyocd 读 SRAM。
 *   布局见文件末尾 bb_diag_dump 注释。 */
#define BB_DIAG  ((volatile uint32_t *)0x24030100u)

/* SHM 紧凑快照区 (SHM 尾部, 256B) */
/* OFF_BB_SNAP 在 engine.h 定义 */

static volatile uint8_t *s_bb_shm = 0;
static volatile uint32_t s_bb_widx = 0;    /* 当前写入槽号 */
static volatile uint8_t s_bb_ready = 0;
static volatile uint32_t s_bb_kicks = 0;   /* kick 次数 (自观测) */
static volatile uint32_t s_last_dst = 0;   /* 上一拍的目的地址 (用于"数据有没有落地") */

void bb_init(uint8_t *shm_base)
{
    s_bb_shm = shm_base;

    /* ⓪ 自观测区清零 */
    { uint32_t i; for (i = 0; i < 40u; i++) BB_DIAG[i] = 0u; }

    /* ① MDMA 时钟 (AHB3ENR bit0) */
    *(volatile uint32_t *)0x580244D4u |= 1u;
    __asm__ volatile("dsb; isb" ::: "memory");

    /* ★★ MDMA 寄存器**活性自检** (写-读回)。
     * 判据必须能失败: 外设时钟没开 / 基址错时, 对外设寄存器的读写会全部丢失
     * (读回 0)。写两个可辨识值再读回, 结果记进 BB_DIAG[20..23]:
     *   [20] CBNDTR 写 0xABCD 后回读 (期望 0xABCD)
     *   [21] CTBR   写 0x00010000 后回读 (期望 0x00010000)
     *   [22] AHB3ENR 回读 (期望 bit0=1 ⇒ MDMAEN)
     *   [23] ch 基址回读 (BB_M 本身不是寄存器, 用 CTCR 掩码校验) */
    {
        volatile uint32_t *bnd = (volatile uint32_t *)BB_M_CBNDTR;
        volatile uint32_t *tbr = (volatile uint32_t *)BB_M_CTBR;
        *bnd = 0x0000ABCDu; __asm__ volatile("dsb; isb" ::: "memory");
        BB_DIAG[20] = *bnd;
        *tbr = 0x00010000u; __asm__ volatile("dsb; isb" ::: "memory");
        BB_DIAG[21] = *tbr;
        BB_DIAG[22] = *(volatile uint32_t *)0x580244D4u;   /* AHB3ENR */
        BB_DIAG[23] = *(volatile uint32_t *)BB_M_CTCR;     /* 上电应为 0 */
    }

    /* ② 清 ch1 标志 + 禁用 */
    *(volatile uint32_t *)BB_M_CIFCR = 0x1Fu;
    *(volatile uint32_t *)BB_M_CCR = 0u;
    __asm__ volatile("dsb; isb" ::: "memory");

    /* ③ 配置 MDMA ch1
     *   CTCR: SWRM(软触发) + **TRGM=01 block** + TLEN=127(≤128, 仅占位)
     *         + SINC/DINC=按 word 递增 + SSIZE/DSIZE=word
     *   CBNDTR: BNDT = 256 字节 ⇒ **一次 SWRQ 搬完一整个槽** (见上方 CTCR 注释的实测结论) */
    *(volatile uint32_t *)BB_M_CTCR = BB_CTCR_SWRM | BB_CTCR_TRGM_BLK
                                    | BB_CTCR_TLEN(128u)
                                    | BB_CTCR_DSIZE_W | BB_CTCR_SSIZE_W
                                    | BB_CTCR_DINC_W | BB_CTCR_SINC_W;
    *(volatile uint32_t *)BB_M_CBNDTR = BB_SLOT_SZ;       /* BNDT = 256 字节 */
    *(volatile uint32_t *)BB_M_CTBR = BB_CTBR_SBUS;       /* 源=DTCM ⇒ SBUS */
    __asm__ volatile("dsb; isb" ::: "memory");
    BB_DIAG[10] = *(volatile uint32_t *)BB_M_CTCR;        /* 回读 CTCR 确认落地 */
    BB_DIAG[11] = *(volatile uint32_t *)BB_M_CBNDTR;

    /* ④ 清环形缓冲 magic (标记"没有未读数据") */
    *(volatile uint32_t *)BB_AXI_BASE = 0u;
    __asm__ volatile("dsb; isb" ::: "memory");

    /* ⑤ 首次 kick (初始化 SHM 紧凑区 + 启动第一次 MDMA) */
    s_bb_ready = 1;
    bb_kick(0);
}

void bb_kick(uint32_t tick)
{
    int samp;
    if (!s_bb_ready || !s_bb_shm) return;

    s_bb_kicks++;
    /* ★ 自观测闸门: 前 3 次 + 每 1024 次采样。成本 ~100 cyc/次被采样拍, 可忽略。 */
    samp = (s_bb_kicks <= 3u) || ((s_bb_kicks & 0x3FFu) == 0u);

    /* ① CPU 拷贝分散数据 → SHM 紧凑快照区 (SHM+0x6F20, 256B) */
    volatile uint32_t *snap = (volatile uint32_t *)(s_bb_shm + OFF_BB_SNAP);
    snap[0] = tick;
    {   /* SENSOR[0..15] @ SHM+0x40 */
        volatile uint32_t *src = (volatile uint32_t *)(s_bb_shm + 0x40u);
        for (uint32_t i = 0; i < 16u; i++) snap[1 + i] = src[i];
    }
    {   /* WIRE[0..15] @ SHM+0x240 */
        volatile uint32_t *src = (volatile uint32_t *)(s_bb_shm + 0x240u);
        for (uint32_t i = 0; i < 16u; i++) snap[17 + i] = src[i];
    }
    {   /* ACTUATOR[0..15] @ SHM+0x140 */
        volatile uint32_t *src = (volatile uint32_t *)(s_bb_shm + 0x140u);
        for (uint32_t i = 0; i < 16u; i++) snap[33 + i] = src[i];
    }
    {   /* 控制状态 */
        uint32_t run = *(volatile uint32_t *)(s_bb_shm + 0x0Du) & 0xFFu;
        uint32_t nr  = *(volatile uint32_t *)(s_bb_shm + 0x0Eu) & 0xFFFFu;
        snap[49] = (run << 24) | (nr & 0xFFFFu);
    }

    /* ② ★★ 每拍必须"关通道 → 改寄存器 → 使能 → 触发"。
     *
     * 实测 (2026-09-12, 由固件自观测取得, 因为 pyocd 读不了 0x5200_xxxx 外设区):
     *   · 首版只在 bb_init 写过一次 CBNDTR; `bb_init→bb_kick(0)` 的**第一次**搬运
     *     是成功的 —— 这正是环里只有第 0 槽是真快照 (tick=0) 的原因;
     *   · 但 **CBNDTR 被上一次传输减到 0 后不会自动重装**, 而 EN=1 期间
     *     CSAR/CDAR/CBNDTR 的新值**不被接受**(影子寄存器) ⇒
     *     **之后每一次 kick 都搬 0 字节**, 环永远只写过第 0 槽。
     *   证据: 自观测读到 `CBNDTR=0`、`CISR=0`(无完成标志)、`CESR=0`(无错误)、
     *        `CCR=1`(EN 挂着), 而 CDAR 停留在 512 拍之前 widx=1 时的旧值
     *        ⇒ 写进去的地址被忽略。 */
    *(volatile uint32_t *)BB_M_CCR = 0u;                  /* ★ 关通道: 之后再改寄存器 */
    __asm__ volatile("dsb" ::: "memory");
    *(volatile uint32_t *)BB_M_CIFCR = 0x1Fu;             /* 清标志 */
    *(volatile uint32_t *)BB_M_CSAR = (uint32_t)(s_bb_shm + OFF_BB_SNAP);
    uint32_t dst = BB_AXI_BASE + s_bb_widx * BB_SLOT_SZ;
    *(volatile uint32_t *)BB_M_CDAR = dst;
    *(volatile uint32_t *)BB_M_CBNDTR = BB_SLOT_SZ;       /* ★ 每拍重装 BNDT=256 */
    __asm__ volatile("dsb; isb" ::: "memory");

    /* ★ 自观测 (采样点 A): 上一拍结束后的状态 —— CISR 的 CTCIF 能告诉我们
     *   "上一次到底搬完没有", CESR 给出错误类别, [17] 是上一拍目的地首字
     *   (从 SRAM 读回, 直接证明数据有没有落到 AXI)。 */
    if (samp) {
        BB_DIAG[0] = s_bb_kicks;
        BB_DIAG[1] = *(volatile uint32_t *)BB_M_CISR;
        BB_DIAG[2] = *(volatile uint32_t *)BB_M_CESR;
        BB_DIAG[3] = *(volatile uint32_t *)BB_M_CCR;
        BB_DIAG[4] = *(volatile uint32_t *)BB_M_CBNDTR;
        BB_DIAG[5] = *(volatile uint32_t *)BB_M_CSAR;
        BB_DIAG[6] = *(volatile uint32_t *)BB_M_CDAR;
        BB_DIAG[7] = *(volatile uint32_t *)BB_M_CTBR;
        BB_DIAG[8] = tick;
        BB_DIAG[9] = s_bb_widx;
        BB_DIAG[17] = (s_last_dst != 0u) ? *(volatile uint32_t *)s_last_dst : 0u;
    }

    /* ③ 软触发: 使能 + SWRQ (标志与寄存器已在 ② 里就位) */
    *(volatile uint32_t *)BB_M_CCR = 1u;                  /* EN */
    __asm__ volatile("dsb; isb" ::: "memory");
    *(volatile uint32_t *)BB_M_CCR |= (1u << 16);         /* SWRQ */
    __asm__ volatile("dsb; isb" ::: "memory");

    /* ★ 自观测 (采样点 B): 触发瞬间的状态 */
    if (samp) {
        BB_DIAG[12] = *(volatile uint32_t *)BB_M_CISR;
        BB_DIAG[13] = *(volatile uint32_t *)BB_M_CESR;
        BB_DIAG[14] = *(volatile uint32_t *)BB_M_CCR;
        BB_DIAG[15] = *(volatile uint32_t *)BB_M_CBNDTR;
        BB_DIAG[16] = *(volatile uint32_t *)BB_M_CTCR;
    }
    s_last_dst = dst;

    /* ④ 环形递增 */
    s_bb_widx++;
    if (s_bb_widx >= BB_SLOTS) s_bb_widx = 0;
}

uint32_t bb_write_idx(void) { return s_bb_widx; }

uint32_t bb_tick_last(void)
{
    /* 读上一次写入槽的 tick (诊断: 最近快照的时间戳) */
    uint32_t idx = (s_bb_widx == 0) ? (BB_SLOTS - 1) : (s_bb_widx - 1);
    return *(volatile uint32_t *)(BB_AXI_BASE + idx * BB_SLOT_SZ);
}
