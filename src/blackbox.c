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
#include "sd.h"   /* sd_cfg_take: 带魔数门的一次性配置字 */
#include "engine.h"
#include "regs.h"
#include "faultlog.h"   /* 台账: 既作为记录流的两列(BB_MAP_SEG_FAULT), 也进日志头快照 */

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
#define BB_DIAG  ((volatile uint32_t *)0x24000300u)

/* ★ 诊断钩子 (SD_CFG @0x24000400, 调试器复位前预写, 读一次即清):
 *   [4] = CTCR 覆盖值 (0 ⇒ 用默认) —— 用来扫出"一次请求到底能搬多少字节"的正确配置
 *   [5] = 1 ⇒ 上电先把 RAM 环整片填 0xDEADBEEF 哨兵, 之后数"有多少字变了" = 传输长度
 */
#define BB_CFG   ((volatile uint32_t *)0x24000400u)
#define BB_SENT  0xDEADBEEFu

/* SHM 紧凑快照区 (SHM 尾部, 256B) */
/* OFF_BB_SNAP 在 engine.h 定义 */

static volatile uint8_t *s_bb_shm = 0;
static volatile uint32_t s_bb_widx = 0;    /* 当前写入槽号 */
static volatile uint8_t s_bb_ready = 0;
static volatile uint32_t s_bb_kicks = 0;   /* 总拍数 (单调, 诊断用) */
static volatile uint32_t s_bb_records = 0; /* ★ 真正写进环的记录数 (单调) —— 环游标 */
static volatile uint32_t s_bb_skipped = 0; /* ★ 因"内容没变"而跳过的拍数 */
static volatile uint32_t s_bb_seq = 0;     /* 记录序号 (与 kick 同步) */
/* ★ "变化率"统计 (2026-09-12): 决定黑匣子该"缩记录"还是"变化才记"。
 *   s_chg_ticks = 至少有一个通道变了的拍数; s_chg_vals = 变化通道总数。
 *   ⇒ 变化率 = s_chg_ticks/总拍数; 平均每次变几个 = s_chg_vals/s_chg_ticks。 */
static volatile uint32_t s_prev[BB_MAP_N];  /* 上一条**已写出**记录的 60 个槽 */
static volatile uint32_t s_prev_ctrl = 0;  /* 上一条已写出记录的控制字 */
static volatile uint32_t s_bb_have_prev = 0;
static volatile uint32_t s_last_dst = 0;   /* 上一拍的目的地址 (用于"数据有没有落地") */

/* ══════════════ ★★ 通道映射 (2026-09-12) ══════════════
 * 记录里 4 字头 + 60 数据字 = 64 字 = 256B。**哪一槽是哪一路通道**由此表决定,
 * 表随日志头写到卡上 ⇒ 数据自带"这一列是哪一路通道"。
 *
 * ★ 默认表 = 前 48 槽仍是旧的 16/16/16 布局 (**前 48 槽逐字节不变**), 后 12 槽从
 *   "白扔的预留"改成绝对时间/通信/DO/强制的标量。改通道只改这张表 (再烧一次),
 *   记录尺寸/环/块结构/PC 解析全不动。
 * ★ 为什么不做"放大记录装下 256 通道": 缓冲条数 = 环字节/记录字节, 丢包风险 =
 *   一次卡内写抖动窗口内产出的条数 > 槽数。256B→1040B 使槽数 960→236 (÷4),
 *   且通道多则活跃度升、产出也升 —— 双重恶化 (220ms 最坏卡顿下会重新丢包)。
 */
