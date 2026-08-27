/**
 * Phase 0: DTCM 双端口并发验证测试
 *
 * 目标: 实测 CPU (D-bus→Port 0) 与 DMA (AHBS→Port 1)
 *       同时访问 DTCM 时是否存在仲裁延迟。
 *
 * 参考: firmware/h723-core0/Src/main.c (稳定运行的生产代码)
 *
 * 测试方法:
 *   1. CPU 在 tight loop 中反复读 DTCM, 用 DWT 测量周期数
 *   2. DMA2 Stream5 SHADOW→GPIOE_ODR 持续运行 (同生产代码)
 *   3. 对比: DMA 空闲 vs DMA 活跃时的 CPU 读 DTCM 延迟
 *
 * 预期: 零仲裁 — CPU Port 0 与 DMA Port 1 物理独立。
 *       实测应显示两种情景延迟相同 (1-2 cycles/read)。
 *
 * 硬件: STM32H723ZG, PE2 作测量输出
 *
 * 构建: 替换 h723-core0/Src/main.c 编译
 *       (使用 h723-core0 的 startup + linker)
 */

#include <stdint.h>

/* ═══════════════════════════════════════════════════════════
 * 寄存器定义 (来自 registers.h, 保持与生产代码一致)
 * ═══════════════════════════════════════════════════════════ */

/* RCC */
#define RCC_BASE      0x58024400UL
#define RCC_CR        (*(volatile uint32_t *)(RCC_BASE + 0x00))
#define RCC_CFGR      (*(volatile uint32_t *)(RCC_BASE + 0x10))
#define RCC_D1CFGR    (*(volatile uint32_t *)(RCC_BASE + 0x18))
#define RCC_D2CFGR    (*(volatile uint32_t *)(RCC_BASE + 0x1C))
#define RCC_PLLCKSELR (*(volatile uint32_t *)(RCC_BASE + 0x28))
#define RCC_PLLCFGR   (*(volatile uint32_t *)(RCC_BASE + 0x2C))
#define RCC_PLL1DIVR  (*(volatile uint32_t *)(RCC_BASE + 0x30))
#define RCC_APB2ENR   (*(volatile uint32_t *)(RCC_BASE + 0x0F0))
#define RCC_APB1LENR  (*(volatile uint32_t *)(RCC_BASE + 0x0E8))
#define RCC_AHB1ENR   (*(volatile uint32_t *)(RCC_BASE + 0x0D8))
#define RCC_AHB4ENR   (*(volatile uint32_t *)(RCC_BASE + 0x0E0))

/* PWR */
#define PWR_BASE      0x58024800UL
#define PWR_D3CR      (*(volatile uint32_t *)(PWR_BASE + 0x18))
#define PWR_CSR1      (*(volatile uint32_t *)(PWR_BASE + 0x04))

/* FLASH */
#define FLASH_BASE    0x52002000UL
#define FLASH_ACR     (*(volatile uint32_t *)(FLASH_BASE + 0x00))

/* GPIO */
#define GPIOE_BASE    0x58021000UL
#define GPIOE_MODER   (*(volatile uint32_t *)(GPIOE_BASE + 0x00))
#define GPIOE_OSPEEDR (*(volatile uint32_t *)(GPIOE_BASE + 0x08))
#define GPIOE_ODR     (*(volatile uint32_t *)(GPIOE_BASE + 0x14))
#define GPIOE_AFRL    (*(volatile uint32_t *)(GPIOE_BASE + 0x20))
#define GPIOE_AFRH    (*(volatile uint32_t *)(GPIOE_BASE + 0x24))

#define GPIOB_BASE    0x58020400UL
#define GPIOB_MODER   (*(volatile uint32_t *)(GPIOB_BASE + 0x00))
#define GPIOB_ODR     (*(volatile uint32_t *)(GPIOB_BASE + 0x14))

