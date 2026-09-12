/**
 * min_uart485.c — 最小 "485/PD6 接收" 固件 (与 DCL 引擎**零关系**)
 *
 * ═══════════════════════════════════════════════════════════════════════════
 * 目的: 把"自研代码"整体排除掉, 用最朴素的方式回答两个问题 ——
 *
 *   ① PC → 板子 (PD6 / USART2) 到底有没有字节到达 MCU? 到达后有没有落进 DTCM?
 *   ② **模块的 TXD 那根线, 究竟插在 GPIOD 的哪个脚上?**
 *
 * ★ 为什么需要它 (前史, 别再走回头路):
 *   485 联机排障里, 板子→PC 早已 35/35 合法应答, 但 PC→板子始终 0 字节。
 *   而"0 字节"这个读数曾经**不可信**: 诊断区在 DTCM, 而本机 CMSIS-DAP 每次
 *   pyocd 会话都会复位目标 ⇒ 读到的只是"我刚清零后的值"。两个独立的电气量
 *   (整口下拉位图 / PD6 波形采样) 后来都指向"PD6 悬空", 但"悬空"仍分不清
 *   "线没插上"与"线插到别的脚了"。本固件把这件事变成一次读数。
 *
 * ★ 干净到什么程度 (刻意的):
 *   · **不配置时钟树** —— 复位默认 HSI 64MHz, SYSCLK=HCLK=PCLK1=PCLK2=64MHz。
 *     少一个变量就少一处能出错的地方 (本项目已知: 时钟树是最容易"看起来配好了"的一块)。
 *   · **一个中断都不用** —— 没有 NVIC、没有 ISR、没有环形缓冲。主循环就是
 *     "读 IDR / 查 RXNE / 收字节" 三件事。于是"接收慢"这个借口也不存在:
 *     64MHz 下这个循环每秒能查几百万次, 115200 每字节 87µs, 差着两个数量级。
 *   · **不做协议帧** —— 收到什么就打什么, 不解析 Modbus。CRC/状态机/分帧如果
 *     也要参与, 那它又会变成一个"可能背锅的人"。
 *
 * ★ 观察窗: **USART1 = PA9(TX)/PA10(RX)** —— 就是你接在 COM14 上的那对脚。
 *   本固件往 USART1 打**纯文本**, 所以观察**全程不需要 pyocd / 不需要调试器**。
 *   (铁律 0: 观测不得改变被测对象。)
 *
 * 接线 (必须共地):
 *   COM14  : PC-TXD → PA10,  PC-RXD ← PA9            ← 观察窗 (纯文本)
 *   被测口 : 模块 TXD → **PD6**, 模块 RXD ← **PD5**   ← 被测对象
 *
 * 预期现象 (串口工具连 COM14, 115200 8N1):
 *   · 上电立刻出横幅 (BRR / GPIOD 配置读回 / 接收缓冲在 DTCM 的地址)
 *   · 之后每轮: A 段 "RX" 打印 PD6 占空比 + 收到的字节 (hex); B 段打印整口扫描表
 *   · 收到任何一个字节 → 板上 LED(PG7) 翻一次, 且该字节以 hex 立刻出现在 COM14
 *
 * 判读表 (B 段扫描输出):
 *   high≈100%  trans≈0    → 静态高 (被外部推挽驱动 / 模块的输入脚带上拉)
 *   high≈0%    trans≈0    → 悬空 (下拉把它拉到底, 没东西驱动)
 *   high≈50%   trans 很大  → ★ 这根脚上有**在跑的数据** ← 就是模块 TXD 该在的地方
 *   high 中等  trans 小    → 拾取到的串扰/噪声 (不是真信号: 115200 的翻转率是它的十几倍)
 */

#include <stdint.h>
#include "regs.h"

/* ---- 复位默认时钟: HSI 64MHz → SYSCLK/HCLK/PCLK1/PCLK2 全 64MHz ---- */
#define PCLK_HZ   64000000u
#define CPU_HZ    64000000u
#define BAUDRATE  115200u
#define BRR_VALUE ((PCLK_HZ + BAUDRATE / 2u) / BAUDRATE)

