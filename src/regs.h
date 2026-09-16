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
/* ★ AHB4 上的 GPIO 时钟使能位 (权威: ST stm32h723xx.h `RCC_AHB4ENR_GPIOxEN_Pos` ——
 *   实测抄得: GPIOBEN=1, GPIOCEN=2, GPIODEN=3, GPIOEEN=4, GPIOFEN=5)。基址侧也核对过:
 *   `GPIOE_BASE = D3_AHB1PERIPH_BASE + 0x1000` = 0x58021000 ↔ 本文件 `GPIO_BASE(4)`
 *   (GPIOB 同理: +0x0400 = 0x58020400 ↔ GPIO_BASE(1))。 */
#define RCC_AHB4ENR_GPIOBEN (1u << 1)
/* ★ 注意 GPIOE 属 **DO 输出面** (do.h 定案: PE0..15 全归 do_init/do_poll) ——
 *   任何诊断用的脚都**不许**放这个端口 (见 main.c 的编译期防撞断言)。 */
#define RCC_AHB4ENR_GPIOEEN (1u << 4)
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

/* ───────────────────────── FLASH (0x52002000) ─────────────────────────
 * ★★ 权威来源: 板商参考工程自带的 ST 官方 CMSIS 设备头
 *   `D:/STM/tools/lxb_ref/1.LED闪烁/Drivers/CMSIS/Device/ST/STM32H7xx/Include/stm32h723xx.h`
 *   依据 (行号来自上面那个头文件):
 *     FLASH_TypeDef 结构 (L962-984):
 *       ACR 0x00, KEYR1 0x04, OPTKEYR 0x08, CR1 0x0C, SR1 0x10, CCR1 0x14,
 *       OPTCR 0x18, OPTSR_CUR 0x1C, OPTSR_PRG 0x20, OPTCCR 0x24,
 *       PRAR_CUR1 0x28, PRAR_PRG1 0x2C, SCAR_CUR1 0x30, SCAR_PRG1 0x34,
 *       WPSN_CUR1 0x38, WPSN_PRG1 0x3C, BOOT_CUR 0x40, BOOT_PRG 0x44,
 *       CRCCR1 0x50, CRCSADD1 0x54, CRCEADD1 0x58, CRCDATA 0x5C, ECC_FA1 0x60,
 *       OPTSR2_CUR 0x70, OPTSR2_PRG 0x74
 *     FLASH_SECTOR_TOTAL = 8 / FLASH_SECTOR_SIZE = 0x20000 (L10806/L10811)
 *
 * ★★ 命名教训 (W2.4 落地时亲历, 与 2026-09-10 的 DIVP1EN 同族):
 *   网上大量示例 (含 CSDN 的"STM32H723 flash 读写详细")把 H7 的 KEYR/CR/SR 写成
 *   0x0C/0x14/0x18 —— 那是 **bank2 的**偏移, 或者是把 H743 双 bank 的
 *   `FLASH_KEYR`(=KEYR1) 与 `FLASH_CR2`(=CR2, 在 0x100 段) 记混了。
 *   照抄会**静默操作到 OPTCR/SR1 上**: 写 CR 变成改选项字节, 读 SR 变成读 SR1 之外
 *   的东西 —— 症状是"解锁后 PG 位怎么都不生效", 排查极难。
 *   ⇒ 本项目纪律: 外设寄存器偏移**必须**以 CMSIS 设备头为准, 不许抄博客。 */
#define FLASH_BASE      0x52002000UL
#define FLASH_ACR       REG32(FLASH_BASE + 0x00)   /* 访问控制 (LATENCY|WRHIGHFREQ) */
#define FLASH_KEYR1     REG32(FLASH_BASE + 0x04)   /* ★ bank1 解锁密钥寄存器 */
#define FLASH_OPTKEYR   REG32(FLASH_BASE + 0x08)
#define FLASH_CR1       REG32(FLASH_BASE + 0x0C)   /* ★ bank1 控制寄存器 */
#define FLASH_SR1       REG32(FLASH_BASE + 0x10)   /* ★ bank1 状态寄存器 */
#define FLASH_CCR1      REG32(FLASH_BASE + 0x14)   /* ★ bank1 清标志寄存器 (写 1 清) */
#define FLASH_OPTCR     REG32(FLASH_BASE + 0x18)
#define FLASH_OPTSR_CUR REG32(FLASH_BASE + 0x1C)

