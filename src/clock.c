/**
 * clock.c — H723 时钟初始化 (HSE 25MHz → VOS0 → PLL1 → CPU 550MHz)
 *
 * 依据: MIGRATE-H723.md §3。每个关键判据都注明来源 (RM0468 章节 / 实测确认)。
 *
 * ★ 执行顺序铁律 (RM0468 §6.8.6 原文):
 *     升性能时 —— 先改电压, 再升频率;
 *     降性能时 —— 先降频率, 再改电压。
 *   所以 VOS0 必须在切 PLL 之前完成, FLASH 等待态也要在升频前设好。
 *
 * ★ 每一步都有超时保护。失败返回错误码而不是死等 —— 这是"排雷"的关键:
 *   卡死在 while 里没有任何信息, 返回错误码就能用 LED 闪码/寄存器读出定位。
 */
#include "clock.h"
#include "regs.h"

/* 超时 (循环次数, 非精确时间; 每个循环含 volatile 访问, 约 3-5 周期)
 * HSE 晶振起振最长 ~5ms (datasheet), 在 64MHz HSI 下给足余量 */
#define TO_HSE   4000000u
#define TO_PWR   200000u
#define TO_PLL   200000u
#define TO_SW    200000u

/* 分频编码 (H7 规则, 与 F4/F7 不同):
 *   D1CPRE / HPRE : 0xxx = /1, 1000 = /2, 1001 = /4, 1010 = /8,
 *                   1011 = /16, 1100 = /64, 1101 = /128, 1110 = /256, 1111 = /512
 *   DxPPRE        : 0xx  = /1, 100  = /2, 101  = /4, 110  = /8,  111  = /16 */
#define CODE_D1CPRE_1   0u
#define CODE_HPRE_2     8u
#define CODE_APB_2      4u

_Static_assert(CLK_PLL1_DIVM1 >= 1u && CLK_PLL1_DIVM1 <= 63u, "DIVM1 must be 1..63");
_Static_assert(CLK_PLL1_DIVN1 >= 4u && CLK_PLL1_DIVN1 <= 512u, "DIVN1 must be 4..512");
/* ★ VCO 必须落在 192-836MHz (datasheet Table 38) */
_Static_assert(CLK_VCO_HZ >= 192000000UL && CLK_VCO_HZ <= 1100000000UL, "PLL1 VCO out of range (exp: 192-1100; spec wide=192-836)");
/* ★ PLL 输入必须落在 2-16MHz */
_Static_assert(CLK_PLL1_IN_HZ >= 2000000UL && CLK_PLL1_IN_HZ <= 16000000UL, "PLL1 input out of range");
/* ★ VOS0 下 CPU 上限 550MHz */
_Static_assert(CLK_CPU_HZ <= 550000000UL, "CPU exceeds VOS0 limit 550MHz");
/* ★ H72x 的 AXI 上限 275MHz */
_Static_assert(CLK_HCLK_HZ <= 275000000UL, "HCLK exceeds H72x AXI limit 275MHz");
/* ★ 100μs 必须能被定时器时钟整除 (否则拍周期不是精确值) */
_Static_assert((CLK_TIMXCLK_HZ % 1000000UL) == 0UL, "TIMxCLK must be integer MHz for exact tick");
_Static_assert(CLK_TICK_TIMCNT * CLK_TICK_US == (CLK_TIMXCLK_HZ / 1000000UL) * CLK_TICK_US * CLK_TICK_US
               || CLK_TICK_TIMCNT == (CLK_TIMXCLK_HZ / 1000000UL) * CLK_TICK_US, "tick count mismatch");

/* ---- 等待辅助: 等某位置 1; 返回 0 成功, -1 超时 ---- */
static inline int wait_set(volatile uint32_t *reg, uint32_t mask, uint32_t to)
{
    while (to--) { if (*reg & mask) return 0; }
    return -1;
}

/* ---- 分频码 → 实际分频值 (供 clock_get_hclk_hz 反推) ---- */
static uint32_t hpre_div(uint32_t code)
{
    static const uint16_t tab[16] = {1,1,1,1,1,1,1,1, 2,4,8,16,64,128,256,512};
    return tab[code & 0xFu];
}
static uint32_t ppre_div(uint32_t code)
{
    return (code < 4u) ? 1u : (1u << (code - 3u));
}