/* ★★ BRR 公式 (同 min_uart.c 踩过的坑, 原样保留告诫):
 *   本外设默认 OVER8=0 (16 倍过采样)。此模式下 BRR **就是**分频值: BRR = fCK/baud
 *   ⇒ 64e6/115200 = 555 (0x22B)。
 *   曾错写成 `(PCLK*16 + baud/2)/baud` —— 那是 OVER8=1 的算法 (高 15 位分频、
 *   低 1 位小数), 结果 BRR = 555×16 = 8889 ⇒ 实际波特率 7200 而非 115200。
 *   症状极隐蔽: TE/RE/TXE/TC/CR1/CR3/PRESC/GPIO **全部正确**, 只有 PC 侧解不出字节。 */

/* ---- 板上 LED: PG7 (高有效) ---- */
#define LED_PORT    6u
#define LED_BIT     7u

/* ---- 被测口: USART2 = PD5(TX) / PD6(RX), AF7 ---- */
#define DUT_PORT    3u          /* GPIOD */
#define DUT_TX_BIT  5u
#define DUT_RX_BIT  6u

#define AF7         7u

/* regs.h 只定义了 RDR (读), TDR (写) 在这里补一份 —— 最小固件刻意自包含 */
#define USART_TDR(u) REG32((u) + 0x28)

/* ==================== 观测窗: USART1 纯文本输出 ==================== */

static uint32_t g_tx_timeouts = 0;

static void putc1(char c)
{
    uint32_t guard = 200000u;                       /* ★ 不无限阻塞 (坏线也不卡死固件) */
    while (!(USART_ISR(USART1_BASE) & USART_ISR_TXE)) {
        if (--guard == 0u) { g_tx_timeouts++; return; }
    }
    REG32(USART1_BASE + 0x28) = (uint32_t)(uint8_t)c;   /* TDR */}

static void puts1(const char *s) { while (*s) putc1(*s++); }

static void put_u32(uint32_t v)
{
    char b[11];
    int i = 0;
    if (!v) { putc1('0'); return; }
    while (v && i < 10) { b[i++] = (char)('0' + (int)(v % 10u)); v /= 10u; }
    while (i--) putc1(b[i]);
}

/** 固定宽度 hex (不带头) —— 便于直接肉眼对齐字段 */
static void put_hex(uint32_t v, int nibbles)
{
    for (int sh = (nibbles - 1) * 4; sh >= 0; sh -= 4) {
        uint32_t d = (v >> sh) & 0xFu;
        putc1((char)(d < 10u ? ('0' + (int)d) : ('A' + (int)d - 10)));
    }
}

/* ==================== 接收缓冲 (DTCM) ==================== */

/* ★ 显式放在这里是有意的: 静态存储 → .bss → **DTCM** (见 ld 脚本与 .map)。
 *   横幅会把它的地址打出来 —— "数据到底有没有到达 DTCM" 因此是一个
 *   可被外部读走的事实, 而不是推断。 */
#define RXBUF_SZ 64u
static volatile uint8_t  g_rxbuf[RXBUF_SZ];
static volatile uint32_t g_rx_n    = 0;   /* 收到的总字节数 (跨轮) */
static volatile uint32_t g_rx_wrap = 0;   /* 缓冲回绕次数 */
static volatile uint32_t g_err_pe = 0, g_err_fe = 0, g_err_ne = 0, g_err_ore = 0;
static volatile uint32_t g_led = 0;

static void led_toggle(void)
{
    g_led = !g_led;
    REG32(GPIO_BASE(LED_PORT) + 0x18) = (g_led ? (1u << LED_BIT)
                                               : (1u << (LED_BIT + 16u)));   /* BSRR */
}

/* ==================== 初始化 ==================== */