/* DMA2 */
#define DMA2_BASE     0x40020400UL
#define DMA2_LIFCR    (*(volatile uint32_t *)(DMA2_BASE + 0x08))
#define DMA2_HIFCR    (*(volatile uint32_t *)(DMA2_BASE + 0x0C))
/* Stream 0 */
#define DMA2_S0CR     (*(volatile uint32_t *)(DMA2_BASE + 0x10))
#define DMA2_S0NDTR   (*(volatile uint32_t *)(DMA2_BASE + 0x14))
#define DMA2_S0PAR    (*(volatile uint32_t *)(DMA2_BASE + 0x18))
#define DMA2_S0M0AR   (*(volatile uint32_t *)(DMA2_BASE + 0x1C))
#define DMA2_S0FCR    (*(volatile uint32_t *)(DMA2_BASE + 0x24))
/* Stream 1 */
#define DMA2_S1CR     (*(volatile uint32_t *)(DMA2_BASE + 0x28))
#define DMA2_S1NDTR   (*(volatile uint32_t *)(DMA2_BASE + 0x2C))
#define DMA2_S1PAR    (*(volatile uint32_t *)(DMA2_BASE + 0x30))
#define DMA2_S1M0AR   (*(volatile uint32_t *)(DMA2_BASE + 0x34))
#define DMA2_S1FCR    (*(volatile uint32_t *)(DMA2_BASE + 0x3C))
/* Stream 2 */
#define DMA2_S2CR     (*(volatile uint32_t *)(DMA2_BASE + 0x40))
#define DMA2_S2NDTR   (*(volatile uint32_t *)(DMA2_BASE + 0x44))
#define DMA2_S2PAR    (*(volatile uint32_t *)(DMA2_BASE + 0x48))
#define DMA2_S2M0AR   (*(volatile uint32_t *)(DMA2_BASE + 0x4C))
#define DMA2_S2FCR    (*(volatile uint32_t *)(DMA2_BASE + 0x54))
/* Stream 5 — 同生产代码: SHADOW→GPIOE_ODR */
#define DMA2_S5CR     (*(volatile uint32_t *)(DMA2_BASE + 0x88))
#define DMA2_S5NDTR   (*(volatile uint32_t *)(DMA2_BASE + 0x8C))
#define DMA2_S5PAR    (*(volatile uint32_t *)(DMA2_BASE + 0x90))
#define DMA2_S5M0AR   (*(volatile uint32_t *)(DMA2_BASE + 0x94))
#define DMA2_S5FCR    (*(volatile uint32_t *)(DMA2_BASE + 0x9C))

/* DMAMUX1 */
#define DMAMUX1_BASE  0x40020800UL
#define DMAMUX1_S0CR  (*(volatile uint32_t *)(DMAMUX1_BASE + 0x00))
#define DMAMUX1_S1CR  (*(volatile uint32_t *)(DMAMUX1_BASE + 0x04))
#define DMAMUX1_S5CR  (*(volatile uint32_t *)(DMAMUX1_BASE + 0x14))
/* DMAMUX 通道: Stream5 使用 Channel 13 */
#define DMAMUX1_CH13CR (*(volatile uint32_t *)(DMAMUX1_BASE + 0x34))

/* TIM2 */
#define TIM2_BASE     0x40000000UL
#define TIM2_CR1      (*(volatile uint32_t *)(TIM2_BASE + 0x00))
#define TIM2_DIER     (*(volatile uint32_t *)(TIM2_BASE + 0x0C))
#define TIM2_SR       (*(volatile uint32_t *)(TIM2_BASE + 0x10))
#define TIM2_PSC      (*(volatile uint32_t *)(TIM2_BASE + 0x28))
#define TIM2_ARR      (*(volatile uint32_t *)(TIM2_BASE + 0x2C))
#define TIM2_CNT      (*(volatile uint32_t *)(TIM2_BASE + 0x24))

