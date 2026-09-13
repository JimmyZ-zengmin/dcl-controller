/* sd.c — microSD (SDMMC1, 4-bit) 裸机驱动 — 2026-09-12 (重写 #2)
 *
 * 目标: 把 AXI 黑匣子缓冲 (128KB) 写到 SD 卡, 供 PC 端直读物理扇区分析。
 *
 * ★ 为什么不建 FAT: 写原始扇区零依赖 (不需要 FATFS 移植), PC 端用
 *   `\\.\PhysicalDriveN` 直接读同一 LBA 即可。代价是卡上的文件系统会被破坏 ——
 *   所以**写在卡的末尾区** (LBA 3000000 起, 约 1.46GB 处), 不碰盘头的分区表/FAT。
 *
 * ★★ 本版重写依据: **商家已验证例程** (鹿小班 LXB723ZG-P1 的 9.SDMMC-SD卡基本数据读写,
 *    其驱动移植于 STM32H743-EVAL, 即官方 HAL/LL)。首版是自己"照着寄存器猜"写的,
 *    逐条比对后找出 12 处偏离 —— 每一处都能单独让初始化卡死。逐条列在上面:
 *
 *   ① **CMD 寄存器整写** ⇒ 清掉了 CMDTRANS。ST 用
 *      `MODIFY_REG(CMD, CMD_CLEAR_MASK, ...)`, 而 CMD_CLEAR_MASK = CMDINDEX |
 *      WAITRESP | WAITINT | WAITPEND | CPSMEN —— **故意不含 CMDTRANS/CMDSTOP**
 *      (`stm32h7xx_ll_sdmmc.h:689`)。整写会把 CMDTRANS 抹掉。
 *   ② **数据通路总开关是 CMDTRANS, 不是 DCTRL.DTEN**。HAL 四条读写路径
 *      (polling/IT/DMA × read/write) 全部是 `DPSM = DISABLE` + `__SDMMC_CMDTRANS_ENABLE`
 *      (`stm32h7xx_hal_sd.c:716-718 / 901-903 / 1086-1088 / 1181-1184 / 1278-1281 / 1377-1381`)。
 *      首版设了 DTEN=1 却从不设 CMDTRANS ⇒ DPSM 永不启动。
 *   ③ DCTRL 的 DBLOCKSIZE 用 9 (=512B), DTMODE=0(块), DTEN=0, DTDIR 按方向。
 *   ④ **WAITRESP 只有三个合法编码**: 00=无响应 / 01=短响应 / 10=长响应。
 *      首版用的 11 在 HAL 里根本没有对应宏 ⇒ 语义未定义。
 *   ⑤ **R3 (ACMD41 的 OCR) 没有 CRC 字段** ⇒ 必须忽略 CCRCFAIL:
 *      `SDMMC_GetCmdResp3()` 只认 CTIMEOUT (`stm32h7xx_ll_sdmmc.c:1454`)。
 *      首版把 CCRCFAIL 当致命错误 ⇒ ACMD41 永远失败。
 *   ⑥ **必须等 CPSMACT 落下**。ST 的等待条件一律是
 *      `((sta & (ERR|REND|TIMEOUT)) == 0) || ((sta & CMDACT) != 0)`,
 *      而 **CMDACT 就是 CPSMACT (bit13)** (`ll_sdmmc.h:624`)。
 *      首版一见 CMDREND 就返回 ⇒ 读 RESP1 时 CPSM 还在跑, 且下一条命令撞上一条。
 *   ⑦ **必须核对 RESPCMD == 发出去的 index** (R1/R6), 否则可能是上一条的残留响应。
 *   ⑧ **GPIO 必须上拉** (商家 `gpio_init_structure.Pull = GPIO_PULLUP`)。SD 规范要求
 *      主机对 CMD/DAT 提供上拉; 缺了它就是靠外部电阻, 高速下极易 CRC 错。首版完全没有。
 *   ⑨ **识别期用 1-bit 总线** (`HAL_SD_InitCard` 里 `Init.BusWide = SDMMC_BUS_WIDE_1B`,
 *      `hal_sd.c:478`), 识别成功后才 ACMD6 + 切 4-bit。首版全程 4-bit。
 *   ⑩ **FIFO 半满整批搬运**: 写看 TXFIFOHE(bit14) 一次灌 8 个字;
 *      读看 RXFIFOHF(bit15) 一次取 8 个字 (`hal_sd.c:935-951`)。
 *      首版用 TXFIFOE/RXFIFOE 一个一个字搬 —— 既慢又跟 DPSM 抢时序。
 *   ⑪ **时钟**: SDMMC_CK = sdmmc_ker_ck / (2 × CLKDIV)。本机 ker_ck = PLL1Q = 100MHz
 *      (CLK_PLL1_DIVQ1=4, VCO 400MHz) ⇒ 400kHz 对应 CLKDIV=125, 25MHz 对应 CLKDIV=2。
 *      商家用 110MHz/CLKDIV=2 = 27.5MHz。**首版"CLKDIV>16 不出时钟"的结论存疑**
 *      (同硅片商家在 401kHz 跑通了识别) ⇒ 本版改成**上电自检扫描 + 记录**, 让芯片自己说。
 *   ⑫ 时钟/GPIO/复位顺序照 `SD_MspInit`: CLK_ENABLE → GPIO → FORCE_RESET/RELEASE_RESET。
 */
#include "sd.h"
#include "itcm.h"       /* ★ ISR 调用树必须住 ITCM —— 见该头文件 (sd_cfg_take) */
#include "regs.h"
#include "blackbox.h"   /* 记录格式 BB_SLOT_SZ / bb_slots_produced() */

/* ── SDMMC1 (D1 域, 0x52007000) ── */
#define SDMMC1_BASE   0x52007000u
#define SD_POWER      REG32(SDMMC1_BASE + 0x00u)
#define SD_CLKCR      REG32(SDMMC1_BASE + 0x04u)
#define SD_ARG        REG32(SDMMC1_BASE + 0x08u)
#define SD_CMD        REG32(SDMMC1_BASE + 0x0Cu)
#define SD_RESPCMD    REG32(SDMMC1_BASE + 0x10u)
#define SD_RESP1      REG32(SDMMC1_BASE + 0x14u)
#define SD_RESP2      REG32(SDMMC1_BASE + 0x18u)
#define SD_RESP3      REG32(SDMMC1_BASE + 0x1Cu)
#define SD_RESP4      REG32(SDMMC1_BASE + 0x20u)
#define SD_DTIMER     REG32(SDMMC1_BASE + 0x24u)
#define SD_DLEN       REG32(SDMMC1_BASE + 0x28u)
#define SD_DCTRL      REG32(SDMMC1_BASE + 0x2Cu)
#define SD_DCOUNT     REG32(SDMMC1_BASE + 0x30u)
#define SD_STA        REG32(SDMMC1_BASE + 0x34u)
#define SD_ICR        REG32(SDMMC1_BASE + 0x38u)
#define SD_IDMACTRL   REG32(SDMMC1_BASE + 0x50u)
#define SD_IDMABSIZE  REG32(SDMMC1_BASE + 0x54u)
#define SD_IDMABASE0  REG32(SDMMC1_BASE + 0x58u)
#define SD_FIFO       REG32(SDMMC1_BASE + 0x80u)

/* ── STA 位 (逐条核准商家 CMSIS stm32h723xx.h 的 SDMMC_STA_*_Pos) ── */
#define STA_CCRCFAIL   (1u << 0)
#define STA_DCRCFAIL   (1u << 1)
#define STA_CTIMEOUT   (1u << 2)
#define STA_DTIMEOUT   (1u << 3)
#define STA_TXUNDERR   (1u << 4)
#define STA_RXOVERR    (1u << 5)
#define STA_CMDREND    (1u << 6)
#define STA_CMDSENT    (1u << 7)
#define STA_DATAEND    (1u << 8)
#define STA_DHOLD      (1u << 9)
#define STA_DBCKEND    (1u << 10)
#define STA_DABORT     (1u << 11)
#define STA_DPSMACT    (1u << 12)
#define STA_CPSMACT    (1u << 13)   /* ★ 就是 HAL 的 CMDACT */
#define STA_TXFIFOHE   (1u << 14)   /* ★ 写用: TX FIFO 半空 */
#define STA_RXFIFOHF   (1u << 15)   /* ★ 读用: RX FIFO 半满 */
#define STA_TXFIFOF    (1u << 16)
#define STA_RXFIFOF    (1u << 17)
#define STA_TXFIFOE    (1u << 18)
#define STA_RXFIFOE    (1u << 19)
#define STA_BUSYD0     (1u << 20)
#define STA_BUSYD0END  (1u << 21)
#define STA_IDMATE     (1u << 27)   /* IDMA 传输错误 */
#define STA_IDMABTC    (1u << 28)   /* IDMA 缓冲区传输完成 */