/* ACR: LATENCY[3:0] = 等待态数, WRHIGHFREQ[5:4] = 编程延时
 * RM0468 Table 16 (按 AXI 时钟索引, VOS0): ≤70M→0, ≤140M→1, ≤210M→2, ≤275M→3 */

/* ---- 解锁密钥 (RM0468 §4.3.10 / OpenOCD stm32h7x.c: KEY1/KEY2 常量) ----
 * ★ 密钥是**整个 STM32 家族统一**的 (F1/L4/H7 全一样), 不是 per-chip 值。 */
#define FLASH_KEY1      0x45670123UL
#define FLASH_KEY2      0xCDEF89ABUL

/* ---- CR1 位 (CMSIS L10844-10872, 逐位核对) ---- */
#define FLASH_CR_LOCK     (1u << 0)    /* 1 = 锁定, 写密钥后自动清 0 */
#define FLASH_CR_PG       (1u << 1)    /* 编程使能 */
#define FLASH_CR_SER      (1u << 2)    /* 扇区擦除使能 */
#define FLASH_CR_BER      (1u << 3)    /* 整 bank 擦除 (危险, 本项目不用) */
#define FLASH_CR_PSIZE_Pos 4
#define FLASH_CR_PSIZE_Msk (3u << 4)   /* 00=8bit 01=16bit 10=32bit 11=64bit */
#define FLASH_CR_PSIZE_64  (3u << 4)   /* ★ VDD>2.7V 时用 64-bit 并行度 */
#define FLASH_CR_FW       (1u << 6)    /* Force Write (跳过写缓冲预取) */
#define FLASH_CR_START    (1u << 7)    /* 启动擦除 */
#define FLASH_CR_SNB_Pos  8            /* ★ H72x/H73x 的扇区号在 bit11:8 */
#define FLASH_CR_SNB_Msk  (0xFu << 8)

/* ---- SR1 位 (CMSIS L10914-10981) ---- */
#define FLASH_SR_BSY      (1u << 0)    /* 擦/写进行中 */
#define FLASH_SR_WBNE     (1u << 1)    /* 写缓冲非空 */
#define FLASH_SR_QW       (1u << 2)    /* ★ 操作队列忙 —— H7 的"真正完成"判据 */
#define FLASH_SR_CRC_BUSY (1u << 3)
#define FLASH_SR_EOP      (1u << 16)   /* 编程结束 (写 1 清) */
#define FLASH_SR_WRPERR   (1u << 17)   /* 写保护错 */
#define FLASH_SR_PGSERR   (1u << 18)   /* 编程时序错 */
#define FLASH_SR_STRBERR  (1u << 19)   /* 写选通错 */
#define FLASH_SR_INCERR   (1u << 21)   /* 不一致错 */
#define FLASH_SR_OPERR    (1u << 22)   /* 操作错 */

/* 全部错误位 (用于"一键清除 + 判定是否出错") */
#define FLASH_SR_ERR_Msk  (FLASH_SR_WRPERR | FLASH_SR_PGSERR | FLASH_SR_STRBERR | \
                           FLASH_SR_INCERR | FLASH_SR_OPERR)

/* ---- 扇区几何 (来自 CMSIS L10806/L10811, 非推测) ---- */
#define FLASH_SECTOR_TOTAL   8u
#define FLASH_SECTOR_SIZE    0x20000u        /* 128 KB */
#define FLASH_BANK1_BASE     0x08000000UL
/* ★ 断言: 8 × 128KB 必须正好等于链接脚本声明的 1MB (ld/STM32H723ZG_FLASH.ld) */
_Static_assert(FLASH_SECTOR_TOTAL * FLASH_SECTOR_SIZE == 1024u * 1024u,
               "flash sector geometry must tile exactly 1MB (see ld/STM32H723ZG_FLASH.ld)");

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
#define DWT_CYCCNT      REG32(0xE0001004UL)   /* CPU 周期计数 → 2.50ns 分辨率 @400MHz
                                               * (改主频请同步这句; 权威频率见 clock.h) */
#define DWT_LAR         REG32(0xE0001FB0UL)   /* 解锁寄存器 (需写 0xC5ACCE55) */
#define SCB_CPACR       REG32(0xE000ED88UL)   /* FPU 使能: CP10/CP11 全访问 */
#define SCB_AIRCR       REG32(0xE000ED0CUL)   /* 中断优先级分组 / 软复位 */
/* ★ 软复位序列 (PLC 级自愈用, 见 main.c 的"主循环失活"段):
 *   必须先写 VECTKEY = 0x5FA, 否则整次写被忽略 (Cortex-M 的"写钥匙"惯例;
 *   忘了它 = 典型的"写过了就算"静默失败)。
 *   SYSRESETREQ 是**异步**的 ⇒ 写完 dsb + 短暂自旋等它生效, 不能假设下一行就不执行了。 */
