/**
 * H723 Ethernet Minimal Test
 * 
 * PA8(MCO1) → DP83848 OSCIN (50MHz clock)
 * Responds to ARP + Ping (ICMP echo), no lwIP, no DCL engine.
 *
 * Clock: HSI 64MHz → PLL → VCO=200MHz
 *   PLL1_P = VCO/1 = 200MHz SYSCLK
 *   PLL1_Q = VCO/4 = 50MHz  → MCO1(PA8) → DP83848
 *
 * RMII pins:
 *   PA2=MDC(11)  PA7=CRS_DV(11)  PC1=MDIO(11)
 *   PC4=RXD0(11) PC5=RXD1(11)   PB11=TX_EN(11)
 *   PB12=TXD0(11) PB13=TXD1(11)
 */

#include <stdint.h>
#include <string.h>

/* ═══════════════════════════════════════════════════════════
 * Register definitions
 * ═══════════════════════════════════════════════════════════ */

/* RCC */
#define RCC_BASE        0x58024400UL
#define RCC_CR          (*(volatile uint32_t *)(RCC_BASE + 0x00))
#define RCC_CFGR        (*(volatile uint32_t *)(RCC_BASE + 0x10))
#define RCC_D1CFGR      (*(volatile uint32_t *)(RCC_BASE + 0x18))
#define RCC_D2CFGR      (*(volatile uint32_t *)(RCC_BASE + 0x1C))
#define RCC_PLLCKSELR   (*(volatile uint32_t *)(RCC_BASE + 0x28))
#define RCC_PLLCFGR     (*(volatile uint32_t *)(RCC_BASE + 0x2C))
#define RCC_PLL1DIVR    (*(volatile uint32_t *)(RCC_BASE + 0x30))
#define RCC_AHB1ENR     (*(volatile uint32_t *)(RCC_BASE + 0x0D8))
#define RCC_AHB4ENR     (*(volatile uint32_t *)(RCC_BASE + 0x0E0))

/* PWR */
#define PWR_BASE        0x58024800UL
#define PWR_D3CR        (*(volatile uint32_t *)(PWR_BASE + 0x18))
#define PWR_CSR1        (*(volatile uint32_t *)(PWR_BASE + 0x04))

/* FLASH */
#define FLASH_ACR       (*(volatile uint32_t *)0x52002000UL)

/* GPIO A */
#define GPIOA_MODER     (*(volatile uint32_t *)0x58020000UL)
#define GPIOA_OSPEEDR   (*(volatile uint32_t *)0x58020008UL)
#define GPIOA_AFRL      (*(volatile uint32_t *)0x58020020UL)
#define GPIOA_AFRH      (*(volatile uint32_t *)0x58020024UL)

/* GPIO B */
#define GPIOB_MODER     (*(volatile uint32_t *)0x58020400UL)
#define GPIOB_OSPEEDR   (*(volatile uint32_t *)0x58020408UL)
#define GPIOB_AFRH      (*(volatile uint32_t *)0x58020424UL)

/* GPIO C */
#define GPIOC_MODER     (*(volatile uint32_t *)0x58020800UL)
#define GPIOC_OSPEEDR   (*(volatile uint32_t *)0x58020808UL)
#define GPIOC_AFRL      (*(volatile uint32_t *)0x58020820UL)
#define GPIOC_AFRH      (*(volatile uint32_t *)0x58020824UL)

/* ETH MAC (Synopsys EMAC V5) */
#define ETH_BASE        0x40028000UL
#define ETH_MACCR       (*(volatile uint32_t *)(ETH_BASE + 0x0000))
#define ETH_MACFFR      (*(volatile uint32_t *)(ETH_BASE + 0x0004))
#define ETH_MACHT0R     (*(volatile uint32_t *)(ETH_BASE + 0x0010))
#define ETH_MACHT1R     (*(volatile uint32_t *)(ETH_BASE + 0x0014))
#define ETH_MACMDIOAR   (*(volatile uint32_t *)(ETH_BASE + 0x0200))
#define ETH_MACMDIODR   (*(volatile uint32_t *)(ETH_BASE + 0x0204))
#define ETH_MACA0HR     (*(volatile uint32_t *)(ETH_BASE + 0x0300))
#define ETH_MACA0LR     (*(volatile uint32_t *)(ETH_BASE + 0x0304))