/* 可写的静态标志 (HAL 的 SDMMC_STATIC_FLAGS; ICR 写 1 清) */
#define STA_STATIC     (STA_CCRCFAIL | STA_DCRCFAIL | STA_CTIMEOUT | STA_DTIMEOUT | \
                        STA_TXUNDERR | STA_RXOVERR  | STA_CMDREND  | STA_CMDSENT  | \
                        STA_DATAEND  | STA_DHOLD    | STA_DBCKEND  | STA_DABORT   | \
                        STA_BUSYD0END | STA_IDMATE | STA_IDMABTC)
/* 数据期致命标志 */
#define STA_DATAERR    (STA_DCRCFAIL | STA_DTIMEOUT | STA_TXUNDERR | STA_RXOVERR | \
                        STA_DABORT | STA_IDMATE)
#define STA_DATAOK     (STA_DATAEND | STA_DBCKEND)

/* ── CMD 位 (SDMMC_CMD_*_Pos 逐条核准) ── */
#define CMD_CMDINDEX_M (0x3Fu)
#define CMD_CMDTRANS   (1u << 6)    /* ★ CPSM 把该命令当数据传输 (数据通路总开关) */
#define CMD_CMDSTOP    (1u << 7)
/* ★★ WAITRESP 只有三个**有意义的值**, 编码沿用经典约定:
 *      00 = 无响应      01 = 短响应(48bit)      **11 = 长响应(136bit)**      10 = 未定义
 *   权威依据 (stm32h7xx_ll_sdmmc.h:418-420):
 *      SDMMC_RESPONSE_NO    = 0x00000000
 *      SDMMC_RESPONSE_SHORT = SDMMC_CMD_WAITRESP_0   -> 0b01 = 1
 *      SDMMC_RESPONSE_LONG  = SDMMC_CMD_WAITRESP     -> 0b11 = 3   (整个 2 位掩码!)
 *   ★★ 这是本驱动第 14 个偏离点, 也是 CMD3 永远 CCRCFAIL 的真因:
 *      长响应曾写成 0b10 (未定义编码) ⇒ CMD2 的 136 位响应没有被完整接收/校验,
 *      残余位仍在 CMD 线上 ⇒ 紧接着发出的 CMD3 命令帧与残位相撞 ⇒ 响应被采坏,
 *      STA = 0x00000001 (CCRCFAIL), RESP1 = 0x17CD3FFF (RCA 像样但状态位全 1 = 垃圾)。
 *      实测 (2026-09-12): 改 0b10→0b11 前, 3/3 轮识别全部卡在 CMD3。 */
#define CMD_WAITRESP_N (0u << 8)    /* 00: 无响应 (CMD0) */
#define CMD_WAITRESP_S (1u << 8)    /* 01: 短响应 R1/R3/R6/R7 */
#define CMD_WAITRESP_L (3u << 8)    /* 11: 长响应 136bit R2 (CID/CSD) ★ 不是 2 */
#define CMD_WAITINT    (1u << 10)
#define CMD_WAITPEND   (1u << 11)
#define CMD_CPSMEN     (1u << 12)
/* ★★ 与 ST 完全一致: **不含 CMDTRANS / CMDSTOP** (ll_sdmmc.h:689) */
#define CMD_CLEAR_MASK (CMD_CMDINDEX_M | CMD_WAITRESP_L | \
                        CMD_WAITINT | CMD_WAITPEND | CMD_CPSMEN)

/* ── CLKCR 位 ── */
#define CLKCR_WIDBUS_1B (0u << 14)
#define CLKCR_WIDBUS_4B (1u << 14)

/* ── SD 协议常量 (与 HAL 同值) ── */
/* ★★ ACMD41 的实参: VOLTAGE_WINDOW_SD | HIGH_CAPACITY, **不带 SD_SWITCH_1_8V_CAPACITY**。
 *   ST 的 HAL 默认带上 1.8V 请求位 (S18R=bit24), 但那只在板上有 1.8V 电平转换
 *   (USE_SD_TRANSCEIVER) 时才有意义 —— 本板**没有**, 而且我们绝不发 CMD11。
 *   实测证据 (2026-09-12): 带 S18R 时 OCR 回 `0xC1FF8000`, **bit24 (S18A) = 1**
 *   ⇒ 卡已被告知"主机可能要切 1.8V" 却永远等不到 CMD11 ⇒ 之后 CMD3 的 R6
 *   判 CRC 失败 (STA=0x00000001)。去掉 S18R 后 OCR 的 bit24 应为 0, 状态机不再悬空。 */
#define SD_ARG_HCS     0xC0100000u  /* HIGH_CAPACITY | VOLTAGE_WINDOW_SD (无 S18R) */
#define SD_ARG_CHECK8  0x000001AAu
#define SD_BLK_BITS    9u           /* DBLOCKSIZE=9 ⇒ 512B */
#define SD_CLK_ID_HZ   400000u      /* 识别期上限 */

volatile uint32_t g_sd_init_stage = 0;
volatile uint32_t g_sd_status = 0;
volatile uint32_t g_sd_rca = 0;
volatile uint32_t g_sd_blocks = 0;

/* ★ 固定地址诊断区 (pyocd 直读, 不依赖符号表):
 *   [0]  stage          [1]  失败点 STA
 *   [2]  RCA            [3]  写成功块数
 *   [4]  返回码         [5]  上一条命令的 STA
 *   [6]  clkcr          [7]  power
 *   [8]  (原 marker, 见 [20]) [9]  RESPCMD
 *   [10] RESP1          [11] 识别分频(k)
 *   [12] 写 LBA         [13] 写失败 STA
 *   [14] 已写块数       [15] 复位次数
 *   [20] marker (0xBAxx=失败点 / 0x600D=全通)  ← 原放 [8]
 *   [21] 最近一次 RESP1      [22] CID[127:96]  [23] CID[95:64]
 *   [28] 读失败时的 STA      [26] 回读校验 (0=256块逐字全对)
 *   [30] 数据期 CLKCR        [31] CMD6(HS) 的 R1   [32] 用的 CLKDIV
 *   [33] 落盘耗时(拍,100us)  [34] 写成块数        [35] 累计块数
 *   [37] sd_cmd(25) 返回码    [38] CMD25 后的 STA   [39] 数据等待结束 STA
 *   [40] CMD12 后的 STA        [41] 卡在第几批      [42] 性能模式标志
 *   [43] CMD23 的 R1           [44] 1=走CMD12收尾 (0=CMD23 已限长)
 *   [24] 识别用了几轮(1..3)   [25] sd_identify 返回码
 *   [26] 回读不符块数(0=全对)  [27] 回读失败 (块号<<8)|错误码
 *   [16..19] 分频自检: (CLKDIV<<8) | 结果码 (0=OK)
 *   结果码: 1=CMD0 无 CMDSENT  2=CMD8 失败  3=CMD8 回显不符
 */
#define SD_DIAG  ((volatile uint32_t *)0x24000200u)

/* ★★ 实验配置字 (2026-09-12, 提速研究用): 调试器**在复位前预写**, 固件读一次即清。
 *   好处: 换 CLKDIV / 开关 High Speed 不需要重新编译烧录 (项目"非侵入式交互"纪律)。
 *   [0] 数据期 CLKDIV (0 / 未写 ⇒ 用默认 SD_DATA_CLKDIV)
 *   [1] =1 ⇒ 识别后发 CMD6 把卡切到 High Speed (50MHz 前必须; 25MHz 不需要)
 * ★★ AXI SRAM 布局 (2026-09-12 重排, 空出整块冻结区):
 *   0x24000000 低 16KB 预留   0x24000200 SD_DIAG   0x24000300 BB_DIAG
 *   0x24000400 SD_CFG         0x24001000 SD 校验 scratch(512B)
 *   0x24003000/0x24003100     do.c 锁存快照 / MDMA 链表节点 (勿动)
 *   0x24004000 + 240KB        黑匣子 RAM 环 (**960** 槽 × 256B = 96ms 缓冲)
 *   0x24040000 + 64KB         **SD 冻结区** (落盘前把这一批拷过来, 见 sd_log_one_batch)
 * ★ 环做大的唯一理由: 丢包判据是"落后量 > 槽数"(avail > BB_SLOTS)。
 *   增大槽数 = 提高对**瞬时卡顿**(SD 卡编程尾巴变长)的吸收量。
 *   读完清零 ⇒ 不会悄悄改变下次上电的行为。 */