int clock_init(void)
{
    /* ── 0) 打开 SYSCFG 时钟 (APB4)。H72x 的 VOS0 不需要 SYSCFG.ODEN,
     *       但后续内存重映射等功能要用, 顺手开。 ── */
    RCC_APB4ENR |= (1u << 1);          /* SYSCFGEN */

    /* ★实测教训 (2026-09-10): 试过在此处 `PWR_CR3 |= LDOEN` (模仿 HAL_PWREx_ConfigSupply)
     * 以及把 PLLCFGR 整个写成 0x01FF0000 (模仿 HAL_RCC_OscConfig) —— **两者都导致
     * 连 450MHz 都无法运行** (stage=5 但 TIM2 无 tick)。已回滚。
     * 结论: HAL 的写法依赖它自己的完整初始化上下文, 裸机不能照搬。 */

    /* ── 1) 启动 HSE (25MHz 晶振, 非旁路模式) ── */
    RCC_CR |= RCC_CR_HSEON;
    if (wait_set(&RCC_CR, RCC_CR_HSERDY, TO_HSE) != 0) return CLK_ERR_HSE;

    /* ── 2) VOS → Scale0 (必须在升频之前) ──
     * ★★ 实测澄清 (2026-09-10, SWD 逐档回读) ★★
     * 本芯片 **不存在** H743 那种"逐级切换 (Scale3→2→1→0)"要求:
     *    写 0b00 (Scale0)  → VOSRDY 置位 ✓   (有效档位)
     *    写 0b10 (Scale2)  → VOSRDY **永不置位**
     *    写 0b11 (Scale1)  → VOSRDY **永不置位**
     *   → 只有 0b01 (复位默认) 与 0b00 两个可用档位。
     *   ST HAL 那句"Transition to Voltage Scale 0 is only possible when the
     *   system is already in Voltage Scale 1" **明确限定 H74x/H75x**; 我一开始
     *   错误地把这条套到 H723 上, 实现"逐级"后 VOSRDY 超时 (错误码 -2)。
     *   libopencm3 对 H72x/3x 的注释正好相反: "VOS0 is implemented on
     *   STM32H72x/3x with **simple VOS setting**"。
     *
     * ★另一个实测坑: 对**相同档位**再写一次, 硬件认为"无需转换", 会把 VOSRDY
     *   清零却不重新置位 → 等待必然超时。故先比较再写 (跳过冗余写)。
     *
     * 编码 (RM0468 §6.8.6, 双源确认): 0b00=Scale0 / 0b01=Scale3 / 0b10=Scale2 / 0b11=Scale1
     * ACTVOS/ACTVOSRDY 不可用作判据 (ACTVOSRDY 仅在 BYPASS 供电模式置位)。 */
    {
        uint32_t want = (uint32_t)PWR_VOS_SCALE0 << PWR_D3CR_VOS_Pos;
        if ((PWR_D3CR & PWR_D3CR_VOS_Msk) != want) {
            PWR_D3CR = (PWR_D3CR & ~PWR_D3CR_VOS_Msk) | want;
        }
        if (wait_set(&PWR_D3CR, PWR_D3CR_VOSRDY, TO_PWR) != 0) return CLK_ERR_VOSRDY;
        if ((PWR_D3CR & PWR_D3CR_VOS_Msk) != want) return CLK_ERR_VOS_ACTIVE;
    }

    /* ── 3) FLASH 等待态 ──
     * RM0468 Table 16 (按 **AXI 时钟** 索引, 不是 CPU 时钟!):
     *   VOS0: ≤70MHz→(0,0), ≤140→(1,1), ≤210→(2,2), ≤275→(3,3)
     * 我们 AXI=275MHz → LATENCY=3, WRHIGHFREQ=3
     * 现在还在 HSI 64MHz, 设高了无害 (等待态多只是慢); 设低了才是致命。 */
    {
        uint32_t want = (CLK_FLASH_WS & 0xFu) | (3u << 4);   /* WRHIGHFREQ 恒 3 (上限) */
        FLASH_ACR = (FLASH_ACR & ~0x3Fu) | want;
        if ((FLASH_ACR & 0x3Fu) != want) return CLK_ERR_FLASH_RB;
    }

    /* ── 4) 配置 PLL1 ──
     * PLLCFGR:  RGE = 0b10 (参考 4-8MHz, 我们 5MHz)
     *           VCOSEL = 0 (宽 VCO, 192-836MHz)
     *           FRACEN = 0 ★整数模式 —— 分数(sigma-delta)模式会 dither 分频器,
     *                        主动引入周期抖动, 与本项目目标相反
     *           ★DIVP1EN = 1 —— 使能 pll1_p_ck 输出。**漏此位 → 切换被静默拒绝**
     *             (SWS 永不跟随, 固件报 CLK_ERR_SWITCH), 是本次排掉的最大一颗雷。
     *             必须在 PLL1ON=0 时写 (RM0468) —— 本函数此处 PLL1 确实未开 ✓ */
    RCC_PLLCFGR = ((uint32_t)CLK_PLL1_RGE << RCC_PLLCFGR_PLL1RGE_Pos)
                | RCC_PLLCFGR_DIVP1EN;
    /* 回读确认 (写入被静默忽略时立刻暴露, 而不是等到切换失败) */
    if ((RCC_PLLCFGR & (RCC_PLLCFGR_DIVP1EN | (3u << RCC_PLLCFGR_PLL1RGE_Pos)))
        != (RCC_PLLCFGR_DIVP1EN | ((uint32_t)CLK_PLL1_RGE << RCC_PLLCFGR_PLL1RGE_Pos)))
        return CLK_ERR_PLLCFGR_RB;

    RCC_PLLCKSELR = ((uint32_t)CLK_PLL1_DIVM1 << RCC_PLLCKSELR_DIVM1_Pos)
                  | (RCC_PLLCKSELR_PLLSRC_HSE << RCC_PLLCKSELR_PLLSRC_Pos);

    /* DIVx 寄存器写的是 (分频值 - 1) */
    RCC_PLL1DIVR = ((uint32_t)(CLK_PLL1_DIVN1 - 1u) << RCC_PLL1DIVR_N1_Pos)
                 | ((uint32_t)(CLK_PLL1_DIVP1 - 1u) << RCC_PLL1DIVR_P1_Pos)
                 | ((uint32_t)(CLK_PLL1_DIVQ1 - 1u) << RCC_PLL1DIVR_Q1_Pos)
                 | ((uint32_t)(CLK_PLL1_DIVR1 - 1u) << RCC_PLL1DIVR_R1_Pos);

    /* ── 5) 使能 PLL1 并等锁定 ── */
    RCC_CR |= RCC_CR_PLL1ON;
    if (wait_set(&RCC_CR, RCC_CR_PLL1RDY, TO_PLL) != 0) return CLK_ERR_PLL_LOCK;

    /* ── 6) 分频器 (仍在 HSI 上执行, 中间频率都很低, 安全) ──
     * CPU = SYSCLK/1, HCLK(AXI) = CPU/2 = 275MHz, APB = HCLK/2 = 137.5MHz
     * TIMxCLK = HCLK = 275MHz (APB 预分频 ≤4 时定时器时钟取 HCLK) */
    RCC_D1CFGR = (RCC_D1CFGR & ~0x7FFu)
               | ((uint32_t)CODE_D1CPRE_1 << RCC_D1CFGR_D1CPRE_Pos)
               | ((uint32_t)CLK_HPRE_CODE << RCC_D1CFGR_HPRE_Pos)
               | ((uint32_t)CLK_APB_CODE  << RCC_D1CFGR_D1PPRE_Pos);
    RCC_D2CFGR = (RCC_D2CFGR & ~0x770u)
               | ((uint32_t)CODE_APB_2 << 4)      /* D2PPRE1 */
               | ((uint32_t)CODE_APB_2 << 8);     /* D2PPRE2 */
    RCC_D3CFGR = (RCC_D3CFGR & ~0x70u)
               | ((uint32_t)CODE_APB_2 << 4);     /* D3PPRE */

    /* ── 7) 切换 SYSCLK → PLL1 ── */
    RCC_CFGR = (RCC_CFGR & ~7u) | RCC_CFGR_SW_PLL1;
    {
        uint32_t t = TO_SW;
        int ok = 0;
        while (t--) {
            if (((RCC_CFGR >> 3) & 7u) == RCC_CFGR_SW_PLL1) { ok = 1; break; }
        }
        if (!ok) return CLK_ERR_SWITCH;
    }

    return CLK_OK;
}