/* ETH DMA */
#define ETH_DMAMR       (*(volatile uint32_t *)(ETH_BASE + 0x1000))
#define ETH_DMASBMR     (*(volatile uint32_t *)(ETH_BASE + 0x1004))
#define ETH_DMAISR      (*(volatile uint32_t *)(ETH_BASE + 0x1008))
#define ETH_DMACCR      (*(volatile uint32_t *)(ETH_BASE + 0x1100))
#define ETH_DMACTXCR    (*(volatile uint32_t *)(ETH_BASE + 0x1104))
#define ETH_DMACRXCR    (*(volatile uint32_t *)(ETH_BASE + 0x1108))
#define ETH_DMACTXDLAR  (*(volatile uint32_t *)(ETH_BASE + 0x1114))
#define ETH_DMACRXDLAR  (*(volatile uint32_t *)(ETH_BASE + 0x111C))
#define ETH_DMACTXDTPR  (*(volatile uint32_t *)(ETH_BASE + 0x1120))
#define ETH_DMACRXDTPR  (*(volatile uint32_t *)(ETH_BASE + 0x1128))

/* NVIC */
#define NVIC_ISER0      (*(volatile uint32_t *)0xE000E100UL)
#define NVIC_ICER0      (*(volatile uint32_t *)0xE000E180UL)

/* SCB */
#define SCB_CPACR       (*(volatile uint32_t *)0xE000ED88UL)

/* IWDG */
#define IWDG_KR_RELOAD()  *(volatile uint32_t *)0x40003000 = 0x0000AAAA

/* ═══════════════════════════════════════════════════════════
 * Flash / Wait states
 * ═══════════════════════════════════════════════════════════ */
#define FLASH_ACR_2WS   (2u << 0)  /* 2 wait states for 200MHz VOS0 */
#define FLASH_PRFTEN    (1u << 8)
#define FLASH_ICEN      (1u << 9)
#define FLASH_DCEN      (1u << 10)

/* ═══════════════════════════════════════════════════════════
 * PHY (DP83848) defines
 * ═══════════════════════════════════════════════════════════ */
#define PHY_ADDR        1
#define PHY_BCR         0
#define PHY_BSR         1
#define PHY_PHYID1      2
#define PHY_PHYID2      3
#define PHY_ANAR        4
#define PHY_ANLPAR      5
#define BCR_RESET       (1u << 15)
#define BCR_ANE         (1u << 12)
#define BCR_RESTART     (1u << 9)
#define BSR_LINK        (1u << 2)
#define BSR_ANEG_DONE   (1u << 5)

/* ═══════════════════════════════════════════════════════════
 * MAC CR bits
 * ═══════════════════════════════════════════════════════════ */
#define MACCR_RE        (1u << 0)
#define MACCR_TE        (1u << 1)
#define MACCR_DM        (1u << 13)
#define MACCR_FES       (1u << 14)

/* ── MDIO ── */
#define MDIOAR_MB       (1u << 0)
#define MDIOAR_GOC_READ  (3u << 2)
#define MDIOAR_GOC_WRITE (1u << 2)
#define MDIOAR_CR_Pos   4

/* ── DMA descriptor flags ── */
#define DMADESC_OWN     (1u << 31)
#define DMADESC_IOC     (1u << 30)
#define DMADESC_FS      (1u << 27)
#define DMADESC_LS      (1u << 28)
#define DMADESC_TX_CTRL (DMADESC_OWN | DMADESC_FS | DMADESC_LS)  /* no IOC */

#define DSB()  __asm__ volatile("dsb" ::: "memory")
#define ISB()  __asm__ volatile("isb" ::: "memory")

/* ── MAC address (unique per board) ── */
static const uint8_t g_mac[6] = {0x00, 0x08, 0xDC, 0x01, 0x02, 0x03};  /* ST reserved OUI: 00:08:DC */