#define SD_CFG      ((volatile uint32_t *)0x24000400u)
#define SD_DATA_CLKDIV_DEF  1u      /* 100MHz/(2*1) = 50MHz (实测 5.63MB/s) */
#define SD_RING    ((const uint8_t *)0x24004000u)   /* 黑匣子 RAM 环 (活的, 512×256B) */
#define SD_STAGE   ((uint8_t *)0x24040000u)         /* 64KB 冻结/暂存区 (紧接 240KB 环之后) */
#define SD_HS_ARG           0x80FFFFF1u  /* CMD6 SET, group1(Access Mode) = 1 (High Speed) */

static uint32_t s_sd_ready = 0;

volatile uint32_t g_sd_capacity_blocks = 0;   /* 卡容量 (512B 块数, 由 CSD 解析) */
static uint32_t g_sd_csd[4];

/* ── SD 日志 (专用裸介质): LBA0 = 头部, LBA1.. = 数据区 (环形回卷) ──
 * 为什么专用裸介质: 要"每拍连续落盘 + 无限增长 + 回卷", 就不可能和文件系统共存
 * (写进数据区会把文件写坏)。PC 端用 tools/sd_read_raw.py / sd_log_read.py 直读。 */
#define LOG_HDR_MAGIC   0x474F4C44u   /* "DLOG" */
#define LOG_VERSION     1u
#define LOG_BATCH_SLOTS 256u          /* 每批 256 条 = 64KB = 128 块 (小批延迟吃掉带宽) */
#define LOG_HDR_EVERY   32u           /* 每 32 批刷一次头部 (≈1MB) */

static uint32_t s_log_ready = 0;
static uint32_t s_log_slot_base = 0;  /* ★ 环消费游标: 下一条要落盘的记录是 bb 的第几条 */
static uint32_t s_log_total = 0;      /* 落到卡上的记录总数 (跨上电累计) */
static uint32_t s_log_blk = 0;        /* 数据区内的块游标 (0 起) */
static uint32_t s_log_batches = 0;
static uint32_t s_log_hdr_flush = 0;
static uint32_t s_log_dropped = 0;
static uint32_t s_log_wrapped = 0;
static uint32_t s_log_last_rc = 0;
static uint32_t s_log_last_seq = 0;
static uint32_t s_log_last_tick = 0;
static uint32_t s_log_polls = 0;        /* poll 调用次数 (自观测) */
static uint32_t s_log_max_avail = 0;    /* 见过的最大待落盘条数 (判据: 是否接近 512) */
static uint32_t s_log_drop_open = 0;    /* 开日志时的丢包基线 ⇒ 可算本次上电丢包 */
static uint32_t s_log_blk_open = 0;     /* 开日志时的块基线 ⇒ 可算本次上电写了多少 */
static uint32_t s_log_hdr_pend = 0;     /* 有亟待刷新的日志头 (见 sd_log_one_batch 注释) */
static uint32_t s_log_batch_err = 0;   /* 批写失败次数 (含 wait_ready 超时) */
static uint32_t s_log_batch = LOG_BATCH_SLOTS;  /* 本批槽数 (可由 SD_CFG[9] 覆盖, 供扫批大小) */
#define LOG_BATCH_MIN 32u
#define LOG_BATCH_MAX LOG_BATCH_SLOTS          /* 上限 = 冻结区能装下的槽数 (64KB/256B) */

#define SD_PWRUP_DLY   60000u       /* ~1ms @400MHz (HAL 用 HAL_Delay(1)) */
#define SD_TMOUT_CMD   4000000u     /* 命令/RESP 等待上限 (循环计数) */
#define SD_TMOUT_DATA  40000000u    /* 数据期等待上限 */

static void sd_delay(uint32_t n) { volatile uint32_t i = n; while (i--) { } }

/* 外设软复位 + 设定识别分频 + 上电 (等价 SD_MspInit 的 FORCE/RELEASE_RESET) */
static void sd_hw_reset(uint32_t cdiv)
{
    *(volatile uint32_t *)0x5802447Cu |= (1u << 16);    /* AHB3RSTR.SDMMC1RST=1 */
    __asm__ volatile("dsb; isb" ::: "memory");
    *(volatile uint32_t *)0x5802447Cu &= ~(1u << 16);   /* =0 释放 */
    __asm__ volatile("dsb; isb" ::: "memory");
    SD_CLKCR = cdiv | CLKCR_WIDBUS_1B;                  /* ★ 识别期一律 1-bit */
    SD_POWER = 3u;                                      /* PWRCTRL=11 上电 */
    sd_delay(SD_PWRUP_DLY);
    SD_DIAG[15]++;
}

/* 发命令 + 严格等响应 (逐条对齐 ST 的 SDMMC_GetCmdResp{1,2,3,7} 与 GetCmdError)。
 * @param resp       CMD_WAITRESP_N / _S / _L
 * @param out        非空则回填 RESP1
 * @param crc_check  1 = 该响应的规范里**有** CRC 字段 (R1/R2/R6/R7) ⇒ CCRCFAIL 算错误;
 *                   0 = **没有** CRC 字段 (R3: ACMD41 的 OCR) ⇒ CCRCFAIL 属正常完成。
 *                   ★★ 这是本驱动的第 13 个偏离点, 也是 ACMD41 卡死的真因:
 *                      `SDMMC_GetCmdResp3()` 只认 CTIMEOUT, **既不要求 CMDREND,
 *                      也不把 CCRCFAIL 当错误** (ll_sdmmc.c:1437-1467)。
 *                      实测证据: STA=0x00000001 (仅 CCRCFAIL) + RESPCMD=55。
 * @retval 0 OK; -2 CTIMEOUT; -3 CCRCFAIL; -4 CPSM 未落; -5 缺 CMDSENT; -6 缺响应; -7 RESPCMD 不符 */
static int sd_cmd(uint32_t idx, uint32_t arg, uint32_t resp, uint32_t *out, int crc_check)
{
    uint32_t g, got = 0, term, st, ok;

    SD_ICR = STA_STATIC;
    SD_ARG = arg;
    /* ★ 只改 CMD_CLEAR_MASK 覆盖的位 —— 保住数据命令需要的 CMDTRANS */
    SD_CMD = (SD_CMD & ~CMD_CLEAR_MASK)
           | (idx & CMD_CMDINDEX_M) | resp | CMD_CPSMEN;

    term = (resp == CMD_WAITRESP_N) ? (STA_CMDSENT | STA_CTIMEOUT)
                                    : (STA_CMDREND | STA_CCRCFAIL | STA_CTIMEOUT);
    for (g = 0; g < SD_TMOUT_CMD; g++) {
        st = SD_STA;
        /* ★★ 关键: 标志到了还不够, 必须等 CPSMACT(CMDACT) 落下 (ll_sdmmc.h:624) */
        if ((st & term) && !(st & STA_CPSMACT)) { got = 1; break; }
    }
    st = SD_STA;
    SD_DIAG[5] = st;
    SD_DIAG[21] = SD_RESP1;      /* 每次都留一份 RESP1 —— 失败时才有东西可看 */
    if (!got) { g_sd_status = st; return -4; }

    if (st & STA_CTIMEOUT)                     { g_sd_status = st; return -2; }
    if (resp == CMD_WAITRESP_N) {
        if (!(st & STA_CMDSENT))               { g_sd_status = st; return -5; }
    } else if (crc_check) {
        if (st & STA_CCRCFAIL)                 { g_sd_status = st; return -3; }
        if (!(st & STA_CMDREND))               { g_sd_status = st; return -6; }
        /* ★ R1/R6 必须核对是本条命令的响应; R2(长响应) 照 ST GetCmdResp2 不核对 */
        if ((resp == CMD_WAITRESP_S) && ((SD_RESPCMD & 0x3Fu) != idx)) {
            g_sd_status = SD_RESPCMD; return -7;
        }
        SD_DIAG[9] = SD_RESPCMD;
    } else {
        /* 无 CRC 字段的响应 (R3): CMDREND 或 CCRCFAIL 都算"收到了", 只认 CTIMEOUT */
        ok = st & (STA_CMDREND | STA_CCRCFAIL);
        if (ok == 0u)                          { g_sd_status = st; return -6; }
        SD_DIAG[9] = SD_RESPCMD;               /* 照 ST 只作观测, 不作判据 */
    }
    if (out) *out = SD_RESP1;
    SD_ICR = STA_STATIC;
    return 0;
}