static void uart1_obs_init(void)      /* 观察窗: PA9/PA10 */
{
    RCC_AHB4ENR |= (1u << 0);            /* GPIOAEN */
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;

    uint32_t mod = GPIO_MODER(0);
    mod &= ~(3u << (9 * 2));  mod |= (2u << (9 * 2));    /* PA9  = AF */
    mod &= ~(3u << (10 * 2)); mod |= (2u << (10 * 2));   /* PA10 = AF */
    GPIO_MODER(0) = mod;

    uint32_t afr = GPIO_AFRH(0);
    afr &= ~(0xFu << ((9 - 8) * 4));  afr |= (AF7 << ((9 - 8) * 4));
    afr &= ~(0xFu << ((10 - 8) * 4)); afr |= (AF7 << ((10 - 8) * 4));
    GPIO_AFRH(0) = afr;

    uint32_t pup = GPIO_PUPDR(0);
    pup &= ~(3u << (9 * 2));  pup |= (1u << (9 * 2));    /* 上拉 */
    pup &= ~(3u << (10 * 2)); pup |= (1u << (10 * 2));
    GPIO_PUPDR(0) = pup;

    GPIO_OSPEEDR(0) |= (3u << (9 * 2));

    USART_CR1(USART1_BASE)   = 0u;                 /* 先关 UE 再改配置 */
    USART_PRESC(USART1_BASE) = 0u;                 /* ÷1 */
    USART_BRR(USART1_BASE)   = BRR_VALUE;
    USART_CR2(USART1_BASE)   = 0u;
    USART_CR3(USART1_BASE)   = 0u;
    USART_ICR(USART1_BASE)   = 0x1FFu;
    (void)USART_RDR(USART1_BASE);
    USART_CR1(USART1_BASE) = USART_CR1_UE | USART_CR1_RE | USART_CR1_TE;
}

static void uart2_dut_init(void)      /* 被测口: PD5/PD6 */
{
    RCC_AHB4ENR |= (1u << DUT_PORT);                 /* ★ GPIOD 自己的时钟 —— 不使能则写入被丢弃 */
    RCC_APB1LENR |= RCC_APB1LENR_USART2EN;

    uint32_t mod = GPIO_MODER(DUT_PORT);
    mod &= ~(3u << (DUT_TX_BIT * 2)); mod |= (2u << (DUT_TX_BIT * 2));
    mod &= ~(3u << (DUT_RX_BIT * 2)); mod |= (2u << (DUT_RX_BIT * 2));
    GPIO_MODER(DUT_PORT) = mod;

    uint32_t afr = GPIO_AFRL(DUT_PORT);
    afr &= ~(0xFu << (DUT_TX_BIT * 4)); afr |= (AF7 << (DUT_TX_BIT * 4));
    afr &= ~(0xFu << (DUT_RX_BIT * 4)); afr |= (AF7 << (DUT_RX_BIT * 4));
    GPIO_AFRL(DUT_PORT) = afr;

    uint32_t pup = GPIO_PUPDR(DUT_PORT);
    pup &= ~(3u << (DUT_RX_BIT * 2)); pup |= (1u << (DUT_RX_BIT * 2));   /* RX 上拉 */
    GPIO_PUPDR(DUT_PORT) = pup;

    USART_CR1(USART2_BASE)   = 0u;
    USART_PRESC(USART2_BASE) = 0u;
    USART_BRR(USART2_BASE)   = BRR_VALUE;
    USART_CR2(USART2_BASE)   = 0u;
    USART_CR3(USART2_BASE)   = 0u;
    USART_ICR(USART2_BASE)   = 0x1FFu;
    (void)USART_RDR(USART2_BASE);
    USART_CR1(USART2_BASE) = USART_CR1_UE | USART_CR1_RE | USART_CR1_TE;
}

/* ==================== A 段: 接收 + PD6 占空比 ==================== */

#define PHASE_A_SAMPLES 4000000u      /* ~0.4~1s 量级 (64MHz) */

