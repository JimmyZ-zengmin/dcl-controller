/**
 * flash.c — STM32H723 裸 Flash 驱动实现
 *
 * 序列全部依据 OpenOCD `stm32h7x.c` 与 RM0468 §4.3.9 (见 flash.h 的前言)。
 *
 * ★ 超时策略: 用**总线周期**计数而不是"循环次数", 因为循环次数依赖主频与优化级别。
 *   擦除 128KB 的实测量级 ~1-4 秒; 这里给 8 秒 (400MHz × 8s = 32e8 周期) 的
 *   宽裕上限 —— 宁可超时判错, 也不无限挂住 (挂了 SWD 还读得到, 但固件死了)。
 */
#include "flash.h"
#include "regs.h"

/* 超时阈值 (DWT 周期数; 400MHz 下 1 秒 = 4e8)。
 * 擦除上限给 8 秒, 编程一个字给 1 秒 —— 都不可能在正常硬件上触发,
 * 只在"控制器真的卡住"时兜底 (此时 SWD 仍可读, 便于定位)。 */
#define FL_ERASE_TIMEOUT_CYC   (8u * 400000000u)
#define FL_WRITE_TIMEOUT_CYC   (1u * 400000000u)

static inline uint32_t fl_cyccnt(void)
{
    return DWT_CYCCNT;
}

/* ★ 诊断面: 记录最近一次"检测到错误标志"时的原始 SR1 / CCR1 以及出错阶段。
 *   为什么必须留这个: 错误码 FL_ERR_SRERROR 只说"有错", 但 ERR_Msk 有 5 个位
 *   (WRPERR/PGSERR/STRBERR/INCERR/OPERR), **哪一个**决定了完全不同的排障方向。
 *   不留原始位就只能靠猜 —— 这正是项目铁律里"凡'写过了就算'的状态必须补一个
 *   能被外部读走的量"的又一次应用。 */
volatile uint32_t g_fl_err_stage = 0;   /* 1=erase 2=write 3=prewait */
volatile uint32_t g_fl_err_sr1   = 0;
volatile uint32_t g_fl_err_cr1   = 0;
volatile uint32_t g_fl_err_cnt   = 0;

/** @brief 等待操作队列排空。★ 判据是 QW, 不是 BSY (见 flash.h 前言第 ③ 条) */
static int fl_wait_qw(uint32_t timeout_cyc)
{
    uint32_t t0 = fl_cyccnt();
    for (;;) {
        uint32_t sr = FLASH_SR1;
        if ((sr & FLASH_SR_QW) == 0u) {
            /* 队列空 = 本次操作结束。此时再看一眼错误标志 —— 有错就不算成功。 */
            if (sr & FLASH_SR_ERR_Msk) {
                g_fl_err_sr1 = sr;
                g_fl_err_cr1 = FLASH_CR1;
                g_fl_err_cnt++;
                FLASH_CCR1 = sr & FLASH_SR_ERR_Msk;   /* 写 1 清 (清 CCR1, 不是 SR1) */
                (void)FLASH_SR1;
                return FL_ERR_SRERROR;
            }
            if (sr & FLASH_SR_EOP) {
                FLASH_CCR1 = FLASH_SR_EOP;            /* 清 EOP, 否则下次误判 */
                (void)FLASH_SR1;
            }
            return FL_OK;
        }
        if ((fl_cyccnt() - t0) > timeout_cyc) return FL_ERR_TIMEOUT;
    }
}

int flash_unlock(void)
{
    if ((FLASH_CR1 & FLASH_CR_LOCK) == 0u) return FL_OK;   /* 已解锁, 幂等 */
    FLASH_KEYR1 = FLASH_KEY1;
    __asm__ volatile("dsb" ::: "memory");
    FLASH_KEYR1 = FLASH_KEY2;
    __asm__ volatile("dsb" ::: "memory");
    /* 只信回读: 密钥写错/时序不对时 LOCK 仍是 1, 必须当场发现 */
    if (FLASH_CR1 & FLASH_CR_LOCK) return FL_ERR_LOCKED;
    return FL_OK;
}

void flash_lock(void)
{
    FLASH_CR1 = FLASH_CR_LOCK;
    __asm__ volatile("dsb" ::: "memory");
}

uint32_t flash_cr1(void) { return FLASH_CR1; }
uint32_t flash_sr1(void) { return FLASH_SR1; }

int flash_addr_ok(uint32_t addr, size_t len)
{
    uint32_t end = addr + (uint32_t)len;
    /* 上界用 >= 避免 end 回绕; FLASH_END = 0x080FFFFF (CMSIS L2075) */
    if (addr < FLASH_BANK1_BASE) return 0;
    if (end > 0x08100000u) return 0;
    if (end < addr) return 0;              /* 回绕 */
    if (addr & 31u) return 0;              /* 32B 对齐 */
    if ((uint32_t)len & 31u) return 0;     /* 长度必须是 32 的倍数 */
    return 1;
}

