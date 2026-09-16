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
/* ★★ 有界喂狗需要 wdt_feed() (2026-09-13, 实测缺陷修复 —— 见 fl_wait_qw 内注释) */
#include "wdt.h"
#include "timebase.h"   /* ★★ 2026-09-16: 超时判据改用**生产时基**(TIM5), 不再用 DWT —— 见下 */

/* 超时阈值 (DWT 周期数; 400MHz 下 1 秒 = 4e8)。
 * 擦除上限给 8 秒, 编程一个字给 1 秒 —— 都不可能在正常硬件上触发,
 * 只在"控制器真的卡住"时兜底 (此时 SWD 仍可读, 便于定位)。 */
/* 超时阈值 —— ★★ 2026-09-16 口径变更: 单位从"DWT 周期(@400MHz)"改成"**时基计数**",
 *   并且**不再把 400000000 写死在常量里**（那是"一个常量两个语义"族:
 *   换时基时这一处必被漏掉）。现在唯一来源是 `timebase.h` 的 `TB_HZ` ⇒ 换档自动跟随。
 *   ★★ 更要紧的是**为什么必须换**: 本函数用超时判据兜"控制器卡住", 而紧跟其后的
 *     "有界喂狗"只在**预算内**喂。若时基被冻住, `(now - t0) > timeout` **永不成立**
 *     ⇒ 超时永不触发 ⇒ **无限喂狗** ⇒ 卡死时主循环永不返回、看门狗也失效。
 *     调试器会话收尾恰好会把 DWT 关掉（pyOCD #1540 / SEGGER KB）⇒ 这是**可达**的故障。
 *     依据与实测: docs/ASSESS-toolchain-2026-09-16.md
 *   8s @200MHz = 1.6e9; 1s @200MHz = 2e8 —— 都在 u32 内。 */
#define FL_ERASE_TIMEOUT_CYC   TB_MS(8000u)
#define FL_WRITE_TIMEOUT_CYC   TB_MS(1000u)

/* ★★ 必须进 ITCM 的属性 (2026-09-13, 一次实测缺陷的直接产物) —— 见 fl_wait_qw 的注释。
 *   本项目对"向量表 + ISR 进 ITCM"早有定案, 这里是同一条纪律的**遗漏面**:
 *   擦扇区期间 **flash 取指被 stall**, 所以**连等待循环本身**也必须在 ITCM 里,
 *   否则 CPU 连一条"喂狗"指令都取不到 (它也在 flash 里) ⇒ 看门狗必复位。 */
#define FL_ITCM  __attribute__((section(".itcm_text"), noinline, used))

FL_ITCM static uint32_t fl_cyccnt(void)
{
    return tb_cyc();      /* ★ 生产时基(TIM5): 不受调试器影响 —— 这就是"不再挂死"的那一步 */
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

/** @brief 等待操作队列排空。★ 判据是 QW, 不是 BSY (见 flash.h 前言第 ③ 条)
 *  ★★ 本函数**必须放在 ITCM** (`FL_ITCM`), 理由是一次实测缺陷:
 *    擦一个 128KB 扇区要几百 ms, 而擦除期间 **flash 取指被 stall** ——
 *    若本等待循环还在 flash 里, CPU 连循环体的一条指令都取不到, 更谈不上"在循环里喂狗"
 *    ⇒ 拍 ISR 也停摆 (它的调用同样踩 flash) ⇒ **看门狗 200ms 到点, 把板子复位**。
 *    实测症状: 每次真实落盘 → 启动次数 +1 / RSR=IWDG1 / PERSIST_STAT 全 0
 *    (即**配置永远存不下去, 而"保存配置"变成了"重启机器"**)。
 *    ★ 项目对"向量表 + ISR 进 ITCM"早有定案 —— 这是同一条纪律的**遗漏面**:
 *      **只要一段代码要在 flash 忙时运行, 它自己就不能在 flash 里。**
 *    ★ 修法验证前它已经骗过我一次: 我先在循环里加了"每 N 轮喂一次", 无效 ——
 *      因为那段代码压根执行不到 (取指就没了)。"加了喂狗" ≠ "喂狗能执行"。 */
FL_ITCM
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

        /* ★★ 有界喂狗 (2026-09-13, 一次**实测缺陷**的修复) ★★
         * 缺陷: 落盘要**擦 128KB 扇区**, 而看门狗超时只有 200ms, 喂狗点在**拍 ISR** ——
         *   擦除期间 ISR 的取指/调用会踩 flash 总线 ⇒ 喂狗停摆 ⇒ **每次真实落盘
         *   都被看门狗复位板子**: 现场后果是"操作员按保存 → 机器重启", 而配置**永远存不下去**
         *   (实测: 落盘瞬间 启动次数+1、RSR=IWDG1、PERSIST_STAT 的 writes/erase_ok 恒 0,
         *    且"上一轮最后拍"就停在落盘那一刻 ⇒ 主循环再没回来)。
         *   ★ 这是典型的**集成回归**: "擦除 816ms"(09-11) 与 "看门狗 200ms"(09-13) 两个决定
         *     各自都验证过, 但**组合从没测过** —— 单看任何一份记录都发现不了。
         * 为什么喂: 看门狗的职责是"抓 CPU 卡死", **不是"给合法长操作设上界"**;
         *   合法长操作应当能把活干完。
         * 为什么"有界": 无条件喂会把"擦除卡死"也变成永远不复位 ⇒ 保护退化成空判据。
         *   ⇒ **只在超时预算内喂**(本行位于超时判据之后); 一旦超预算立即 return 并停止喂狗,
         *     真卡死时看门狗照常动手 (失败安全)。
         *   ★ 形状与主循环停滞判据的"带截止时间的窗口"完全一致 —— 本项目的一贯做法。
         * ★★ 为什么是**每轮都喂**而不是"N 轮喂一次": 第一版写的是 `++div >= 20000` 才喂,
         *   结果**仍然复位** —— 因为擦除期间每次读 `FLASH_SR1` 都要等 flash 总线
         *   (单次迭代可能到 µs 级), 20000 次 ≈ **200ms = 看门狗超时** ⇒ 第一次喂狗来得太晚。
         *   ⇒ 教训: **"隔 N 次做一次"这类写法在"单次耗时未知"的循环里是不可靠的**;
         *     KR 写只有几周期, 每轮都喂没有代价。 */
        wdt_feed();
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