/* 识别期分频自检: 该分频下 CMD0 + CMD8 能不能走通。
 * 这是"能失败"的判据 —— CMD8 必须回显 0x1AA 才算过。 */
static int sd_probe_div(uint32_t cdiv)
{
    uint32_t r = 0;
    sd_hw_reset(cdiv);
    if (sd_cmd(0, 0, CMD_WAITRESP_N, 0, 0) != 0) return 1;
    if (sd_cmd(8, SD_ARG_CHECK8, CMD_WAITRESP_S, &r, 1) != 0) return 2;
    if ((r & 0xFFFu) != SD_ARG_CHECK8) return 3;
    return 0;
}

/* 一次完整的卡识别 (CMD0 → CMD8 → ACMD41 → CMD2 → CMD3 → CMD9 → CMD7 → CMD16
 * → 提频 → ACMD6 → 4-bit)。全程 1-bit 总线, 照 HAL_SD_InitCard 的次序。
 * @retval 0 = 成功 (s_sd_ready 由调用方置位) */
static int sd_identify(uint32_t chosen)
{
    uint32_t r = 0;

    sd_hw_reset(chosen);
    SD_DIAG[6] = SD_CLKCR; SD_DIAG[7] = SD_POWER;

    g_sd_init_stage = 3;
    { int rc = sd_cmd(0, 0, CMD_WAITRESP_N, 0, 0);        /* CMD0 GO_IDLE_STATE */
      SD_DIAG[0] = 3; SD_DIAG[4] = (uint32_t)rc;
      if (rc != 0) { SD_DIAG[1] = SD_STA; SD_DIAG[20] = 0xBAD0; return -2; } }

    g_sd_init_stage = 4;
    if (sd_cmd(8, SD_ARG_CHECK8, CMD_WAITRESP_S, &r, 1) != 0) {   /* CMD8 SEND_IF_COND */
        SD_DIAG[0] = 4; SD_DIAG[1] = SD_STA; SD_DIAG[20] = 0xBAD8; return -3;
    }
    if ((r & 0xFFFu) != SD_ARG_CHECK8) {
        SD_DIAG[0] = 5; SD_DIAG[1] = r; SD_DIAG[20] = 0xBAD9; return -4;
    }

    g_sd_init_stage = 5;
    {   /* ACMD41 循环: R3 无 CRC ⇒ crc_check=0 */
        uint32_t tries = 0;
        r = 0;
        do {
            if (sd_cmd(55, 0, CMD_WAITRESP_S, 0, 1) != 0) {     /* CMD55 APP_CMD */
                SD_DIAG[0] = 6; SD_DIAG[1] = SD_STA; SD_DIAG[20] = 0xBA55; return -5;
            }
            if (sd_cmd(41, SD_ARG_HCS, CMD_WAITRESP_S, &r, 0) != 0) {  /* ACMD41 OCR */
                SD_DIAG[0] = 7; SD_DIAG[1] = SD_STA; SD_DIAG[20] = 0xBA41; return -6;
            }
            if (++tries > 20000u) { SD_DIAG[0] = 8; SD_DIAG[1] = r; return -7; }
        } while (!(r & 0x80000000u));       /* 等 busy=0 (上电完成) */
        SD_DIAG[10] = r;
    }

    g_sd_init_stage = 6;
    if (sd_cmd(2, 0, CMD_WAITRESP_L, &r, 1) != 0) {      /* CMD2 ALL_SEND_CID (R2) */
        SD_DIAG[0] = 9; SD_DIAG[1] = SD_STA; SD_DIAG[20] = 0xBA02; return -8;
    }
    /* ★ CID 前两字留档: RESP1>>24 应是厂家 ID (合法值 0x01..0x9F) ——
     *   这是"CMD2 到底有没有收到真 CID"的独立判据, 不靠"命令返回 0"自证。 */
    SD_DIAG[22] = r;
    SD_DIAG[23] = SD_RESP2;

    g_sd_init_stage = 7;
    r = 0;
    if (sd_cmd(3, 0, CMD_WAITRESP_S, &r, 1) != 0) {      /* CMD3 SEND_REL_ADDR (R6) */
        SD_DIAG[0] = 10; SD_DIAG[1] = SD_STA; SD_DIAG[20] = 0xBA03; return -9;
    }
    g_sd_rca = (r >> 16) & 0xFFFFu;
    if (g_sd_rca == 0u) { SD_DIAG[0] = 11; SD_DIAG[20] = 0xBA3A; return -10; }
    SD_DIAG[2] = g_sd_rca;

    g_sd_init_stage = 8;
    if (sd_cmd(9, g_sd_rca << 16, CMD_WAITRESP_L, 0, 1) != 0) {   /* CMD9 SEND_CSD (R2) */
        SD_DIAG[0] = 12; SD_DIAG[1] = SD_STA; SD_DIAG[20] = 0xBA09; return -11;
    }
    /* ★ CSD 四字留档 → 供卡容量解析 (日志回卷要用) */
    g_sd_csd[0] = r; g_sd_csd[1] = SD_RESP2; g_sd_csd[2] = SD_RESP3; g_sd_csd[3] = SD_RESP4;
    {   /* CSD v2 (SDHC/SDXC): C_SIZE = CSD[69:48]; 容量 = (C_SIZE+1) × 512KB */
        uint32_t csize = ((g_sd_csd[1] & 0x3Fu) << 16) | ((g_sd_csd[2] >> 16) & 0xFFFFu);
        g_sd_capacity_blocks = (csize + 1u) * 1024u;
        SD_DIAG[46] = csize;
        SD_DIAG[54] = g_sd_capacity_blocks;
    }
    g_sd_init_stage = 9;
    if (sd_cmd(7, g_sd_rca << 16, CMD_WAITRESP_S, 0, 1) != 0) {   /* CMD7 SELECT (R1b) */
        SD_DIAG[0] = 13; SD_DIAG[1] = SD_STA; SD_DIAG[20] = 0xBA07; return -12;
    }
    g_sd_init_stage = 10;
    if (sd_cmd(16, SD_BLK_SZ, CMD_WAITRESP_S, 0, 1) != 0) {       /* CMD16 SET_BLOCKLEN */
        SD_DIAG[0] = 14; SD_DIAG[1] = SD_STA; SD_DIAG[20] = 0xBA10; return -13;
    }

    /* 数据期总线宽度 = **1-bit**, 时钟由实验配置字决定 (默认 25MHz)。
     *
     * ★★ 为什么不做 ACMD6 切 4-bit (HAL 会做) —— 实测证据 (2026-09-12):
     *   · 4-bit @25MHz: 写 256/256 块全过, 但**回读第 0 块即 DCRCFAIL**
     *     (STA bit1), 降到 6.25MHz 仍同一位置同一错误 ⇒ 与时钟/采样余量无关;
     *   · ACMD6(arg=2) 的 R1 = 0x920, 逐位与 SDMMC_OCR_ERRORBITS(0xFDFFE008)
     *     相与为 0 ⇒ **卡声称接受了 4-bit**;
     *   · 但双端一起回到 1-bit 后, 回读 256/256 块**逐字完全一致** (校验不符 = 0)。
     *   ⇒ 现象是"主机→卡"方向 4 条线都通(否则卡的 CRC 会失败), 只有"卡→主机"方向
     *     的多通道采样不成 —— **这是我们的问题, 不是卡的问题**(卡为正品, 用户确认)。
     *     嫌疑: CLKCR.SELCLKRX 接收时钟选择 / 多通道采样余量。
     *   ⏳ 待查清后再开 4-bit; 在此之前用 1-bit + 提高时钟 + 多块写达到带宽。 */
    {   uint32_t cdiv = SD_CFG[0];
        uint32_t hs   = SD_CFG[1];
        SD_CFG[0] = 0u; SD_CFG[1] = 0u;                  /* 一次性 */
        if (cdiv == 0u) cdiv = SD_DATA_CLKDIV_DEF;
        /* HS 默认规则: cdiv==1(50MHz) 时默认开 (50MHz 正式档是 High Speed);
         * [1]==2 可强制关掉做对照, [1]==1 强制开。 */
        if (hs == 2u) hs = 0u;
        else if (hs == 0u) hs = (cdiv == 1u) ? 1u : 0u;
        SD_DIAG[32] = cdiv;
        if (hs != 0u) {
            /* CMD6 SWITCH_FUNC: 把 Access Mode 切到 High Speed (50MHz 前必须)。
             * 在**当前低速**下发, 成功后再提频。 */
            (void)sd_cmd(6, SD_HS_ARG, CMD_WAITRESP_S, &r, 1);
            SD_DIAG[31] = r;                             /* R1 留档 */
        }
        SD_CLKCR = cdiv | CLKCR_WIDBUS_1B;
        sd_delay(SD_PWRUP_DLY);
        SD_DIAG[30] = SD_CLKCR;
    }
    (void)r;
    return 0;
}