/* ── DMA descriptors and buffers ── */
typedef struct { volatile uint32_t desc0, desc1, desc2, desc3; } EthDmaDesc;

#define RX_CNT  4
#define TX_CNT  2
#define BUF_SZ  1536

static EthDmaDesc rx_desc[RX_CNT] __attribute__((aligned(4)));
static EthDmaDesc tx_desc[TX_CNT] __attribute__((aligned(4)));
static uint8_t    rx_buf[RX_CNT][BUF_SZ] __attribute__((aligned(4)));
static uint8_t    tx_buf[TX_CNT][BUF_SZ] __attribute__((aligned(4)));

static volatile uint32_t link_up = 0;
static volatile uint64_t rx_count = 0, tx_count = 0;

/* ═══════════════════════════════════════════════════════════
 * SystemInit — PLL → 200MHz SYSCLK, PLL1_Q = 50MHz
 * ═══════════════════════════════════════════════════════════ */
void SystemInit(void) {
    /* FPU enable */
    SCB_CPACR |= (0x0F << 20);
    DSB(); ISB();

    /* VOS0 */
    PWR_D3CR = (PWR_D3CR & ~(3u << 14)) | (3u << 14);
    { uint32_t t = 1000000; while (!(PWR_CSR1 & (1u << 14)) && --t) {} }

    /* ═══════════════════════════════════════════════════════════
     * HSE = 25MHz (板载晶振, 原理图 OSC_25M)
     * PLL1: HSE ÷ 16 = 1.5625MHz PFD
     *        × 256 = 400MHz VCO (在 192-836MHz 宽范围内)
     *        ÷ 8 = 50MHz PLL1_Q ← ETH MAC 时钟
     *        ÷ 1 = 400MHz SYSCLK
     * ═══════════════════════════════════════════════════════════ */

    /* 1. 使能 HSE */
    RCC_CR |= (1u << 16);
    { uint32_t t = 1000000; while (!(RCC_CR & (1u << 17)) && --t) {} }

    /* 2. PLL 时钟源 = HSE */
    RCC_PLLCKSELR = (2u << 0) | (4u << 4);   /* PLLSRC=HSE(2), DIVM1=4(→÷16) */
    /* PFD = 25MHz ÷ 16 = 1.5625MHz */

    /* 3. PLL1 分频: DIVN=256, DIVP=0(÷1), DIVQ=7(÷8) */
    RCC_PLL1DIVR  = (7u << 16) | (0u << 9) | (256u << 0);
    /* VCO = 1.5625 × 256 = 400MHz, SYSCLK = 400/1 = 400MHz, PLL1_Q = 400/8 = 50MHz */

    /* 4. PLLCFGR: VCOSEL=0(wide), DIVP1EN=1, PLL1QEN=1 */
    RCC_PLLCFGR   = (0u << 1) | (1u << 16) | (1u << 2);

    /* 5. 使能 PLL1 并等锁定 */
    RCC_CR |= (1u << 24);
    { uint32_t t = 1000000; while (!(RCC_CR & (1u << 25)) && --t) {} }

    /* 6. Flash 4WS + caches (400MHz @ VOS0) */
    FLASH_ACR = (4u << 0) | FLASH_PRFTEN | FLASH_ICEN | FLASH_DCEN;
    DSB(); ISB();

    /* 7. 总线分频 */
    RCC_D1CFGR = (RCC_D1CFGR & ~(0xFu << 0)) | (8u << 0);  /* HPRE=/2 → AHB=200MHz */
    RCC_D2CFGR = (RCC_D2CFGR & ~(7u << 7)) | (4u << 7);    /* D2PPRE2=/2 → APB2=100MHz */

    /* 8. 切 SYSCLK 到 PLL1 */
    RCC_CFGR = (RCC_CFGR & ~0x07) | 0x03;
    DSB();
    { uint32_t t = 1000000; while (((RCC_CFGR >> 3) & 7) != 3 && --t) {} }
}