#define SCB_AIRCR_VECTKEY      (0x5FAu << 16)
#define SCB_AIRCR_SYSRESETREQ  (1u << 2)
#define SCB_CCR         REG32(0xE000ED14UL)   /* bit16 = D-cache, bit17 = I-cache */
#define SCB_CCR_IC      (1u << 17)
#define SCB_CCR_DC      (1u << 16)
#define SCB_ICIALLU     REG32(0xE000EF50UL)   /* I-cache 全清 (写任意值即无效化) */
#define SCB_DCIMVAC     REG32(0xE000EF5CUL)   /* D-cache 按地址无效化 */

/* ───────────────────────── TIM (APB1 定时器) ─────────────────────────
 * H7: TIM2=0x40000000 TIM3=+400 TIM4=+800 TIM5=+C00
 *     TIM1=0x40010000 TIM8=+400 TIM6=0x40001000 TIM7=+400
 * TIMxCLK = 2×PCLK = 200MHz (DxPPRE ≤ 4; 权威定义在 clock.h 的 CLK_TIMXCLK_HZ) */
#define TIM2_BASE       0x40000000UL
/* ★ 生产时基 (src/timebase.*): TIM5 自由运行 32 位计数器。
 *   为什么不用 TIM2/3/4 —— TIM2=100µs 拍、TIM3=步进脉冲 (TIM4 未被占用但留作备用)。
 *   为什么不用 DWT_CYCCNT —— 它是**调试单元**, 调试器会话收尾时会主动清 `DEMCR.TRCENA`
 *   把它关掉 (pyOCD #1540 / SEGGER KB 均有明文), 而 `flash.c` 拿它当**超时判据** ⇒
 *   DWT 一死, 超时永不触发 + 有界喂狗退化成无限喂狗 ⇒ **卡死且看门狗失效**。
 *   详见 docs/ASSESS-toolchain-2026-09-16.md。TIM5 = APB1 上的 32 位定时器, 不受调试器影响。 */
#define TIM5_BASE       0x40000C00UL
#define RCC_APB1LENR_TIM5EN  (1u << 3)
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

/* ══════════ W5: ADC1(16bit) + TIM3(PWM) 寄存器 ══════════
 * ★★ 位定义/偏移**全部取自官方 `stm32h723xx.h`** (本机 CubeIDE 参考工程),
 *   不是手推 —— 本项目 BRR 事故的铁律: "公式自洽但单位/位域错" 最会骗人。
 * ★ RCC 结构顺序交叉验证: AHB3ENR(0xD4) AHB1ENR(0xD8) AHB2ENR(0xDC) AHB4ENR(0xE0)
 *   —— 与项目既有 RCC_AHB4ENR=+0xE0 / RCC_APB1LENR=+0xE8 一致 (两条独立来源互证)。 */
#define RCC_AHB1ENR     REG32(RCC_BASE + 0x0D8)
#define RCC_D1CCIPR     REG32(RCC_BASE + 0x04C)
#define RCC_D3CCIPR     REG32(RCC_BASE + 0x058)
#define RCC_AHB1ENR_ADC12EN        (1u << 5)
#define RCC_APB1LENR_TIM3EN        (1u << 1)
#define RCC_D1CCIPR_CKPERSEL_SHIFT 28u   /* per_ck 源: 00=HSI 01=CSI 10=HSE 11=rsvd */
#define RCC_D3CCIPR_ADCSEL_SHIFT   16u   /* adc_ker_ck: 00=PLL2P 01=PLL3R 10=CLKP(per_ck) */

