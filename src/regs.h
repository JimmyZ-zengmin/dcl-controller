/**
 * regs.h — STM32H723 外设寄存器定义 (阶段 0 所需最小集)
 *
 * 每个地址都经**实机核对** (pyocd 读取, 见 .workbuddy/memory/2026-09-10.md):
 *   PWR_D3CR 读回 0x6000 → 证实 VOS[15:14] / VOSRDY[13] 布局
 *   FLASH_ACR 读回 0x0037 → 证实 LATENCY[3:0] / WRHIGHFREQ[5:4] 布局
 *   PWR 可读且非 0 → 证实该外设时钟在复位后可用
 *
 * 不做 CMSIS 依赖 (CubeIDE 1.5.1 未附带 STM32H7 设备头), 保持自包含、可审计。
 */
#ifndef DCL_REGS_H
#define DCL_REGS_H

#include <stdint.h>

#define REG32(addr)  (*(volatile uint32_t *)(addr))

/* ───────────────────────── RCC (0x58024400) ─────────────────────────
 * H7 的 RCC 与 F4/F7 完全不同: 有 D1/D2/D3 三个域的分频寄存器,
 * 系统时钟切换在 CFGR.SW, CPU 分频在 D1CFGR.D1CPRE。 */
#define RCC_BASE        0x58024400UL
#define RCC_CR          REG32(RCC_BASE + 0x000)   /* HSI/HSE/PLLx ON/RDY */
#define RCC_CFGR        REG32(RCC_BASE + 0x010)   /* SW[2:0] 系统时钟选择 */
#define RCC_D1CFGR      REG32(RCC_BASE + 0x018)   /* D1CPRE[7:4] CPU 分频, HPRE[3:0] AXI 分频 */
#define RCC_D2CFGR      REG32(RCC_BASE + 0x01C)
#define RCC_D3CFGR      REG32(RCC_BASE + 0x020)
#define RCC_PLLCKSELR   REG32(RCC_BASE + 0x028)   /* PLLSRC[1:0] + DIVM1[9:4] */
#define RCC_PLLCFGR     REG32(RCC_BASE + 0x02C)   /* PLL1RGE / PLL1VCOSEL / PLL1FRACEN ... */
#define RCC_PLL1DIVR    REG32(RCC_BASE + 0x030)   /* DIVN1[8:0] DIVP1[15:9] DIVQ1[22:16] DIVR1[29:23] */
#define RCC_AHB4ENR     REG32(RCC_BASE + 0x0E0)
#define RCC_APB1LENR    REG32(RCC_BASE + 0x0E8)
#define RCC_APB4ENR     REG32(RCC_BASE + 0x0F4)

/* RCC_CR 位 */
#define RCC_CR_HSION        (1u << 0)
#define RCC_CR_HSIRDY       (1u << 2)
#define RCC_CR_HSIDIV_Pos   3
#define RCC_CR_HSIDIV_Msk   (3u << 3)
#define RCC_CR_HSIDIVF      (1u << 5)
#define RCC_CR_HSEON        (1u << 16)
#define RCC_CR_HSERDY       (1u << 17)
#define RCC_CR_HSEBYP       (1u << 18)
#define RCC_CR_CSSHSEON     (1u << 19)   /* 时钟安全系统 (CSS) */
#define RCC_CR_PLL1ON       (1u << 24)
#define RCC_CR_PLL1RDY      (1u << 25)

/* RCC_CFGR.SW[2:0] 系统时钟源 */
#define RCC_CFGR_SW_HSI     0u
#define RCC_CFGR_SW_HSE     1u
#define RCC_CFGR_SW_PLL1    3u

/* RCC_D1CFGR */
#define RCC_D1CFGR_HPRE_Pos     0
#define RCC_D1CFGR_D1CPRE_Pos   4
#define RCC_D1CFGR_D1PPRE_Pos   8

/* RCC_PLLCKSELR */
#define RCC_PLLCKSELR_DIVM1_Pos 4
#define RCC_PLLCKSELR_PLLSRC_Pos 0
#define RCC_PLLCKSELR_PLLSRC_HSE 2u    /* 00=HSI 01=CSI 10=HSE 11=-- */

