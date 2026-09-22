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

/* ══════════ TX 队列（2026-09-22）══════════
 * ★ **线性缓冲，不是环形**：因为 `uart1_write` 保证"下一条写入前先把残余推完"
 *   ⇒ 任意时刻待发数据一定是**连续的一段**，不需要绕回。
 * ★ 大小 `UART_TXQ_SZ` 定义在 `uart.h`（**唯一源**）—— 因为 `main.c` 要用它做
 *   `_Static_assert(FRAME_TOTAL_MAX_V2 <= UART_TXQ_SZ)`；把它埋在 .c 里就断言不了。
 * ★ 8 KB 静态 RAM：DTCM 有 ~62 KB 余量（`MEM_STAT.headroom` 75 KB − 栈 0.9 KB − 余量下限 8 KB）。*/
static uint8_t  s_txq[UART_TXQ_SZ];
static uint32_t s_txq_pos = 0u;        /* 下一个要发的下标 */
static uint32_t s_txq_len = 0u;        /* 还剩几个待发 */
static uint32_t s_txq_trunc_n = 0u;    /* 被截断次数（应为 0；非 0 ⇒ 断言失效，可读回）*/

/* 环形缓冲: 主循环排空。512B 足够 —— 主循环每轮只做几十条指令的事,
 * 而一字节要 86.8μs 才到。写满即丢弃并计数(不覆盖, 避免"看起来正常"的静默损坏)。 */
/* ★★★ 2026-09-16: 512 → **4096**。
 * 为什么（有实测依据）: 512 B 只装得下 ≈44 ms 的数据（115200 bps），
 *   而主循环里的 `sd_log_poll()` 等**会停顿数十~数百 ms** ⇒ 环溢出、丢字节
 *   （`uart_drop` 实测累积到 767）⇒ 帧被截断 ⇒ 设备"失聪"。
 *   4096 B ≈ 355 ms 的容忍度，足以盖住一次 SD 批写。
 * ★ 配套：`transport.h` 的**帧组装超时**（万一还是溢出，也会自愈而不是永久失聪）。
 * ★ 空间: DTCM 128 KB 仅用 41%（53.7 KB）⇒ +3.5 KB 无压力。
 *   （★ 这**不是**根治：真正的根治是"别让主循环停那么久"，那是另一刀。）*/
#define RX_RING_SZ   4096u
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
    /* ★★★ 2026-09-22: **由"逐字节死等"改为"入队 + 尽量推"**。
     *
     * ## 为什么改（实测证据）
     *   原实现逐字节 `while(!TXE)` 死等 + 结尾等 TC ⇒ CPU **100% 自旋**：
     *   115200 下每字节 86.8 µs ⇒ 一次 1040 B（`sub=26` 的应答）**纯自旋 90 ms**。
     *   而主循环正是编码器采样的调度者（`s_as_next = g_tick_count + as5600_period_now()`）
     *   ⇒ **观测改变了被测对象**：对照实测（同台架、同 1500 Hz 运动）
     *       不读 sub=26：编码器 **91.1 Hz**
     *       读  sub=26：编码器 **60.4 Hz**   ← 掉 34%
     *   ★ 违反项目铁律「观测不得改变被测对象」。
     *
     * ## 语义为什么不变
     *   ① **先推完上一条的残余才允许覆盖缓冲** ⇒ 与原来"发完才返回"**在下一条到来时等价**，
     *      只是"等待"被挪到了下一条，而**正常情况（主循环 `uart1_tx_pump()` 在两帧之间跑了）
     *      残余早已推完 ⇒ 零等待**。
     *   ② 全双工链路 ⇒ **不需要等 TC**：TXE=1 时上一字节已进移位寄存器、正在上线；
     *      只有"发完就断电"才会缺尾巴，而协议是请求-应答，下一个字节总会接上。
     *      （注释④那条"等 TC"对**半双工翻方向**和**一次性发完就停**才必要。）
     *
     * ## 背压
     *   若上位机不等应答狂发 ⇒ ① 会**等**（真积压才发生）。不丢字节 —— 与
     *   RX 侧"写满即丢弃并计数"的取舍**不同**，因为发送丢字节会**破坏已承诺的应答**。
     */
    while (s_txq_len > 0u) {                       /* ① 推完残余（正常情况这里立刻退出）*/
        if ((USART_ISR(USART1_BASE) & USART_ISR_TXE) == 0u) { continue; }   /* 真积压: 等 */
        USART_TDR(USART1_BASE) = s_txq[s_txq_pos++];
        s_txq_len--;
    }
    /* ② 入队（拷贝 ⇒ 调用者的缓冲不必常驻，`s_txbuf` 可被下一条复用）*/
    if (n > UART_TXQ_SZ) { n = UART_TXQ_SZ; s_txq_trunc_n++; }   /* 断言已保证不会发生 */
    for (uint32_t i = 0; i < n; i++) { s_txq[i] = p[i]; }
    s_txq_pos = 0u;
    s_txq_len = n;
    uart1_tx_pump();                               /* ③ 尽量推，推不完就返回（不阻塞）*/
}

/* 主循环每圈调用：把待发字节尽量推出去。
 * ★ 为什么放在主循环而不是 ISR：ISR 优先级低于 100 µs 拍（本文件①条），
 *   在 ISR 里发字节会拉长 ISR；而主循环每圈有充足余量（一字节要 86.8 µs 才到）。
 * ★ 为什么不做 TXE 中断：那要给 USART1 开 TXE 使能，而本驱动的 ISR 现在只处理 RX
 *   （见 USART1_IRQHandler）；主循环轮询已经够 —— 而且**不改 ISR 就不动
 *   "拍 ISR 优先级"那条不变量**。 */
void uart1_tx_pump(void)
{
    while (s_txq_len > 0u) {
        if ((USART_ISR(USART1_BASE) & USART_ISR_TXE) == 0u) { break; }
        USART_TDR(USART1_BASE) = s_txq[s_txq_pos++];
        s_txq_len--;
    }
}

uint32_t uart1_tx_pending(void) { return s_txq_len; }

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