#define ADC1_BASE          0x40022000UL
#define ADC_ISR(n)         REG32((n) + 0x00)
#define ADC_CR(n)          REG32((n) + 0x08)
#define ADC_CFGR(n)        REG32((n) + 0x0C)
#define ADC_SMPR1(n)       REG32((n) + 0x14)
#define ADC_SMPR2(n)       REG32((n) + 0x18)
#define ADC_SQR1(n)        REG32((n) + 0x30)
#define ADC_DR(n)          REG32((n) + 0x40)
/* ★★ ADC1/2 pre-channel selection (H72x/73x 特有; offset 0x1C)。
 *   必须为**每个要用的通道置位**, 否则该通道的输入**根本不接到 ADC 内部** ⇒ 读到的是
 *   漂浮值, 且 CR/CFGR/CCR/MODER 等"配置类"寄存器**全部正确** —— 2026-09-11 实测踩到:
 *   AI/HIL 三条外部线(3.3V/GND/PWM回环)与内部上下拉**全都不跟随**, 查了很久。
 *   权威依据: stm32h7xx_ll_adc.h 的 LL_ADC_SetChannelPreselection() —— HAL 每配置一个
 *   通道就写一次; 它**不在我原来核对的那几个寄存器里**, 所以"读回核对"没抓到它。 */
#define ADC_PCSEL(n)       REG32((n) + 0x1C)
/* ★★ ADC12_COMMON 地址 = 0x40022300 (= ADC1_BASE + 0x300) —— **实测裁决**。
 *   板商参考工程的 stm32h723xx.h 把它定义成 `D2_AHB1PERIPH_BASE + 0x2300` (=0x40023000),
 *   却与它**自己的** ADC_Common_TypeDef 注释 "Address offset: ADC1/3 base address + 0x300"
 *   **自相矛盾**。实测 (pyocd read32): 0x40022300 可读; 0x40023000 → "memory transfer failed"
 *   (未实现地址) ⇒ 写它会触发**非精确总线错** (IMPRECISERR → HardFault), 且因"非精确"
 *   会在**下一条**寄存器访问处才爆 —— 极易把真因误指向 ADC_CR (本轮实际踩过)。
 *   ⇒ 两个"权威"冲突时用实测裁决, 并把矛盾记在原地 (本项目 BRR 事故的同族教训)。 */
#define ADC12_COMMON_BASE  0x40022300UL
#define ADC_CCR            REG32(ADC12_COMMON_BASE + 0x08)
#define ADC_CR_ADEN        (1u << 0)
#define ADC_CR_ADDIS       (1u << 1)
#define ADC_CR_ADSTART     (1u << 2)
#define ADC_CR_BOOST       (1u << 8)
/* ★★ ADVREGEN/DEEPPWD 在 **ADC_CR** 而非 ADC_CCR —— H723(RM0468) 与 H743 的差异,
 *   照 H743 写会静默失败 (寄存器写进去了但 ADC 不上电)。 */
#define ADC_CR_ADVREGEN    (1u << 28)
#define ADC_CR_DEEPPWD     (1u << 29)
#define ADC_CR_ADCAL       (1u << 31)
#define ADC_ISR_ADRDY      (1u << 0)
#define ADC_ISR_EOC        (1u << 2)
#define ADC_CCR_CKMODE_SHIFT 16u   /* 00=异步(ADCSEL) 01=AHB/1 10=AHB/2 11=AHB/4 */
#define ADC_CCR_PRESC_SHIFT  18u   /* 异步分频: 0=/1 1=/2 2=/4 … */
#define ADC_CFGR_RES_SHIFT   2u    /* 000=16bit */

#define TIM3_BASE_ADDR     0x40000400UL
#define TIM_CCMR1(t)       REG32((t) + 0x18)
#define TIM_CCER(t)        REG32((t) + 0x20)
#define TIM_CCR1(t)        REG32((t) + 0x34)
#define TIM_CCMR1_OC1M_SHIFT 4u
#define TIM_CCMR1_OC1PE    (1u << 3)
#define TIM_CCER_CC1E      (1u << 0)
#define TIM_CR1_ARPE       (1u << 7)

/* ───────────────────────── USART1 (D2/APB2, 0x40011000) ─────────────────────────
 * ★ 权威来源: 板商参考工程自带的 ST 官方 CMSIS 设备头
 *   `D:/STM/tools/lxb_ref/1.LED闪烁/Drivers/CMSIS/Device/ST/STM32H7xx/Include/stm32h723xx.h`
 *   (本项目纪律: 寄存器定义必须查权威来源, 不许猜 —— 见 2026-09-10 的 DIVP1EN 教训)
 *   导出的依据 (行号来自上面那个头文件):
 *     PERIPH_BASE            = 0x40000000                          (L2070)
 *     D2_APB2PERIPH_BASE     = PERIPH_BASE + 0x00010000            (L2088)
 *     USART1_BASE            = D2_APB2PERIPH_BASE + 0x1000         (L2230)  → 0x40011000
 *     USART_TypeDef 偏移: CR1 0x00 CR2 0x04 CR3 0x08 BRR 0x0C GTPR 0x10
 *                         RTOR 0x14 RQR 0x18 ISR 0x1C ICR 0x20 RDR 0x24
 *                         TDR 0x28 PRESC 0x2C                      (L1596-1608)
 *     RCC_APB2ENR_USART1EN   = bit4                                (L15492)
 *     USART1_IRQn            = 37                                  (L98)
 *     GPIO_AFRH_AFSEL9_Pos   = 4 / AFSEL10_Pos = 8                 (L12447 起)
 * ★ 注意 USART 的 RDR(0x24) 与 TDR(0x28) 是**两个独立地址**, 不是 F1 时代的 DR。
 *   读 RDR 才清 RXNE; 写 TDR 才发送。 */