/* 落盘耗时/吞吐自观测 (由调用方计时后回填; 用拍数 100us/拍, 不依赖 DWT) */
void sd_set_perf(uint32_t write_ticks, uint32_t verify_ticks, uint32_t verify_res)
{
    SD_DIAG[33] = write_ticks;            /* 写耗时 (拍, 100us/拍) */
    SD_DIAG[34] = SD_DIAG[14];            /* 写成块数 */
    SD_DIAG[35] = verify_ticks;           /* 校验耗时 (拍) */
    SD_DIAG[36] = verify_res;             /* 校验结果 */
}

int sd_init(void)
{
    uint32_t chosen = 0, k;
    static const uint16_t cdivs[] = { 125u, 32u, 8u, 2u };   /* 400k / 1.56M / 6.25M / 25M */

    /* ⓪ 诊断区先清零。
     * ★ 2026-09-12 教训: 不清零时"没写过"和"写成了垃圾"外观完全一样 ——
     *   上一轮把没写的字读成"合法值", 据此编出了两个错误结论。
     *   清零后每个字的语义唯一: 0 = 未到达, 非 0 = 到达并有结论。 */
    for (k = 0; k < 64u; k++) SD_DIAG[k] = 0u;

    /* ① 时钟: SDMMC1 在 D1 域 AHB3; 内核时钟 = PLL1Q (D1CCIPR.SDMMCSEL=0) */
    *(volatile uint32_t *)0x580244D4u |= (1u << 16);   /* RCC_AHB3ENR.SDMMC1EN */
    *(volatile uint32_t *)0x5802444Cu &= ~(1u << 16);  /* SDMMCSEL = 0 ⇒ PLL1Q = 100MHz */
    __asm__ volatile("dsb; isb" ::: "memory");

    /* ② GPIO: PC8=D0 PC9=D1 PC10=D2 PC11=D3 PC12=CK + PD2=CMD, AF12
     *    MODER=AF / AFR=12 / OSPEEDR=very high / ★ PUPDR=上拉 (首版完全缺失) */
    RCC_AHB4ENR |= (1u << 2) | (1u << 3);              /* GPIOCEN | GPIODEN */
    __asm__ volatile("dsb; isb" ::: "memory");
    {
        volatile uint32_t *moder = (volatile uint32_t *)0x58020800u;   /* GPIOC */
        volatile uint32_t *ospeed = (volatile uint32_t *)0x58020808u;
        volatile uint32_t *pupdr = (volatile uint32_t *)0x5802080Cu;
        volatile uint32_t *afrh  = (volatile uint32_t *)0x58020824u;
        *moder  = (*moder  & ~(0x3FFFu << 16)) | (0x2AAu << 16);   /* PC8..12 = AF */
        *afrh   = (*afrh   & ~(0xFFFFFu))       | (0xCCCCCu);      /* AFRH[4:0]=12 */
        *ospeed = (*ospeed & ~(0x3FFFu << 16)) | (0x3FFFu << 16);  /* very high */
        *pupdr  = (*pupdr  & ~(0x3FFFu << 16)) | (0x1555u << 16);  /* ★ 全上拉 01 */
    }
    {
        volatile uint32_t *moder = (volatile uint32_t *)0x58020C00u;   /* GPIOD */
        volatile uint32_t *ospeed = (volatile uint32_t *)0x58020C08u;
        volatile uint32_t *pupdr = (volatile uint32_t *)0x58020C0Cu;
        volatile uint32_t *afrl  = (volatile uint32_t *)0x58020C20u;
        *moder  = (*moder  & ~(3u << 4)) | (2u << 4);   /* PD2 = AF */
        *pupdr  = (*pupdr  & ~(3u << 4)) | (1u << 4);   /* ★ 上拉 01 */
        *ospeed = (*ospeed & ~(3u << 4)) | (3u << 4);   /* very high */
        *afrl   = (*afrl   & ~(0xFu << 8)) | (0xCu << 8); /* AF12 */
    }

    /* ③ 识别期分频自检 (把"芯片实际能出时钟的分频"写成可读回的证据) */
    g_sd_init_stage = 1;
    for (k = 0; k < (uint32_t)(sizeof(cdivs) / sizeof(cdivs[0])); k++) {
        int pr = sd_probe_div(cdivs[k]);
        SD_DIAG[16 + k] = ((uint32_t)cdivs[k] << 8) | (uint32_t)(pr & 0xFFu);
        if (pr == 0) { chosen = cdivs[k]; break; }
    }
    if (chosen == 0u) {
        g_sd_init_stage = 2;
        SD_DIAG[0] = 2; SD_DIAG[1] = SD_STA; SD_DIAG[20] = 0xBAD1;
        return -1;
    }
    SD_DIAG[11] = chosen;

    /* ④ 正式识别: 整轮最多重试 3 次。
     * ★ 为什么是"整轮"重试而不是"单条"重试: 卡的状态迁移不可逆 ——
     *   CMD3 若"命令发出去了、但响应被采坏", 卡其实已经进入 STBY 并分到了 RCA,
     *   此时单发 CMD3 属于非法命令。**唯一干净的恢复手段是 CMD0 重来。**
     *   尝试次数记进 [24]: "第几次才通过"本身就是结论的一部分。 */
    {   uint32_t at = 0;
        int irc = -1;
        for (at = 0; at < 3u; at++) {
            irc = sd_identify(chosen);
            if (irc == 0) break;
            sd_delay(SD_PWRUP_DLY);
        }
        SD_DIAG[24] = at + 1u;
        SD_DIAG[25] = (uint32_t)irc;
        if (irc != 0) { SD_DIAG[0] = 17; SD_DIAG[20] = 0xBAFF; return -16; }
    }

    s_sd_ready = 1;
    g_sd_init_stage = 11;
    SD_DIAG[0] = 11; SD_DIAG[6] = SD_CLKCR; SD_DIAG[20] = 0x600D;
    return 0;
}

/* 轮询 CMD13 直到卡回到 TRAN(4) 态 (等价 BSP_SD_GetCardState) */
static int sd_wait_ready(void)
{
    uint32_t i, r;
    for (i = 0; i < 20000u; i++) {
        if (sd_cmd(13, g_sd_rca << 16, CMD_WAITRESP_S, &r, 1) != 0) return -1;
        if (((r >> 9) & 0xFu) == 4u) return 0;          /* CURRENT_STATE == TRAN */
    }
    return -2;
}

/* 单块写 (CMD24) —— 数据通路走 SDMMC **内部 DMA (IDMA)**, CPU 不碰 FIFO。
 *
 * ★ 为什么不用 CPU 喂 FIFO: 首版照 HAL 的 polling 路径逐句抄 (TXFIFOHE 一次灌 8 字),
 *   实测第一步就 `STA=0x00045010` = **TXUNDERR** (TX FIFO 下溢, 且 FIFO 全空)。
 *   即 DPSM 已发车、FIFO 还没被填上 —— 这条路对 CPU 抢占/时序太敏感。
 *   HAL 自己的 `_DMA` 变体用的是 IDMA 且**配置在发命令之前**:
 *     DCTRL=0 → ConfigData(DPSM=DISABLE) → CMDTRANS → IDMABASE0=buf →
 *     IDMACTRL=ENABLE → 再发 CMD24   (`stm32h7xx_hal_sd.c:1361-1399`)
 *   ⇒ 数据搬运交给硬件, 时序不再由软件决定。本实现采用同一结构。 */