/* RCC_PLLCFGR */
#define RCC_PLLCFGR_PLL1RGE_Pos   2    /* 参考时钟范围: 00=1-2M 01=2-4M 10=4-8M 11=8-16M */
#define RCC_PLLCFGR_PLL1VCOSEL    (1u << 1)   /* 0=宽VCO(192-836M) 1=中VCO */
#define RCC_PLLCFGR_PLL1FRACEN    (1u << 0)   /* ★必须为 0: 整数模式, 禁用 sigma-delta */
/* ★★ DIVP1EN = bit16 —— 差点漏掉的关键位 (2026-09-10 实证) ★★
 * 含义: 使能 PLL1 的 pll1_p_ck 输出。**不置位则 PLL1 没有输出** →
 *       写 RCC_CFGR.SW=PLL1 时 SWS 永不跟随 (切换被静默拒绝)。
 * 约束: "This bit can be written only when the PLL1 is disabled (PLL1ON=0 && PLL1RDY=0)"
 * 来源: libopencm3 RCC_PLLCFGR_DIVP1EN=BIT16 / docs.rs stm32h7 "Bit 16" /
 *       stm32-rs stm32h7 RCC_PLLCFGR.DIVP1EN offset=16
 *       ★踩坑: 曾误猜在 bit4-7 (受某个 H745 例子里 SET_BIT 的位置暗示),
 *         扫 0x18/0x38/0x58/0x78/0xF8 全无效, 浪费一轮 —— 权威定义必须查, 不能猜。 */
#define RCC_PLLCFGR_DIVP1EN       (1u << 16)
#define RCC_PLLCFGR_DIVQ1EN       (1u << 17)
#define RCC_PLLCFGR_DIVR1EN       (1u << 18)

/* RCC_PLL1DIVR 位域 */
#define RCC_PLL1DIVR_N1_Pos   0
#define RCC_PLL1DIVR_P1_Pos   9
#define RCC_PLL1DIVR_Q1_Pos   16
#define RCC_PLL1DIVR_R1_Pos   23

/* ───────────────────────── PWR (0x58024800) ───────────────────────── */
#define PWR_BASE        0x58024800UL
#define PWR_CR1         REG32(PWR_BASE + 0x00)
#define PWR_CSR1        REG32(PWR_BASE + 0x04)   /* ACTVOS[15:14] / ACTVOSRDY[13] */
#define PWR_CR3         REG32(PWR_BASE + 0x10)   /* SCUEN(2)/LDOEN(1)/BYPASS(0) — 原写 0x0C 是错的(那是CSR2) */
#define PWR_D3CR        REG32(PWR_BASE + 0x18)   /* VOS[15:14] / VOSRDY[13] */

#define PWR_D3CR_VOS_Pos    14
#define PWR_D3CR_VOS_Msk    (3u << 14)
#define PWR_D3CR_VOSRDY     (1u << 13)


/* ★ RM0468 §6.8.6 — H72x/H73x 的 VOS 编码 (与 H743 完全不同!)
 *   0b00 = Scale 0 (VOS0, 最高性能, 支持 550MHz)
 *   0b01 = Scale 3 (复位默认; 实测本芯片读回即 0b01)
 *   0b10 = Scale 2
 *   0b11 = Scale 1
 * ★ H72x/H73x 没有 overdrive 位 (H743 才有 SYSCFG.ODEN), VOS0 直接写 D3CR 即可 */
#define PWR_VOS_SCALE0      0u
#define PWR_VOS_SCALE3      1u
#define PWR_VOS_SCALE2      2u
#define PWR_VOS_SCALE1      3u

#define PWR_CR3_LDOEN       (1u << 1)
#define PWR_CSR1_ACTVOS_Pos 14
#define PWR_CSR1_ACTVOS_Msk (3u << 14)
#define PWR_CSR1_ACTVOSRDY  (1u << 13)

/* ───────────────────────── FLASH (0x52002000) ───────────────────────── */
#define FLASH_BASE      0x52002000UL
#define FLASH_ACR       REG32(FLASH_BASE + 0x00)
/* ACR: LATENCY[3:0] = 等待态数, WRHIGHFREQ[5:4] = 编程延时
 * RM0468 Table 16 (按 AXI 时钟索引, VOS0): ≤70M→0, ≤140M→1, ≤210M→2, ≤275M→3 */

/* ───────────────────────── SYSCFG (0x58000400) ───────────────────────── */
#define SYSCFG_BASE     0x58000400UL
#define SYSCFG_PWRCR    REG32(SYSCFG_BASE + 0x04)   /* H743 用 ODEN; H72x 不需要 */