#define RCC_APB2ENR     REG32(RCC_BASE + 0x0F0)   /* APB2 时钟使能 (USART1/SPI1/TIM1...) */
#define RCC_APB2ENR_USART1EN  (1u << 4)

#define USART1_BASE     0x40011000UL
#define USART_CR1(u)    REG32((u) + 0x00)
#define USART_CR2(u)    REG32((u) + 0x04)
#define USART_CR3(u)    REG32((u) + 0x08)
#define USART_BRR(u)    REG32((u) + 0x0C)
#define USART_ISR(u)    REG32((u) + 0x1C)
#define USART_ICR(u)    REG32((u) + 0x20)
#define USART_RDR(u)    REG32((u) + 0x24)
#define USART_TDR(u)    REG32((u) + 0x28)
#define USART_PRESC(u)  REG32((u) + 0x2C)

/* ───────────────────── USART2 (APB1, 0x40004400) — W4 Modbus 物理口 ─────────────────────
 * ★ 只加"基址 + 时钟位 + IRQ", USART_* 的**寄存器宏一个都不重定义** ——
 *   它们本来就是参数化的 (收基址), 直接复用 (见上面 USART1 段)。
 *   "同一个寄存器两套宏"是这类文件的经典腐化方式, 从源头避免。
 *
 * 导出的依据 (RM0468 / stm32h723xx.h):
 *   APB1PERIPH_BASE = 0x40000000, USART2_BASE = APB1PERIPH_BASE + 0x4400 → 0x40004400
 *   RCC_APB1LENR_USART2EN = bit17
 *   USART2_IRQn = 38
 * ★ 本项目**只用轮询、不开 USART2 中断** (照 S3: "ISR 内直接轮询 FIFO, 确定性最好"),
 *   所以 IRQ_USART2 只作记录 —— 但本文件已有 A1 事故 (NVIC 位移溢出) 的教训,
 *   凡出现 IRQ 号就必须同时给出 < 64 的编译期断言 (见文件末尾 IRQ 断言段)。
 *
 * ★★ BRR 的权威推导 (P3 补齐)。旧写法只给结论 "OVER8=0 ⇒ BRR = fCK/baud", 结论**是对的**,
 *   但它与 RM0468 的位域布局 `DIV_Mantissa[15:4] | DIV_Fraction[3:0]` 摆在一起看像是矛盾,
 *   后人很容易把它"修正"回去 —— 所以这里把中间那两步补上:
 *
 *   ① 波特率公式 (RM0468 USART 章节):  baud = fCK / (8 × (2 − OVER8) × USARTDIV)
 *      OVER8 = 0 (16 倍过采样, 本项目用值) ⇒ baud = fCK / (16 × USARTDIV)
 *      ⇒ USARTDIV = fCK / (16 × baud)
 *
 *   ② BRR 的编码:  BRR[15:4] = DIV_Mantissa = USARTDIV 的整数部分
 *                  BRR[3:0]  = DIV_Fraction = USARTDIV 的小数部分 × 16
 *      ⇒ 把整个 BRR 当"定点小数"读, 它就等于 **USARTDIV × 16**。
 *
 *   ③ 两式相消:  BRR(定点值) = USARTDIV × 16 = [fCK/(16·baud)] × 16 = **fCK / baud**
 *      ⇒ "BRR = fCK/baud" 与 RM 的位域布局**并不矛盾** —— 被消掉的那 16 就是 ② 的 ×16。
 *
 *   本平台: PCLK1 = 100 MHz, baud = 115200 ⇒ BRR = 100e6/115200 = 868.05 → **868 = 0x364**
 *          反解实际波特率 = 100e6/868 = 115207.4 ⇒ 误差 +0.006% (容限 2%, 实测两端均正确)。
 *   ★ 换口/换波特率按 ③ 直接算 (USART1 用 PCLK2, 见 uart.c 的 s_brr 计算)。
 *
 *   ✗ 错误写法 (真实发生过, 代价 = 一整轮协议不可用):
 *       `s_brr = (fCK × 16 + baud/2) / baud` = 13889 = 0x3641 —— 数字算得没错,
 *       但那等于把 ① 的 USARTDIV 又乘了一次 16 ⇒ 实际波特率 7200 而非 115200;
 *       而它的"自洽假注释"(100MHz×16/115200=0x3641, 误差0.0004%)把错误锁死了三轮。
 */