int sd_write_block(uint32_t lba, const uint8_t *buf)
{
    uint32_t g;

    if (!s_sd_ready) return -1;
    SD_DIAG[12] = lba;

    SD_DCTRL = 0u;
    SD_DTIMER = 0xFFFFFFFFu;
    SD_DLEN   = SD_BLK_SZ;
    SD_DCTRL  = (SD_BLK_BITS << 4) | (0u << 1) | (0u << 2);  /* 512B / 写 / 块 / DTEN=0 */
    SD_ICR    = STA_STATIC;
    SD_CMD   |= CMD_CMDTRANS;                    /* ★★ 数据通路总开关 */
    SD_IDMABASE0 = (uint32_t)buf;                /* ★ IDMA 源 = 待写缓冲 */
    SD_IDMACTRL  = 1u;                           /* ★ IDMAEN (单缓冲) */

    if (sd_cmd(24, lba, CMD_WAITRESP_S, 0, 1) != 0) {       /* CMD24 WRITE_BLOCK */
        SD_DIAG[13] = SD_STA;
        SD_CMD &= ~CMD_CMDTRANS; SD_IDMACTRL = 0u;
        return -2;
    }

    for (g = 0; g < SD_TMOUT_DATA; g++) {
        uint32_t st = SD_STA;
        if (st & STA_DATAERR) {
            SD_DIAG[13] = st; SD_CMD &= ~CMD_CMDTRANS; SD_IDMACTRL = 0u; return -4;
        }
        if (st & STA_DATAOK) break;
    }
    { uint32_t st = SD_STA;
      SD_CMD &= ~CMD_CMDTRANS; SD_IDMACTRL = 0u;
      if (!(st & STA_DATAOK)) { SD_DIAG[13] = st; return -5; }
      if (st & STA_DATAERR)   { SD_DIAG[13] = st; return -6; } }
    SD_ICR = STA_STATIC;

    if (sd_wait_ready() != 0) { SD_DIAG[13] = SD_STA; return -7; }
    g_sd_blocks++;
    return 0;
}

int sd_read_block(uint32_t lba, uint8_t *buf)
{
    uint32_t g;

    if (!s_sd_ready) return -1;

    SD_DCTRL = 0u;
    SD_DTIMER = 0xFFFFFFFFu;
    SD_DLEN   = SD_BLK_SZ;
    SD_DCTRL  = (SD_BLK_BITS << 4) | (1u << 1) | (0u << 2);  /* 512B / 读 / 块 / DTEN=0 */
    SD_ICR    = STA_STATIC;
    SD_CMD   |= CMD_CMDTRANS;
    SD_IDMABASE0 = (uint32_t)buf;                /* ★ IDMA 目的 = 接收缓冲 */
    SD_IDMACTRL  = 1u;

    if (sd_cmd(17, lba, CMD_WAITRESP_S, 0, 1) != 0) {       /* CMD17 READ_SINGLE_BLOCK */
        SD_CMD &= ~CMD_CMDTRANS; SD_IDMACTRL = 0u;
        return -2;
    }
    for (g = 0; g < SD_TMOUT_DATA; g++) {
        uint32_t st = SD_STA;
        if (st & STA_DATAERR) {
            SD_DIAG[28] = st;
            SD_CMD &= ~CMD_CMDTRANS; SD_IDMACTRL = 0u; return -4;
        }
        if (st & STA_DATAOK) break;
    }
    {   uint32_t st = SD_STA;
        SD_DIAG[28] = st;
        SD_CMD &= ~CMD_CMDTRANS; SD_IDMACTRL = 0u;
        SD_ICR = STA_STATIC;
        if (st & STA_DATAERR) return -6;
        return (st & STA_DATAOK) ? 0 : -5;
    }
}

#define SD_BURST_BLKS 64u    /* 每批块数: 小一点便于失败定位 */

/* 多块写 (CMD23 限长 + CMD25 多块写) —— 单块 CMD24 的根本问题是"每块都要一次命令+
 * 响应+等卡内部编程", 实测只有 ~0.3 MB/s; 多块写实测 **2.95MB/s@25MHz / 5.63@50MHz**。
 * ★★ 必须先用 **CMD23 (SET_BLOCK_COUNT)** 把块数告诉卡:
 *   实测 (2026-09-12) 不告诉卡时, 主机侧 DLEN 到点就 DATAEND 收工, 而**卡仍在等它
 *   以为没发完的数据** ⇒ 它不响应 CMD12 (STA=0x0004 CTIMEOUT) ⇒ 卡永久卡在
 *   "接收数据"状态, 后续命令全失联。卡支持 CMD23 (R1=0x900 无错位) ⇒ 不再需要 CMD12。
 *   ★ CMD23 必须在 **CMDTRANS 置位之前**发 —— 否则 CPSM 会把它当数据命令。
 *   ★ 若卡不支持 CMD23 (R1 有错位) ⇒ 自动退回"发完再 CMD12"的 HAL 路径。
 * @param buf 4 字节对齐的 SRAM 源; nblk 块数 (1..1024); 每批建议 ≤64 便于失败定位 */
int sd_write_multi(uint32_t lba, const uint8_t *buf, uint32_t nblk)
{
    uint32_t g, st = 0u, r1 = 0u;
    int rc, use_cmd12;

    if (!s_sd_ready) return -1;
    if (nblk == 0u || nblk > 1024u) return -1;
    if (((uint32_t)buf & 3u) != 0u) return -1;

    SD_DCTRL = 0u;
    SD_DTIMER = 0xFFFFFFFFu;
    SD_DLEN   = nblk * SD_BLK_SZ;
    SD_DCTRL  = (SD_BLK_BITS << 4) | (0u << 1) | (0u << 2);   /* 512B/写/块/DTEN=0 */
    SD_ICR    = STA_STATIC;

    rc = sd_cmd(23, nblk, CMD_WAITRESP_S, &r1, 1);            /* SET_BLOCK_COUNT */
    SD_DIAG[43] = r1;
    use_cmd12 = ((rc != 0) || ((r1 & 0xFDFFE008u) != 0u)) ? 1 : 0;
    SD_DIAG[44] = (uint32_t)use_cmd12;

    SD_CMD   |= CMD_CMDTRANS;
    SD_IDMABASE0 = (uint32_t)buf;
    SD_IDMACTRL  = 1u;

    rc = sd_cmd(25, lba, CMD_WAITRESP_S, 0, 1);               /* WRITE_MULTIPLE_BLOCK */
    SD_DIAG[37] = (uint32_t)rc;
    SD_DIAG[38] = SD_STA;
    if (rc != 0) { SD_DIAG[13] = SD_STA; goto fail; }

    /* ★ 多块写**只能等 DATAEND**: DBCKEND 是"每块结束"都会置位
     *   (单块写用它没问题, 多块写用它 ⇒ 第 1 块结束就误判成功)。 */
    for (g = 0; g < SD_TMOUT_DATA; g++) {
        st = SD_STA;
        if (st & STA_DATAERR) break;
        if (st & STA_DATAEND) break;
    }
    SD_DIAG[39] = st;
    SD_CMD &= ~CMD_CMDTRANS;
    SD_IDMACTRL = 0u;

    if (use_cmd12) {
        SD_ICR = STA_STATIC;
        SD_CMD &= ~(CMD_CMDTRANS | CMD_CMDSTOP);
        SD_CMD |= CMD_CMDSTOP | 12u | CMD_WAITRESP_S | CMD_CPSMEN;
        for (g = 0; g < SD_TMOUT_CMD; g++) {
            uint32_t s2 = SD_STA;
            if ((s2 & (STA_CMDREND | STA_CCRCFAIL | STA_CTIMEOUT)) && !(s2 & STA_CPSMACT)) break;
        }
        SD_DIAG[40] = SD_STA;
        SD_CMD &= ~CMD_CMDSTOP;
        SD_ICR = STA_STATIC;
    }

    if (st & STA_DATAERR)    { SD_DIAG[13] = st; goto fail; }
    if (!(st & STA_DATAEND)) { SD_DIAG[13] = st; goto fail; }
    if (sd_wait_ready() != 0) { SD_DIAG[13] = SD_STA; goto fail; }
    g_sd_blocks += nblk;
    return 0;

fail:
    /* ★ 失败必须**把卡和数据通路收干净**, 否则 DPSM 挂着会把后续操作全毒掉
     *   (实测: 失败后 CMD13 直接 CTIMEOUT)。 */
    SD_CMD &= ~CMD_CMDTRANS;
    SD_IDMACTRL = 0u;
    SD_CMD &= ~(CMD_CMDTRANS | CMD_CMDSTOP);
    SD_CMD |= CMD_CMDSTOP | 12u | CMD_WAITRESP_S | CMD_CPSMEN;
    { uint32_t gg; for (gg = 0; gg < 200000u; gg++) { if (!(SD_STA & STA_CPSMACT)) break; } }
    SD_CMD &= ~CMD_CMDSTOP;
    SD_ICR = STA_STATIC;
    SD_DCTRL = 0u;
    return -8;
}