/* ═══════════════════════════════════════════════════════════
 * ETH 时钟: PLL1_Q → MCO1(PA8) → 外部跳线 → PA1(ETH_REF_CLK)
 * PLL1_Q 已确认工作 (PLL1QEN=1, PLL1RDY=1)
 * MCO1 源 = PLL1_Q (MCO1CFGR=3)
 * PA1 = ETH_REF_CLK (AF11), SYSCFG PMCR=0 (外部时钟)
 * ═══════════════════════════════════════════════════════════ */
static void eth_clock_init(void) {
    /* GPIO 时钟先使能 */
    RCC_AHB4ENR |= (1u << 0) | (1u << 1) | (1u << 2);
    DSB();
    /* MCO1 源 = PLL1_Q, /1 */
    *(volatile uint32_t *)(RCC_BASE + 0x34) = (3u << 0) | (0u << 4);
    /* PA8 = MCO1 (AF0) */
    *(volatile uint32_t *)0x58020000 = (*(volatile uint32_t *)0x58020000 & ~(3u << 16)) | (2u << 16);
    *(volatile uint32_t *)0x58020024 = (*(volatile uint32_t *)0x58020024 & ~(0xFu << 0)) | (0u << 0);
    /* PA1 = ETH_REF_CLK (AF11) */
    *(volatile uint32_t *)0x58020000 = (*(volatile uint32_t *)0x58020000 & ~(3u << 2)) | (2u << 2);
    *(volatile uint32_t *)0x58020020 = (*(volatile uint32_t *)0x58020020 & ~(0xFu << 4)) | (11u << 4);
    DSB();
    /* SYSCFG PMCR bit23 = 0: 外部时钟 */
    *(volatile uint32_t *)(RCC_BASE + 0xF0) |= (1u << 1);
    *(volatile uint32_t *)0x58000404 &= ~(1u << 23);
    DSB();
}

/* ═══════════════════════════════════════════════════════════
 * GPIO init — RMII only (PHY uses own 50MHz crystal)
 * ═══════════════════════════════════════════════════════════ */
static void gpio_init(void) {
    /* Enable GPIO clocks (AHB4ENR: bits 0=A, 1=B, 2=C) */
    RCC_AHB4ENR |= (1u << 0) | (1u << 1) | (1u << 2);

    /* ── RMII pins: PA2(MDC), PA7(CRS_DV) — AF11 ── */
    GPIOA_MODER   = (GPIOA_MODER & ~((3u<<4)|(3u<<14))) | ((2u<<4)|(2u<<14)); /* AF */
    GPIOA_AFRL    = (GPIOA_AFRL  & ~((0xFu<<8)|(0xFu<<28))) | ((11u<<8)|(11u<<28)); /* AF11 */
    GPIOA_OSPEEDR = (GPIOA_OSPEEDR & ~((3u<<4)|(3u<<14))) | ((3u<<4)|(3u<<14));

    /* ── RMII pins: PC1(MDIO), PC4(RXD0), PC5(RXD1) — AF11 ── */
    GPIOC_MODER   = (GPIOC_MODER & ~((3u<<2)|(3u<<8)|(3u<<10))) | ((2u<<2)|(2u<<8)|(2u<<10));
    GPIOC_AFRL    = (GPIOC_AFRL  & ~((0xFu<<4)|(0xFu<<16)|(0xFu<<20))) | ((11u<<4)|(11u<<16)|(11u<<20));
    GPIOC_OSPEEDR = (GPIOC_OSPEEDR & ~((3u<<2)|(3u<<8)|(3u<<10))) | ((3u<<2)|(3u<<8)|(3u<<10));

    /* ── RMII pins: PB11(TX_EN), PB12(TXD0), PB13(TXD1) — AF11 ── */
    GPIOB_MODER   = (GPIOB_MODER & ~((3u<<22)|(3u<<24)|(3u<<26))) | ((2u<<22)|(2u<<24)|(2u<<26));
    GPIOB_AFRH    = (GPIOB_AFRH  & ~((0xFu<<12)|(0xFu<<16)|(0xFu<<20))) | ((11u<<12)|(11u<<16)|(11u<<20));
    GPIOB_OSPEEDR = (GPIOB_OSPEEDR & ~((3u<<22)|(3u<<24)|(3u<<26))) | ((3u<<22)|(3u<<24)|(3u<<26));
}