static const uint32_t s_bb_map_def[BB_MAP_N] = {
    /* [0..15]  SENSOR[0..15]  —— 与旧布局的 [4..19] 逐字节对应 */
    BB_ME(0,  0), BB_ME(0,  1), BB_ME(0,  2), BB_ME(0,  3),
    BB_ME(0,  4), BB_ME(0,  5), BB_ME(0,  6), BB_ME(0,  7),
    BB_ME(0,  8), BB_ME(0,  9), BB_ME(0, 10), BB_ME(0, 11),
    BB_ME(0, 12), BB_ME(0, 13),
    /* [14..15] ★ 故障台账 (2026-09-13) —— 占用原 SENSOR[14..15] 两个槽。
     *   为什么从这里让位: 默认表把 SENSOR/WIRE/ACT 各映射 16 路, 而三个段本身
     *   支持 64/128/64 路 —— 16 只是**采样**, 且实际用量 (DI 4 + AI 3 → SENSOR[0..10])
     *   离 14 还远。映射表随头落卡 ⇒ 读端自动按新表打标签, 不用改记录尺寸/环/解析。 */
    BB_ME(8,  0), BB_ME(8,  1),
    /* [16..31] WIRE[0..15]    —— 旧 [20..35] */
    BB_ME(1,  0), BB_ME(1,  1), BB_ME(1,  2), BB_ME(1,  3),
    BB_ME(1,  4), BB_ME(1,  5), BB_ME(1,  6), BB_ME(1,  7),
    BB_ME(1,  8), BB_ME(1,  9), BB_ME(1, 10), BB_ME(1, 11),
    BB_ME(1, 12), BB_ME(1, 13), BB_ME(1, 14), BB_ME(1, 15),
    /* [32..47] ACTUATOR[0..15] —— 旧 [36..51] */
    BB_ME(2,  0), BB_ME(2,  1), BB_ME(2,  2), BB_ME(2,  3),
    BB_ME(2,  4), BB_ME(2,  5), BB_ME(2,  6), BB_ME(2,  7),
    BB_ME(2,  8), BB_ME(2,  9), BB_ME(2, 10), BB_ME(2, 11),
    BB_ME(2, 12), BB_ME(2, 13), BB_ME(2, 14), BB_ME(2, 15),
    /* [48..49] 绝对时间 —— 让 PC 能把 tick 换算成挂钟, 且**每条记录最多隔 1 秒**
     *          (TR 每秒必变 ⇒ "变化才记"至少每秒落一条 = 天然的秒级心跳) */
    BB_ME(4,  0), BB_ME(4,  1),
    /* [50..54] 通信域 Modbus —— 帧数/响应数/CRC错/异常/状态机首字 */
    BB_ME(5,  0), BB_ME(5,  1), BB_ME(5,  2), BB_ME(5,  3), BB_ME(5,  4),
    /* [55]     DO 打包位图 (= 真正锁存到引脚的电平, 与"引擎算出的输出"互为佐证) */
    BB_ME(6,  0),
    /* [56..59] 强制位图 128 bit —— 哪几路被强制过, 一次说清 */
    BB_ME(7,  0), BB_ME(7,  1), BB_ME(7,  2), BB_ME(7,  3),
};

/* ★ 绑定结果: 每槽一个**预解析好的源指针**。为什么不全在拍里查表:
 *   快照填充在 10kHz 拍内跑, 一次查表 + 分支比一次取数贵; 绑定只在 init 做一次。 */
static volatile uint32_t *s_map_p[BB_MAP_N];
static volatile uint32_t s_map_zero = 0u;   /* 空槽的源: 恒 0 */

/* ★★ 通信域被记录的量 —— 偏移用 offsetof **从结构体类型取**, 不手写数字:
 *   结构体一改, 编译期就断 (下面有断言), 不会静默错位。
 *   ★ MbCtrl_t 是 `packed` ⇒ 这四个 u32 落在偏移 9/13/17/21 (**非 4 的倍数**)。
 *     裸 u32 读在 Cortex-M7 上允许 (UNALIGN_TRP 默认关), 本文件早有先例
 *     (读 SHM+0x0D 的 u32 取 ENGINE_RUN)。 */
#define BB_COM_N 5u
static const uint32_t s_bb_com_off[BB_COM_N] = {
    (uint32_t)__builtin_offsetof(MbCtrl_t, frames_rx),   /* 0 完整帧数 */
    (uint32_t)__builtin_offsetof(MbCtrl_t, frames_tx),   /* 1 发出响应数 */
    (uint32_t)__builtin_offsetof(MbCtrl_t, err_crc),     /* 2 CRC 校验失败 */
    (uint32_t)__builtin_offsetof(MbCtrl_t, err_exc),     /* 3 异常响应数 */
    0u,                                                  /* 4 首字 state|slave|rx_len|rx_pos */
};
_Static_assert(sizeof(MbCtrl_t) == 40, "MbCtrl_t 尺寸变了 => 通信域记录偏移必须重核");
_Static_assert((uint32_t)__builtin_offsetof(MbCtrl_t, frames_rx) == 9u,
               "MbCtrl_t.frames_rx 偏移变了 => PC 端 COMM0 标签要同步");