/* ═══════════ SD 日志 (专用裸介质, 每拍连续落盘, 环形回卷) ═══════════
 * 布局:  LBA0 = 头部块 (512B)      LBA1 .. 容量-1 = 数据区 (环形)
 * 数据:  每 512B 块放 2 条 256B 记录, 每条自带 magic/tick/seq (见 blackbox.h)
 * 语义:  写指针到末端就回卷覆盖最旧; PC 端凭头部 magic 找到日志, 凭每条记录的
 *        seq 找最新、按 tick 对齐时间 —— **不依赖文件系统**。
 * 时序:  ISR 每拍写 256B 进 RAM 环 (512 槽 = 51.2ms 缓冲); 主循环调 sd_log_poll()
 *        成批冻结+落盘。**批量要够大**: 16KB/批 时命令+编程延迟把吞吐压到追不上产量
 *        (实测每 15s 丢 1.8 万条); 64KB/批 才能吃到 5.6MB/s 的量级。 */

/* ★★ SD_CFG 在 **AXI SRAM**, 而 AXI 段是 (NOLOAD) —— **上电不清零, 内容是随机垃圾**。
 *   ⇒ 必须用**魔数门**: 只有 [15] == SD_CFG_MAGIC 时才认这些配置字, 否则一律当 0。
 *   实测血证 (2026-09-12): 没加魔数门时, `BB_CFG[7]` 随机非零 ⇒ 每一拍随机走 MDMA
 *   路径(只搬 16 字)而不是 CPU 拷贝; `BB_CFG[4]` 随机 ⇒ CTCR 扫描扫的全是随机值,
 *   得出一整套无意义的"与配置无关"结论。**随机 SRAM 会伪装成"配置没效果"。**
 *   调试器预写: [15]=魔数 + 需要的字; 固件用 sd_cfg_take() 逐个取走 (读一次即清)。 */
#define SD_CFG_MAGIC 0xF00DBEEFu
static uint32_t sd_cfg_take_raw(uint32_t idx)
{
    uint32_t v;
    if (SD_CFG[15] != SD_CFG_MAGIC) return 0u;    /* ★ 门 */
    if (idx > 14u) return 0u;
    v = SD_CFG[idx];
    SD_CFG[idx] = 0u;                            /* 取走即清, 只生效一次 */
    return v;
}

/* ★ 用到的下标是 0..8 (含 blackbox.c 的 4/5/7 与 main 的 8), 上限必须留够 ——
 *   第一版写 `idx > 3 → 0` 把 4/5/7/8 全挡死, 是"加了保护反而废掉功能"的典型。 */
/* ★ 卡顿归因观测 (由主循环喂进来): [59] 两次落盘最大间隔 / [60] 落盘内部最长耗时 /
 *   [61] 慢轮询(>20ms)次数。两个数一对比就能把"卡顿"劈成两半:
 *   [59] 大而 [60] 小 ⇒ 主循环被别的事占住; [60] 大 ⇒ 就是 SD 卡写得慢。 */
void sd_log_diag_gap(uint32_t gap_ticks, uint32_t inpoll_ticks, uint32_t slow_cnt)
{
    SD_DIAG[59] = gap_ticks;
    SD_DIAG[60] = inpoll_ticks;
    SD_DIAG[61] = slow_cnt;
}

/* ★★ 2026-09-13: 加 DCL_ITCM —— 闸门证明它"从 ISR 可达却落在 FLASH"(0x080070FC)。 */
DCL_ITCM uint32_t sd_cfg_take(uint32_t idx)
{
    if (idx > 14u) return 0u;
    return sd_cfg_take_raw(idx);
}

/* 刷新头部 (单块写) */
static int sd_log_flush_header(void)
{
    uint32_t *h = (uint32_t *)SD_STAGE;
    uint32_t i, sum = 0;
    if (!s_log_ready) return -1;
    for (i = 0; i < 16u; i++) h[i] = 0u;
    h[0]  = LOG_HDR_MAGIC;
    h[1]  = LOG_VERSION;
    h[2]  = BB_SLOT_SZ;                                  /* 记录 256B */
    h[3]  = 2u;                                          /* 每块 2 条 */
    h[4]  = SD_BLK_SZ;
    h[5]  = 1u;                                          /* 数据区起始 LBA */
    h[6]  = g_sd_capacity_blocks - 1u;                   /* 数据区块数 */
    h[7]  = 1u + s_log_blk;                              /* 下一块写哪 */
    h[8]  = s_log_total;
    h[9]  = s_log_last_tick;
    h[10] = s_log_last_seq;
    h[11] = s_log_dropped;
    h[12] = s_log_wrapped;
    h[13] = s_log_batches;
    h[14] = s_log_hdr_flush;
    for (i = 0; i < 15u; i++) sum += h[i];
    h[15] = sum;                                         /* 简单校验和 (语义不变) */
    /* ★★ 通道映射随头落卡 ⇒ 数据自带"这一列是哪一路通道"。
     *   为什么**不升 LOG_VERSION**: h[0..15] 的语义一个没变 (h[15] 仍只校验 h[0..14]),
     *   旧固件读新头照常续写, 新固件读旧头 (h[76] 不是魔数) 退回默认表。
     *   一旦升版本 ⇒ sd_log_open 判定"版本不符" ⇒ 当新卡重建 ⇒ s_log_blk 归零,
     *   **从 LBA1 开始覆写, 把卡上已有的几 GB 记录逐步毁掉**。 */
    {   const uint32_t *m = bb_map();
        for (i = 0; i < BB_MAP_N; i++) h[BB_MAP_HDR_OFF + i] = m[i];
        h[BB_MAP_HDR_OFF + BB_MAP_N] = BB_MAP_HDR_MAGIC;
        h[BB_MAP_HDR_SUM_OFF]        = bb_map_sum(m);
    }
    /* ★ 故障台账全景 (h[78..111]): 让"读一次 LBA0"就能拿到故障全景。
     *   ★ 放在**头校验和之后**是必须的 —— h[15] 只校验 h[0..14], 本段不参与校验,
     *     所以加它不会破坏任何既有判据, **不必升 LOG_VERSION** (升版本会毁卡上数据)。 */
    bb_flt_into_hdr(h);
    s_log_hdr_flush++;
    return sd_write_block(0u, (const uint8_t *)h);
}

/* ★ 上位机显式触发: 把当前故障台账全景刷进日志头 (SD_CFG[12])。
 *   为什么是"显式触发"而不是固件定时刷: 刷新要**写一次 LBA0**, 而写 SD 有停顿代价;
 *   什么时候"现在值得落一个全景"只有上位机知道 (同本项目既有纪律:
 *   "别让固件自己按时间猜窗口")。 */
int sd_flt_snapshot(void)
{
    if (!s_log_ready) return -1;
    SD_DIAG[17]++;                       /* 刷新次数 (可被外部读走, 证明真的刷过; 17 是空槽) */
    return sd_log_flush_header();
}

/* 打开日志: 读头部, 能对上就续写, 否则新建 */
int sd_log_open(void)
{
    uint32_t *h = (uint32_t *)SD_STAGE;
    int rc;
    if (!s_sd_ready || g_sd_capacity_blocks < 1024u) { SD_DIAG[53] = 0xE001u; return -1; }
    rc = sd_read_block(0u, (uint8_t *)SD_STAGE);
    if (rc == 0 && h[0] == LOG_HDR_MAGIC && h[1] == LOG_VERSION
        && h[2] == BB_SLOT_SZ && h[6] == (g_sd_capacity_blocks - 1u)) {
        s_log_blk         = (h[7] >= 1u) ? (h[7] - 1u) : 0u;   /* 续写: 接着上次的指针 */
        s_log_total       = h[8];
        s_log_last_tick   = h[9];
        s_log_last_seq    = h[10];
        s_log_dropped     = h[11];
        s_log_wrapped     = h[12];
        s_log_batches     = h[13];
        s_log_hdr_flush   = h[14];
        SD_DIAG[45] = 1u;                                      /* 1 = 续写旧日志 */
    } else {
        s_log_blk = 0u; s_log_total = 0u; s_log_last_tick = 0u; s_log_last_seq = 0u;
        s_log_dropped = 0u; s_log_wrapped = 0u; s_log_batches = 0u; s_log_hdr_flush = 0u;
        SD_DIAG[45] = 2u;                                      /* 2 = 新建日志 */
    }
    s_log_ready = 1;
    {   /* ★ 批大小可由 SD_CFG[9] 覆盖 (扫"批大小 vs 单批卡顿"用; 有魔数门) */
        uint32_t b = sd_cfg_take_raw(9u);
        if (b >= LOG_BATCH_MIN && b <= LOG_BATCH_MAX) s_log_batch = b & ~1u;
        else s_log_batch = LOG_BATCH_SLOTS;
        SD_DIAG[62] = s_log_batch;
    }
    (void)sd_log_flush_header();
    /* ★ 只记录"从此刻起"新产出的快照: 环里已有的要么是陈旧 SRAM, 要么是上一轮已经
     *   落过盘的 (续写场景) —— 重复落盘只会把日志写乱。 */
    s_log_slot_base = bb_slots_produced();
    s_log_drop_open = s_log_dropped;
    s_log_blk_open = s_log_total;
    s_log_polls = 0; s_log_max_avail = 0; s_log_batch_err = 0;
    SD_DIAG[44] = s_log_blk;
    SD_DIAG[47] = 1u + s_log_blk;
    SD_DIAG[48] = s_log_total;
    SD_DIAG[50] = s_log_wrapped;
    return 0;
}