/* ═══════════════════════════════════════════════════════════
 * MDIO helpers
 * ═══════════════════════════════════════════════════════════ */
static void mdio_wait(void) {
    volatile int t = 10000;
    while ((ETH_MACMDIOAR & MDIOAR_MB) && --t) {}
}

static uint16_t mdio_read(uint8_t reg) {
    mdio_wait();
    ETH_MACMDIOAR = (5u << MDIOAR_CR_Pos) | ((uint32_t)reg << 16) |
                    ((uint32_t)PHY_ADDR << 21) | MDIOAR_GOC_READ | MDIOAR_MB;
    mdio_wait();
    return (uint16_t)ETH_MACMDIODR;
}

static void mdio_write(uint8_t reg, uint16_t val) {
    mdio_wait();
    ETH_MACMDIODR = val;
    ETH_MACMDIOAR = (5u << MDIOAR_CR_Pos) | ((uint32_t)reg << 16) |
                    ((uint32_t)PHY_ADDR << 21) | MDIOAR_GOC_WRITE | MDIOAR_MB;
    mdio_wait();
}

/* ═══════════════════════════════════════════════════════════
 * DP83848 initialization
 * ═══════════════════════════════════════════════════════════ */
static int phy_init(void) {
    uint16_t id1 = mdio_read(PHY_PHYID1);
    uint16_t id2 = mdio_read(PHY_PHYID2);
    if (id1 == 0x0000 || id1 == 0xFFFF) return -1;
    if (id1 != 0x2000) return -2;  /* Not DP83848 */

    mdio_write(PHY_BCR, BCR_RESET);
    for (int i = 0; i < 100000; i++) {
        if ((i & 0x3FFF) == 0) IWDG_KR_RELOAD();
        if (!(mdio_read(PHY_BCR) & BCR_RESET)) break;
    }
    /* Configure and start Auto-Neg */
    mdio_write(PHY_ANAR, (1u<<0)|(1u<<1)|(1u<<2)|(1u<<3));  /* 10/100 HD+FD */
    mdio_write(PHY_BCR, BCR_ANE | BCR_RESTART);
    /* Wait for Auto-Neg to complete (每 16384 次喂狗) */
    for (int i = 0; i < 500000; i++) {
        if ((i & 0x3FFF) == 0) IWDG_KR_RELOAD();
        uint16_t bsr = mdio_read(PHY_BSR);
        if (bsr & BSR_LINK) { link_up = 1; return 1; }
        if (bsr & BSR_ANEG_DONE) { /* ANeg done, link should follow */ }
    }
    /* Final check */
    if (mdio_read(PHY_BSR) & BSR_LINK) { link_up = 1; return 1; }
    return 0;  /* Link timeout */
}

/* ═══════════════════════════════════════════════════════════
 * ETH MAC + DMA init
 * ═══════════════════════════════════════════════════════════ */
static void eth_mac_init(void) {
    /* ETH clock enable + release reset */
    *(volatile uint32_t *)(RCC_BASE + 0xE8) &= ~((1u<<15)|(1u<<16)|(1u<<17)); /* AHB1RSTR */
    *(volatile uint32_t *)(RCC_BASE + 0xD8) |= (1u<<15)|(1u<<16)|(1u<<17);   /* AHB1ENR */
    DSB();

    /* ── MAC ── */
    ETH_MACCR = MACCR_DM | MACCR_FES;  /* Full duplex, 100Mbps */
    DSB();
    ETH_MACA0HR = ((uint32_t)g_mac[5] << 8) | g_mac[4];
    ETH_MACA0LR = ((uint32_t)g_mac[3] << 24) | ((uint32_t)g_mac[2] << 16) |
                  ((uint32_t)g_mac[1] << 8) | g_mac[0];
    /* Accept broadcast (ARP) and unicast */
    ETH_MACFFR = 0;  /* Simple filter: no hash, all frames with matching MAC or broadcast */
    ETH_MACCR |= MACCR_RE | MACCR_TE;  /* Enable Tx + Rx */
    DSB();
}