_Static_assert((uint32_t)__builtin_offsetof(MbCtrl_t, err_exc) == 21u,
               "MbCtrl_t.err_exc 偏移变了 => PC 端 COMM3 标签要同步");

static void bb_map_bind(const uint32_t *map)
{
    uint32_t i;
    for (i = 0; i < BB_MAP_N; i++) {
        uint32_t e = map[i], seg = e >> 16, idx = e & 0xFFFFu;
        volatile uint32_t *p = &s_map_zero;
        if (seg == BB_MAP_SEG_SENSOR && idx < 64u) {
            p = (volatile uint32_t *)(s_bb_shm + OFF_SENSOR_MAP) + idx;
        } else if (seg == BB_MAP_SEG_WIRE && idx < 128u) {
            p = (volatile uint32_t *)(s_bb_shm + OFF_WIRE_MAP) + idx;
        } else if (seg == BB_MAP_SEG_ACT && idx < 64u) {
            p = (volatile uint32_t *)(s_bb_shm + OFF_ACTUATOR_STATUS) + idx;
        } else if (seg == BB_MAP_SEG_TIME && idx <= 1u) {
            p = (volatile uint32_t *)(s_bb_shm + (idx ? OFF_RTC_DR : OFF_RTC_TR));
        } else if (seg == BB_MAP_SEG_COMM && idx < BB_COM_N) {
            p = (volatile uint32_t *)(s_bb_shm + OFF_MB_CTRL + s_bb_com_off[idx]);
        } else if (seg == BB_MAP_SEG_DO && idx == 0u) {
            p = (volatile uint32_t *)(s_bb_shm + OFF_DO_SHADOW);
        } else if (seg == BB_MAP_SEG_FORCE && idx < 4u) {
            p = (volatile uint32_t *)(s_bb_shm + OFF_FORCE_MASK + idx * 4u);
        } else if (seg == BB_MAP_SEG_FAULT && idx < 2u) {
            /* 故障台账 (faultlog.h): idx 0 = 累计故障数, 1 = 末例分类码。
             * ★ 偏移用 __builtin_offsetof 从结构体取, 不手写数字 (同 COMM 的做法)。 */
            const uint8_t *b = (const uint8_t *)s_bb_shm + OFF_FAULT_LOG;
            p = (volatile uint32_t *)(b + (idx == 0u
                ? (uint32_t)__builtin_offsetof(FaultLedger_t, total)
                : (uint32_t)__builtin_offsetof(FaultLedger_t, l_code)));
        }
        s_map_p[i] = p;
    }
}

const uint32_t *bb_map(void) { return s_bb_map_def; }

uint32_t bb_map_sum(const uint32_t *map)
{
    /* FNV-1a 32bit —— 固件与 PC 端同一算法, 用来判"表头和固件是不是同一份映射" */
    uint32_t h = 2166136261u, i;
    for (i = 0; i < BB_MAP_N; i++) { h ^= map[i]; h *= 16777619u; }
    return h;
}

/* 故障台账全景 → 日志头 (布局见 blackbox.h: BB_FLT_HDR_OFF)。
 * ★ 与"记录流里的两列"的分工: 这条给**全景**(24 类计数 + 首例现场),
 *   记录流给**时间轴**(故障计数的每一次变化都带 tick 落一条)。
 * ★ 只在"头刷新"时写 ⇒ 需要**上位机显式触发** (SD_CFG[12]) 才是新鲜快照,
 *   否则就是开日志那一刻的 (通常是全 0)。这与本项目"别让固件自己按时间猜窗口"
 *   是同一条纪律: 什么时候该落一个全景, 只有上位机知道。 */
void bb_flt_into_hdr(uint32_t *h)
{
    const FaultLedger_t *lg = fault_ledger_r((const uint8_t *)s_bb_shm);
    uint32_t i;
    h[BB_FLT_HDR_OFF + 0u] = BB_FLT_HDR_MAGIC;
    h[BB_FLT_HDR_OFF + 1u] = lg->total;
    for (i = 0u; i < FAULT_N_CATS; i++) h[BB_FLT_HDR_OFF + 2u + i] = lg->cats[i];
    h[BB_FLT_HDR_OFF + 2u + FAULT_N_CATS + 0u] = lg->f_code;
    h[BB_FLT_HDR_OFF + 2u + FAULT_N_CATS + 1u] = lg->f_tick;
    h[BB_FLT_HDR_OFF + 2u + FAULT_N_CATS + 2u] = lg->f_c0;
    h[BB_FLT_HDR_OFF + 2u + FAULT_N_CATS + 3u] = lg->f_c1;
    h[BB_FLT_HDR_OFF + 2u + FAULT_N_CATS + 4u] = lg->l_code;
    h[BB_FLT_HDR_OFF + 2u + FAULT_N_CATS + 5u] = lg->l_tick;
    h[BB_FLT_HDR_OFF + 2u + FAULT_N_CATS + 6u] = lg->l_c0;
    h[BB_FLT_HDR_OFF + 2u + FAULT_N_CATS + 7u] = lg->l_c1;
}


