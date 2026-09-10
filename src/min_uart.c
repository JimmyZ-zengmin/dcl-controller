/**
 * min_uart.c — 最小 UART 固件 (与 DCL 引擎完全无关)
 *
 * 目的: 在**排除全部自研代码**的前提下, 用最朴素的"轮询收发"验证 USART1 通路。
 *   排障时的价值: 当前 DCL 固件里有中断、环形缓冲、协议帧、deploy、引擎扫描…
 *   任何一个环节出问题都会表现为"串口不通"。这个文件把它们全去掉, 只留:
 *
 *   ① **不配置时钟树** —— 复位默认就是 HSI 64MHz (SYSCLK=HSI, HPRE=/1, D2PPRE2=/1),
 *      PCLK2 = 64MHz。少一个变量, 少一个出错的地方。
 *   ② **一个中断都不用** —— 主循环直接轮询 RXNE/TXE。没有 NVIC、没有 ISR、
 *      没有环形缓冲, 也就没有"错误标志没清把接收卡死"这类坑。
 *   ③ **不做协议帧** —— 收到什么回什么 (原样回声), 外加每秒一行心跳。
 *
 * 接线: PA9 = USART1_TX, PA10 = USART1_RX, 115200 8N1, 必须共地
 * 预期现象 (用任意串口工具连上, 115200):
 *   · 上电立刻看到两行横幅 (以 '=' 包裹)
 *   · 之后每秒一行 "HB <n> rx=<收到字节数>"
 *   · 你敲任何字符, 它原样回显 (回车会回显为 CR LF 两个字符)
 */

#include <stdint.h>

#define REG32(a)  (*(volatile uint32_t *)(uintptr_t)(a))

/* ---- RCC ---- */
#define RCC_AHB4ENR    REG32(0x580244E0UL)
#define RCC_APB2ENR    REG32(0x580244F0UL)

/* ---- GPIOA ---- */
#define GPIOA_MODER    REG32(0x58020000UL)
#define GPIOA_OSPEEDR  REG32(0x58020008UL)
#define GPIOA_PUPDR    REG32(0x5802000CUL)
#define GPIOA_AFRH     REG32(0x58020024UL)
#define GPIOA_BSRR     REG32(0x58020018UL)

/* ---- GPIOG (板上 LED: PG7) ---- */
#define GPIOG_MODER    REG32(0x58021800UL)
#define GPIOG_BSRR     REG32(0x58021818UL)

/* ---- USART1 ---- */
#define USART1_CR1     REG32(0x40011000UL)
#define USART1_CR2     REG32(0x40011004UL)
#define USART1_CR3     REG32(0x40011008UL)
#define USART1_BRR     REG32(0x4001100CUL)
#define USART1_ISR     REG32(0x4001101CUL)
#define USART1_ICR     REG32(0x40011020UL)
#define USART1_RDR     REG32(0x40011024UL)
#define USART1_TDR     REG32(0x40011028UL)
#define USART1_PRESC   REG32(0x4001102CUL)

#define ISR_PE    (1u << 0)
#define ISR_FE    (1u << 1)
#define ISR_NE    (1u << 2)
#define ISR_ORE   (1u << 3)
#define ISR_RXNE  (1u << 5)
#define ISR_TC    (1u << 6)
#define ISR_TXE   (1u << 7)

/* ---- DWT (只要一个 1 秒节拍, 不碰定时器外设) ---- */
#define DEMCR        REG32(0xE000EDFCUL)
#define DWT_CTRL     REG32(0xE0001000UL)
#define DWT_CYCCNT   REG32(0xE0001004UL)

/* 复位默认时钟: HSI 64MHz → SYSCLK 64MHz → PCLK2 64MHz */
#define PCLK2_HZ     64000000u
#define CPU_HZ       64000000u
#define BAUDRATE     115200u

#ifndef MIN_TX_STRESS
#define MIN_TX_STRESS 0      /* 1 = 不停发 (让 PA9 一直忙) —— 供 SWD 采样/外部仪器判"到底有没有在发" */
#endif

static volatile uint32_t g_rx_bytes = 0;
static volatile uint32_t g_heartbeat = 0;
static volatile uint32_t g_err = 0;

static void uart_putc(char c)
{
    while (!(USART1_ISR & ISR_TXE)) { }
    USART1_TDR = (uint32_t)(uint8_t)c;
}

static void uart_puts(const char *s)
{
    while (*s) uart_putc(*s++);
}

static void uart_put_u32(uint32_t v)
{
    char b[11]; int i = 0;
    if (!v) { uart_putc('0'); return; }
    while (v && i < 10) { b[i++] = (char)('0' + (v % 10u)); v /= 10u; }
    while (i--) uart_putc(b[i]);
}

static void led_toggle(void)
{
    /* 板上 LED 接 PG7 (高有效), 每收一个字节翻一次 —— 不用电脑也能看出"收到了" */
    static uint32_t on = 0;
    on = !on;
    GPIOG_BSRR = on ? (1u << 7) : (1u << (7 + 16));
}