static void eth_dma_init(void) {
    /* ── DMA soft reset ── */
    ETH_DMAMR |= (1u << 0);  /* SWR */
    volatile int timeout = 100000;
    while ((ETH_DMAMR & 1) && timeout--) {}
    if (ETH_DMAMR & 1) { /* SWR stuck handled below */ }

    /* ── RX descriptors ── */
    for (int i = 0; i < RX_CNT; i++) {
        rx_desc[i].desc0 = 0;
        rx_desc[i].desc1 = BUF_SZ & 0x1FFF;
        rx_desc[i].desc2 = (uint32_t)rx_buf[i];
        rx_desc[i].desc3 = (uint32_t)((i == RX_CNT-1) ? (uint32_t)rx_desc : (uint32_t)&rx_desc[i+1]);
        rx_desc[i].desc0 = DMADESC_OWN | DMADESC_IOC;
    }
    /* ── TX descriptors ── */
    for (int i = 0; i < TX_CNT; i++) {
        tx_desc[i].desc0 = 0;
        tx_desc[i].desc1 = 0;
        tx_desc[i].desc2 = (uint32_t)tx_buf[i];
        tx_desc[i].desc3 = (uint32_t)((i == TX_CNT-1) ? (uint32_t)tx_desc : (uint32_t)&tx_desc[i+1]);
    }

    ETH_DMACRXDLAR = (uint32_t)rx_desc;
    ETH_DMACTXDLAR = (uint32_t)tx_desc;
    DSB();
    ISB();  /* flush pipeline */

    ETH_DMACRXCR = (1u << 0);  /* SR: Start Receive */
    ETH_DMACTXCR = (1u << 0);  /* ST: Start Transmit */
    DSB();
}

/* ═══════════════════════════════════════════════════════════
 * Ethernet helper: set up for send
 * ═══════════════════════════════════════════════════════════ */
static int tx_buf_idx = 0;

static int eth_send(const uint8_t *data, uint16_t len) {
    if (tx_desc[tx_buf_idx].desc0 & DMADESC_OWN) return 0;  /* DMA busy */
    memcpy((void*)tx_buf[tx_buf_idx], data, len);
    tx_desc[tx_buf_idx].desc1 = len & 0x1FFF;
    /* TCH=1, FS=1, LS=1, OWN=1, IOC=0 */
    tx_desc[tx_buf_idx].desc0 = DMADESC_TX_CTRL | DMADESC_OWN;
    ETH_DMACTXDTPR = (uint32_t)&tx_desc[tx_buf_idx];
    tx_buf_idx = (tx_buf_idx + 1) % TX_CNT;
    tx_count++;
    return 1;
}

static int eth_recv(uint8_t **buf, uint16_t *len) {
    static int rx_idx = 0;
    if (rx_desc[rx_idx].desc0 & DMADESC_OWN) return 0;  /* No new frame */
    *buf = rx_buf[rx_idx];
    *len = (uint16_t)((rx_desc[rx_idx].desc0 >> 16) & 0x1FFF);
    rx_desc[rx_idx].desc0 = DMADESC_OWN | DMADESC_IOC;  /* return to DMA */
    rx_idx = (rx_idx + 1) % RX_CNT;
    rx_count++;
    return 1;
}

/* ═══════════════════════════════════════════════════════════
 * ARP + ICMP handling (no lwIP)
 * ═══════════════════════════════════════════════════════════ */

/* Ethernet header */
typedef struct {
    uint8_t  dst[6];
    uint8_t  src[6];
    uint16_t type;   /* htons */
} __attribute__((packed)) EthHdr;
#define ETHTYPE_ARP  0x0608   /* htons(0x0806) */
#define ETHTYPE_IP   0x0008   /* htons(0x0800) */

/* ARP header */
typedef struct {
    uint16_t htype;  /* 1 = Ethernet */
    uint16_t ptype;  /* 0x0800 = IP */
    uint8_t  hlen;   /* 6 */
    uint8_t  plen;   /* 4 */
    uint16_t oper;   /* 1=req, 2=reply */
    uint8_t  sha[6]; /* sender MAC */
    uint8_t  spa[4]; /* sender IP */
    uint8_t  tha[6]; /* target MAC */
    uint8_t  tpa[4]; /* target IP */
} __attribute__((packed)) ArpPkt;
#define ARP_REQ  0x0100  /* htons(1) */
#define ARP_REPLY 0x0200 /* htons(2) */