/* 写一批 (最多 LOG_BATCH_SLOTS 条)。@retval 本次落盘条数 (0 = 暂时没什么可写) */
static uint32_t sd_log_one_batch(void)
{
    uint32_t p, avail, n, i, k, nblk, rc, cap_data;
    cap_data = g_sd_capacity_blocks - 1u;
    p = bb_slots_produced();
    avail = p - s_log_slot_base;
    if (avail > s_log_max_avail) s_log_max_avail = avail;
    if (avail == 0u) return 0u;
    if (avail > 512u) {                        /* 环已被覆盖 ⇒ 这一段真的丢了 */
        s_log_dropped += avail - 512u;
        s_log_slot_base = p - 512u;
        avail = 512u;
    }
    /* ★ 攒批: 不满一批就等下一轮 (小批的命令开销会把带宽吃掉);
     *   但**落后到 3/4 环时立刻写**, 否则环被覆盖就真丢数据。 */
    if (avail < s_log_batch && avail <= (BB_SLOTS * 3u / 4u)) return 0u;
    n = avail & ~1u;                           /* 必须成对 (2 条 = 1 块) */
    if (n > s_log_batch) n = s_log_batch;
    if (n == 0u) return 0u;

    /* ① 冻结这一批 (逐槽搬 ⇒ 天然处理环形回绕) */
    for (i = 0; i < n; i++) {
        const uint32_t *src = (const uint32_t *)(SD_RING + (((s_log_slot_base + i) % BB_SLOTS) * BB_SLOT_SZ));
        uint32_t *dst = (uint32_t *)(SD_STAGE + i * BB_SLOT_SZ);
        for (k = 0; k < (BB_SLOT_SZ / 4u); k++) dst[k] = src[k];
    }
    {   /* 记下这一批最后一条的 tick/seq (写进头部, PC 端可快速定位最新) */
        const uint32_t *last = (const uint32_t *)(SD_STAGE + (n - 1u) * BB_SLOT_SZ);
        s_log_last_tick = last[1];
        s_log_last_seq  = last[2];
    }
    /* ② 分块写; 绝不跨过数据区末端 (到末端就回卷) */
    nblk = n / 2u;
    if (s_log_blk + nblk > cap_data) nblk = cap_data - s_log_blk;
    if (nblk == 0u) { s_log_blk = 0u; s_log_wrapped = 1u; return 0u; }

    rc = sd_write_multi(1u + s_log_blk, SD_STAGE, nblk);
    s_log_last_rc = (uint32_t)rc;
    if (rc != 0u) { s_log_batch_err++; return 0u; }   /* 失败: 不推进游标, 下轮重试同一批 */
    s_log_blk += nblk;
    if (s_log_blk >= cap_data) { s_log_blk = 0u; s_log_wrapped = 1u; }
    s_log_slot_base += nblk * 2u;
    s_log_total     += nblk * 2u;
    s_log_batches++;
    /* ★ 刷日志头移出关键路径: 它是**单块写**, 要等一整段卡编程(几 ms) ——
     *   若在追赶途中插进去, 正好把 avail 推过环容量 ⇒ 丢包。
     *   改成"攒够次数 + 且此刻没有积压"才刷 (积压时优先保证数据不丢)。 */
    if (((s_log_batches % LOG_HDR_EVERY) == 0u) || s_log_wrapped) s_log_hdr_pend = 1u;
    return nblk * 2u;
}

/* ★ 由上位机触发的"重新初始化 SD + 开日志" (卡插晚了 / 换卡后)。
 * 为什么必须外部触发: sd_init 要阻塞约 1s (分频自检 + 识别), 在裸机上
 * **"什么时候可以阻塞几百 ms 只有上位机知道"** —— 固件自己定时重试必然猜错
 * (g_persist_req 上方记着三次血证: 固件按时间猜窗口, 套件通过数 21/30 → 5/30)。
 * 触发方式: 调试器预写 SD_CFG[8]=1 + 魔数 [15], 主循环取走后执行一次。 */
int sd_reopen_log(void)
{
    if (!s_sd_ready) {
        if (sd_init() != 0) { SD_DIAG[53] = 0xE002u; return -1; }
    }
    s_log_ready = 0;
    return sd_log_open();
}

/* 主循环调用: 把 RAM 环里新产出的快照成批冻结 + 追加(回卷)落盘。
 * ★★ 必须**连续追到追平**再返回: 单批只有 64 条(=6.4ms 产量) 却要 ~3ms 才写完,
 *    每轮只写一批的话消费速率 ~2.1MB/s < 产量 2.56MB/s ⇒ 环被周期性覆盖。
 *    实测: 每轮一批 ⇒ 每 12s 丢 1.2 万条; 连续追平后应接近 0。
 *    上限 8 批/轮 (≈24ms) 防止把主循环饿死。 */
void sd_log_poll(void)
{
    uint32_t iter, wrote = 0u;
    if (!s_log_ready) return;
    s_log_polls++;
    for (iter = 0; iter < 8u; iter++) {
        uint32_t n = sd_log_one_batch();
        if (n == 0u) break;
        wrote += n;
    }
    /* ★ 只在"这一轮没写出去东西"时才刷头 ⇒ 永远不与追赶抢卡 */
    if (s_log_hdr_pend != 0u && wrote == 0u) { (void)sd_log_flush_header(); s_log_hdr_pend = 0u; }
    SD_DIAG[47] = 1u + s_log_blk;
    SD_DIAG[48] = s_log_total;
    SD_DIAG[49] = s_log_dropped;
    SD_DIAG[50] = s_log_wrapped;
    SD_DIAG[51] = s_log_batches;
    SD_DIAG[52] = s_log_hdr_flush;
    SD_DIAG[53] = s_log_last_rc;
    SD_DIAG[55] = s_log_polls;
    SD_DIAG[56] = s_log_max_avail;
    SD_DIAG[57] = s_log_dropped - s_log_drop_open;
    SD_DIAG[58] = (s_log_total - s_log_blk_open) / 2u;   /* 本次上电写了多少块 */
    SD_DIAG[63] = s_log_batch_err;                       /* ★ 批写失败次数 */
}

/* 一次性吞吐自检: 从冻结区(当 scratch)连续写 256 块, **只测写速度**。
 * ★ 不再做"回读逐字比对": 那条判据已被"日志 + PC 端读卡"取代 ——
 *   自校验只能证明"搬运没坏", 证明不了"内容对"; 内容对必须拿第三方源 (SHM 原文) 比。
 *   而拿活环当靶子更糟: 环每 100us 被覆写, 比对必然报假不符 (实测 182/256)。 */
void sd_dump_write(void)
{
    uint32_t i, k;
    volatile uint32_t *st = (volatile uint32_t *)SD_STAGE;
    if (!s_sd_ready) return;
    for (k = 0; k < (SD_BURST_BLKS * SD_BLK_SZ) / 4u; k++) st[k] = 0xA5A50000u + k;
    (void)sd_wait_ready();
    for (i = 0; i < 256u; i += SD_BURST_BLKS) {
        SD_DIAG[41] = i;
        if (sd_write_multi(3000000u + i, SD_STAGE, SD_BURST_BLKS) != 0) break;
        SD_DIAG[14] = i + SD_BURST_BLKS;
    }
    SD_DIAG[3] = g_sd_blocks;
}

void sd_dump_blackbox(void)
{
    sd_dump_write();
}