int flash_erase_sector(uint32_t sector)
{
    if (sector >= FLASH_SECTOR_TOTAL) return FL_ERR_NOSECTOR;
    int r = flash_unlock();
    if (r != FL_OK) return r;

    /* 先确保队列空 —— 有残留操作时写 CR 会被 PGSERR 拒 */
    g_fl_err_stage = 3;
    r = fl_wait_qw(FL_ERASE_TIMEOUT_CYC);
    if (r != FL_OK) return r;

    /* SER | PSIZE_64 | (sector << 8); 先写配置再单独置 START
     * ★ H72x/H73x 的 SNB 在 bit11:8 (OpenOCD `stm32h74_h75xx_compute_flash_cr`
     *   用的就是 `snb << 8`, 而 H72x/H73x 与 H74x/H75x 同族)。 */
    FLASH_CR1 = FLASH_CR_SER | FLASH_CR_PSIZE_64 | ((sector & 0xFu) << FLASH_CR_SNB_Pos);
    __asm__ volatile("dsb" ::: "memory");
    FLASH_CR1 |= FLASH_CR_START;
    __asm__ volatile("dsb" ::: "memory");

    g_fl_err_stage = 1;
    r = fl_wait_qw(FL_ERASE_TIMEOUT_CYC);

    /* 清干净 CR 的工作位 (START/SER), 保留 LOCK 由调用方决定 */
    FLASH_CR1 &= ~(FLASH_CR_SER | FLASH_CR_START);
    __asm__ volatile("dsb" ::: "memory");
    return r;
}

int flash_write(uint32_t addr, const void *src, size_t len)
{
    if (!flash_addr_ok(addr, len)) {
        /* 区分"对齐错"与"范围错", 便于排障 (也给工具一个可判定的原因) */
        if ((addr & 31u) || ((uint32_t)len & 31u)) return FL_ERR_ALIGN;
        return FL_ERR_RANGE;
    }
    int r = flash_unlock();
    if (r != FL_OK) return r;

    g_fl_err_stage = 3;
    r = fl_wait_qw(FL_WRITE_TIMEOUT_CYC);
    if (r != FL_OK) return r;

    /* PG | PSIZE_64 (PSIZE 已在 CR 里是 0b11, 但显式写一遍: 不依赖复位值) */
    FLASH_CR1 = FLASH_CR_PG | FLASH_CR_PSIZE_64;
    __asm__ volatile("dsb" ::: "memory");

    /* ★★ H7 的"flash word" = 256 bit = 8 × u32。必须**连续 8 次 32 位写**到
     *    32B 对齐的地址, 硬件才会把这一行真正编程进 flash。
     *
     *    ☆☆ 实测踩坑 (2026-09-11): 第一版写成 `for (i) *(volatile u32*)(addr+i*4) = w[i];`
     *       结果 SR1 回报 **PGSERR(bit18) + INCERR(bit21)** —— 而数据"看起来写进去了"
     *       (回读能读到正确 header), 极易被误判成"回读校验太严"。
     *       反汇编 (objdump) 显示编译器把循环生成成:
     *           adds r2, r0, r3        <- 每轮重算目标地址
     *           ldr.w r4, [r3], #4     <- 从 src 装载 (夹在两次 flash 写之间)
     *           cmp  r3, r1
     *           str  r4, [r2, #0]      <- flash 写
     *       即**每次 flash 写之间夹了一条访存 + 一条分支**。RM0468 要求这 8 次写
     *       构成不可打断的序列, 被夹断就报 PGSERR。
     *
     *    ⇒ 修法: **展开成 8 次显式写**, 且把 8 个源字**先全部装进寄存器**
     *      (装载发生在 PG=1 之前/序列之外), 序列本体只有 8 条不间段的 `str`。
     *      编译器无法在这 8 条之间插入任何东西 (它们没有数据依赖, 但都是
     *      volatile 写 → 不能重排/删除)。
     *
     *    ★ 注意: 源缓冲必须在**非 flash** 区 (本项目是 DTCM 的 s_blob), 否则
     *      装载本身会去读 flash, 干扰编程。persist 已保证这一点。 */
    const uint32_t *w = (const uint32_t *)src;
    uint32_t nwords = (uint32_t)(len / 4u);
    for (uint32_t base = 0; base < nwords; base += 8u) {
        /* ① 先装载 8 个字 (此时 PG 已置位但尚未发写命令, 装载不进序列) */
        uint32_t v0 = w[base + 0], v1 = w[base + 1], v2 = w[base + 2], v3 = w[base + 3];
        uint32_t v4 = w[base + 4], v5 = w[base + 5], v6 = w[base + 6], v7 = w[base + 7];
        volatile uint32_t *d = (volatile uint32_t *)(uintptr_t)(addr + base * 4u);
        /* ② 8 次连续写, 中间**不得有任何其他指令** —— 用一条内联汇编块钉死,
         *    否则编译器仍有自由插桩 (实测 -O2 下它确实会插)。 */
        __asm__ volatile(
            "str %[v0], [%[d], #0]   \n\t"
            "str %[v1], [%[d], #4]   \n\t"
            "str %[v2], [%[d], #8]   \n\t"
            "str %[v3], [%[d], #12]  \n\t"
            "str %[v4], [%[d], #16]  \n\t"
            "str %[v5], [%[d], #20]  \n\t"
            "str %[v6], [%[d], #24]  \n\t"
            "str %[v7], [%[d], #28]  \n\t"
            :
            : [d] "r" (d),
              [v0] "r" (v0), [v1] "r" (v1), [v2] "r" (v2), [v3] "r" (v3),
              [v4] "r" (v4), [v5] "r" (v5), [v6] "r" (v6), [v7] "r" (v7)
            : "memory");
    }
    __asm__ volatile("dsb" ::: "memory");

    g_fl_err_stage = 2;
    r = fl_wait_qw(FL_WRITE_TIMEOUT_CYC);

    FLASH_CR1 &= ~(FLASH_CR_PG);
    __asm__ volatile("dsb" ::: "memory");
    return r;
}
