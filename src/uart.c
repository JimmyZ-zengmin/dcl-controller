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
 * 波特率: BRR = round(PCLK2 / baud)   (OVER8=0 即 16 倍过采样, PRESC=0)
 *   H723 本项目: PCLK2 = 100 MHz, 115200 → 868 = 0x364
 *   实际波特率 100e6/868 = 115207 → 误差 0.006% (远优于 2% 容限)
 *
 * ★★ 公式勘误 (真事故, 别再改回去): 这里曾写成 `PCLK2*16/baud`,
 *   注释还"自证"说 100e6*16/115200 = 0x3641 是对的 —— 其实那是 16 倍分频,
 *   实际波特率只有 7200。OVER8=0 时 BRR 就是**分频值本身**, 不乘 16。
 *   症状: CR1/BRR/GPIO 读写全"正常", TE 开着、TXE/TC 都为 1,
 *        唯独对端一个字节都解不出来 —— 因为波特率差了 16 倍。
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

/* ★ 诊断用计数 (供外部 SWD 读走): "只收到 1 个字节" 这类故障无法靠猜,
 *   必须把 ISR 的**进入次数 / 每次看到的标志 / 收到的字节**都暴露出来。 */
static volatile uint32_t s_isr_n    = 0;     /* ISR 进入次数 */
static volatile uint32_t s_ore_n    = 0;     /* ISR 里看到 ORE 的次数 */
static volatile uint32_t s_fe_n     = 0;     /* 帧错误 (FE) 次数 —— 非 0 说明线上波形不对 */
static volatile uint32_t s_ne_n     = 0;     /* 噪声错误 (NE) 次数 */
static volatile uint32_t s_push_n   = 0;     /* 真正推进环形的字节数 */
static volatile uint32_t s_last_isr = 0;     /* 最近一次读到的 ISR 寄存器原值 */
static volatile uint32_t s_last_byte= 0;     /* 最近一次收到的字节 */

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
    s_brr = (pclk2_hz + baud / 2u) / baud;    /* OVER8=0: BRR = fCK/baud */
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
uint32_t uart1_isr_count(void)  { return s_isr_n; }
uint32_t uart1_isr_ore(void)    { return s_ore_n; }
uint32_t uart1_fe_count(void)   { return s_fe_n; }
uint32_t uart1_ne_count(void)   { return s_ne_n; }
uint32_t uart1_push_count(void) { return s_push_n; }
uint32_t uart1_last_isr(void)   { return s_last_isr; }
uint32_t uart1_last_byte(void)  { return s_last_byte; }

/* ★ 直接读 NVIC 的 ISER 位, 不是本地缓存 —— 这样"写错寄存器"也能被抓到 (A1 事故) */
uint32_t uart1_irq_enabled(void) { return nvic_is_enabled(IRQ_USART1); }

UART_ISR_PLACE void USART1_IRQHandler(void)
{
    uint32_t isr = USART_ISR(USART1_BASE);
    s_isr_n++;
    s_last_isr = isr;
    /* ★★ 错误标志必须**逐个显式清除** —— 这是本轮实测撞出来的真缺陷:
     *   原实现只清 ORE, 不管 FE/NE。结果实测 (诊断固件) 每帧 6 字节**只收到 1 个**,
     *   且那一个字节的 ISR 原值 0x006010F4 里 NE=1 —— 也就是"第一个字节带着噪声错误
     *   进来, 之后整帧就再也收不到了"。错误标志不清会**卡住接收通路**,
     *   表现完全不像"线断了"(ISR 次数=帧数, ORE=0, drop=0, 一切"看起来正常")。 */
    uint32_t icr = 0;
    if (isr & USART_ISR_PE) { icr |= USART_ICR_PECF; }
    if (isr & USART_ISR_FE) { s_fe_n++; icr |= USART_ICR_FECF; }   /* 帧错误: 线上波形不对 */
    if (isr & USART_ISR_NE) { s_ne_n++; icr |= USART_ICR_NECF; }   /* 噪声: 起始位附近有毛刺 */
    if (icr) USART_ICR(USART1_BASE) = icr;

    /* ★★ ORE 必须**先于** RXNE 处理, 且清完之后**再读一次 ISR**:
     *   ORE 置位时 RDR 里是**旧数据**; 只有清掉 ORE 才知道 RXNE 是不是新的。 */
    if (isr & USART_ISR_ORE) {
        USART_ICR(USART1_BASE) = USART_ICR_ORECF;
        s_ore_cnt++;
        isr = USART_ISR(USART1_BASE);
        s_last_isr = isr;
    }
    if (isr & USART_ISR_RXNE) {
        uint8_t b = (uint8_t)(USART_RDR(USART1_BASE) & 0xFFu);
        s_last_byte = b;
        uint32_t nh = (s_head + 1u) & RX_RING_MASK;
        if (nh != s_tail) { s_ring[s_head] = b; s_head = nh; s_push_n++; }
        else              { s_drop_cnt++; }         /* 写满: 丢弃并计数, 不覆盖 */
    }
}