/* TIM1 */
#define TIM1_BASE     0x40010000UL
#define TIM1_CR1      (*(volatile uint32_t *)(TIM1_BASE + 0x00))
#define TIM1_CR2      (*(volatile uint32_t *)(TIM1_BASE + 0x04))
#define TIM1_DIER     (*(volatile uint32_t *)(TIM1_BASE + 0x0C))
#define TIM1_SR       (*(volatile uint32_t *)(TIM1_BASE + 0x10))
#define TIM1_PSC      (*(volatile uint32_t *)(TIM1_BASE + 0x28))
#define TIM1_ARR      (*(volatile uint32_t *)(TIM1_BASE + 0x2C))
#define TIM1_CCR4     (*(volatile uint32_t *)(TIM1_BASE + 0x40))

/* DWT (Data Watchpoint and Trace) */
#define DEMCR         (*(volatile uint32_t *)0xE000EDFC)
#define DWT_CTRL      (*(volatile uint32_t *)0xE0001000)
#define DWT_CYCCNT    (*(volatile uint32_t *)0xE0001004)

/* SCB */
#define SCB_ICIALLU   (*(volatile uint32_t *)0xE000EF50)

/* ──── 常量 ──── */
#define TIMEOUT       8000000
#define DTCM_BASE     0x20000000UL

/* ──── DTCM 测试缓冲 ──── */
/* 使用 DTCM 中未使用的区域:
 *   ENGINE 区 0x20000000-0x200016FF
 *   我们使用 0x2000E000-0x2000E0FF 作为测试缓冲区 */
#define TEST_BUF      ((volatile uint32_t *)(DTCM_BASE + 0xE000))
#define TEST_RESULT0  (*(volatile uint32_t *)(DTCM_BASE + 0xE100))
#define TEST_RESULT1  (*(volatile uint32_t *)(DTCM_BASE + 0xE104))
#define TEST_RESULT2  (*(volatile uint32_t *)(DTCM_BASE + 0xE108))
#define TEST_RESULT3  (*(volatile uint32_t *)(DTCM_BASE + 0xE10C))
#define TEST_RESULT4  (*(volatile uint32_t *)(DTCM_BASE + 0xE110))
#define TEST_RESULT5  (*(volatile uint32_t *)(DTCM_BASE + 0xE114))
#define TEST_MAGIC    (*(volatile uint32_t *)(DTCM_BASE + 0xE120))
#define TEST_DONE     (*(volatile uint32_t *)(DTCM_BASE + 0xE124))

/* SHADOW_GPIO 地址 (同生产代码) */
#define SHADOW_GPIO_ADDR   (DTCM_BASE + 0x00E0)
#define SHADOW_GPIO        (*(volatile uint32_t *)SHADOW_GPIO_ADDR)

/* 错误率计数器 */
#define ERR_CNT_ADDR       (DTCM_BASE + 0xE128)
#define ERR_CNT            (*(volatile uint32_t *)ERR_CNT_ADDR)

/* ──── 测试参数 ──── */
#define SAMPLE_COUNT   100000     /* 每次测试读 10 万次 */
#define DMA_BUF_ADDR   0x20000000 /* DMA 读的 DTCM 地址 (ENGINE_TIMING_BASE) */
#define GPIO_DUMMY_ADDR ((uint32_t)&GPIOB_ODR)  /* DMA 写目标 (GPIOB 作 dummy) */

/* ═══════════════════════════════════════════════════════════
 * 时钟初始化 (同 main.c line 267-309)
 * ═══════════════════════════════════════════════════════════ */