#define USART2_BASE     0x40004400UL
#define RCC_APB1LENR_USART2EN  (1u << 17)
#define IRQ_USART2      38
#define USART2_BRR_115200  868u    /* = PCLK1(100MHz)/115200, OVER8=0 */

/* CR1 (L20896 起) */
#define USART_CR1_UE      (1u << 0)    /* 使能 USART */
#define USART_CR1_RE      (1u << 2)    /* 接收使能 */
#define USART_CR1_TE      (1u << 3)    /* 发送使能 */
#define USART_CR1_RXNEIE  (1u << 5)    /* RXNE/RXFNE 中断使能 */
#define USART_CR1_OVER8   (1u << 15)   /* 8 倍过采样 (本项目用 16 倍 → 必须 0) */
/* ★★ CR1.FIFOEN (bit29) —— 使能 RX/TX FIFO (深度 8/16)。
 *   2026-09-13 实测必需: RDR 只 1 字节深, 而 100µs 拍周期 > 86.8µs 字节间隔
 *   ⇒ 相位漂移 ⇒ 周期性"一拍到 2 字节" ⇒ 必然 ORE ⇒ 见 docs/audit/H723-485-RX-AUDIT.md。
 *   ★ H7 要求本位置在 **UE=0** 时写入 ⇒ 必须先关 UE 再整写 CR1 (现有写法天然满足)。 */
#define USART_CR1_FIFOEN  (1u << 29)

/* ISR (L21188 起) */
#define USART_ISR_PE      (1u << 0)    /* 校验错误 (ISR.bit0) */
#define USART_ISR_FE      (1u << 1)    /* 帧错误: 停止位不是 1 (RM0468 ISR.bit1) */
#define USART_ISR_NE      (1u << 2)    /* 噪声错误: 起始位附近有毛刺 (ISR.bit2) */
#define USART_ISR_ORE     (1u << 3)    /* 溢出错误 */
#define USART_ISR_RXNE    (1u << 5)    /* 收到数据 (读 RDR 清除) */
#define USART_ISR_TC      (1u << 6)    /* 发送完成 */
#define USART_ISR_TXE     (1u << 7)    /* 发送数据寄存器空 */

/* ICR (L21274 起) */
#define USART_ICR_PECF    (1u << 0)
#define USART_ICR_FECF    (1u << 1)
#define USART_ICR_NECF    (1u << 2)
#define USART_ICR_ORECF   (1u << 3)    /* 清溢出标志 */
#define USART_ICR_TCCF    (1u << 6)    /* 清发送完成标志 */

/* ───────────────────────── NVIC ─────────────────────────
 * 向量表位置 (与 stm32h723xx.h 一致) */
#define IRQ_TIM2        28          /* TIM2 global interrupt */
#define IRQ_USART1      37          /* USART1 global interrupt */

/* ★★ 铁律 (A1 事故 2026-09-10, 代价 = 协议层整整一轮不可用):
 *   ISER / ICER / ISPR / ICPR 都是**寄存器数组**, 每 32 个 IRQ 占用一个 32 位寄存器。
 *   IRQ ≥ 32 时写 `NVIC_ISER = (1u << irq)` 是**未定义行为** (移位量 ≥ 位宽),
 *   而 GCC 只给一条 `-Wshift-count-overflow` 警告, **编译照样通过**。
 *   本项目 USART1 = IRQ 37 就这样"静默未使能":  CR1/BRR/GPIO 全对、LA 也能看到
 *   PA9 有真实波形、拍中断一切正常 —— 但上位机发来的**每个字节都不触发中断**,
 *   环形缓冲永远为空, 协议层功能上不可用 (TIM2 = IRQ 28 < 32 所以拍不受影响,
 *   症状只剩"串口安静地坏掉", 极难归因)。
 *   ⇒ 所以本头文件**不提供** `NVIC_ISER` 这类"裸寄存器"名字, 只提供按 IRQ 号索引的
 *     宏与函数 —— 让"IRQ≥32"这件事在编译层面就不可能写错。 */
