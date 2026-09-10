/**
 * uart.c — H723 USART1 驱动 (协议物理层)
 *
 * 设计要点 (每条都有理由, 不是惯例):
 *  ① **中断优先级低于 100μs 拍**。拍抖动是 DCL 的命根子; 外设中断抢拍 = 自毁。
 *     USART1 在 NVIC 里排 0x80, TIM2 排 0 —— 拍永远能抢占串口。
 *  ② RX 中断里只做两件事: 读 RDR、塞环形缓冲。**不在中断里解析、不回调业务**。
 *     实测: 115200 下一字节 86.8μs, 主循环有充足余量把缓冲排空。
 *  ③ 溢出(ORE)**必须显式清**。ORE 置位后若不清, 接收通路会被卡住 ——
 *     表现为"串口突然收不到东西", 极难排查。这里清标志并计数, 让故障可见。
 *  ④ 发送等 **TC 而不是 TXE** —— TC 保证最后一字节已完整移出线,
 *     否则 LA 抓到的波形会缺尾巴, 且半双工场景下会踩到对方。
 *  ⑤ 接收中断体放 ITCM (每字节都走, 属热路径; flash 取指成本随落位摆动 15%,
 *     见 docs/REF-flash-placement.md)。
 *
 * 波特率: BRR = round(PCLK2 × 16 / baud)  (OVER8=0, PRESC=0)
 *   H723 本项目: PCLK2 = 100 MHz, 115200 → 13889 = 0x3641
 *   实际波特率 100e6/(868 + 1/16) = 115199.6 → 误差 0.0004% (远优于 2% 容限)
 */
#include "uart.h"
#include "regs.h"

#define UART_ISR_PLACE  __attribute__((section(".itcm_text"), used))

static uint32_t  s_brr = 0;

/* 环形缓冲: 主循环排空。512B 足够 —— 主循环每轮只做几十条指令的事,
 * 而一字节要 86.8μs 才到。写满即丢弃并计数(不覆盖, 避免"看起来正常"的静默损坏)。 */
#define RX_RING_SZ   512u
#define RX_RING_MASK (RX_RING_SZ - 1u)
static volatile uint8_t  s_ring[RX_RING_SZ];
static volatile uint32_t s_head = 0, s_tail = 0;
static volatile uint32_t s_ore_cnt = 0;      /* 硬件溢出次数 */
static volatile uint32_t s_drop_cnt = 0;     /* 软件满丢弃次数 */

void uart1_init(uint32_t pclk2_hz, uint32_t baud)
{
    s_head = s_tail = 0;
    s_ore_cnt = s_drop_cnt = 0;

    RCC_AHB4ENR  |= (1u << 0);          /* GPIOAEN */
    RCC_APB2ENR  |= RCC_APB2ENR_USART1EN;

    /* ── PA9 = USART1_TX, PA10 = USART1_RX (均为 AF7) ──
     * ★ 一律"读-改-写", 绝不整寄存器赋值 —— PA8(拍输出) 就在同一个 MODER 里,
     *   整写会把它改掉 (这类事故在阶段 1 踩过)。 */
    uint32_t mod = GPIO_MODER(0);
    mod &= ~(3u << 18);  mod |= (2u << 18);   /* PA9  = AF */
    mod &= ~(3u << 20);  mod |= (2u << 20);   /* PA10 = AF */
    GPIO_MODER(0) = mod;

    uint32_t afr = GPIO_AFRH(0);              /* PA8..PA15 在 AFRH */
    afr &= ~(0xFu << 4);   afr |= (7u << 4);  /* PA9  → AF7 (AFSEL9_Pos  = 4) */
    afr &= ~(0xFu << 8);   afr |= (7u << 8);  /* PA10 → AF7 (AFSEL10_Pos = 8) */
    GPIO_AFRH(0) = afr;

    uint32_t pup = GPIO_PUPDR(0);
    pup &= ~(3u << 18);  pup |= (1u << 18);   /* PA9  上拉 (空闲高, 起始位才可靠) */
    pup &= ~(3u << 20);  pup |= (1u << 20);   /* PA10 上拉 (悬空不会读出假帧) */
    GPIO_PUPDR(0) = pup;

    GPIO_OSPEEDR(0) |= (3u << 18);            /* PA9 高速档 */

    /* ── 波特率 ── */
    USART_CR1(USART1_BASE) = 0;               /* 先关 UE 再改配置 */
    USART_PRESC(USART1_BASE) = 0;             /* 时钟预分频 ÷1 */
    s_brr = (pclk2_hz * 16u + baud / 2u) / baud;
    USART_BRR(USART1_BASE) = s_brr;

    USART_ICR(USART1_BASE) = USART_ICR_ORECF | USART_ICR_TCCF;   /* 清残留标志 */
    (void)USART_RDR(USART1_BASE);             /* 读一次清 RXNE */

    USART_CR1(USART1_BASE) = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
    USART_CR1(USART1_BASE) |= USART_CR1_RXNEIE;

    /* ── NVIC: 优先级必须**低于**拍 (数值更大 = 优先级更低) ── */
    NVIC_IPB(IRQ_USART1) = 0x80u;
    /* ★★ 必须用 nvic_enable_irq(IRQ), **不能**写 `NVIC_ISER = (1u<<IRQ_USART1)`。
     *   USART1 = IRQ 37 ≥ 32 ⇒ 裸移位是未定义行为, 中断永远不会被使能,
     *   而编译器只给一条警告、所有配置寄存器检查全绿 (详见 regs.h 的 A1 事故记录)。 */
    nvic_enable_irq(IRQ_USART1);
}

void uart1_write(const uint8_t *p, uint32_t n)
{
    for (uint32_t i = 0; i < n; i++) {
        while (!(USART_ISR(USART1_BASE) & USART_ISR_TXE)) { }
        USART_TDR(USART1_BASE) = p[i];
    }
    while (!(USART_ISR(USART1_BASE) & USART_ISR_TC)) { }   /* 等最后一字节出线 */
}

uint32_t uart1_brr(void) { return s_brr; }

/* 主循环调用: 取出一个已收到的字节, 返回 0 = 缓冲空 */
int uart1_rx_pop(uint8_t *out)
{
    if (s_tail == s_head) return 0;
    *out = s_ring[s_tail];
    s_tail = (s_tail + 1u) & RX_RING_MASK;
    return 1;
}

uint32_t uart1_ore_count(void)  { return s_ore_cnt; }
uint32_t uart1_drop_count(void) { return s_drop_cnt; }

/* ★ 直接读 NVIC 的 ISER 位, 不是本地缓存 —— 这样"写错寄存器"也能被抓到 (A1 事故) */
uint32_t uart1_irq_enabled(void) { return nvic_is_enabled(IRQ_USART1); }

UART_ISR_PLACE void USART1_IRQHandler(void)
{
    uint32_t isr = USART_ISR(USART1_BASE);

    if (isr & USART_ISR_ORE) {                     /* ③ 必须显式清, 否则接收被卡 */
        USART_ICR(USART1_BASE) = USART_ICR_ORECF;
        s_ore_cnt++;
    }
    if (isr & USART_ISR_RXNE) {
        uint8_t b = (uint8_t)(USART_RDR(USART1_BASE) & 0xFFu);
        uint32_t nh = (s_head + 1u) & RX_RING_MASK;
        if (nh != s_tail) { s_ring[s_head] = b; s_head = nh; }
        else              { s_drop_cnt++; }         /* 写满: 丢弃并计数, 不覆盖 */
    }
}