static void phase_rx(void)
{
    uint32_t low6 = 0, tot = 0;
    uint32_t got = 0;
    uint32_t n0 = g_rx_n;

    puts1("A|RX start (PD6 占空比 + USART2 轮询, 无中断)\r\n");

    while (tot < PHASE_A_SAMPLES) {
        uint32_t idr = GPIO_IDR(DUT_PORT);
        if (!(idr & (1u << DUT_RX_BIT))) low6++;
        tot++;

        uint32_t isr = USART_ISR(USART2_BASE);
        if (isr & (USART_ISR_PE | USART_ISR_FE | USART_ISR_NE | USART_ISR_ORE)) {
            USART_ICR(USART2_BASE) = 0x1FFu;       /* ★ 不清错会把接收卡住 */
            if (isr & USART_ISR_PE) g_err_pe++;
            if (isr & USART_ISR_FE) g_err_fe++;
            if (isr & USART_ISR_NE) g_err_ne++;
            if (isr & USART_ISR_ORE) g_err_ore++;
            continue;
        }
        if (isr & USART_ISR_RXNE) {
            uint8_t b = (uint8_t)(USART_RDR(USART2_BASE) & 0xFFu);
            if (got < 24u) {                       /* 前 24 字节逐个 hex 打出来 */
                puts1("A|RX byte[");
                put_u32(got);
                puts1("]=0x");
                put_hex(b, 2);
                puts1("\r\n");
            }
            g_rxbuf[g_rx_n % RXBUF_SZ] = b;        /* ★ 落进 DTCM */
            g_rx_n++;
            if ((g_rx_n % RXBUF_SZ) == 0u) g_rx_wrap++;
            got++;
            led_toggle();
        }
    }

    puts1("A|PD6 low=");
    put_u32(low6);
    puts1(" / ");
    put_u32(tot);
    puts1("  duty_low=");
    put_u32((low6 * 1000u) / tot / 10u);
    putc1('.');
    put_u32(((low6 * 1000u) / tot) % 10u);
    puts1("%\r\n");

    puts1("A|USART2 isr=0x");
    put_hex(USART_ISR(USART2_BASE), 8);
    puts1("  CR1=0x");
    put_hex(USART_CR1(USART2_BASE), 8);
    puts1("  BRR=0x");
    put_hex(USART_BRR(USART2_BASE), 8);
    puts1("\r\n");

    puts1("A|rx_n=");
    put_u32(g_rx_n);
    puts1(" (this phase +");
    put_u32(got);
    puts1(")  wrap=");
    put_u32(g_rx_wrap);
    puts1("  err pe/fe/ne/ore=");
    put_u32(g_err_pe); putc1('/');
    put_u32(g_err_fe); putc1('/');
    put_u32(g_err_ne); putc1('/');
    put_u32(g_err_ore);
    puts1("\r\n");

    puts1("A|DTCM buf @0x");
    put_hex((uint32_t)(uintptr_t)&g_rxbuf[0], 8);
    puts1(" =");
    for (uint32_t i = 0; i < 16u; i++) {
        putc1(' ');
        put_hex(g_rxbuf[i], 2);
    }
    puts1("\r\n");
    if (g_rx_n != n0) puts1("A|==> ★ 有字节到达 MCU 并写入 DTCM\r\n");
    else              puts1("A|==> 本段 0 字节\r\n");
}

/* ==================== B 段: 全端口逐脚活动扫描 ==================== */

/* ★ SCAN_N 的取值理由: 115200 下最坏情况 (对端一直发 0x55) 翻转率 = baud/2 ≈ 57600/s。
 *   每脚 400k 次采样在 64MHz 下约 37ms ⇒ 期望翻转 ≈ 2100 次, 远高于"扰/噪声"的量级
 *   (悬空脚拾取串扰通常几十~几百)。所以 300 这个阈值能**分开**"真数据线"与"噪声"。 */
#define SCAN_N     400000u
#define SCAN_PORTS 7u          /* GPIOA..GPIOG (H723ZG LQFP144 只引出到 G) */