static void system_clock_init(void) {
    /* VOS0 */
    PWR_D3CR = (PWR_D3CR & ~(3u << 14)) | (3u << 14);
    { uint32_t t = TIMEOUT; while (!(PWR_CSR1 & (1u << 14)) && --t) {} }

    /* 关 PLL */
    RCC_CR &= ~(1 << 24);
    { uint32_t t = TIMEOUT; while ((RCC_CR & (1 << 25)) && --t) {} }

    /* 配 PLL: HSI=64M, DIVM1=32 → 2MHz, ×240 → 480MHz VCO */
    RCC_PLLCKSELR = (0 << 0) | (5 << 4);
    RCC_PLL1DIVR  = (0 << 24) | (0 << 16) | (0 << 9) | (24 << 0);
    RCC_PLLCFGR   = (0 << 1) | (1 << 16);
    RCC_CR |= (1 << 24);
    { uint32_t t = TIMEOUT; while (!(RCC_CR & (1 << 25)) && --t) {} }

    /* Flash 4WS + cache */
    FLASH_ACR = 0x00000704;
    __asm__ volatile("dsb; isb"); (void)FLASH_ACR;

    /* HPRE=/2 → 240MHz AHB, D2PPRE2=/2 → 120MHz APB2 */
    { uint32_t v = *(volatile uint32_t *)(RCC_BASE + 0x18);
      v &= ~(0xFu << 0); v |= (8u << 0);
      *(volatile uint32_t *)(RCC_BASE + 0x18) = v; }
    { uint32_t v = *(volatile uint32_t *)(RCC_BASE + 0x1C);
      v &= ~(7u << 7); v |= (4u << 7);
      *(volatile uint32_t *)(RCC_BASE + 0x1C) = v; }

    /* 切到 PLL */
    RCC_CFGR = (RCC_CFGR & ~0x07) | 0x03;
    __asm__ volatile("dsb; isb");
    { uint32_t t = TIMEOUT; while (((RCC_CFGR >> 3) & 7) != 3 && --t) {} }
}

/* ═══════════════════════════════════════════════════════════
 * GPIO 初始化 (同 main.c PE2 配置)
 * PE2 = 输出, 用于示波器/逻辑分析仪测量
 * ═══════════════════════════════════════════════════════════ */
static void gpio_init(void) {
    RCC_AHB4ENR |= (1 << 4);  /* GPIOEEN */
    __asm__ volatile("dsb");

    /* PE2 输出 (moder bit 4:5 = 01) */
    uint32_t moder = GPIOE_MODER;
    moder &= ~(3u << 4);
    moder |= (1u << 4);       /* 01=通用输出 */
    GPIOE_MODER = moder;
    GPIOE_OSPEEDR |= (3u << 4); /* very high speed */
    GPIOE_ODR = 0;

    /* 也开 GPIOB 时钟 (DMA 写目标) */
    RCC_AHB4ENR |= (1 << 1); /* GPIOBEN */
    __asm__ volatile("dsb");
}

/* ═══════════════════════════════════════════════════════════
 * DWT 周期计数器初始化
 * ═══════════════════════════════════════════════════════════ */
static void dwt_init(void) {
    DEMCR |= (1 << 24);       /* TRCENA=1 */
    DWT_CYCCNT = 0;
    DWT_CTRL |= 1;            /* CYCCNTENA=1 */
}

/* ──── 微秒级延时 (忙等) ──── */
static void delay_us(uint32_t us) {
    uint32_t start = DWT_CYCCNT;
    uint32_t ticks = us * 240;  /* 240MHz HCLK */
    while ((DWT_CYCCNT - start) < ticks) {}
}

/* ═══════════════════════════════════════════════════════════
 * DMA2 Stream5 配置 — 同生产代码 (main.c line 1619-1634)
 *
 * 与生产代码的关键区别:
 *   触发源从 TIM1_CC4 改为 TIM2_UP (1μs 周期)
 *   写目标从 GPIOE_ODR 改为 GPIOB_ODR (避免干扰测量脚)
 * ═══════════════════════════════════════════════════════════ */