uint32_t clock_get_hclk_hz(void)
{
    uint32_t sw = RCC_CFGR & 7u;
    uint32_t sysclk;

    if (sw == RCC_CFGR_SW_PLL1) {
        sysclk = CLK_SYSCLK_HZ;                       /* 我们自己的配置 */
    } else if (sw == RCC_CFGR_SW_HSE) {
        sysclk = CLK_HSE_HZ;
    } else {
        /* HSI 64MHz 除以 HSIDIV (1,2,4,8,16,64,128) — 复位默认 /1 */
        static const uint16_t hsidiv_tab[8] = {1,2,4,8,16,64,128,128};
        sysclk = 64000000UL / hsidiv_tab[(RCC_CR >> RCC_CR_HSIDIV_Pos) & 7u];
    }

    uint32_t cpu  = sysclk / hpre_div((RCC_D1CFGR >> RCC_D1CFGR_D1CPRE_Pos) & 0xFu);
    uint32_t hclk = cpu / hpre_div(RCC_D1CFGR & 0xFu);
    return hclk;
}

void dwt_enable(void)
{
    DWT_LAR = 0xC5ACCE55u;        /* 解锁 DWT (CoreSight 锁定寄存器) */
    DEMCR |= DEMCR_TRCENA;        /* DWT 总开关 */
    DWT_CYCCNT = 0;
    DWT_CTRL |= 1u;               /* CYCCNTENA */
    __asm__ volatile("dsb; isb");
}

uint32_t dwt_cyccnt(void)
{
    return DWT_CYCCNT;
}