void bb_init(uint8_t *shm_base)
{
    s_bb_shm = shm_base;

    /* ★ 绑定通道映射 (把 60 个槽解析成 SHM 源指针)。必须在 s_bb_shm 之后。 */
    bb_map_bind(s_bb_map_def);

    /* ⓪ 自观测区清零 */
    { uint32_t i; for (i = 0; i < 40u; i++) BB_DIAG[i] = 0u; }

    /* ★ 映射绑定自检 (必须放在 ⓪ 清零之后): 默认表 60 槽**全部有效** (0 个空槽) */
    {   uint32_t i, n = 0u;
        for (i = 0; i < BB_MAP_N; i++) if (s_map_p[i] != &s_map_zero) n++;
        BB_DIAG[36] = n;                     /* 实际绑到的槽数 (默认表应为 60) */
        BB_DIAG[37] = bb_map_sum(s_bb_map_def);
    }

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
    {   uint32_t ctcr_ov = sd_cfg_take(4u);
        *(volatile uint32_t *)BB_M_CTCR = (ctcr_ov != 0u) ? ctcr_ov
            : (BB_CTCR_SWRM | BB_CTCR_TRGM_BLK | BB_CTCR_TLEN(128u)
               | BB_CTCR_DSIZE_W | BB_CTCR_SSIZE_W | BB_CTCR_DINC_W | BB_CTCR_SINC_W);
        BB_DIAG[30] = *(volatile uint32_t *)BB_M_CTCR;   /* 生效的 CTCR 回读 */
    }
    {   uint32_t sent = sd_cfg_take(5u);
        if (sent != 0u) {   /* 哨兵填充: 数"变了几个字" 就是每拍实际搬运长度 */
            uint32_t i; volatile uint32_t *r = (volatile uint32_t *)BB_AXI_BASE;
            for (i = 0; i < (BB_TOTAL / 4u); i++) r[i] = BB_SENT;
            BB_DIAG[31] = 1u;
        }
    }
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
    /* ★ 每条记录自带"是哪一拍"的标注: magic + tick + seq + ctrl */
    snap[0] = BBLOG_REC_MAGIC;
    snap[1] = tick;
    snap[2] = 0u;                                /* seq 只在真写记录时才分配 */
    {   uint32_t run = *(volatile uint32_t *)(s_bb_shm + 0x0Du) & 0xFFu;
        uint32_t nr  = *(volatile uint32_t *)(s_bb_shm + 0x0Eu) & 0xFFFFu;
        snap[3] = (run << 24) | (nr & 0xFFFFu); }
    {   /* ★★ 60 个数据槽全部来自**通道映射表** (已预解析成源指针 ⇒ 拍内不查表、不分枝)。
         *   默认表 = SENSOR[0..15] / WIRE[0..15] / ACTUATOR[0..15] / 12 空槽
         *   ⇒ 与旧的"三段硬编码 16 字拷贝"**逐字节相同** (零行为变化)。
         *   改通道只改 s_bb_map_def 一张表; 记录尺寸/环/块结构/PC 解析全不动。 */
        uint32_t i;
        for (i = 0; i < BB_MAP_N; i++) snap[4 + i] = *s_map_p[i];
    }
    /* ══════════ ★★★ 变化才记 (change-triggered logging) ══════════
     * 与"上一条**已写出**记录"的 60 个槽 + 控制字逐项比; 全同 ⇒ 这一拍不写。
     *
     * ★ 为什么这样是无损的: 每条记录都是**一个完整的 256B 全量状态**且自带 tick
     *   ⇒ 两条记录之间的所有拍, 其值**必然与前者完全相同** ⇒ PC 端按 tick 就能
     *   逐拍精确复原。省掉的只是"重复", 不是"数据"。
     * ★ 为什么不做变长记录: 保持固定的 256B 槽 ⇒ 环/块结构/PC 解析全不用改,
     *   风险最小, 而收益(缓冲时间)已经拿到。
     * ★ 优雅退化: 若程序真每拍都变, 就退化成"逐拍全量" = 原行为, 不会更差。
     * 实测 (2026-09-12): 变化率 7.49% ⇒ 记录率 ~750/s ⇒ 960 槽缓冲
     *   从 96ms 提升到约 1.28 秒; 带宽 2.56MB/s -> 192KB/s。
     * ★ 比较范围 = 映射表里的 60 个槽 (不是"全部 256 通道"): 映射外的通道
     *   **根本不记** —— 这是"选择", 不是"近似"。 */
    {   uint32_t i, chg = 0u;
        for (i = 0; i < BB_MAP_N; i++) {
            if (snap[4 + i] != s_prev[i]) { chg = 1u; break; }
        }
        if (chg == 0u && snap[3] == s_prev_ctrl && s_bb_have_prev != 0u) {
            s_bb_skipped++;
            BB_DIAG[34] = s_bb_skipped;
            BB_DIAG[35] = s_bb_kicks;
            return;                              /* 不写, 也不占环槽 */
        }
        for (i = 0; i < BB_MAP_N; i++) s_prev[i] = snap[4 + i];
        s_prev_ctrl = snap[3];
        s_bb_have_prev = 1u;
    }
    snap[2] = s_bb_seq++;
    s_bb_records++;
    BB_DIAG[33] = s_bb_records;                  /* 写出的记录数 */
    BB_DIAG[34] = s_bb_skipped;                  /* 跳过的拍数 */
    BB_DIAG[35] = s_bb_kicks;                    /* 总拍数 */

    /* ② ★★ 快照搬运 = **CPU 字拷贝** (默认)。
     *
     * 为什么放弃 MDMA ch1: 实测 (tools/mdma_ctcr_sweep.py) 它**每拍只搬 64 字节 / 256**
     *   —— 用哨兵法数"变了几个字"扫了 10 组 CTCR (TRGM 00/01/10/11 × TLEN 0/63/127/255),
     *   结果与配置几乎无关, 一律 32~64 字节 ⇒ 槽的 3/4 永远是陈旧 SRAM。
     *   而 256B 的 CPU 字拷贝只要 ~130 拍, 占 100us 拍预算 **0.3%** ——
     *   为一个 256 字节的搬运留一条语义含糊的 MDMA 通道 + 一堆未定语义的 CTCR 位域,
     *   收益为负。**判据: 能简单做对的, 不要用复杂做错。**
     * MDMA 路径保留, SD_CFG[7]=1 时启用 (供对照)。 */
    if (sd_cfg_take(7u) != 0u) {
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

    s_last_dst = dst;
    }
    else
    {
        /* ★ CPU 字拷贝: 快照区 → 环槽 (64 字 = 256B) */
        volatile uint32_t *d32 = (volatile uint32_t *)(BB_AXI_BASE + s_bb_widx * BB_SLOT_SZ);
        uint32_t i;
        for (i = 0; i < (BB_SLOT_SZ / 4u); i++) d32[i] = snap[i];
    }

    /* ★ 自观测 (采样点 B): 触发瞬间的状态 */
    if (samp) {
        BB_DIAG[12] = *(volatile uint32_t *)BB_M_CISR;
        BB_DIAG[13] = *(volatile uint32_t *)BB_M_CESR;
        BB_DIAG[14] = *(volatile uint32_t *)BB_M_CCR;
        BB_DIAG[15] = *(volatile uint32_t *)BB_M_CBNDTR;
        BB_DIAG[16] = *(volatile uint32_t *)BB_M_CTCR;
    }

    /* ④ 环形递增 */
    s_bb_widx++;
    if (s_bb_widx >= BB_SLOTS) s_bb_widx = 0;
}

uint32_t bb_write_idx(void) { return s_bb_widx; }
uint32_t bb_slots_produced(void) { return s_bb_records; }   /* ★ 环里是真的记录数 */

uint32_t bb_tick_last(void)
{
    /* 读上一次写入槽的 tick (诊断: 最近快照的时间戳) */
    uint32_t idx = (s_bb_widx == 0) ? (BB_SLOTS - 1) : (s_bb_widx - 1);
    return *(volatile uint32_t *)(BB_AXI_BASE + idx * BB_SLOT_SZ);
}