static void dma_stress_init(void) {
    /* 开 DMA2 + DMAMUX1 时钟 */
    RCC_AHB1ENR |= (1 << 1) | (1 << 2);  /* DMA2EN + DMAMUX1EN */
    __asm__ volatile("dsb; isb");
    { volatile uint32_t chk = RCC_AHB1ENR; (void)chk; }

    /* 配 DMAMUX: Stream5 由 TIM2_UP 触发
     * DMAMUX request ID for TIM2_UP on H723 = ??? */
    /* 查 RM0468 DMAMUX 表: TIM2_UP = 请求 ID 11 */
    DMAMUX1_CH13CR = 11;  /* TIM2_UP → DMA2_Stream5 */

    /* 禁用 Stream5 */
    DMA2_S5CR = 0;
    { uint32_t tout = 8000000; while ((DMA2_S5CR & 1) && --tout) {} }
    DMA2_HIFCR = 0x00000F7C;  /* 清 Stream5 标志 */
    __asm__ volatile("dsb; isb");

    DMA2_S5NDTR = 0;        /* 解锁 M0AR */
    DMA2_S5PAR  = GPIO_DUMMY_ADDR;  /* 目标: GPIOB_ODR (dummy) */
    DMA2_S5M0AR = DMA_BUF_ADDR;     /* 源: DTCM (ENGINE TIMING BASE) */
    DMA2_S5NDTR = 1;                  /* 1 字传输 */
    DMA2_S5FCR  = 0;                  /* 直通模式 */

    /* CR: M2P + CIRC + PL=最高 */
    { uint32_t cr = (1 << 6)          /* DIR=01 (M2P) */
                   | (1 << 8)          /* CIRC=1 */
                   | (3 << 16);        /* PL=最高 */
      DMA2_S5CR = cr; }
    __asm__ volatile("dsb");
    DMA2_S5CR |= 1;                   /* EN=1 */
    __asm__ volatile("dsb; isb");
}

/* ═══════════════════════════════════════════════════════════
 * TIM2 初始化 — 1μs 周期, 仅 DMA 触发 (UIE=0)
 * ═══════════════════════════════════════════════════════════ */
static void tim2_dma_trigger_init(void) {
    /* 开 TIM2 时钟 (APB1) */
    RCC_APB1LENR |= (1 << 0);  /* TIM2EN */
    __asm__ volatile("dsb");

    TIM2_CR1 = 0;   /* stop */
    __asm__ volatile("dsb");

    /* Timer clock = 240MHz (APB1×2, APB1=120MHz)
     * 1μs period: ARR = 240 - 1 = 239 */
    TIM2_PSC = 0;
    TIM2_ARR = 239;     /* 1μs @ 240MHz */
    TIM2_DIER = (1 << 8);  /* UDE=1 (DMA), UIE=0 (无中断) */
    TIM2_CNT = 0;
    __asm__ volatile("dsb; isb");
    TIM2_CR1 = 1;      /* start */
    __asm__ volatile("dsb; isb");
}

/* ═══════════════════════════════════════════════════════════
 * TIM1 初始化 — 同生产代码, 用于同步 DMA 触发 (CC4)
 * 可选备用: 如果 TIM2_UP 作为 DMA 触发不稳定
 * ═══════════════════════════════════════════════════════════ */
static void tim1_init(void) {
    RCC_APB2ENR |= (1 << 0);  /* TIM1EN */
    __asm__ volatile("dsb");
    TIM1_CR1 = 0;
    __asm__ volatile("dsb");

    TIM1_PSC = 0;
    TIM1_ARR = 11999;         /* 100μs @ 240MHz */
    TIM1_CCR4 = 11700;        /* 97.5μs (DMA 触发点) */

    /* CH4: PWM mode 2 */
    *(volatile uint16_t *)(TIM1_BASE + 0x1C) = (6 << 4) | (1 << 3); /* CCMR2 */
    *(volatile uint16_t *)(TIM1_BASE + 0x20) |= (1 << 12); /* CCER CC4E */

    /* CR2: MMS=100 OC4REF 作为 TRGO */
    TIM1_CR2 = (4 << 4);

    /* DIER: 仅 CC4DE, 无 UIE (不产生 ISR) */
    TIM1_DIER = (1 << 12);
    __asm__ volatile("dsb; isb");
    TIM1_CR1 = 1;  /* start */
    __asm__ volatile("dsb; isb");
}