static void scan_one_port(uint32_t port)
{
    uint32_t m0 = GPIO_MODER(port),  p0 = GPIO_PUPDR(port);
    uint32_t a0 = GPIO_AFRL(port),   h0 = GPIO_AFRH(port);
    uint32_t hi[16], tr[16];

    /* ★★ 顺序很重要 (本固件自己踩到过两次):
     *   ① **置低易、复原难**: 设成"输入+下拉"后恢复时, 若先写 MODER (→AF7) 而 PUPDR
     *      仍停在下拉, 那一瞬 RX 脚被下拉成低 = 一个 **break 条件** ⇒ USART 必然收到
     *      一个 0x00 + FE。实测每轮扫描正好多 1 字节 0x00 + 1 次 FE —— 看起来像
     *      "真有数据来了", 其实是**观测动作自己造的字节**。
     *      DCL 固件的 mb_line_test / mb_line_probe 是同一个错序, 已一并修。
     *   ② **扫 GPIOA 时不能边扫边打印**: 观察窗 USART1_TX 就是 PA9 —— 把 GPIOA 全设成
     *      输入的那一刻起, TX 脚就悬空了, 此时打出去的字**全部丢在空气里**。
     *      (实测: 第一版正是这样, 16 行 PAxx 一行都没回来, 而 PB00 那行前面还多出
     *       半截被截断的判读文字。) ⇒ 采样结果先存本地, **恢复之后**再统一打印。 */
    GPIO_MODER(port) = 0u;                 /* 全部输入 */
    GPIO_PUPDR(port) = 0xAAAAAAAAu;        /* 每脚 10b = 下拉 (有驱动的脚仍读 1) */
    __asm__ volatile("dsb" ::: "memory");

    for (uint32_t b = 0; b < 16u; b++) {
        uint32_t bit = 1u << b;
        uint32_t high = 0, trans = 0, prev, i;
        prev = GPIO_IDR(port) & bit;
        for (i = 0; i < SCAN_N; i++) {
            uint32_t cur = GPIO_IDR(port) & bit;
            if (cur) high++;
            if (cur != prev) trans++;
            prev = cur;
        }
        hi[b] = high / (SCAN_N / 100u);    /* 百分比 */
        tr[b] = trans;
    }

    GPIO_AFRL(port)  = a0;                 /* 先 AF 选择 */
    GPIO_AFRH(port)  = h0;
    GPIO_PUPDR(port) = p0;                 /* ★ 再上拉 (见上面的顺序说明) */
    GPIO_MODER(port) = m0;                 /* ★ 最后才接上 AF */
    __asm__ volatile("dsb" ::: "memory");

    for (uint32_t b = 0; b < 16u; b++) {   /* ★ 恢复之后才打印 */
        puts1("B|P");
        putc1((char)('A' + (int)port));
        putc1((char)('0' + (int)(b / 10u)));
        putc1((char)('0' + (int)(b % 10u)));
        puts1(" high=");
        put_u32(hi[b]);
        puts1("%  trans=");
        put_u32(tr[b]);
        if (tr[b] >= 300u) puts1("   <== ★★ 这根脚上有数据在跑");
        else if (hi[b] >= 99u) puts1("   (静态高)");
        else if (hi[b] == 0u) puts1("   (悬空/接地)");
        else puts1("   (中间态/低占空)");
        puts1("\r\n");
    }
}

static void phase_scan(void)
{
    puts1("B|scan ALL PORTS (全输入+全下拉, 逐脚 40 万次采样)\r\n");
    puts1("B|判读: trans>=300 ⇒ 有数据在跑; high=100% & trans=0 ⇒ 静态高(上拉/被驱动);"
          " high=0% & trans=0 ⇒ 悬空\r\n");
    for (uint32_t p = 0; p < SCAN_PORTS; p++) {
        RCC_AHB4ENR |= (1u << p);          /* 该口时钟 (不使能则 MODER 写不进去) */
    }
    for (uint32_t p = 0; p < SCAN_PORTS; p++) scan_one_port(p);
    puts1("B|done (所有口已原样恢复)\r\n");
}

