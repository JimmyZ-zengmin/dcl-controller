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
#include "regs.h"

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
 *   [31] 保留               [32] 保留
 *   [24] 识别用了几轮(1..3)   [25] sd_identify 返回码
 *   [26] 回读不符块数(0=全对)  [27] 回读失败 (块号<<8)|错误码
 *   [16..19] 分频自检: (CLKDIV<<8) | 结果码 (0=OK)
 *   结果码: 1=CMD0 无 CMDSENT  2=CMD8 失败  3=CMD8 回显不符
 */
#define SD_DIAG  ((volatile uint32_t *)0x24030000u)

static uint32_t s_sd_ready = 0;

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
    g_sd_init_stage = 9;
    if (sd_cmd(7, g_sd_rca << 16, CMD_WAITRESP_S, 0, 1) != 0) {   /* CMD7 SELECT (R1b) */
        SD_DIAG[0] = 13; SD_DIAG[1] = SD_STA; SD_DIAG[20] = 0xBA07; return -12;
    }
    g_sd_init_stage = 10;
    if (sd_cmd(16, SD_BLK_SZ, CMD_WAITRESP_S, 0, 1) != 0) {       /* CMD16 SET_BLOCKLEN */
        SD_DIAG[0] = 14; SD_DIAG[1] = SD_STA; SD_DIAG[20] = 0xBA10; return -13;
    }

    /* 数据期总线宽度 = **1-bit**, 提频到 25MHz。
     *
     * ★★ 为什么不做 ACMD6 切 4-bit (HAL 会做) —— 实测证据 (2026-09-12):
     *   · 4-bit @25MHz: 写 256/256 块全过, 但**回读第 0 块即 DCRCFAIL**
     *     (STA bit1), 降到 6.25MHz 仍同一位置同一错误 ⇒ 与时钟/采样余量无关;
     *   · ACMD6(arg=2) 的 R1 = 0x920, 逐位与 SDMMC_OCR_ERRORBITS(0xFDFFE008)
     *     相与为 0 ⇒ **卡声称接受了 4-bit**;
     *   · 但双端一起回到 1-bit 后, 回读 256/256 块**逐字完全一致** (校验不符 = 0)。
     *   ⇒ 机制: 卡实际只在 DAT0 上驱动数据。写方向主机驱 4 条线、卡只采 DAT0,
     *     所以写全对; 读方向卡只驱 DAT0, 主机按 4-bit 采样 DAT1..3 得悬空高电平
     *     ⇒ 数据块 CRC16 必错, 且与频率无关。
     *   ⇒ 判定为**卡侧 DAT1..3 不工作** (该卡 CID = 0x00343253_44313647:
     *     厂家 ID = 0x00、OID = "42", 都不是 SD 协会分配的正规值, 疑似非原厂卡)。
     *   1-bit @25MHz = 3.125MB/s, 128KB 只需 41ms, 对本用途足够。
     *   ⇒ 因此**不发 ACMD6**, 数据期固定 1-bit; 这比"照抄 HAL 发 ACMD6 却读不回来"诚实。 */
    SD_CLKCR = 2u | CLKCR_WIDBUS_1B;
    sd_delay(SD_PWRUP_DLY);
    (void)r;
    return 0;
}

int sd_init(void)
{
    uint32_t chosen = 0, k;
    static const uint16_t cdivs[] = { 125u, 32u, 8u, 2u };   /* 400k / 1.56M / 6.25M / 25M */

    /* ⓪ 诊断区先清零。
     * ★ 2026-09-12 教训: 不清零时"没写过"和"写成了垃圾"外观完全一样 ——
     *   上一轮把没写的字读成"合法值", 据此编出了两个错误结论。
     *   清零后每个字的语义唯一: 0 = 未到达, 非 0 = 到达并有结论。 */
    for (k = 0; k < 40u; k++) SD_DIAG[k] = 0u;

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

/* 回读 256 块并逐字比对。
 * @retval 低 16 位 = 内容不符块数; bit16 置位 = 中途读失败, (块号<<8)|错误码 在低 16 位 */
static uint32_t sd_verify(const uint8_t *src, uint8_t *scratch, uint32_t lba0)
{
    uint32_t i, j, mism = 0;
    for (i = 0; i < 256u; i++) {
        int rr = sd_read_block(lba0 + i, scratch);
        if (rr != 0) return 0x10000u | ((i << 8) & 0xFF00u) | (uint32_t)(-rr);
        {
            const uint32_t *a = (const uint32_t *)(src + i * SD_BLK_SZ);
            const uint32_t *b = (const uint32_t *)scratch;
            for (j = 0; j < (SD_BLK_SZ / 4u); j++) {
                if (a[j] != b[j]) { mism++; break; }
            }
        }
    }
    return mism;
}

/* 把 AXI 黑匣子缓冲 (BB_TOTAL = 128KB = 256 块) 写到卡末尾区, 然后**回读校验**。
 * ★ 为什么不以"写命令返回 0"为判据: 写成功只说明主机侧协议走完了,
 *   不说明卡真的把数据存住了 (写保护/坏块/寻址容错都可能吞掉)。
 *   真正的对端证据是 **读回来的字节**: 256 块逐字比对, 不符块数记进 [26]。
 *   回读缓冲放在 AXI 低 16KB 预留区 (0x24001000), 与黑匣子区不重叠。 */
void sd_dump_blackbox(void)
{
    uint32_t i;
    const uint8_t *src = (const uint8_t *)0x24004000u;   /* AXI 黑匣子区 */
    uint8_t *scratch   = (uint8_t *)0x24001000u;         /* AXI 低 16KB 预留区 */
    const uint32_t base_lba = 3000000u;                  /* 约 1.46GB 处, 避开盘头 */

    if (!s_sd_ready) return;
    (void)sd_wait_ready();

    /* ① 写 256 块 (1-bit @25MHz) */
    for (i = 0; i < 256u; i++) {
        if (sd_write_block(base_lba + i, src + i * SD_BLK_SZ) != 0) break;
        SD_DIAG[14] = i + 1u;
    }
    SD_DIAG[3] = g_sd_blocks;

    /* ② 回读逐字比对 (对端证据; 低 16 位 = 不符块数, bit16 = 中途读失败) */
    SD_DIAG[26] = sd_verify(src, scratch, base_lba);

    SD_DIAG[20] = (SD_DIAG[14] == 256u && SD_DIAG[26] == 0u) ? 0x600D : 0x600E;
}
