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

/* MDMA CCR: EN=bit0, SWRQ=bit16 */
#define MDMA_CCR_EN     1u
#define MDMA_CCR_SWRQ   (1u << 16)
/* CTCR: SWRM=bit30, TLEN[7:0]=bits[25:18], SINC_1=bit16, SINCOS_1=bit15,
 *        DINC_1=bit18, DINCOS_1=bit17, PSIZE/MSIZE 按 RM0468 */
#define BB_CTCR_SWRM    (1u << 30)   /* 软件请求模式 */
#define BB_CTCR_TLEN(n) (((n) - 1u) << 18)  /* buffer transfer length: bits[25:18], 值=n-1 */
#define BB_CTCR_DINC_2  (2u << 2)    /* 目的地址按 size 递增 (bits[3:2]=10) */
#define BB_CTCR_SINC_2  2u           /* 源地址按 size 递增 (bits[1:0]=10) */
/* CTBR: SBUS(bit16)=源走 DTCM/TCM 端口; DBUS(bit17)=目的走 DTCM/TCM 端口 */
#define BB_CTBR_SBUS    (1u << 16)

/* SHM 紧凑快照区 (SHM 尾部, 256B) */
/* OFF_BB_SNAP 在 engine.h 定义 */

static volatile uint8_t *s_bb_shm = 0;
static volatile uint32_t s_bb_widx = 0;    /* 当前写入槽号 */
static volatile uint8_t s_bb_ready = 0;

void bb_init(uint8_t *shm_base)
{
    s_bb_shm = shm_base;

    /* ① MDMA 时钟 (AHB3ENR bit0, h723-core0 实测地址) */
    *(volatile uint32_t *)0x580244D4u |= 1u;
    __asm__ volatile("dsb; isb" ::: "memory");

    /* ② 清 ch1 标志 + 禁用 */
    *(volatile uint32_t *)BB_M_CIFCR = 0x1Fu;
    *(volatile uint32_t *)BB_M_CCR = 0u;
    __asm__ volatile("dsb; isb" ::: "memory");

    /* ③ 配置 MDMA ch1 (照 h723-core0 mdma_kick4 实测配方, 改 BNDT/SINC/DINC/CTBR)
     *   CTCR: SWRM(软触发) + TLEN=255(=256B) + SINC/DINC=按 size 递增(word 递增)
     *   ★ byte 尺寸+TLEN 模式 (旧项目实测: word 尺寸会 BSE);
     *     TLEN=(256-1) ⇒ 每次触发搬 256 字节 ⇒ 64 次 word(32bit) 传输 */
    /* CTCR = SWRM(软触发) | TLEN=(256-1)<<18 (256B 传输) | SINC/DINC=按size递增
     * ★ 位域引自 h723-core0 实测配方 CTCR_4B_SW = 0x40000000|(3<<18)|(2<<2)|2
     *   (TLEN 在 bits[25:18], DINC 在 bits[3:2], SINC 在 bits[1:0]);
     *   首版把 SINC 写在 bit16/DINC 写在 bit18 —— 与 TLEN 重叠, 全错 */
    *(volatile uint32_t *)BB_M_CTCR = BB_CTCR_SWRM | BB_CTCR_TLEN(BB_SLOT_SZ)
                                    | BB_CTCR_DINC_2 | BB_CTCR_SINC_2;
    *(volatile uint32_t *)BB_M_CBNDTR = BB_SLOT_SZ;       /* BNDT = 256 字节 */
    *(volatile uint32_t *)BB_M_CTBR = BB_CTBR_SBUS;       /* 源=DTCM ⇒ SBUS */
    __asm__ volatile("dsb; isb" ::: "memory");

    /* ④ 清环形缓冲 magic (标记"没有未读数据") */
    *(volatile uint32_t *)BB_AXI_BASE = 0u;
    __asm__ volatile("dsb; isb" ::: "memory");

    /* ⑤ 首次 kick (初始化 SHM 紧凑区 + 启动第一次 MDMA) */
    s_bb_ready = 1;
    bb_kick(0);
}

void bb_kick(uint32_t tick)
{
    if (!s_bb_ready || !s_bb_shm) return;

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

    /* ② 更新 MDMA ch1 源/目的地址
     * ★ 源地址也必须每次重写 —— SINC=按 size 递增 ⇒ 首次传输后 CSAR 已漂移,
     *   不重写的话后续 kick 读的是漂移后的错误地址 ⇒ 垃圾数据 (实测撞到) */
    *(volatile uint32_t *)BB_M_CSAR = (uint32_t)(s_bb_shm + OFF_BB_SNAP);
    uint32_t dst = BB_AXI_BASE + s_bb_widx * BB_SLOT_SZ;
    *(volatile uint32_t *)BB_M_CDAR = dst;

    /* ③ 软触发: EN + SWRQ (h723-core0 kick4 同款序列) */
    *(volatile uint32_t *)BB_M_CIFCR = 0x1Fu;             /* 清标志 */
    __asm__ volatile("dsb; isb" ::: "memory");
    *(volatile uint32_t *)BB_M_CCR = 1u;                  /* EN */
    __asm__ volatile("dsb; isb" ::: "memory");
    *(volatile uint32_t *)BB_M_CCR |= (1u << 16);         /* SWRQ */
    __asm__ volatile("dsb; isb" ::: "memory");

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