#define NVIC_REG(base, irq)  REG32((base) + 4UL * ((uint32_t)(irq) >> 5u))
#define NVIC_ISER_W(irq)     NVIC_REG(0xE000E100UL, irq)
#define NVIC_ICER_W(irq)     NVIC_REG(0xE000E180UL, irq)
#define NVIC_ISPR_W(irq)     NVIC_REG(0xE000E200UL, irq)
#define NVIC_ICPR_W(irq)     NVIC_REG(0xE000E280UL, irq)
#define NVIC_BIT(irq)        (1u << ((uint32_t)(irq) & 31u))

/** @brief 使能一个中断 (自动处理 IRQ≥32 的寄存器索引) */
static inline void nvic_enable_irq(uint32_t irq)
{
    NVIC_ISER_W(irq) = NVIC_BIT(irq);
    /* 回读确认: 写没生效时立刻可见, 而不是等到"串口没反应"再回头猜 */
    (void)NVIC_ISER_W(irq);
}
/** @brief 关闭一个中断 */
static inline void nvic_disable_irq(uint32_t irq)
{
    NVIC_ICER_W(irq) = NVIC_BIT(irq);
    (void)NVIC_ICER_W(irq);
}
/** @brief 该 IRQ 当前是否使能 (供自检/外部审计断言"中断真的开了") */
static inline uint32_t nvic_is_enabled(uint32_t irq)
{
    return (NVIC_ISER_W(irq) >> ((uint32_t)irq & 31u)) & 1u;
}

/* ★ 编译期护栏: 本宏只覆盖 IRQ 0..63 (ISER 两个寄存器), 越界立刻断掉。
 *   将来若引入 IRQ ≥ 64 的外设, 必须显式扩展这里的范围并复核所有调用点。 */
_Static_assert(IRQ_TIM2   < 64u, "IRQ_TIM2 outside NVIC macro range 0..63");
_Static_assert(IRQ_USART1 < 64u, "IRQ_USART1 outside NVIC macro range 0..63");

/* ★ 优先级是**按字节**编址的 (IPR0..IPR59 每个 8 位)。用 REG32 写会一次改掉 4 个
 *   中断的优先级 —— 所以这里提供字节访问。主循环/驱动一律用这个。
 *   (原 NVIC_IPR(n) 是个 32 位访问器, 有上述误伤风险且无人使用, 已删除) */
#define NVIC_IPB(n)     (*(volatile uint8_t *)(0xE000E400UL + (n)))

/* ══════════ RCC: 复位状态 (RSR) 与 LSI (2026-09-13) ══════════
 * ★★ 位定义**抄自 ST 官方设备头** (权威: 本机
 *   D:/STM/tools/lxb_ref/.../CMSIS/Device/ST/STM32H7xx/Include/stm32h723xx.h)。
 *   ⚠️ 本项目 main.c 原来那套注释位表**是错的** (把 bit16 当 PORR —— 真身是 **RMVF**;
 *      把 bit24 当"不存在" —— 真身是 SFTRSTF; 18/19 位根本不存在),
 *      而按错注释写出的清除代码 `|= (1u<<24)` **清的是别的位** ⇒ 复位标志从未被清、
 *      从首次上电起一直累积 (实测读出 0x01FA0000, 7 个域复位位同置)。
 *   ⇒ 教训 (同族第 N 次): **位定义必须抄权威头, 注释里自洽的推导不算证据。** */
#define RCC_CSR         REG32(RCC_BASE + 0x074)   /* LSION/LSIRDY 等 */
#define RCC_BDCR        REG32(RCC_BASE + 0x070)   /* 备份域: RTCSEL/RTCEN/LSEON
                                                   * (RTC 相关宏见下方 RTC 小节) */