/* ═══════════════════════════════════════════════════════════
 * 测量函数: CPU 读 DTCM 延迟
 *
 * 使用 GPIO toggle 包裹读操作, 可用示波器直接观察.
 * PE2 = 高表示"正在读 DTCM", 脉宽 = 一次读的耗时.
 *
 * 同时用 DWT 累加器做精确计数.
 * ═══════════════════════════════════════════════════════════ */

/* ──── 测试 A: DMA 空闲时读 DTCM ──── */
static uint32_t test_read_dtcm_idle(void) {
    uint32_t start, end, sum = 0;
    volatile uint32_t dummy = 0;
    volatile uint32_t *addr = &TEST_BUF[0];
    int i;

    /* 初始化测试缓冲 */
    TEST_BUF[0] = 0xDEADBEEF;

    /* 预热 I-cache */
    for (i = 0; i < 10; i++) {
        dummy += *addr;
    }

    /* 正式测量 */
    GPIOE_ODR |= (1 << 2);   /* PE2↑ — scope trigger */
    start = DWT_CYCCNT;

    for (i = 0; i < SAMPLE_COUNT; i++) {
        dummy += *addr;       /* 读 DTCM (核心被测操作) */
    }

    end = DWT_CYCCNT;
    GPIOE_ODR &= ~(1 << 2);  /* PE2↓ */

    TEST_RESULT0 = start;
    TEST_RESULT1 = end;
    TEST_RESULT2 = end - start;      /* 总周期数 */
    TEST_RESULT3 = (end - start) / SAMPLE_COUNT;  /* 每读一次平均周期 */

    (void)dummy;
    return end - start;
}

/* ──── 测试 B: DMA 活跃时读 DTCM ──── */
static uint32_t test_read_dtcm_dma_active(void) {
    uint32_t start, end;
    volatile uint32_t dummy = 0;
    volatile uint32_t *addr = &TEST_BUF[0];
    int i;

    /* 启动 DMA (TIM2 已运行, 这里使能 DMA Stream5) */
    DMA2_S5CR |= 1;                  /* EN=1, DMA 开始主动读 DTCM */
    __asm__ volatile("dsb; isb");

    /* 等几个 DMA 周期确保 DMA 活跃 */
    delay_us(10);

    /* 预热 */
    for (i = 0; i < 10; i++) {
        dummy += *addr;
    }

    /* 正式测量 — DMA 在后台持续读 DTCM */
    GPIOE_ODR |= (1 << 2);   /* PE2↑ */
    start = DWT_CYCCNT;

    for (i = 0; i < SAMPLE_COUNT; i++) {
        dummy += *addr;       /* 读 DTCM (同时 DMA 也在读 DTCM) */
    }

    end = DWT_CYCCNT;
    GPIOE_ODR &= ~(1 << 2);  /* PE2↓ */

    /* 停 DMA */
    DMA2_S5CR &= ~1;
    __asm__ volatile("dsb; isb");

    TEST_RESULT4 = end - start;
    TEST_RESULT5 = (end - start) / SAMPLE_COUNT;

    (void)dummy;
    return end - start;
}