/* ==================== C 段: 反向发 (验"PC 侧那个口是不是链路末端") ==================== */

/* ★ 为什么必须有这一段 (它验的是一个**隐含前提**, 而不是一个结论):
 *   前面 A/B 两段的全部意义都建立在"COM15 就是那个 USB→485 dongle"之上 ——
 *   如果 COM15 其实是个没接线的 USB-TTL, 那"灌了 2 万帧却没有一根脚在翻转"
 *   就是**废话**(信号压根没进 485 总线)。
 *   本段让板子**从 PD5 反向发一段可识别文本**, 走"PD5 → 模块 RXD → 485 → dongle → PC"。
 *   在 COM15 上收到 ⇒ 该口确实是链路末端, 且"板子→PC"方向通 ⇒
 *   于是 A/B 段的结论成立: **PC→板子 这一段是断的**。
 *   收不到 ⇒ 这个前提本身有问题, 前面所有推论都要作废 (先回头查端口/接线)。 */
static void phase_tx(void)
{
    static const char PAT[] = "DCL485TX-0123456789\r\n";
    puts1("C|USART2 TX burst on PD5 (200×19B) —— 请在 485 那个口上找 'DCL485TX'\r\n");
    for (uint32_t r = 0; r < 200u; r++) {
        for (const char *p = PAT; *p; p++) {
            uint32_t guard = 200000u;
            while (!(USART_ISR(USART2_BASE) & USART_ISR_TXE)) {
                if (--guard == 0u) { g_tx_timeouts++; goto done; }
            }
            USART_TDR(USART2_BASE) = (uint32_t)(uint8_t)(*p);
        }
    }
done:
    {   uint32_t guard = 200000u;
        while (!(USART_ISR(USART2_BASE) & USART_ISR_TC)) { if (--guard == 0u) break; }
    }
    puts1("C|done (TX timeouts=");
    put_u32(g_tx_timeouts);
    puts1(")\r\n");
}

/* ==================== 主 ==================== */

int main(void)
{
    RCC_AHB4ENR |= (1u << LED_PORT);            /* GPIOGEN */

    uart1_obs_init();
    uart2_dut_init();

    /* LED: 推挽输出 */
    uint32_t gm = GPIO_MODER(LED_PORT);
    gm &= ~(3u << (LED_BIT * 2)); gm |= (1u << (LED_BIT * 2));
    GPIO_MODER(LED_PORT) = gm;

    puts1("\r\n======== MIN 485 RX TEST (no engine / no IRQ / HSI 64MHz) ========\r\n");
    puts1("观察窗 USART1: PA9=TX PA10=RX  115200 8N1  BRR=0x");
    put_hex(USART_BRR(USART1_BASE), 8);
    puts1("\r\n被测口 USART2: PD5=TX PD6=RX  115200 8N1  BRR=0x");
    put_hex(USART_BRR(USART2_BASE), 8);
    puts1("\r\n配置读回: GPIOD MODER=0x");
    put_hex(GPIO_MODER(DUT_PORT) & 0xFFFFu, 4);
    puts1(" AFRL=0x");
    put_hex(GPIO_AFRL(DUT_PORT), 8);
    puts1(" PUPDR=0x");
    put_hex(GPIO_PUPDR(DUT_PORT) & 0xFFFFu, 4);
    puts1("\r\n接收缓冲 (DTCM) @0x");
    put_hex((uint32_t)(uintptr_t)&g_rxbuf[0], 8);
    puts1("  size=");
    put_u32(RXBUF_SZ);
    puts1("\r\nA=收字节+量PD6占空比; B=**全端口**(GPIOA..G)逐脚活动扫描; C=PD5 反向发一段文本\r\n");
    puts1("每收到 1 字节翻一次 LED(PG7)。B 段扫到 GPIOA 时观察窗会短暂静默 (PA9 也被扫)\r\n");

    for (;;) {
        phase_rx();
        phase_scan();
        phase_tx();
        puts1("---- 一轮结束, 继续 ----\r\n");
    }
}