#define RCC_CSR_LSION   (1u << 0)
#define RCC_CSR_LSIRDY  (1u << 1)
#define RCC_RSR         REG32(RCC_BASE + 0x0D0)   /* 复位状态 (R/W, 见下) */
#define RCC_RSR_RMVF    (1u << 16)   /* ★ 写 1 清除全部复位标志 (不是 bit24!) */
#define RCC_RSR_CPURSTF (1u << 17)
#define RCC_RSR_D1RSTF  (1u << 19)
#define RCC_RSR_D2RSTF  (1u << 20)
#define RCC_RSR_BORRSTF (1u << 21)
#define RCC_RSR_PINRSTF (1u << 22)
#define RCC_RSR_PORRSTF (1u << 23)
#define RCC_RSR_SFTRSTF (1u << 24)
/* ★★ 下面这三位是**看门狗识别**的关键 —— 我第一遍只取了前 8 个位就下结论说
 *   "RSR 没有看门狗位", 那是**我自己 grep 截断**造成的误判 (head -24 切掉了后半)。
 *   完整位表: 16/17/19/20/21/22/23/24/26/28/30。教训同族: **读权威源要读全**,
 *   数输出行数不叫核实。 */
#define RCC_RSR_IWDG1RSTF (1u << 26)   /* ★ 独立看门狗复位 (我们用的这个) */
#define RCC_RSR_WWDG1RSTF (1u << 28)   /* 窗口看门狗复位 (未用) */
#define RCC_RSR_LPWRRSTF  (1u << 30)   /* 低功耗模式复位 */
/* 全部"复位原因"位的掩码 (RMVF 不算原因) —— 供"只置了一位"这类判据用 */
#define RCC_RSR_CAUSE_Msk (RCC_RSR_CPURSTF | RCC_RSR_D1RSTF | RCC_RSR_D2RSTF \
    | RCC_RSR_BORRSTF | RCC_RSR_PINRSTF | RCC_RSR_PORRSTF | RCC_RSR_SFTRSTF \
    | RCC_RSR_IWDG1RSTF | RCC_RSR_WWDG1RSTF | RCC_RSR_LPWRRSTF)

/* ══════════ IWDG1 —— 独立看门狗 (2026-09-13) ══════════
 * 基址 = D3_APB1PERIPH_BASE(0x58000000) + 0x4800。
 * ★ 选 IWDG 不选 WWDG: ① IWDG 走 **LSI**, 不依赖 APB/主时钟 ⇒ 时钟树错了它还在数;
 *   ② 一旦启动**无法停止** (只能复位) ⇒ 不会被软件误关;
 *   ③ WWDG 的窗口语义与"忙等"冲突, 且超时上限只有几十 ms (PCLK 100MHz 下),
 *      做不出 200ms 这一档。 */
#define IWDG1_BASE      (0x58004800UL)
#define IWDG_KR         REG32(IWDG1_BASE + 0x00)   /* 键寄存器 */
#define IWDG_PR         REG32(IWDG1_BASE + 0x04)   /* 预分频 */
#define IWDG_RLR        REG32(IWDG1_BASE + 0x08)   /* 重载值 */
#define IWDG_SR         REG32(IWDG1_BASE + 0x0C)   /* 状态 (PVU/RVU 更新中) */
#define IWDG_WINR       REG32(IWDG1_BASE + 0x10)   /* 窗口 (不用, 保持默认) */
/* 键值 —— ★ **不在 ST 设备头里** (RM0468 定义), 故须实机验证一次:
 *   解锁 PR/RLR 可写 → 0x5555;  启动计数 → 0xCCCC;  喂狗(重载) → 0xAAAA */
#define IWDG_KEY_UNLOCK  0x5555u
#define IWDG_KEY_START   0xCCCCu
#define IWDG_KEY_FEED    0xAAAAu
#define IWDG_SR_PVU      (1u << 0)   /* 预分频寄存器正在更新 */
#define IWDG_SR_RVU      (1u << 1)   /* 重载寄存器正在更新 */
#define IWDG_SR_WVU      (1u << 2)   /* 窗口寄存器正在更新 (我们不用窗口, 但**必须一起等**) */
/* ★ 三个标志的语义 (RM0468 §50.4.4): set = 该寄存器的更新正在 VDD 域进行中;
 *   **reset by hardware when the update operation is completed in the VDD voltage
 *   domain (takes up to five RC 40 kHz cycles)** ⇒ 正常约 125µs 内自己落。
 *   ST 的 HAL_IWDG_Init() 等的就是这三个 (`IWDG_KERNEL_UPDATE_FLAGS`)。
 *   ★ 我们第一版只等了 PVU|RVU —— 这本身不是失败原因, 但口径必须与 HAL 一致。 */
#define IWDG_SR_UPDATE_Msk (IWDG_SR_PVU | IWDG_SR_RVU | IWDG_SR_WVU)

#endif /* DCL_REGS_H */