/* ───────────────────────── GPIO (0x58020000 + 0x400×n) ─────────────────────────
 * H723 的 GPIO 在 AHB4, 与 F4 的 0x4002xxxx 完全不同 */
#define GPIO_BASE(n)    (0x58020000UL + 0x400UL * (n))   /* n: 0=A 1=B 2=C 3=D 4=E */
#define GPIO_MODER(n)   REG32(GPIO_BASE(n) + 0x00)
#define GPIO_OTYPER(n)  REG32(GPIO_BASE(n) + 0x04)
#define GPIO_OSPEEDR(n) REG32(GPIO_BASE(n) + 0x08)
#define GPIO_PUPDR(n)   REG32(GPIO_BASE(n) + 0x0C)
#define GPIO_IDR(n)     REG32(GPIO_BASE(n) + 0x10)
#define GPIO_ODR(n)     REG32(GPIO_BASE(n) + 0x14)
#define GPIO_BSRR(n)    REG32(GPIO_BASE(n) + 0x18)
#define GPIO_AFRL(n)    REG32(GPIO_BASE(n) + 0x20)
#define GPIO_AFRH(n)    REG32(GPIO_BASE(n) + 0x24)

/* ───────────────────────── Cortex-M7 内核调试 ───────────────────────── */
#define DEMCR           REG32(0xE000EDFCUL)   /* bit24 = TRCENA (DWT 总开关) */
#define DEMCR_TRCENA    (1u << 24)
#define DWT_CTRL        REG32(0xE0001000UL)   /* bit0 = CYCCNTENA */
#define DWT_CYCCNT      REG32(0xE0001004UL)   /* CPU 周期计数 → 1.82ns 分辨率 @550MHz */
#define DWT_LAR         REG32(0xE0001FB0UL)   /* 解锁寄存器 (需写 0xC5ACCE55) */
#define SCB_CPACR       REG32(0xE000ED88UL)   /* FPU 使能: CP10/CP11 全访问 */
#define SCB_AIRCR       REG32(0xE000ED0CUL)   /* 中断优先级分组 */
#define SCB_CCR         REG32(0xE000ED14UL)   /* bit16 = D-cache, bit17 = I-cache */
#define SCB_CCR_IC      (1u << 17)
#define SCB_CCR_DC      (1u << 16)
#define SCB_ICIALLU     REG32(0xE000EF50UL)   /* I-cache 全清 (写任意值即无效化) */
#define SCB_DCIMVAC     REG32(0xE000EF5CUL)   /* D-cache 按地址无效化 */

/* ───────────────────────── TIM (APB1 定时器) ─────────────────────────
 * H7: TIM2=0x40000000 TIM3=+400 TIM4=+800 TIM5=+C00
 *     TIM1=0x40010000 TIM8=+400 TIM6=0x40001000 TIM7=+400
 * TIMxCLK = 275MHz (APB 预分频 ≤4 时取 HCLK; 见 MIGRATE-H723.md §3.3) */
#define TIM2_BASE       0x40000000UL
#define TIM_CR1(t)      REG32((t) + 0x00)
#define TIM_DIER(t)     REG32((t) + 0x0C)
#define TIM_SR(t)       REG32((t) + 0x10)
#define TIM_EGR(t)      REG32((t) + 0x14)
#define TIM_CNT(t)      REG32((t) + 0x24)
#define TIM_PSC(t)      REG32((t) + 0x28)
#define TIM_ARR(t)      REG32((t) + 0x2C)

#define TIM_CR1_CEN     (1u << 0)
#define TIM_DIER_UIE    (1u << 0)
#define TIM_SR_UIF      (1u << 0)
#define TIM_EGR_UG      (1u << 0)   /* 立即产生更新事件 (把 PSC 立刻装载) */

/* ───────────────────────── NVIC ───────────────────────── */
#define NVIC_ISER       REG32(0xE000E100UL)
#define NVIC_ICER       REG32(0xE000E180UL)
#define NVIC_IPR(n)     REG32(0xE000E400UL + 4UL * ((n) / 4u))
#define IRQ_TIM2        28          /* TIM2 global interrupt 在向量表的位置 */
#define IRQ_USART1      37

#endif /* DCL_REGS_H */