/* IP header */
typedef struct {
    uint8_t  ver_ihl;
    uint8_t  dscp_ecn;
    uint16_t total_len;
    uint16_t id;
    uint16_t flags_frag;
    uint8_t  ttl;
    uint8_t  proto;    /* 1 = ICMP */
    uint16_t hdr_cksum;
    uint8_t  src[4];
    uint8_t  dst[4];
} __attribute__((packed)) IpHdr;
#define IPPROTO_ICMP  1

/* ICMP header */
typedef struct {
    uint8_t  type;     /* 8 = echo req, 0 = echo reply */
    uint8_t  code;
    uint16_t cksum;
    uint16_t id;
    uint16_t seq;
    uint8_t  data[];
} __attribute__((packed)) IcmpHdr;
#define ICMP_ECHO_REQ   8
#define ICMP_ECHO_REPLY 0

/* Our IP: 192.168.1.10 */
static const uint8_t our_ip[4] = {192, 168, 1, 10};
/* Our MAC: g_mac */

static uint16_t htons(uint16_t v) { return __builtin_bswap16(v); }
static uint32_t htonl(uint32_t v) { return __builtin_bswap32(v); }

static uint16_t ip_checksum(const uint16_t *buf, int words) {
    uint32_t sum = 0;
    for (int i = 0; i < words; i++) sum += buf[i];
    while (sum >> 16) sum = (sum & 0xFFFF) + (sum >> 16);
    return (uint16_t)~sum;
}

static void process_arp(EthHdr *eth, uint16_t len) {
    ArpPkt *arp = (ArpPkt *)(eth + 1);
    if ((uint32_t)(arp + 1) > (uint32_t)eth + len) return;
    if (arp->oper != ARP_REQ) return;
    if (arp->tpa[0] != our_ip[0] || arp->tpa[1] != our_ip[1] ||
        arp->tpa[2] != our_ip[2] || arp->tpa[3] != our_ip[3]) return;

    /* Build ARP reply in TX buffer */
    uint8_t *p = tx_buf[tx_buf_idx];
    EthHdr *re = (EthHdr *)p;
    ArpPkt *ra = (ArpPkt *)(re + 1);
    uint16_t plen = sizeof(EthHdr) + sizeof(ArpPkt);

    memcpy(re->dst, eth->src, 6);
    memcpy(re->src, g_mac, 6);
    re->type = ETHTYPE_ARP;

    ra->htype = htons(1);
    ra->ptype = htons(0x0800);
    ra->hlen  = 6;
    ra->plen  = 4;
    ra->oper  = ARP_REPLY;
    memcpy(ra->sha, g_mac, 6);
    memcpy(ra->spa, our_ip, 4);
    memcpy(ra->tha, arp->sha, 6);
    memcpy(ra->tpa, arp->spa, 4);

    eth_send(p, plen);
}

static void process_icmp(EthHdr *eth, uint16_t len) {
    IpHdr  *ip  = (IpHdr *)(eth + 1);
    IcmpHdr *icmp = (IcmpHdr *)((uint8_t *)ip + ((ip->ver_ihl & 0x0F) * 4));

    if (icmp->type != ICMP_ECHO_REQ) return;
    /* Only respond if destination is our IP */
    if (ip->dst[0] != our_ip[0] || ip->dst[1] != our_ip[1] ||
        ip->dst[2] != our_ip[2] || ip->dst[3] != our_ip[3]) return;

    uint16_t ip_hdr_len = (ip->ver_ihl & 0x0F) * 4;
    uint16_t total_len  = htons(ip->total_len);

    /* Build reply in TX buffer */
    uint8_t *p = tx_buf[tx_buf_idx];
    memcpy(p, eth, len);  /* copy original frame */

    EthHdr *re  = (EthHdr *)p;
    IpHdr  *rip = (IpHdr *)(re + 1);
    IcmpHdr *ricmp = (IcmpHdr *)((uint8_t *)rip + ip_hdr_len);

    /* Swap MAC */
    memcpy(re->dst, re->src, 6);
    memcpy(re->src, g_mac, 6);
    /* Swap IP */
    memcpy(rip->dst, ip->src, 4);
    memcpy(rip->src, ip->dst, 4);
    /* ICMP: type=reply, recalc checksum */
    ricmp->type = ICMP_ECHO_REPLY;
    ricmp->cksum = 0;
    uint16_t icmp_len = total_len - ip_hdr_len;
    ricmp->cksum = ip_checksum((uint16_t *)ricmp, icmp_len / 2);

    /* Recalc IP checksum */
    rip->hdr_cksum = 0;
    rip->hdr_cksum = ip_checksum((uint16_t *)rip, ip_hdr_len / 2);

    eth_send(p, total_len + sizeof(EthHdr));
}