int main(void)
{
    /* ── 1. 时钟使能 ── */
    RCC_AHB4ENR |= (1u << 0) | (1u << 6);        /* GPIOAEN | GPIOGEN */
    RCC_APB2ENR |= (1u << 4);                    /* USART1EN */

    /* ── 2. PA9 = USART1_TX, PA10 = USART1_RX (AF7) ── */
    uint32_t mod = GPIOA_MODER;
    mod &= ~(3u << 18);  mod |= (2u << 18);
    mod &= ~(3u << 20);  mod |= (2u << 20);
    GPIOA_MODER = mod;

    uint32_t afr = GPIOA_AFRH;
    afr &= ~(0xFu << 4);  afr |= (7u << 4);      /* AFSEL9  = AF7 */
    afr &= ~(0xFu << 8);  afr |= (7u << 8);      /* AFSEL10 = AF7 */
    GPIOA_AFRH = afr;

    uint32_t pup = GPIOA_PUPDR;
    pup &= ~(3u << 18);  pup |= (1u << 18);      /* PA9  上拉 */
    pup &= ~(3u << 20);  pup |= (1u << 20);      /* PA10 上拉 */
    GPIOA_PUPDR = pup;
    GPIOA_OSPEEDR |= (3u << 18);

    /* PG7 = 推挽输出 (LED) */
    uint32_t gmod = GPIOG_MODER;
    gmod &= ~(3u << 14);  gmod |= (1u << 14);
    GPIOG_MODER = gmod;

    /* ── 3. USART1: 115200 8N1 ── */
    USART1_CR1 = 0;                              /* 先关 UE 再改配置 */
    USART1_PRESC = 0;                            /* ÷1 */
    /* ★★ BRR 公式 (踩过的坑, 别再改回来):
     *   本外设**默认 OVER8=0 (16 倍过采样)**。此模式下 BRR 直接就是分频值:
     *        BRR = fCK / baud        →  64e6 / 115200 = 555 (0x22B)
     *   曾经写成 `(PCLK2*16 + baud/2)/baud` —— 那是把 "OVER8=1 时 BRR 高 15 位
     *   放分频、低 1 位放小数" 的算法套错了地方, 结果 BRR = 8889 = 555×16,
     *   **实际波特率变成 7200 而不是 115200**。现象极隐蔽: TE 开着、TXE=1、
     *   TC=1、CR1/CR3/PRESC/GPIO 全部正确, 只有 PC 侧一个字节都解不出来。
     *   实测证据: SWD 读回 USART1_BRR = 0x22B9 (8889), 而正确值应为 0x22B (555)。 */
    USART1_BRR = (PCLK2_HZ + BAUDRATE / 2u) / BAUDRATE;
    USART1_CR2 = 0;                              /* 1 停止位, 无校验 */
    USART1_CR3 = 0;                              /* 无流控 */
    USART1_ICR  = 0x1FFu;                        /* 清全部残留标志 */
    (void)USART1_RDR;
    USART1_CR1 = (1u << 0) | (1u << 2) | (1u << 3);   /* UE | RE | TE */

    /* ── 4. DWT 1 秒节拍 ── */
    DEMCR |= (1u << 24);
    DWT_CYCCNT = 0;
    DWT_CTRL |= 1u;
#if !MIN_TX_STRESS
    uint32_t t_next = CPU_HZ;                    /* 1 秒 */
#endif

    /* ── 5. 横幅 ── */
    uart_puts("\r\n==== MIN UART (no engine, no IRQ, HSI 64MHz) ====\r\n");
    uart_puts("PA9=TX PA10=RX  115200 8N1  BRR=0x");
    {
        uint32_t b = USART1_BRR;
        for (int sh = 12; sh >= 0; sh -= 4) {
            uint32_t d = (b >> sh) & 0xFu;
            uart_putc((char)(d < 10u ? '0' + d : 'A' + d - 10u));
        }
    }
    uart_puts("\r\n每收到 1 字节就原样回显, 并翻一次板上 LED(PG7)\r\n");

    for (;;) {
        /* 有心跳到点就打印一行 */
#if MIN_TX_STRESS
        /* 压力模式: 一直发, 让 TX 线几乎无空隙 —— 便于用"电平采样"或"对端是否收到"
         * 这两种外部手段判定"MCU 到底有没有在 PA9 上发"。 */
        g_heartbeat++;
        uart_puts("STRESS ");
        uart_put_u32(g_heartbeat);
        uart_puts("\r\n");
#else
        if ((int32_t)(DWT_CYCCNT - t_next) >= 0) {
            t_next += CPU_HZ;
            g_heartbeat++;
            uart_puts("HB ");
            uart_put_u32(g_heartbeat);
            uart_puts("  rx=");
            uart_put_u32(g_rx_bytes);
            if (g_err) { uart_puts("  err="); uart_put_u32(g_err); }
            uart_puts("\r\n");
        }
#endif

        /* 收: 有错先清错 (不清会卡住接收), 有字节就回声 */
        uint32_t isr = USART1_ISR;
        if (isr & (ISR_PE | ISR_FE | ISR_NE | ISR_ORE)) {
            USART1_ICR = 0x1FFu;
            g_err++;
            isr = USART1_ISR;
        }
        if (isr & ISR_RXNE) {
            char c = (char)(USART1_RDR & 0xFFu);
            g_rx_bytes++;
            led_toggle();
            uart_putc(c);                        /* 原样回显 */
        }
    }
}