/* ──── 测试 C: DMA 写 DTCM 同时 CPU 读 DTCM ──── */
static uint32_t test_dma_write_cpu_read(void) {
    uint32_t start, end;
    volatile uint32_t dummy = 0;
    volatile uint32_t *dma_write_addr = &TEST_BUF[4];
    volatile uint32_t *cpu_read_addr  = &TEST_BUF[5];
    int i;

    /* 重新配置 DMA: 这次将 DTCM 数据搬到另一个 DTCM 地址?
     * DMA M2M 只支持 DMA1, 所以换一种方式:
     * 用 Stream5 从 TEST_BUF[4] → GPIOB_ODR,
     * CPU 读 TEST_BUF[5] (不同地址, 同 DTCM) */
    DMA2_S5M0AR = (uint32_t)dma_write_addr;  /* DMA 读 DTCM[4] */
    DMA2_S5PAR  = GPIO_DUMMY_ADDR;
    DMA2_S5NDTR = 1;
    DMA2_S5FCR  = 0;
    { uint32_t cr = (1 << 6) | (1 << 8) | (3 << 16);
      DMA2_S5CR = cr | 1; }
    __asm__ volatile("dsb; isb");
    delay_us(10);

    /* CPU 读 DTCM[5] (不同地址) 同时 DMA 读 DTCM[4] */
    GPIOE_ODR |= (1 << 2);
    start = DWT_CYCCNT;

    for (i = 0; i < SAMPLE_COUNT; i++) {
        dummy += *cpu_read_addr;  /* CPU 读 DTCM[5] */
    }

    end = DWT_CYCCNT;
    GPIOE_ODR &= ~(1 << 2);

    DMA2_S5CR &= ~1;

    TEST_RESULT4 = end - start;
    TEST_RESULT5 = (end - start) / SAMPLE_COUNT;

    (void)dummy;
    return end - start;
}

/* ═══════════════════════════════════════════════════════════
 * 主函数
 * ═══════════════════════════════════════════════════════════ */
int main(void) {
    /* ── 初始化 ── */
    system_clock_init();
    gpio_init();
    dwt_init();

    /* 标记测试开始 */
    TEST_MAGIC = 0x50483000;   /* "PH0" Phase 0 start marker */
    TEST_DONE  = 0;

    /* ── 测量前: 校验 DTCM 可正常访问 ── */
    TEST_BUF[0] = 0x12345678;
    if (TEST_BUF[0] != 0x12345678) {
        /* DTCM 不可用 — 严重故障 */
        TEST_MAGIC = 0x44454144;  /* "DEAD" */
        while (1) {}
    }
    TEST_MAGIC = 0x50483001;

    /* ── 测试 A: 基准 — DMA 空闲 ── */
    {
        uint32_t t = test_read_dtcm_idle();
        ERR_CNT = 0; (void)t;
    }
    TEST_MAGIC = 0x50483002;

    /* ── 初始化定时器 (DMA 触发源) ── */
    tim2_dma_trigger_init();
    /* 或用 TIM1: tim1_init(); */
    TEST_MAGIC = 0x50483003;

    /* ── 初始化 DMA (不启动, 先配好) ── */
    dma_stress_init();
    /* DMA Stream5 此时已配好但未使能 (EN=1 在 test_b 中) */
    TEST_MAGIC = 0x50483004;

    /* ── 测试 B: DMA 活跃 — 同地址 ── */
    {
        uint32_t t = test_read_dtcm_dma_active();
        ERR_CNT = 0; (void)t;
    }
    TEST_MAGIC = 0x50483005;

    /* ── 测试 C: DMA 写某地址 + CPU 读不同地址 ── */
    {
        uint32_t t = test_dma_write_cpu_read();
        ERR_CNT = 0; (void)t;
    }
    TEST_MAGIC = 0x50483006;

    /* ── 全部完成 ── */
    TEST_DONE = 1;
    TEST_MAGIC = 0x5048444E;  /* "PHDN" Phase 0 Done */

    /* 用 PE2 闪烁表示测试完成 */
    while (1) {
        GPIOE_ODR ^= (1 << 2);
        { volatile uint32_t delay;
          for (delay = 0; delay < 10000000; delay++) {} }
    }
}