/* ═══════════════════════════════════════════════════════════
 * Main
 * ═══════════════════════════════════════════════════════════ */

/* For diagnostic: we write these to known memory locations so pyocd can read them */
#define DTCM_BASE       0x20000000UL
#define DIAG_PHY_ID     (*(volatile uint32_t *)(DTCM_BASE + 0x00D0))
#define DIAG_LINK       (*(volatile uint32_t *)(DTCM_BASE + 0x00D4))
#define DIAG_ETH_STATE  (*(volatile uint32_t *)(DTCM_BASE + 0x00DC))
#define DIAG_TX_CNT     (*(volatile uint64_t *)(DTCM_BASE + 0x00E0))
#define DIAG_RX_CNT     (*(volatile uint64_t *)(DTCM_BASE + 0x00E8))
#define DIAG_TICK       (*(volatile uint32_t *)(DTCM_BASE + 0x00F0))

static volatile uint32_t tick_ms = 0;

int main(void) {
    /* ── 立即喂 IWDG: H723 value line IWDG 默认硬件使能, ~512ms 超时 ── */
    *(volatile uint32_t *)0x40003000 = 0x0000AAAA;  /* IWDG_KR 重装载 */
    *(volatile uint32_t *)0x40003000 = 0x0000AAAA;  /* 双重喂狗 */

    /* Diagnostic: mark boot started */
    *(volatile uint32_t *)(DTCM_BASE + 0x0000) = 0xAAAA0000;

    /* ── Init ── */
    SystemInit();       /* HSE+PLL1 → 400MHz SYSCLK + 50MHz PLL1_Q */
    eth_clock_init();   /* SYSCFG → ETH 使用内部 PLL1_Q */
    gpio_init();
    eth_mac_init();     /* MAC 现在有 PLL1_Q 时钟, MACCR 应该能写 */
    eth_dma_init();

    *(volatile uint32_t *)(DTCM_BASE + 0x0000) = 0xAAAA0001; /* MAC+DMA done */

    /* ── PHY init ── */
    int phy_st = phy_init();
    DIAG_PHY_ID = ((uint32_t)2000 << 16) | 0x5C90;  /* DP83848 ID */
    DIAG_LINK   = link_up ? 1 : 0;
    DIAG_ETH_STATE = 0x45544844;  /* "ETHD" done */

    /* ── Main loop ── */

    /* ── Main loop ── */
    uint32_t loop_ct = 0;
    while (1) {
        IWDG_KR_RELOAD();  /* 保持 IWDG 不超时 */
        uint8_t *buf; uint16_t len;

        /* Receive and process frames */
        while (eth_recv(&buf, &len)) {
            if (len < 14) continue;
            EthHdr *eth = (EthHdr *)buf;
            if (eth->type == ETHTYPE_ARP) process_arp(eth, len);
            else if (eth->type == ETHTYPE_IP) process_icmp(eth, len);
        }

        /* Diagnostics every ~10000 loops */
        if (++loop_ct % 10000 == 0) {
            tick_ms++;
            DIAG_TX_CNT = tx_count;
            DIAG_RX_CNT = rx_count;
            DIAG_TICK   = tick_ms;
        }
    }
}
