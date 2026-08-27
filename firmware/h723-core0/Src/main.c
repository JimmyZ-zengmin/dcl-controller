/**
 * 核心0 H723 �?ISR 引擎移植�?
 *
 * 完整移植 ESP32-S3 核心0 架构�?STM32H723:
 *   - 六种寄存器空�?(SENSOR/WIRE/PARAM/STATE/LUT/ACTUATOR)
 *   - 路由表扫描引�?(34 原语)
 *   - 抖动直方�?(256 bin, 180万样�?3分钟)
 *
 * 时钟: VOS0 + PLL 544MHz, HPRE=/2, D2PPRE2=/4
 * ISR: 100μs TIM1, DWT CYCCNT 测量 @136MHz (7.4ns 分辨�?
 */
#include <stdint.h>
#include "memory_map.h"

/* ══════════════════════════════════════════════════════════�?
 * 寄存器定�?
 * ══════════════════════════════════════════════════════════�?*/

#define PWR_BASE     0x58024800UL
#define PWR_CR3      (*(volatile uint32_t *)(PWR_BASE + 0x0C))

#define RCC_BASE      0x58024400UL
#define RCC_CR        (*(volatile uint32_t *)(RCC_BASE + 0x00))
#define RCC_CFGR      (*(volatile uint32_t *)(RCC_BASE + 0x10))
#define RCC_D1CFGR    (*(volatile uint32_t *)(RCC_BASE + 0x18))
#define RCC_D2CFGR    (*(volatile uint32_t *)(RCC_BASE + 0x1C))
#define RCC_PLLCKSELR (*(volatile uint32_t *)(RCC_BASE + 0x28))
#define RCC_PLLCFGR   (*(volatile uint32_t *)(RCC_BASE + 0x2C))
#define RCC_PLL1DIVR  (*(volatile uint32_t *)(RCC_BASE + 0x30))
#define RCC_APB2ENR   (*(volatile uint32_t *)(RCC_BASE + 0x0F0))
/* H723 value line: AHB1ENR @ 0xD8 (H743�?xD0!) */
#define RCC_AHB1ENR   (*(volatile uint32_t *)(RCC_BASE + 0x0D8))
#define RCC_AHB4ENR   (*(volatile uint32_t *)(RCC_BASE + 0x0E0))

/* GPIOE: AHB4总线, 0x58021000 (Cortex-M7无bit-band, 0x42xxxxxx非法) */
#define GPIOE_BASE    0x58021000UL
#define GPIOE_MODER   (*(volatile uint32_t *)(GPIOE_BASE + 0x00))
#define GPIOE_OSPEEDR (*(volatile uint32_t *)(GPIOE_BASE + 0x08))
#define GPIOE_ODR     (*(volatile uint32_t *)(GPIOE_BASE + 0x14))
#define GPIOE_AFRL    (*(volatile uint32_t *)(GPIOE_BASE + 0x20))
#define GPIOE_AFRH    (*(volatile uint32_t *)(GPIOE_BASE + 0x24))
/* GPIOD: PD0/PD1 �?FDCAN1, PD5/PD6 �?USART2 (0x58020C00, �?x42020C00) */
#define GPIOD_BASE    0x58020C00UL
#define GPIOD_MODER   (*(volatile uint32_t *)(GPIOD_BASE + 0x00))
#define GPIOD_AFRL    (*(volatile uint32_t *)(GPIOD_BASE + 0x20))

/* DMA2_BASE �?Stream1/2/5 保留 */
#define DMA2_BASE     0x40020400UL
#define DMAMUX1_BASE  0x40020800UL

/* DMA2 Stream1: ADC1 �?DTCM (ADC_RAW) */
#define DMA2_S1CR    (*(volatile uint32_t *)(DMA2_BASE + 0x28))
#define DMA2_S1NDTR  (*(volatile uint32_t *)(DMA2_BASE + 0x2C))
#define DMA2_S1PAR   (*(volatile uint32_t *)(DMA2_BASE + 0x30))
#define DMA2_S1M0AR  (*(volatile uint32_t *)(DMA2_BASE + 0x34))
#define DMA2_S1FCR   (*(volatile uint32_t *)(DMA2_BASE + 0x3C))

/* DMA2 Stream5: DTCM(SHADOW) → GPIOE_ODR — TIM1_CC4 硬件触发 (周期末尾) */
#define DMA2_S5CR    (*(volatile uint32_t *)(DMA2_BASE + 0x88))
#define DMA2_S5NDTR  (*(volatile uint32_t *)(DMA2_BASE + 0x8C))
#define DMA2_S5PAR   (*(volatile uint32_t *)(DMA2_BASE + 0x90))
#define DMA2_S5M0AR  (*(volatile uint32_t *)(DMA2_BASE + 0x94))
#define DMA2_S5FCR   (*(volatile uint32_t *)(DMA2_BASE + 0x9C))

/* DMAMUX1: Stream5 → channel 13 (=8+5),地址偏移 0x34 */
#define DMAMUX1_S1CR  (*(volatile uint32_t *)(DMAMUX1_BASE + 0x04))
#define DMAMUX1_CH13CR  (*(volatile uint32_t *)(DMAMUX1_BASE + 0x34))

/* SHADOW 输出缓冲 — CPU 写这里,DMA 搬到 GPIOE_ODR */
/* SHADOW_GPIO 定义在 memory_map.h */

/* H723 DMAMUX 请求ID: TIM1_UP=15, ADC1=9 */
#define DMAMUX_REQ_TIM1_UP  15
#define DMAMUX_REQ_TIM1_CH4 14
#define DMAMUX_REQ_ADC1     9

/* DMA2 LIFCR/HIFCR 中断标志清除 */
#define DMA2_LIFCR   (*(volatile uint32_t *)(DMA2_BASE + 0x08))
#define DMA2_HIFCR   (*(volatile uint32_t *)(DMA2_BASE + 0x0C))

/* ADC1 */
#define ADC1_BASE     0x40022000UL
#define ADC1_ISR      (*(volatile uint32_t *)(ADC1_BASE + 0x00))
#define ADC1_CR       (*(volatile uint32_t *)(ADC1_BASE + 0x08))
#define ADC1_CFGR     (*(volatile uint32_t *)(ADC1_BASE + 0x0C))
#define ADC1_SMPR1    (*(volatile uint32_t *)(ADC1_BASE + 0x14))
#define ADC1_SMPR2    (*(volatile uint32_t *)(ADC1_BASE + 0x18))
#define ADC1_PCSEL    (*(volatile uint32_t *)(ADC1_BASE + 0x1C))
#define ADC1_SQR1     (*(volatile uint32_t *)(ADC1_BASE + 0x30))
#define ADC1_DR       (*(volatile uint32_t *)(ADC1_BASE + 0x40))

/* ADC12 Common */
#define ADC12_COMMON  0x40022300UL
#define ADC12_CCR     (*(volatile uint32_t *)(ADC12_COMMON + 0x08))

/* CANopen: FDCAN1 (PD0=RX, PD1=TX, AF9), 500kbps */
#define FDCAN1_BASE   0x4000AC00UL
#define FDCAN1_IR     (*(volatile uint32_t *)(FDCAN1_BASE + 0x00)) /* ID */
#define FDCAN1_CREL   (*(volatile uint32_t *)(FDCAN1_BASE + 0x04))
#define FDCAN1_ENDN   (*(volatile uint32_t *)(FDCAN1_BASE + 0x08))
#define FDCAN1_DBTP   (*(volatile uint32_t *)(FDCAN1_BASE + 0x0C))
#define FDCAN1_TEST   (*(volatile uint32_t *)(FDCAN1_BASE + 0x10))
#define FDCAN1_RWD    (*(volatile uint32_t *)(FDCAN1_BASE + 0x14))
#define FDCAN1_CCCR   (*(volatile uint32_t *)(FDCAN1_BASE + 0x18))
#define FDCAN1_NBTP   (*(volatile uint32_t *)(FDCAN1_BASE + 0x1C))
#define FDCAN1_TSCC   (*(volatile uint32_t *)(FDCAN1_BASE + 0x20))
#define FDCAN1_IRQ    (*(volatile uint32_t *)(FDCAN1_BASE + 0x50))
#define FDCAN1_IE     (*(volatile uint32_t *)(FDCAN1_BASE + 0x54))
#define FDCAN1_ILS    (*(volatile uint32_t *)(FDCAN1_BASE + 0x58))
#define FDCAN1_ILE    (*(volatile uint32_t *)(FDCAN1_BASE + 0x5C))
#define FDCAN1_RXF0C  (*(volatile uint32_t *)(FDCAN1_BASE + 0xA0))
#define FDCAN1_RXF0S  (*(volatile uint32_t *)(FDCAN1_BASE + 0xA4))
#define FDCAN1_RXF0A  (*(volatile uint32_t *)(FDCAN1_BASE + 0xA8))
#define FDCAN1_TXBC   (*(volatile uint32_t *)(FDCAN1_BASE + 0xC0))
#define FDCAN1_TXFQS  (*(volatile uint32_t *)(FDCAN1_BASE + 0xC4))
#define FDCAN1_TXBAR  (*(volatile uint32_t *)(FDCAN1_BASE + 0xCC))
/* FDCAN Message RAM: 2560B @ 0x4000B400 (FDCAN1) */
#define FDCAN1_MSGRAM 0x4000B400UL
#define FDCAN1_RX_FIFO0_OFFSET 0
#define FDCAN1_TX_FIFO_OFFSET  0x300
#define FDCAN_FLT_NONE  0
#define FDCAN_CCCR_INIT  (1<<0)
#define FDCAN_CCCR_CCE   (1<<1)

/* CANopen NMT状�?*/
#define NMT_INITIALISING   0
#define NMT_PREOP          127
#define NMT_OPERATIONAL    5
#define NMT_STOPPED        4
#define COB_NMT      0x000  /* NMT命令 */
#define COB_SYNC     0x080  /* SYNC消息 */
#define COB_EMERG    0x080  /* Emergency + nodeID */
#define COB_TPDO1    0x180  /* TPDO1 + nodeID */
#define COB_RPDO1    0x200  /* RPDO1 + nodeID */
#define COB_TSDO     0x580  /* SDO响应 + nodeID */
#define COB_RSDO     0x600  /* SDO请求 + nodeID */
#define COB_HEARTBEAT 0x700 /* Heartbeat + nodeID */
#define NODE_ID      1

/* CANopen 对象字典索引 */
#define OD_DEVICE_TYPE      0x1000
#define OD_ERROR_REGISTER   0x1001
#define OD_HEARTBEAT_TIME   0x1017
#define OD_IDENTITY         0x1018

static uint8_t  canopen_state;
static uint32_t canopen_hb_timer;
static uint16_t canopen_hb_period; /* ms */

/* ── UART 帧协�?── */
#define FRAME_CMD   0xC0
#define FRAME_STS   0xC1
#define CMD_DEPLOY  0x10
#define CMD_START   0x11
#define CMD_STOP    0x12
#define CMD_RESET   0x13
#define CMD_READ    0x20
#define CMD_WRITE   0x21
#define CMD_READ_ALARMS 0x22
#define STS_WIRE_DATA  0x20
#define STS_ACK        0x30
#define STS_ERROR      0x40

#define UART_RX_BUF_SIZE 256
static uint8_t uart_rx_buf[UART_RX_BUF_SIZE] __attribute__((aligned(4)));
static uint32_t uart_rx_read_pos;

/* Frame parser state */
#define FP_MAX_PAYLOAD 8192
enum { FP_IDLE, FP_CMD, FP_LEN0, FP_LEN1, FP_PAYLOAD, FP_CRC0, FP_CRC1 };
static uint8_t  fp_state = FP_IDLE;
static uint8_t  fp_cmd;
static uint16_t fp_len;
static uint16_t fp_pos;
static uint16_t fp_crc_rx;
static uint8_t  fp_payload[FP_MAX_PAYLOAD];

static volatile uint8_t engine_running = 0;

#define FLASH_ACR     (*(volatile uint32_t *)0x52002000)

/* IWDG 调试冻结: DBGMCU_APB1FZ2 @ 0x58004C0C, bit6=IWDG1_STOP */
#define DBGMCU_APB1FZ2 (*(volatile uint32_t *)0x58004C0C)
/* 独立看门狗 */
#define IWDG_KR       (*(volatile uint32_t *)0x40003000)
#define IWDG_PR       (*(volatile uint32_t *)0x40003004)
#define IWDG_RLR      (*(volatile uint32_t *)0x40003008)
#define IWDG_SR       (*(volatile uint32_t *)0x4000300C)
#define IWDG_WINR     (*(volatile uint32_t *)0x40003010)
#define IWDG_KEY_RELOAD 0x0000AAAA
#define IWDG_KEY_ENABLE 0x0000CCCC
#define IWDG_KEY_ACCESS 0x00005555

#define SCB_CPACR    (*(volatile uint32_t *)0xE000ED88)
#define SCB_VTOR     (*(volatile uint32_t *)0xE000ED08)
#define SCB_CFSR     (*(volatile uint32_t *)0xE000ED28)
#define SCB_BFAR     (*(volatile uint32_t *)0xE000ED38)
#define DEMCR        (*(volatile uint32_t *)0xE000EDFC)
#define DWT_CTRL     (*(volatile uint32_t *)0xE0001000)
#define DWT_CYCCNT   (*(volatile uint32_t *)0xE0001004)
#define NVIC_ISER0   (*(volatile uint32_t *)0xE000E100)

/* ── SEGGER RTT: 非侵入式 SWD 监测 (pyocd rtt) ── */
#define RTT_CB_ADDR      (DTCM_BASE + 0x8800)
#define RTT_UP0_BUF      (DTCM_BASE + 0x8900)
#define RTT_UP0_BUF_SIZE 1024  /* 原来是 256,10ms 上报频率下不够用 */
#define RTT_DOWN0_BUF    (DTCM_BASE + 0x8A00)
#define RTT_DOWN0_BUF_SIZE 16

typedef struct {
    char     acID[16];              // "SEGGER RTT"
    int      MaxNumUpChannels;      // 1
    int      MaxNumDownChannels;    // 0
    struct {
        const char *sName;          // 通道名指针 (指向字符串常量)
        char    *pBuffer;           // 环形缓冲区指针
        unsigned SizeOfBuffer;      // 缓冲区大小
        unsigned WrOff;             // 写偏移 (目标端)
        unsigned RdOff;             // 读偏移 (主机端)
        unsigned Flags;             // 标志
    } aUp[1];                       // 上行通道 (目标→主机)
} RTT_CB_t;

/* ── 告警系统 ── */

/* 告警码 */
#define ALARM_JITTER_SPIKE   0x01  /* 抖动超阈值 */
#define ALARM_PERIOD_HIGH    0x02  /* 周期过长 (ISR 执行超时风险) */
#define ALARM_ROUTES_CHANGED 0x03  /* 路由数异常变化 */
#define ALARM_ENGINE_STOPPED 0x04  /* engine_running 变 0 */
#define ALARM_SAMPLES_FROZEN 0x05  /* SAMPLES 停滞 */
#define ALARM_IWDG_RESET     0x06  /* 看门狗复位（在 main() 首行检测） */

/* 告警阈值 */
#define ALARM_JITTER_THRESHOLD  500   /* DWT 周期, ~3.7us @136MHz */
#define ALARM_PERIOD_THRESHOLD  15000 /* DWT 周期, ~110us @136MHz (正常 13600) */
#define ALARM_FROZEN_THRESHOLD  5     /* 连续 5 次 SAMPLES 不变判定停滞 */

/* 告警条目: 8 bytes */
typedef struct {
    uint8_t  code;          /* ALARM_* */
    uint8_t  reserved;      /* 填充 */
    uint16_t padding;       /* 填充 */
    uint32_t samples;       /* 触发时的 SAMPLES 值 (时间戳) */
} AlarmEntry_t;

#define TIM1_BASE    0x40010000UL
#define TIM1_CR1     (*(volatile uint32_t *)(TIM1_BASE + 0x00))
#define TIM1_DIER    (*(volatile uint32_t *)(TIM1_BASE + 0x0C))
#define TIM1_SR      (*(volatile uint32_t *)(TIM1_BASE + 0x10))
#define TIM1_PSC     (*(volatile uint32_t *)(TIM1_BASE + 0x28))
#define TIM1_ARR     (*(volatile uint32_t *)(TIM1_BASE + 0x2C))

/* USART2: PD5=TX/PD6=RX, AF7, APB1=68MHz */
#define USART2_BASE    0x40004400UL  /* H723: USART2 @ APB1 */
#define USART2_CR1     (*(volatile uint32_t *)(USART2_BASE + 0x00))
#define USART2_CR2     (*(volatile uint32_t *)(USART2_BASE + 0x04))
#define USART2_CR3     (*(volatile uint32_t *)(USART2_BASE + 0x08))
#define USART2_BRR     (*(volatile uint32_t *)(USART2_BASE + 0x0C))
#define USART2_ISR     (*(volatile uint32_t *)(USART2_BASE + 0x1C))
#define USART2_ICR     (*(volatile uint32_t *)(USART2_BASE + 0x20))
#define USART2_RDR     (*(volatile uint32_t *)(USART2_BASE + 0x24))
#define USART2_TDR     (*(volatile uint32_t *)(USART2_BASE + 0x28))
#define USART2_PRESC   (*(volatile uint32_t *)(USART2_BASE + 0x30))

#define RCC_APB1LENR   (*(volatile uint32_t *)(RCC_BASE + 0xE8))

/* DMA2 Stream 2: USART2_RX �?circular buffer (H7: Stream2�?x40开�? */
/* Stream 2 offset = 0x10 + 2 × 0x18 = 0x40 */
#define DMA2_S2CR     (*(volatile uint32_t *)(DMA2_BASE + 0x40))
#define DMA2_S2NDTR   (*(volatile uint32_t *)(DMA2_BASE + 0x44))
#define DMA2_S2PAR    (*(volatile uint32_t *)(DMA2_BASE + 0x48))
#define DMA2_S2M0AR   (*(volatile uint32_t *)(DMA2_BASE + 0x4C))
#define DMA2_S2FCR    (*(volatile uint32_t *)(DMA2_BASE + 0x54))

/* DMAMUX1 Channel 2 �?USART2_RX (request 35) */
#define DMAMUX1_S2CR  (*(volatile uint32_t *)(DMAMUX1_BASE + 0x08))
#define DMAMUX_REQ_USART2_RX  35

#define TIMEOUT      8000000

/* ═══════════════════════════════════════════════════════════
 * DTCM memory layout (moved to memory_map.h)
 * ═══════════════════════════════════════════════════════════ */

/* ── Debug log area (DTCM 0xD000-0xD300) ── */
#define LOG_BASE         (DTCM_BASE + 0xD000)
#define LOG_BUF          ((volatile uint32_t *)(LOG_BASE + 0x000))
#define LOG_WRAP         128
#define LOG_PERIOD       100                       /* 每 100 周期 (10ms) 记一次 */
#define LOG_COUNT        (*(volatile uint32_t *)(LOG_BASE + 0x2000))  /* 已写入总数 */
#define DEPLOY_MARK      (*(volatile uint32_t *)(LOG_BASE + 0x2004))  /* 0xDEADBEEF = deploy 跑到末尾 */
#define DEPLOY_N         (*(volatile uint32_t *)(LOG_BASE + 0x2008))  /* deploy 设的 N_ROUTES */
#define ROUTE49_CHECK    (*(volatile uint32_t *)(LOG_BASE + 0x200C))  /* 路由 49 的 dword[0..3] */

/* ══════════════════════════════════════════════════════════�?
 * 数据结构 (�?ESP32 完全一�?
 * ══════════════════════════════════════════════════════════�?*/

typedef enum {
    SRC_SENSOR = 0,
    SRC_WIRE   = 1,
    SRC_CONST  = 2
} SourceType_t;

typedef enum {
    DST_WIRE = 3
} OutputType_t;

/* SourceType_t, OutputType_t, RouteEntry_t, ParamEntry_t, StateEntry_t defined in memory_map.h */

/* ══════════════════════════════════════════════════════════�?
 * 原语操作�?
 * ══════════════════════════════════════════════════════════�?*/

enum {
    OP_DIRECT   = 0x00,
    OP_CMP      = 0x01,
    OP_HYST     = 0x02,
    OP_CLAMP    = 0x03,
    OP_LPF      = 0x04,
    OP_PID      = 0x05,
    OP_RATE     = 0x06,
    OP_DEADBAND = 0x07,
    OP_MUX      = 0x08,
    OP_EDGE     = 0x09,
    OP_LUT      = 0x0A,
    OP_CNT      = 0x0B,
    OP_TIMER    = 0x0C,
    OP_SCALE    = 0x0E,
    OP_AND      = 0x0F,
    OP_OR       = 0x10,
    OP_NOT      = 0x11,
    OP_REG      = 0x12,
    OP_ADD      = 0x13,
    OP_SUB      = 0x14,
    OP_MUL      = 0x15,
    OP_DIV      = 0x16,
    OP_BITAND   = 0x17,
    OP_BITOR    = 0x18,
    OP_BITXOR   = 0x19,
    OP_BITNOT   = 0x1A,
    OP_SR       = 0x1B,
    OP_RS       = 0x1C,
    OP_COUNTER  = 0x1D,
    OP_LIMIT    = 0x1E,
    OP_MAX      = 0x1F,
    OP_MIN      = 0x20,
    OP_ABS      = 0x21,
    OP_EQ       = 0x22,
    OP_NE       = 0x23,
};

/* MAX_ROUTES, MAX_PARAMS, MAX_STATES defined in memory_map.h */
#define MAX_SENSORS     64
#define MAX_ACTUATORS   64    /* 支持 actuator_idx 0-63: 0-31=模拟输出, 32-63=数字GPIO */
#define MAX_WIRES       1024
#define MAX_LUT         256
#define ROUTE_ENABLED   0x01

/* ══════════════════════════════════════════════════════════�?
 * 时钟初始�?(VOS0 + 192MHz)
 * ══════════════════════════════════════════════════════════�?*/

static uint32_t hpre_enc(uint32_t d) {
    if (d<=1) return 0; if (d==2) return 8;
    if (d==4) return 9; if (d==8) return 10;
    if (d==16) return 11; return 12;
}
static uint32_t d2ppre_enc(uint32_t d) {
    if (d<=1) return 0; if (d==2) return 4;
    if (d==4) return 5; return 6;
}

/* ── SEGGER RTT 实现 ── */
static inline void rtt_init(void) {
    volatile RTT_CB_t *cb = (volatile RTT_CB_t *)RTT_CB_ADDR;
    /* 清零控制块 */
    for (int i = 0; i < (int)sizeof(RTT_CB_t)/4; i++)
        ((volatile uint32_t *)RTT_CB_ADDR)[i] = 0;
    /* 填充签名 "SEGGER RTT" */
    cb->acID[0]='S'; cb->acID[1]='E'; cb->acID[2]='G'; cb->acID[3]='G';
    cb->acID[4]='E'; cb->acID[5]='R'; cb->acID[6]=' '; cb->acID[7]='R';
    cb->acID[8]='T'; cb->acID[9]='T'; cb->acID[10]=0; cb->acID[11]=0;
    cb->MaxNumUpChannels = 1;
    cb->MaxNumDownChannels = 0;
    /* 上行通道 0: 状态上报 */
    cb->aUp[0].sName = "Status";
    cb->aUp[0].pBuffer = (char *)RTT_UP0_BUF;
    cb->aUp[0].pBuffer = (char *)RTT_UP0_BUF;
    cb->aUp[0].SizeOfBuffer = RTT_UP0_BUF_SIZE;
    cb->aUp[0].WrOff = 0;
    cb->aUp[0].RdOff = 0;
    cb->aUp[0].Flags = 2; /* Mode: Skip (缓冲区满时丢弃) */
}

/* 写数据到上行通道 0, 返回实际写入字节数 */
static inline unsigned rtt_write0(const char *data, unsigned len) {
    volatile RTT_CB_t *cb = (volatile RTT_CB_t *)RTT_CB_ADDR;
    unsigned wrOff = cb->aUp[0].WrOff;
    unsigned rdOff = cb->aUp[0].RdOff;
    unsigned cap = cb->aUp[0].SizeOfBuffer;
    if (cap == 0) return 0;
    /* 计算可用空间 (留 1 字节区分满/空) */
    unsigned avail;
    if (wrOff >= rdOff) {
        avail = cap - 1 - (wrOff - rdOff);
    } else {
        avail = rdOff - wrOff - 1;
    }
    if (len > avail) len = avail;
    if (len == 0) return 0;
    /* 写入 (可能环绕) */
    for (unsigned i = 0; i < len; i++) {
        ((volatile char *)RTT_UP0_BUF)[wrOff] = data[i];
        wrOff = (wrOff + 1) % cap;
    }
    cb->aUp[0].WrOff = wrOff;
    return len;
}

/* 轻量 unsigned int → 十进制字符串, 返回写入长度 */
static inline unsigned u32_to_str(char *buf, uint32_t val) {
    char tmp[10];
    int n = 0;
    if (val == 0) { buf[0] = '0'; return 1; }
    while (val > 0 && n < 10) {
        tmp[n++] = '0' + (val % 10);
        val /= 10;
    }
    for (int i = 0; i < n; i++) buf[i] = tmp[n - 1 - i];
    return (unsigned)n;
}

/* 上报引擎状态到 RTT (在 ISR 中定期调用) */
static inline void rtt_report_status(int32_t gpio_delta) {
    char buf[64];
    char *p = buf;
    *p++ = 'S'; *p++ = '=';
    p += u32_to_str(p, SAMPLES);
    *p++ = ' '; *p++ = 'P'; *p++ = '=';
    p += u32_to_str(p, PERIOD_MIN);
    *p++ = '.'; *p++ = '.';
    p += u32_to_str(p, PERIOD_MAX);
    *p++ = ' '; *p++ = 'R'; *p++ = '=';
    p += u32_to_str(p, *(volatile uint32_t *)N_ROUTES_ADDR);
    *p++ = ' '; *p++ = 'E'; *p++ = '=';
    p += u32_to_str(p, engine_running);
    /* GPIO 输出延迟: >0=DMA已触发(旧值输出), <0=余量周期数 */
    *p++ = ' '; *p++ = 'G'; *p++ = '=';
    if (gpio_delta < 0) { *p++ = '-'; p += u32_to_str(p, (uint32_t)(-gpio_delta)); }
    else { p += u32_to_str(p, (uint32_t)gpio_delta); }
    *p++ = '\n';
    rtt_write0(buf, (unsigned)(p - buf));
}

/* 写告警到环形缓冲区 (ISR 内调用, 1-2 周期) */
static inline void alarm_write(uint8_t code) {
    volatile uint32_t *base = (volatile uint32_t *)ALARM_BUF_ADDR;
    /* base[0] = write_idx, base[1] = overflow_count */
    uint32_t idx = base[0];
    uint32_t entry_ofs = 2 + (idx % ALARM_MAX_ENTRIES) * 2;
    base[entry_ofs]     = ((uint32_t)code << 24) | (SAMPLES & 0xFFFFFF);
    base[entry_ofs + 1] = PERIOD_MAX; /* 记录触发时的抖动峰值 */
    base[0] = (idx + 1) % (ALARM_MAX_ENTRIES * 2); /* wrap 2遍后归零 */
    if (idx >= ALARM_MAX_ENTRIES * 2) base[1]++; /* overflow 计数 */
}

void SystemInit(void) {
    SCB_CPACR |= (0x0F << 20);
    __asm__ volatile("dsb; isb");

    /* ═══════════════════════════════════════════════════════════════
     * 固定频率 PLL — 480MHz, 一次配好, 永不切换
     *
     * 路径: HSI(64M) ÷ 32 = 2M (PLL input, 在 1-2MHz spec 内)
     *            × 24 = 480MHz VCO (VCOSEL=0, 宽范围 192-960MHz)
     *            ÷ 1 = 480MHz SYSCLK
     *            ÷ 2 = 240MHz AHB (RCC_D1CFGR.HPRE)
     *            ÷ 2 = 120MHz APB2 (RCC_D2CFGR.D2PPRE2)
     *
     * Flash: 4WS (rm0468 Table: 480MHz @ VOS0 需要 4 wait states)
     * ═══════════════════════════════════════════════════════════════ */

    /* 1. VOS0 — 写 PWR_D3CR (bit 15:14 = VOS) */
    #define PWR_D3CR  (*(volatile uint32_t *)0x58024818)
    #define PWR_CSR1  (*(volatile uint32_t *)0x58024804)
    PWR_D3CR = (PWR_D3CR & ~(3u << 14)) | (3u << 14);  /* VOS0 = max performance */
    { uint32_t t = TIMEOUT; while (!(PWR_CSR1 & (1u << 14)) && --t) {}  /* ACTVOSRDY */ }

    /* 2. 关 PLL */
    RCC_CR &= ~(1 << 24);
    { uint32_t t = TIMEOUT; while ((RCC_CR & (1 << 25)) && --t) {} }

    /* 3. 配 PLL */
    RCC_PLLCKSELR = (0 << 0) | (5 << 4);        /* HSI=64M, DIVM1=32 → 2MHz PLL input */
    RCC_PLL1DIVR  = (0 << 24) | (0 << 16) | (0 << 9) | (24 << 0); /* DIVP=0, N=24 → 480MHz VCO */
    RCC_PLLCFGR   = (0 << 1) | (1 << 16);       /* VCOSEL=0 (wide), DIVP1EN=1 */
    RCC_CR |= (1 << 24);
    { uint32_t t = TIMEOUT; while (!(RCC_CR & (1 << 25)) && --t) {}  /* PLL1RDY */ }

    /* 4. Flash 4WS + bus dividers */
    FLASH_ACR = 0x00000704;                     /* LATENCY=4 + PRFTEN + ICEN + DCEN */
    __asm__ volatile("dsb; isb"); (void)FLASH_ACR;

    { uint32_t v = *(volatile uint32_t *)(RCC_BASE + 0x18);     /* RCC_D1CFGR */
      v &= ~(0xFu << 0); v |= (8u << 0);                        /* HPRE=/2 → 240MHz AHB */
      *(volatile uint32_t *)(RCC_BASE + 0x18) = v; }

    { uint32_t v = *(volatile uint32_t *)(RCC_BASE + 0x1C);     /* RCC_D2CFGR */
      v &= ~(7u << 7); v |= (4u << 7);                          /* D2PPRE2=/2 → 120MHz APB2 */
      *(volatile uint32_t *)(RCC_BASE + 0x1C) = v; }

    /* 5. Switch to PLL */
    RCC_CFGR = (RCC_CFGR & ~0x07) | 0x03;
    __asm__ volatile("dsb; isb");
    { uint32_t t = TIMEOUT; while (((RCC_CFGR >> 3) & 7) != 3 && --t) {} }

    /* 6. ECC + fault enables */
    /* Cortex-M7 TCM ECC is always-on by hardware (1-bit correct, 2-bit detect). */
    /* Enable fault exceptions so ECC errors/sub-bus faults are caught, not silently ignored. */
    *(volatile uint32_t *)0xE000ED24 = 0x00070000;  /* SCB.SHCSR: USGFAULTENA|BUSFAULTENA|MEMFAULTENA */

    CLOCK_HZ = 480000000;
    TIMER_HZ = 120000000;   /* APB2 = 120MHz (TIM1 timer clock @ 100μs → ARR=11999) */

    *(volatile uint32_t *)(DTCM_BASE + 0x0000) = 0xAA000000;
}

/* ══════════════════════════════════════════════════════════�?
 * 原语实现
 * ══════════════════════════════════════════════════════════�?*/

__attribute__((section(".itcm_code"), always_inline))
static inline uint32_t ccnt(void) { return DWT_CYCCNT; }

__attribute__((section(".itcm_code")))
static inline float execute_primitive(uint8_t op, float src,
    const ParamEntry_t *p, StateEntry_t *s, float dt)
{
    switch (op) {

    case OP_DIRECT:
        return src;

    case OP_SCALE:
        return p->value_a * src + p->value_b;

    case OP_CLAMP: {
        float clamped;
        if (src < p->value_a) clamped = p->value_a;
        else if (src > p->value_b) clamped = p->value_b;
        else clamped = src;
        /* 如果�?state (PID CLAMP), 将限幅值写�?state_c �?PID 积分限幅 */
        if (s->state_c == 0.0f && p->value_b > 0.0f)
            s->state_c = p->value_b;
        return clamped;
    }

    case OP_CMP: {
        int mode = (int)p->value_b;
        if (mode == 0) return (src >  p->value_a) ? 1.0f : 0.0f;
        if (mode == 1) return (src >= p->value_a) ? 1.0f : 0.0f;
        if (mode == 2) return (src <  p->value_a) ? 1.0f : 0.0f;
        if (mode == 3) return (src <= p->value_a) ? 1.0f : 0.0f;
        return 0.0f;
    }

    case OP_HYST: {
        if (src > p->value_a) s->state_a = 1.0f;
        else if (src < p->value_b) s->state_a = 0.0f;
        return s->state_a;
    }

    case OP_LPF: {
        float alpha = p->value_a;
        float out = s->state_a * (1.0f - alpha) + src * alpha;
        s->state_a = out;
        return out;
    }

    case OP_PID: {
        /* value_d=SP, value_a=KP, value_b=Ki*dt, value_c=Kd/dt
         * state_a = 积分累积�?
         * state_b = 上次误差 (用于微分)
         * state_c = 积分限幅�?(由编译器�?CLAMP 路由设置) */
        float err = p->value_d - src;
        float i_limit = (s->state_c != 0.0f) ? s->state_c : 100.0f;
        float acc = s->state_a + p->value_b * err;
        acc = (acc >  i_limit) ?  i_limit : acc;
        acc = (acc < -i_limit) ? -i_limit : acc;
        s->state_a = acc;
        float out = p->value_a * err + acc
                  + p->value_c * (err - s->state_b);  /* D: Kd/dt × Δerr */
        s->state_b = err;
        return out;
    }

    case OP_RATE: {
        float rate = (src - s->state_a) / dt;
        s->state_a = src;
        return rate;
    }

    case OP_EDGE: {
        float prev = s->state_a;
        s->state_a = src;
        int edge_type = (int)p->value_a;
        if (edge_type == 0) return (prev <= 0.5f && src > 0.5f) ? 1.0f : 0.0f;
        if (edge_type == 1) return (prev > 0.5f && src <= 0.5f) ? 1.0f : 0.0f;
        return (prev != src) ? 1.0f : 0.0f;
    }

    case OP_CNT: {
        float prev = s->state_b;
        s->state_b = src;
        if (prev <= p->value_a && src > p->value_a)
            s->state_a += 1.0f;
        return s->state_a;
    }

    case OP_DEADBAND: {
        float diff = src - s->state_a;
        if (diff < -p->value_a || diff > p->value_a) {
            s->state_a = src;
            return src;
        }
        return s->state_a;
    }

    case OP_AND:
        return ((src > 0.5f) && (WIRE_MAP[(int)p->value_a] > 0.5f)) ? 1.0f : 0.0f;

    case OP_OR:
        return ((src > 0.5f) || (WIRE_MAP[(int)p->value_a] > 0.5f)) ? 1.0f : 0.0f;

    case OP_NOT:
        return (src > 0.5f) ? 0.0f : 1.0f;

    case OP_ADD:
        return src + WIRE_MAP[(int)p->value_a];

    case OP_SUB:
        return src - WIRE_MAP[(int)p->value_a];

    case OP_MUL:
        return src * WIRE_MAP[(int)p->value_a];

    case OP_DIV: {
        float b = WIRE_MAP[(int)p->value_a];
        return (b != 0.0f) ? src / b : 0.0f;
    }

    case OP_REG:
        s->state_a = src;
        return s->state_a;

    case OP_TIMER: {
        int mode    = (int)p->value_a;
        float pt    = p->value_b;
        int out_sel = (int)p->value_c;
        int prev_in = (s->state_b > 0.5f);
        int cur_in  = (src > 0.5f);
        s->state_b  = src;
        int fsm     = (int)s->state_a;
        float et    = s->state_c;
        float q     = 0.0f;
        float dt_ms = dt * 1000.0f;
        if (mode == 0) {
            if (cur_in) {
                if (fsm == 0 && !prev_in) { fsm = 1; et = 0.0f; }
                if (fsm == 1) { et += dt_ms; if (et >= pt) { et = pt; fsm = 2; } }
                q = (fsm == 2) ? 1.0f : 0.0f;
            } else { fsm = 0; et = 0.0f; q = 0.0f; }
        } else if (mode == 1) {
            if (cur_in) { fsm = 0; et = 0.0f; q = 1.0f; }
            else {
                if (prev_in && !cur_in) { fsm = 1; et = 0.0f; }
                if (fsm == 1) { et += dt_ms; if (et >= pt) { et = pt; fsm = 2; } else q = 1.0f; }
            }
        } else {
            if (!prev_in && cur_in) { fsm = 1; et = 0.0f; }
            if (fsm == 1) { et += dt_ms; if (et >= pt) { et = pt; fsm = 2; q = 0.0f; } else q = 1.0f; }
            if (fsm == 2 && !cur_in) { fsm = 0; et = 0.0f; }
        }
        s->state_a = (float)fsm; s->state_c = et;
        return (out_sel == 0) ? q : et;
    }

    case OP_COUNTER: {
        int mode    = (int)p->value_a;
        float pv    = p->value_b;
        int out_sel = (int)p->value_c;
        float aux   = WIRE_MAP[(int)p->value_d];
        float prev  = s->state_b; s->state_b = src;
        float cv    = s->state_a; float thr = 0.5f;
        if (mode == 0) {
            if (aux > thr) cv = 0.0f;
            else if (prev <= thr && src > thr && cv < pv) cv += 1.0f;
        } else if (mode == 1) {
            if (aux > thr) cv = pv;
            else if (prev > thr && src <= thr && cv > 0.0f) cv -= 1.0f;
        } else {
            float cd  = WIRE_MAP[(int)p->value_d];
            float r   = aux;  /* R=aux, same as CTU/CTD reset signal */
            int prev_cd = (s->state_d > thr); int cur_cd = (cd > thr);
            s->state_d = cd;
            if (r > thr) cv = 0.0f;
            else {
                if (prev <= thr && src > thr && !(prev_cd <= thr && cur_cd > thr) && cv < pv) cv += 1.0f;
                if (prev_cd <= thr && cur_cd > thr && !(prev <= thr && src > thr) && cv > 0.0f) cv -= 1.0f;
            }
        }
        s->state_a = cv;
        if (out_sel == 1) return (cv >= pv) ? 1.0f : 0.0f;
        if (out_sel == 2) return (cv <= 0.0f) ? 1.0f : 0.0f;
        return cv;
    }

    case OP_MUX: {
        return (src > 0.5f) ? WIRE_MAP[(int)p->value_b] : WIRE_MAP[(int)p->value_a];
    }

    case OP_SR: {
        float r = WIRE_MAP[(int)p->value_a]; float q1 = s->state_a;
        if (src > 0.5f) q1 = 1.0f; else if (r > 0.5f) q1 = 0.0f;
        s->state_a = q1; return q1;
    }

    case OP_RS: {
        float r1 = WIRE_MAP[(int)p->value_a]; float q1 = s->state_a;
        if (r1 > 0.5f) q1 = 0.0f; else if (src > 0.5f) q1 = 1.0f;
        s->state_a = q1; return q1;
    }

    case OP_BITAND:
        return (float)((uint32_t)src & (uint32_t)WIRE_MAP[(int)p->value_a]);

    case OP_BITOR:
        return (float)((uint32_t)src | (uint32_t)WIRE_MAP[(int)p->value_a]);

    case OP_BITXOR:
        return (float)((uint32_t)src ^ (uint32_t)WIRE_MAP[(int)p->value_a]);

    case OP_BITNOT:
        return (float)(~(uint32_t)src);

    case OP_LIMIT:
        /* LIMIT(lo, src, hi): value_a=lo, value_b=hi */
        if (src < p->value_a) return p->value_a;
        if (src > p->value_b) return p->value_b;
        return src;

    case OP_MAX:
        /* MAX(src, wire_b): value_a=wire_b index */
        { float b = WIRE_MAP[(int)p->value_a];
          return (src > b) ? src : b; }

    case OP_MIN:
        /* MIN(src, wire_b): value_a=wire_b index */
        { float b = WIRE_MAP[(int)p->value_a];
          return (src < b) ? src : b; }

    case OP_ABS:
        return (src < 0.0f) ? -src : src;

    case OP_EQ:
        /* EQ(src, threshold): value_a=threshold */
        return (src == p->value_a) ? 1.0f : 0.0f;

    case OP_NE:
        /* NE(src, threshold): value_a=threshold */
        return (src != p->value_a) ? 1.0f : 0.0f;

    default:
        return src;
    }
}

/* ══════════════════════════════════════════════════════════�?
 * CANopen: FDCAN1驱动 + NMT状态机 + 心跳
 * ══════════════════════════════════════════════════════════�?*/

/* 标准CAN帧发�?(11-bit ID) */
static int can_send(uint32_t id, uint8_t *data, uint8_t len) {
    if (!(FDCAN1_TXFQS & (1<<5))) return -1; /* TX FIFO�?*/
    uint32_t *ram = (uint32_t *)(FDCAN1_MSGRAM + FDCAN1_TX_FIFO_OFFSET);
    uint32_t w0 = (id << 18) | (len << 16) | (1<<15); /* XTD=0, ESI=0, FDF=0 */
    uint32_t w1 = (data[0]<<24) | (data[1]<<16) | (data[2]<<8) | data[3];
    ram[0] = w0; ram[1] = w1; /* �?字节 */
    if (len > 4) {
        uint32_t w2 = (data[4]<<24)|(data[5]<<16)|(data[6]<<8)|data[7];
        ram[2] = w2;
    }
    FDCAN1_TXBAR = (1 << 0); /* 请求发�?*/
    return 0;
}

/* CAN接收 (非阻�? 有消息返回长�? 无消息返�?) */
static int can_recv(uint32_t *id, uint8_t *data) {
    if (!(FDCAN1_RXF0S & 1)) return 0; /* 无消�?*/
    uint32_t *ram = (uint32_t *)(FDCAN1_MSGRAM + FDCAN1_RX_FIFO0_OFFSET);
    uint32_t w0 = ram[0], w1 = ram[1];
    *id = (w0 >> 18) & 0x7FF;
    uint8_t len = (w0 >> 16) & 0x0F;
    data[0] = w1 >> 24; data[1] = w1 >> 16; data[2] = w1 >> 8; data[3] = w1;
    if (len > 4) { uint32_t w2 = ram[2];
        data[4]=w2>>24; data[5]=w2>>16; data[6]=w2>>8; data[7]=w2; }
    FDCAN1_RXF0A = 0; /* 释放FIFO */
    return len;
}

/* CANopen公共状�?*/
static uint32_t canopen_ticks; /* 1ms计数�?*/

/* 发送CANopen心跳 */
static void canopen_send_heartbeat(void) {
    uint8_t d[1] = { canopen_state };
    can_send(COB_HEARTBEAT + NODE_ID, d, 1);
}

/* 处理NMT命令 */
static void canopen_handle_nmt(uint8_t *data) {
    uint8_t cmd = data[0], node = data[1];
    if (node != 0 && node != NODE_ID) return;
    switch (cmd) {
    case 1:  canopen_state = NMT_OPERATIONAL;  break;
    case 2:  canopen_state = NMT_STOPPED;      break;
    case 128: canopen_state = NMT_PREOP;       break;
    case 129: canopen_state = NMT_INITIALISING; break;
    case 130: canopen_state = NMT_INITIALISING; break; /* Reset communication */
    }
}

/* 处理SDO请求 (仅Expedited, 最�?字节) */
static void canopen_handle_sdo(uint32_t cob_id, uint8_t *data) {
    uint8_t rsp[8] = {0};
    uint16_t idx = (data[1] << 8) | data[2];
    uint8_t  sub = data[3];
    /* 读对象字�?*/
    if ((data[0] & 0xE0) == 0x40) { /* SDO Read */
        rsp[0] = 0x4F | ((data[0] & 3) << 2); /* expedited */
        rsp[1] = data[1]; rsp[2] = data[2]; rsp[3] = sub;
        if (idx == OD_DEVICE_TYPE && sub == 0) {
            uint32_t v = 0x00010191; /* CiA401 IO Device */
            rsp[4]=v; rsp[5]=v>>8; rsp[6]=v>>16; rsp[7]=v>>24;
        } else if (idx == OD_HEARTBEAT_TIME && sub == 0) {
            rsp[4]=canopen_hb_period; rsp[5]=canopen_hb_period>>8;
        } else { rsp[0] = 0x80; /* 错误: 对象不存�?*/ }
    } else { rsp[0] = 0x80; /* 不支持的命令 */ }
    can_send(COB_TSDO + NODE_ID, rsp, 8);
}

/* CANopen主处�?(在主循环中调�? 非ISR!) */
static void canopen_poll(void) {
    uint32_t cob_id; uint8_t data[8];
    int len = can_recv(&cob_id, data);
    if (len > 0) {
        if (cob_id == COB_NMT) {
            canopen_handle_nmt(data);
        } else if ((cob_id & 0xFF80) == (COB_RSDO & 0xFF80)) {
            if (len >= 4) canopen_handle_sdo(cob_id, data);
        }
    }
}

/* FDCAN1 初始�?(500kbps @ 68MHz APB1) */
static void fdcan_init(void) {
    /* �?INIT + CCE 位进入配置模�?*/
    FDCAN1_CCCR = FDCAN_CCCR_INIT | FDCAN_CCCR_CCE;
    { uint32_t t=8000000; while(!(FDCAN1_CCCR & FDCAN_CCCR_INIT)&&--t){} }

    /* 位时�? 500kbps @ 68MHz �?68M/500k=136 tq
     * NTSEG1=105, NTSEG2=20, NSJW=10 �?136 tq = 500kbps */
    FDCAN1_NBTP = (105 << 16) | (20 << 8) | (10 << 0) | (1<<25); /* NBRP=0 */
    FDCAN1_DBTP = 0; /* 经典CAN, 不用FD */

    /* 退出配置模�?*/
    FDCAN1_CCCR = 0;
    { uint32_t t=8000000; while((FDCAN1_CCCR & FDCAN_CCCR_INIT)&&--t){} }

    /* 配置RX FIFO0 */
    FDCAN1_RXF0C = (1<<31) | (FDCAN1_RX_FIFO0_OFFSET / 4); /* 4个元�?*/
    FDCAN1_RXF0A = 0;

    /* 配置TX FIFO */
    FDCAN1_TXBC = (FDCAN1_TX_FIFO_OFFSET / 4); /* 4个元�?*/

    /* 全局滤波�? 接收所有消�?*/
    uint32_t *sram = (uint32_t *)FDCAN1_MSGRAM;
    sram[0x800/4] = 0; /* 标准滤波 */
    sram[0x800/4 + 1] = (1<<27); /* Store in FIFO0 */

    /* 启动: 启用中断 (RX FIFO0 new message) */
    FDCAN1_IE = (1 << 0); /* RF0NE */
    FDCAN1_ILS = 0; /* Line 0 */
    FDCAN1_ILE = (1 << 0);
}

/* ══════════════════════════════════════════════════════════�?
 * UART 通信: 帧协�?+ 命令处理
 *   PC→H723: [0xC0] [CMD] [LEN:2B LE] [PAYLOAD] [CRC16:2B LE]
 *   H723→PC: [0xC1] [STS] [LEN:2B LE] [PAYLOAD] [CRC16:2B LE]
 *   CRC-16/CCITT: poly=0x1021, init=0xFFFF, covers CMD+LEN+PAYLOAD
 * ══════════════════════════════════════════════════════════�?*/

/* ── CRC-16/CCITT ── */
static uint16_t crc16_ccitt_update(uint16_t crc, const uint8_t *data, uint32_t len) {
    for (uint32_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (int j = 0; j < 8; j++) {
            if (crc & 0x8000)
                crc = (crc << 1) ^ 0x1021;
            else
                crc <<= 1;
        }
    }
    return crc;
}

static uint16_t crc16_ccitt(const uint8_t *data, uint32_t len) {
    return crc16_ccitt_update(0xFFFF, data, len);
}

/* ── USART2 初始�?── */
static void usart2_init(void) {
    /* 使能 GPIOD 时钟 (AHB4) */
    RCC_AHB4ENR |= (1 << 3);  /* GPIODEN */
    __asm__ volatile("dsb");

    /* PD5=USART2_TX (AF7), PD6=USART2_RX (AF7) */
    GPIOD_MODER &= ~((3u << 10) | (3u << 12));   /* 清除 PD5/PD6 模式 */
    GPIOD_MODER |=  (2u << 10) | (2u << 12);     /* AF 模式 */
    GPIOD_AFRL &= ~((0xFu << 20) | (0xFu << 24)); /* 清除 AF */
    GPIOD_AFRL |=  (7u << 20) | (7u << 24);       /* AF7 = USART2 */

    /* 使能 USART2 时钟 */
    RCC_APB1LENR |= (1 << 17);  /* USART2EN */
    __asm__ volatile("dsb");

    /* 禁用 USART2 */
    USART2_CR1 &= ~(1u << 0);   /* UE=0 */

    /* 配置: 8N1, 过采�?6, 收发使能 */
    USART2_CR1 = (1u << 3) | (1u << 2);   /* TE=1, RE=1, UE 暂不使能 */
    USART2_CR2 = 0;                        /* 1 停止�?*/
    USART2_CR3 = (1u << 6);                /* DMAR=1 (DMA 接收) */
    USART2_PRESC = 0;                      /* 无预分频 */

    /* BRR: APB1=68MHz, 115200bps �?USARTDIV=590.25, BRR=(590<<4)|4 */
    USART2_BRR = (590u << 4) | 4u;

    /* 使能 USART2 */
    USART2_CR1 |= (1u << 0);   /* UE=1 */

    /* ── DMA2 Stream 2: USART2_RDR �?uart_rx_buf (循环模式) ── */
    DMAMUX1_S2CR = DMAMUX_REQ_USART2_RX;

    DMA2_S2CR = 0;   /* 先禁�?*/
    { uint32_t tout = TIMEOUT; while ((DMA2_S2CR & 1) && --tout) {} }

    DMA2_S2PAR  = (uint32_t)&USART2_RDR;
    DMA2_S2M0AR = (uint32_t)uart_rx_buf;
    DMA2_S2NDTR = UART_RX_BUF_SIZE;
    DMA2_S2FCR  = (1u << 2);  /* 直通模�?*/

    /* CR: P→M, 循环, MINC, 8bit/8bit, 高优先级 */
    DMA2_S2CR = (1u << 8) |    /* CIRC */
                (1u << 10) |   /* MINC: 内存地址递增 */
                (2u << 16);    /* PL=�?*/
    __asm__ volatile("dsb");
    DMA2_S2CR |= 1;   /* EN */
    __asm__ volatile("dsb; isb");

    uart_rx_read_pos = 0;
}

/* ── UART TX (轮询) ── */
static void uart_send_bytes(const uint8_t *data, uint32_t len) {
    for (uint32_t i = 0; i < len; i++) {
        while (!(USART2_ISR & (1u << 7))) {}  /* 等待 TXE */
        USART2_TDR = data[i];
    }
    while (!(USART2_ISR & (1u << 6))) {}  /* 等待 TC */
}

/* ── 发送状态帧 ── */
static void uart_send_status(uint8_t sts, const uint8_t *payload, uint16_t len) {
    uint8_t hdr[4] = { FRAME_STS, sts, len & 0xFF, (len >> 8) & 0xFF };
    uart_send_bytes(hdr, 4);
    if (len > 0 && payload)
        uart_send_bytes(payload, len);
    /* CRC 覆盖 STS+LEN+PAYLOAD */
    uint16_t crc = crc16_ccitt(&hdr[1], 3);
    if (len > 0 && payload)
        crc = crc16_ccitt_update(crc, payload, len);
    uint8_t crc_buf[2] = { crc & 0xFF, (crc >> 8) & 0xFF };
    uart_send_bytes(crc_buf, 2);
}

/* ── 停止 ISR 引擎 (安全) ── */
static void engine_stop(void) {
    *(volatile uint16_t *)(TIM1_BASE + 0x0C) &= ~(1u << 0); /* 禁用 UIE */
    *(volatile uint16_t *)(TIM1_BASE + 0x10) = ~(uint16_t)0; /* 清除所有 SR 标志 */
    *(volatile uint16_t *)(TIM1_BASE + 0x00) = 0;            /* CEN=0 */
    engine_running = 0;
}

static void engine_start(void) {
    EXEC_MIN = 0xFFFFFFFF; EXEC_MAX = 0;
    SAMPLES = 0; LAST_ENTRY = 0;
    *(volatile uint16_t *)(TIM1_BASE + 0x0C) = (1u << 0) | (1u << 9); /* UIE + UDE */
    __asm__ volatile("dsb");
    *(volatile uint16_t *)(TIM1_BASE + 0x00) = 1;  /* CEN=1 */
    engine_running = 1;
}

/* ── 命令处理 ── */
static void handle_deploy(const uint8_t *payload, uint16_t len) {
    /* 新格式: [ProgramHeader:16B] [routes: n_routes×16B] [params: n_params×16B] [states: n_states×16B]
     * ProgramHeader: [magic:4][format:4][n_routes:2][n_params:2][n_states:2][reserved:2] */
    if (len < 24) {
        const char *e = "DEPLOY: too short";
        uart_send_status(STS_ERROR, (const uint8_t *)e, 17);
        return;
    }

    uint32_t magic = payload[0] | (payload[1]<<8) | (payload[2]<<16) | (payload[3]<<24);
    if (magic != PROGRAM_MAGIC_VALID) {
        const char *e = "DEPLOY: bad magic";
        uart_send_status(STS_ERROR, (const uint8_t *)e, 17);
        return;
    }

    uint16_t n_routes = payload[8] | (payload[9] << 8);
    uint16_t n_params = payload[10] | (payload[11] << 8);
    uint16_t n_states = payload[12] | (payload[13] << 8);

    if (n_routes > MAX_ROUTES || n_params > MAX_PARAMS || n_states > MAX_STATES) {
        const char *e = "DEPLOY: too many";
        uart_send_status(STS_ERROR, (const uint8_t *)e, 16);
        return;
    }

    uint32_t expected = 16 + (uint32_t)n_routes * 16 + (uint32_t)n_params * 16 + (uint32_t)n_states * 16;
    if (len < expected) {
        const char *e = "DEPLOY: incomplete";
        uart_send_status(STS_ERROR, (const uint8_t *)e, 18);
        return;
    }

    /* 停止引擎 */
    engine_stop();

    /* 清零所有表 */
    for (int i = 0; i < MAX_ROUTES; i++) ROUTE_TABLE[i].flags = 0;
    for (int i = 0; i < MAX_PARAMS; i++) {
        PARAM_TABLE[i].value_a = 0.0f;
        PARAM_TABLE[i].value_b = 0.0f;
        PARAM_TABLE[i].value_c = 0.0f;
        PARAM_TABLE[i].value_d = 0.0f;
    }
    for (int i = 0; i < MAX_STATES; i++) {
        STATE_TABLE[i].state_a = 0.0f;
        STATE_TABLE[i].state_b = 0.0f;
        STATE_TABLE[i].state_c = 0.0f;
        STATE_TABLE[i].state_d = 0.0f;
    }
    for (int i = 0; i < MAX_WIRES; i++)     WIRE_MAP[i] = 0.0f;
    for (int i = 0; i < MAX_ACTUATORS; i++) ACTUATOR_STATUS[i] = 0.0f;

    /* 拷贝路由�?*/
    const uint8_t *rt = &payload[16];
    for (uint16_t i = 0; i < n_routes; i++) {
        const uint8_t *s = &rt[i * 16];
        ROUTE_TABLE[i].src_type     = s[0];
        ROUTE_TABLE[i].src_index    = s[1];
        ROUTE_TABLE[i].dst_type     = s[2];
        ROUTE_TABLE[i].dst_channel  = s[3];
        ROUTE_TABLE[i].op           = s[4];
        ROUTE_TABLE[i].flags        = s[5];
        ROUTE_TABLE[i].param_idx    = s[6] | (s[7] << 8);
        ROUTE_TABLE[i].state_offset = s[8] | (s[9] << 8);
        ROUTE_TABLE[i].actuator_idx = s[10] | (s[11] << 8);
        ROUTE_TABLE[i].wire2_idx    = s[12] | (s[13] << 8);
    }

    /* copy param table */
    const uint8_t *pt = &rt[n_routes * 16];
    for (uint16_t i = 0; i < n_params; i++) {
        volatile uint8_t *d = (volatile uint8_t *) &PARAM_TABLE[i];
        const uint8_t *s = &pt[i * 16];
        for (int j = 0; j < 16; j++) d[j] = s[j];
    }

    /* copy state table */
    const uint8_t *st = &pt[n_params * 16];
    for (uint16_t i = 0; i < n_states; i++) {
        volatile uint8_t *d = (volatile uint8_t *) &STATE_TABLE[i];
        const uint8_t *s = &st[i * 16];
        for (int j = 0; j < 16; j++) d[j] = s[j];
    }

    *(volatile uint32_t *)N_ROUTES_ADDR = n_routes;
    *(volatile uint32_t *)N_PARAMS_ADDR = n_params;
    *(volatile uint32_t *)N_STATES_ADDR = n_states;
    *(volatile uint32_t *)PROGRAM_MAGIC_ADDR = PROGRAM_MAGIC_VALID;

    /* ACK: 返回 n_routes + n_params */
    uint8_t ack[4] = { n_routes & 0xFF, (n_routes >> 8) & 0xFF,
                       n_params & 0xFF, (n_params >> 8) & 0xFF };
    uart_send_status(STS_ACK, ack, 4);

    /* DEPLOY 完自动启动引擎 (省去 PC 再发 START) */
    EXEC_MIN = 0xFFFFFFFF; EXEC_MAX = 0;
    SAMPLES = 0; LAST_ENTRY = 0;
    engine_start();
}

static void handle_start(void) {
    if (engine_running) {
        const char *e = "ALREADY_RUNNING";
        uart_send_status(STS_ACK, (const uint8_t *)e, 15);
        return;
    }
    EXEC_MIN = 0xFFFFFFFF; EXEC_MAX = 0;
    SAMPLES = 0; LAST_ENTRY = 0;

    *(volatile uint16_t *)(TIM1_BASE + 0x0C) = (1u << 0) | (1u << 9); /* UIE + UDE */
    __asm__ volatile("dsb");
    *(volatile uint16_t *)(TIM1_BASE + 0x00) = 1;  /* CEN=1 */
    engine_running = 1;

    const char *e = "STARTED";
    uart_send_status(STS_ACK, (const uint8_t *)e, 7);
}

static void handle_stop(void) {
    engine_stop();
    const char *e = "STOPPED";
    uart_send_status(STS_ACK, (const uint8_t *)e, 7);
}

static void handle_reset(void) {
    engine_stop();

    for (int i = 0; i < MAX_WIRES; i++)     WIRE_MAP[i] = 0.0f;
    for (int i = 0; i < MAX_STATES; i++) {
        STATE_TABLE[i].state_a = 0.0f;
        STATE_TABLE[i].state_b = 0.0f;
        STATE_TABLE[i].state_c = 0.0f;
        STATE_TABLE[i].state_d = 0.0f;
    }
    for (int i = 0; i < MAX_ACTUATORS; i++) ACTUATOR_STATUS[i] = 0.0f;

    /* 安全状�? 所有输出归�?*/
    *(volatile uint16_t *)(TIM1_BASE + 0x34) = 0;
    *(volatile uint16_t *)(TIM1_BASE + 0x38) = 0;
    *(volatile uint16_t *)(TIM1_BASE + 0x3C) = 0;
    GPIOE_ODR = 0;

    EXEC_MIN = 0xFFFFFFFF; EXEC_MAX = 0;
    SAMPLES = 0; LAST_ENTRY = 0; HEARTBEAT = 0;

    const char *e = "RESET";
    uart_send_status(STS_ACK, (const uint8_t *)e, 5);
}

static void handle_read(const uint8_t *payload, uint16_t len) {
    if (len < 4) {
        const char *e = "READ: need start,count";
        uart_send_status(STS_ERROR, (const uint8_t *)e, 22);
        return;
    }
    uint16_t start = payload[0] | (payload[1] << 8);
    uint16_t count = payload[2] | (payload[3] << 8);

    if (start + count > MAX_WIRES) count = MAX_WIRES - start;
    if (count > 128) count = 128;  /* 最�?28个WIRE (512字节) */

    /* 响应: [start:2B] [count:2B] [wire0:4B] ... [wireN:4B] */
    uint16_t resp_len = 4 + count * 4;
    static uint8_t resp_buf[516];
    resp_buf[0] = start & 0xFF;
    resp_buf[1] = (start >> 8) & 0xFF;
    resp_buf[2] = count & 0xFF;
    resp_buf[3] = (count >> 8) & 0xFF;

    for (uint16_t i = 0; i < count; i++) {
        float v = WIRE_MAP[start + i];
        uint8_t *f = (uint8_t *)&v;
        resp_buf[4 + i * 4 + 0] = f[0];
        resp_buf[4 + i * 4 + 1] = f[1];
        resp_buf[4 + i * 4 + 2] = f[2];
        resp_buf[4 + i * 4 + 3] = f[3];
    }

    uart_send_status(STS_WIRE_DATA, resp_buf, resp_len);
}

static void handle_read_alarms(const uint8_t *payload, uint16_t len) {
    /* 返回告警缓冲区内容: [write_idx:4B] [overflow:4B] [entries...] */
    volatile uint32_t *base = (volatile uint32_t *)ALARM_BUF_ADDR;
    uint32_t write_idx = base[0];
    uint32_t overflow  = base[1];

    uint8_t resp[8 + ALARM_MAX_ENTRIES * 8];
    resp[0] = write_idx & 0xFF; resp[1] = (write_idx >> 8) & 0xFF;
    resp[2] = (write_idx >> 16) & 0xFF; resp[3] = (write_idx >> 24) & 0xFF;
    resp[4] = overflow & 0xFF; resp[5] = (overflow >> 8) & 0xFF;
    resp[6] = (overflow >> 16) & 0xFF; resp[7] = (overflow >> 24) & 0xFF;

    /* 只返回最新的 32 条 (256 字节), 避免帧过大 */
    uint32_t n = (write_idx < 32) ? write_idx : 32;
    for (uint32_t i = 0; i < n; i++) {
        uint32_t idx = (write_idx - n + i) % ALARM_MAX_ENTRIES;
        uint32_t e = base[2 + idx * 2];
        uint32_t v = base[2 + idx * 2 + 1];
        resp[8 + i * 8 + 0] = e & 0xFF;
        resp[8 + i * 8 + 1] = (e >> 8) & 0xFF;
        resp[8 + i * 8 + 2] = (e >> 16) & 0xFF;
        resp[8 + i * 8 + 3] = (e >> 24) & 0xFF;
        resp[8 + i * 8 + 4] = v & 0xFF;
        resp[8 + i * 8 + 5] = (v >> 8) & 0xFF;
        resp[8 + i * 8 + 6] = (v >> 16) & 0xFF;
        resp[8 + i * 8 + 7] = (v >> 24) & 0xFF;
    }
    uint16_t resp_len = 8 + n * 8;
    uart_send_status(0x50, resp, resp_len); /* STS_ALARM_DATA = 0x50 */
}

static void handle_write(const uint8_t *payload, uint16_t len) {
    if (len < 6) {
        const char *e = "WRITE: need idx,value";
        uart_send_status(STS_ERROR, (const uint8_t *)e, 21);
        return;
    }
    uint16_t idx = payload[0] | (payload[1] << 8);
    float value;
    uint8_t *f = (uint8_t *)&value;
    f[0] = payload[2]; f[1] = payload[3]; f[2] = payload[4]; f[3] = payload[5];

    if (idx >= MAX_WIRES) {
        const char *e = "WRITE: out of range";
        uart_send_status(STS_ERROR, (const uint8_t *)e, 19);
        return;
    }

    WIRE_MAP[idx] = value;

    /* ACK: 回显写入�?*/
    uint8_t ack[6];
    ack[0] = idx & 0xFF; ack[1] = (idx >> 8) & 0xFF;
    ack[2] = f[0]; ack[3] = f[1]; ack[4] = f[2]; ack[5] = f[3];
    uart_send_status(STS_ACK, ack, 6);
}

static void uart_handle_command(uint8_t cmd, const uint8_t *payload, uint16_t len) {
    switch (cmd) {
    case CMD_DEPLOY: handle_deploy(payload, len); break;
    case CMD_START:  handle_start(); break;
    case CMD_STOP:   handle_stop(); break;
    case CMD_RESET:  handle_reset(); break;
    case CMD_READ:   handle_read(payload, len); break;
    case CMD_WRITE:  handle_write(payload, len); break;
    case CMD_READ_ALARMS: handle_read_alarms(payload, len); break;
    default: {
        const char *e = "UNKNOWN CMD";
        uart_send_status(STS_ERROR, (const uint8_t *)e, 11);
        break;
    }
    }
}

/* ── 帧解析器 (处理单个字节) ── */
static void uart_process_byte(uint8_t b) {
    switch (fp_state) {
    case FP_IDLE:
        if (b == FRAME_CMD)
            fp_state = FP_CMD;
        break;
    case FP_CMD:
        fp_cmd = b;
        fp_len = 0; fp_pos = 0;
        fp_state = FP_LEN0;
        break;
    case FP_LEN0:
        fp_len = b;
        fp_state = FP_LEN1;
        break;
    case FP_LEN1:
        fp_len |= ((uint16_t)b << 8);
        if (fp_len == 0)
            fp_state = FP_CRC0;
        else if (fp_len > FP_MAX_PAYLOAD)
            fp_state = FP_IDLE;  /* 过大, 丢弃 */
        else
            fp_state = FP_PAYLOAD;
        break;
    case FP_PAYLOAD:
        fp_payload[fp_pos++] = b;
        if (fp_pos >= fp_len)
            fp_state = FP_CRC0;
        break;
    case FP_CRC0:
        fp_crc_rx = b;
        fp_state = FP_CRC1;
        break;
    case FP_CRC1:
        fp_crc_rx |= ((uint16_t)b << 8);
        {
            /* 校验 CRC: 覆盖 CMD+LEN+PAYLOAD */
            uint8_t hdr[3] = { fp_cmd, fp_len & 0xFF, (fp_len >> 8) & 0xFF };
            uint16_t crc = crc16_ccitt(hdr, 3);
            if (fp_len > 0)
                crc = crc16_ccitt_update(crc, fp_payload, fp_len);
            if (crc == fp_crc_rx)
                uart_handle_command(fp_cmd, fp_payload, fp_len);
        }
        fp_state = FP_IDLE;
        break;
    }
}

/* ── UART 轮询 (在主循环中调�? ──
 * 限制每次调用最多处�?4字节, 防止RX悬空时DMA持续收噪声导致死循环�?
 * 帧解析状态机会自动忽略非0xC0开头的垃圾字节�?
 */
#define UART_POLL_LIMIT  64
static void uart_poll(void) {
    uint32_t dma_pos = UART_RX_BUF_SIZE - DMA2_S2NDTR;
    uint32_t cnt = 0;
    while (uart_rx_read_pos != dma_pos && cnt < UART_POLL_LIMIT) {
        uart_process_byte(uart_rx_buf[uart_rx_read_pos]);
        uart_rx_read_pos = (uart_rx_read_pos + 1) % UART_RX_BUF_SIZE;
        dma_pos = UART_RX_BUF_SIZE - DMA2_S2NDTR;
        cnt++;
    }
}

/* ══�?FDCAN1 中断处理 (安全兜底, 防止 Default_Handler 死循�? ══�?*/
__attribute__((section(".itcm_code")))
void FDCAN1_IT0_IRQHandler(void) {
    /* 清除所�?FDCAN 中断标志, 防止重复进入 */
    FDCAN1_IR = 0xFFFFFFFF;
    __asm__ volatile("dsb; isb");
}

/* ═══════════════════════════════════════════════════════════
 * DMA Stream5 TCIF: GPIO 输出抖动测量
 * 每次 DMA 完成 SHADOW→ODR 搬运时记录 DWT 时间戳
 * ═══════════════════════════════════════════════════════════ */
#define GPIO_JITTER_BUF_SIZE 256
static volatile uint32_t gpio_jitter_buf[GPIO_JITTER_BUF_SIZE];
static volatile uint32_t gpio_jitter_idx = 0;
static volatile int32_t gpio_jitter_max = 0;

__attribute__((section(".itcm_code")))
void DMA2_Stream5_IRQHandler(void) {
    #define DMA2_HIFCR (*(volatile uint32_t *)0x4002040C)
    DMA2_HIFCR = (1 << 22);  /* CTCIF5 */

    uint32_t now = DWT_CYCCNT;
    static uint32_t prev = 0;
    if (prev) {
        uint32_t period = now - prev;
        int32_t delta = (int32_t)period - 13600;
        if (delta < 0) delta = -delta;
        if (delta > gpio_jitter_max) gpio_jitter_max = delta;
    }
    prev = now;
    gpio_jitter_buf[gpio_jitter_idx % GPIO_JITTER_BUF_SIZE] = now;
    gpio_jitter_idx++;
}




/* ═══════════════════════════════════════════════════════════
 * ISR: 核心扫描引擎
 * ═══════════════════════════════════════════════════════════ */

__attribute__((section(".itcm_code")))
void TIM1_UP_IRQHandler(void) {
    static int32_t gpio_output_delta = 0;  /* GPIO 输出延迟测量值, RTT 上报用 */
    /* 诊断: ISR入口翻转GPIOE bit2 (PE2), 直接写 ODR, 逻辑分析仪可见 */
    GPIOE_ODR ^= (1 << 2);
    uint32_t t0 = ccnt();
    TIM1_SR = 0;             /* 清UIF, 避免下次中断不响应 */
    ADC1_ISR = (1 << 4);     /* 清OVR (火车模型: 站点之间清标志) */

    /* ── ADC输入: DMA已将ADC1_DR搬运至ADC_RAW (ADC_RAW_BASE),
     *           ISR仅需读ADC_RAW→换算→写SENSOR_MAP[0] */
    {
        uint32_t raw = (*(volatile uint32_t *)(ADC_RAW_BASE)) & 0xFFF;
        SENSOR_MAP[0] = (float)raw * (3.3f / 4095.0f);
    }

    /* ── ADC DMA 数据有效验证 ── */
    if (DMA2_S1M0AR != (ADC_RAW_BASE)) {
        /* DMA未配置: 回退到直接读ADC1_DR */
        SENSOR_MAP[0] = (float)(ADC1_DR & 0xFFF) * (3.3f / 4095.0f);
    }

    /* ── Period (精简: �?MIN/MAX, 无分�?dev 追踪) ── */
    uint32_t last = LAST_ENTRY;
    if (last) {
        uint32_t period = (t0 >= last) ? (t0 - last)
                         : ((0xFFFFFFFFu - last) + t0 + 1u);
        if (SAMPLES > 3) {
            if (period < PERIOD_MIN) PERIOD_MIN = period;
            if (period > PERIOD_MAX) PERIOD_MAX = period;
        } else if (SAMPLES == 3) {
            PERIOD_MIN = period;
            PERIOD_MAX = period;
        }
    }
    LAST_ENTRY = t0;

    /* ── Route table: 硬编码字节偏�? 零栈拷贝 ── */
    /* RouteEntry_t layout (packed, 16B):
       [0]src_type [1]src_index [2]dst_type [3]dst_channel
       [4]op [5]flags [6-7]param_idx [8-9]state_offset */
    uint32_t n = *(volatile uint32_t *)N_ROUTES_ADDR;
    uint8_t *rp = (uint8_t *)ROUTE_TABLE;
    uint8_t *end = rp + n * 16;
    do {
        if (!(rp[5] & ROUTE_ENABLED)) continue;

        float src;
        uint8_t st = rp[0];
        if (st == SRC_SENSOR)
            src = SENSOR_MAP[rp[1]];
        else if (st == SRC_WIRE)
            src = WIRE_MAP[rp[1]];
        else
            src = PARAM_TABLE[*(uint16_t *)(rp+6)].value_d;

        uint16_t pi = *(uint16_t *)(rp + 6);
        uint16_t so = *(uint16_t *)(rp + 8);
        float out;
        uint8_t op = rp[4];
        /* 内联高频原语 (消函数调�? */
        if (op == OP_DIRECT)
            out = src;
        else if (op == OP_SCALE)
            out = PARAM_TABLE[pi].value_a * src + PARAM_TABLE[pi].value_b;
        else if (op == OP_CMP) {
            int cm = (int)PARAM_TABLE[pi].value_b;
            if (cm == 0) out = (src >  PARAM_TABLE[pi].value_a) ? 1.0f : 0.0f;
            else if (cm == 1) out = (src >= PARAM_TABLE[pi].value_a) ? 1.0f : 0.0f;
            else if (cm == 2) out = (src <  PARAM_TABLE[pi].value_a) ? 1.0f : 0.0f;
            else out = (src <= PARAM_TABLE[pi].value_a) ? 1.0f : 0.0f;
        }
        else
            out = execute_primitive(op, src,
                      (ParamEntry_t *)&PARAM_TABLE[pi], (StateEntry_t *)&STATE_TABLE[so], 0.0001f);
        WIRE_MAP[rp[3]] = out;
        /* 输出映射: 如果 actuator_idx > 0, 同步写入 ACTUATOR_STATUS */
        uint16_t ai = *(uint16_t *)(rp + 10);
        if (ai > 0 && ai < MAX_ACTUATORS)
            ACTUATOR_STATUS[ai] = out;
    } while ((rp += 16) < end);

    /* ── Execution time measurement ── */
    uint32_t t1 = ccnt();
    uint32_t exec_time = t1 - t0;
    if (exec_time < EXEC_MIN) EXEC_MIN = exec_time;
    if (exec_time > EXEC_MAX) EXEC_MAX = exec_time;

    SAMPLES++;
    HEARTBEAT++;
    canopen_ticks++; /* 100us per tick, 10 ticks=1ms */

    /* ── 异常检测 ── */
    if (SAMPLES >= 4) { /* 跳过前几个不稳定周期 */
        uint32_t period = (t0 >= LAST_ENTRY) ? (t0 - LAST_ENTRY) : 0;

        /* 抖动检测: PERIOD_MAX - PERIOD_MIN 超过阈值 */
        uint32_t jitter = (PERIOD_MAX > PERIOD_MIN) ? (PERIOD_MAX - PERIOD_MIN) : 0;
        if (jitter > ALARM_JITTER_THRESHOLD) {
            alarm_write(ALARM_JITTER_SPIKE);
        }

        /* 单次周期过长 (可能 ISR 快要超时) */
        if (period > ALARM_PERIOD_THRESHOLD) {
            alarm_write(ALARM_PERIOD_HIGH);
        }

        /* 路由数异常变化 */
        uint32_t n = *(volatile uint32_t *)N_ROUTES_ADDR;
        static uint32_t prev_routes = 0;
        if (prev_routes == 0) prev_routes = n;
        if (n != prev_routes) {
            alarm_write(ALARM_ROUTES_CHANGED);
            prev_routes = n;
        }
    }

    /* ── RTT 状态上报: 每 1000 周期 (~100ms) 一次 ── */
    static uint32_t last_samples_check = 0;
    if ((SAMPLES % 100) == 0) {  /* 每 10ms 上报一次 */
        if (!engine_running) alarm_write(ALARM_ENGINE_STOPPED);
        if (last_samples_check != 0 && SAMPLES == last_samples_check)
            alarm_write(ALARM_SAMPLES_FROZEN);
        last_samples_check = SAMPLES;
        rtt_report_status(gpio_output_delta);
    }

    /* �?输出映射: ACTUATOR_STATUS �?物理硬件 */
    /* actuator_idx: 1=TIM1_CH1, 2=TIM1_CH2, 3=TIM1_CH3, 4=TIM1_CH4(ADC触发)
     *               32+N=GPIO_PE_N (数字输出) */
    {
        volatile float *ap = ACTUATOR_STATUS;
        if (ap[1] >= 0.0f) { /* CH1 PWM */
            float v = ap[1]; if (v > 100.0f) v = 100.0f;
            *(volatile uint16_t *)(TIM1_BASE + 0x34) = (uint16_t)(v * 119.99f);
        }
        if (ap[2] >= 0.0f) { /* CH2 PWM */
            float v = ap[2]; if (v > 100.0f) v = 100.0f;
            *(volatile uint16_t *)(TIM1_BASE + 0x38) = (uint16_t)(v * 119.99f);
        }
        if (ap[3] >= 0.0f) { /* CH3 PWM */
            float v = ap[3]; if (v > 100.0f) v = 100.0f;
            *(volatile uint16_t *)(TIM1_BASE + 0x3C) = (uint16_t)(v * 119.99f);
        }
        /* CH4:CCR4 在 main() 已配置,此处无需再写 */
        /* 数字输出: actuator_idx 32~63 → GPIOE bit0~31 */
        /* CPU 写 SHADOW,DMA Stream5 在 TIM1_UP 时刻搬到 GPIOE_ODR */
        uint32_t gpio_bits = 0;
        for (int i = 32; i < 64 && i < MAX_ACTUATORS; i++) {
            if (ap[i] > 0.5f) gpio_bits |= (1u << (i - 32));
        }
        /* CPU 写 SHADOW — DMA 在 TIM1_CC4 匹配时搬到 ODR
         * 输出时刻锁定在周期末尾 (CCR4=11700 @136MHz ≈ 86μs) */
        SHADOW_GPIO = gpio_bits;
        /* GPIO 输出延迟: TIM1_CNT - CCR4, >0=已触发(用旧值), <0=有余量 */
        {   volatile uint16_t *tim1_cnt = (volatile uint16_t *)(TIM1_BASE + 0x24);
            gpio_output_delta = (int32_t)(uint16_t)*tim1_cnt - 11700; }
    }

    /* ── 调试日志: 每 100 周期记录一次关键状态 (火车模型, 站点之间) ──
     * 24B/entry: [0]SAMPLES [1]N_ROUTES [2]ACT[32] [3]ACT[63] [4]SHADOW [5]ODR
     * 环形缓冲 128 条, 自动覆盖最旧
     */
    if ((SAMPLES % LOG_PERIOD) == 0) {
        uint32_t s = SAMPLES;
        uint32_t idx = (s / LOG_PERIOD) % LOG_WRAP;
        volatile uint32_t *e = &LOG_BUF[idx * 6];
        e[0] = s;
        e[1] = *(volatile uint32_t *)N_ROUTES_ADDR;    /* N_ROUTES */
        e[2] = *(volatile uint32_t *)(DTCM_BASE + 0x200 + 32*4); /* ACTUATOR_STATUS[32] bits */
        e[3] = *(volatile uint32_t *)(DTCM_BASE + 0x200 + 63*4); /* ACTUATOR_STATUS[63] bits */
        e[4] = SHADOW_GPIO;                                /* CPU 写入值 */
        e[5] = GPIOE_ODR;                                  /* 读回实际 GPIO 输出 */
        LOG_COUNT = s;
    }
}

/* ══════════════════════════════════════════════════════════�?
 * 测试程序: 双通道温度控制 + 诊断
 *
 * Channel A (Heater):  SENSOR[0] �?SCALE �?LPF �?PID �?CLAMP
 * Channel B (Cooler):  SENSOR[1] �?SCALE �?LPF �?PID �?CLAMP
 * Diagnostics: CMP×3, RATE, HYST, EDGE, CNT, DEADBAND, AND, ADD, SUB, DIRECT
 * Total: 20 routes, 9 primitive types
 * ══════════════════════════════════════════════════════════�?*/


#include "sin_lut.inc"

/* ═══════════════════════════════════════════════════════
 * 测试部署函数 (在 main 之外定义, 避免嵌套函数问题)
 * 32条新路由: CONST(1.0) → ACTUATOR[32+i] → GPIOE[0..31]
 * 验证 ISR 路由→ACTUATOR_STATUS→GPIOE_ODR 直接输出全链路
 * ⚠️ __attribute__((used,noinline)) 防止 -O2 删除"看似无副作用"的代码
 * ═══════════════════════════════════════════════════════ */
static void __attribute__((used, noinline)) deploy_test_routes(void) {
    volatile uint32_t *n_routes_p = (volatile uint32_t *)N_ROUTES_ADDR;
    volatile uint32_t old_n = *n_routes_p;
    volatile uint32_t new_n = old_n + 32;
    /* PARAM[0].value_d = 1.0  (SRC_CONST 的常量源) */
    PARAM_TABLE[0].value_d = 1.0f;
    /* 32条路由: CONST(1.0) → ACTUATOR[32..63] → GPIOE[0..31] */
    for (volatile uint32_t i = 0; i < 32; i++) {
        uint8_t *rp = (uint8_t *)&ROUTE_TABLE[old_n + i];
        rp[0]  = SRC_CONST;       /* src_type = CONST */
        rp[1]  = 0;               /* src_index (param[0]) */
        rp[2]  = DST_WIRE;        /* dst_type */
        rp[3]  = 0;               /* dst_channel */
        rp[4]  = OP_DIRECT;       /* op = 直通 */
        rp[5]  = ROUTE_ENABLED;   /* flags */
        rp[6]  = 0; rp[7]  = 0;   /* param_idx = 0 */
        rp[8]  = 0; rp[9]  = 0;   /* state_offset = 0 */
        rp[10] = (32 + i) & 0xFF; rp[11] = ((32 + i) >> 8) & 0xFF;  /* actuator_idx = 32+i */
        rp[12] = 0; rp[13] = 0;   /* wire2_idx = 0 */
    }
    /* 内存屏障: 强制 DTCM 写入完成后再写 N_ROUTES */
    __asm__ volatile("dsb sy" ::: "memory");
    __asm__ volatile("isb sy" ::: "memory");
    *n_routes_p = new_n;  /* 激活 */
    /* 备份: 路由 49 的前 4 字节 (src=2 si=0 dt=3 dc=0 → 0x00030002 LE) */
    ROUTE49_CHECK = *(volatile uint32_t *)(uint8_t *)&ROUTE_TABLE[old_n];
    /* 标记: deploy 跑到末尾 (写到 LOG_BASE+0x2004, 远离 SENSOR_MAP[0]) */
    DEPLOY_MARK = 0xDEADBEEF;
    DEPLOY_N    = new_n;
    LOG_COUNT   = 0;            /* 清空日志计数 */
    __asm__ volatile("dsb sy" ::: "memory");
}

static void iwdg_disable_debug(void) {
    /* H723 IWDG option byte 默认 = 硬件看门狗, 软件无法禁用! */
    /* 唯一策略: 立刻 feed, 不碰 PR/RLR (避免PVU/RVU等待期间counter跑完) */
    *(volatile uint32_t *)0x40003000 = 0x0000AAAA; /* KEY: 立刻 feed */
    *(volatile uint32_t *)0x40003000 = 0x0000AAAA; /* 双重 feed 保险 */
}

int main(void) {
    /* ── IWDG 复位检测 (比 IWDG 初始化更早!) ── */
    /* RCC_CSR bit29 = IWDGRSTF: 上一次复位是否由看门狗触发 */
    #define RCC_CSR (*(volatile uint32_t *)0x520020D8)
    if (RCC_CSR & (1u << 29)) {
        /* 看门狗的 DTCM 没被清零,告警缓冲区内容保留 → 追加一条记录 */
        volatile uint32_t *a = (volatile uint32_t *)ALARM_BUF_ADDR;
        uint32_t idx = a[0];
        uint32_t n = (a[1] > 0) ? (uint32_t)ALARM_MAX_ENTRIES : (idx % (uint32_t)ALARM_MAX_ENTRIES);
        a[2 + n * 2]     = 0x06 << 24;  /* ALARM_IWDG_RESET */
        a[2 + n * 2 + 1] = 0;           /* 触发时 SAMPLES 未知(reset了) */
        a[0] = (n + 1) % (ALARM_MAX_ENTRIES * 2);
        RCC_CSR |= (1u << 29);  /* 写 1 清标志 */
    }
    /* 上一行之后,IWDG 已经被配置且 RDY=1,开始自动喂狗周期 */

    /* ── IWDG 调试冻结 ── */
    /* 使用 DTCM+0x0050 (N_ENGINE 暂存区, 不影响 N_ROUTES) */
    *(volatile uint32_t *)0x20000050 = 0xA1000000;
    iwdg_disable_debug();
    *(volatile uint32_t *)0x20000050 = 0xA2000000;

    /* ── DWT ── */
    DEMCR |= (1 << 24);
    DWT_CYCCNT = 0;
    DWT_CTRL |= (1 << 0);

    /* ── Copy sin LUT from Flash to DTCM (cold load, zero jitter at runtime) ── */
    for (int i = 0; i < SIN_LUT_SIZE; i++) SIN_LUT[i] = sin_lut[i];

    /* ── Init timing vars ── */
    EXEC_MIN     = 0xFFFFFFFF;
    EXEC_MAX     = 0;
    PERIOD_MIN   = 0xFFFFFFFF;
    PERIOD_MAX   = 0;
    SAMPLES      = 0;
    LAST_ENTRY   = 0;
    HEARTBEAT    = 0;
    EXEC_TOTAL   = 0;
    DEV_ABS_MAX  = 0;
    DEV_ABS_MAX_SMP = 0;
    DEV_POS_MAX  = 0;
    DEV_NEG_MAX  = 0;
    PERIOD_EXACT = 0;
    PERIOD_FAR   = 0;

    /* ── Zero all register spaces ── */
    /* 检查是否已部署: PROGRAM_MAGIC_ADDR == PROGRAM_MAGIC_VALID 则保留现有程序 */
    if (*(volatile uint32_t *)PROGRAM_MAGIC_ADDR != PROGRAM_MAGIC_VALID) {
    /* ====== 首次启动: 清空运行程序区 ====== */
    for (int i = 0; i < MAX_SENSORS; i++)   SENSOR_MAP[i] = 0.0f;
    for (int i = 0; i < MAX_ACTUATORS; i++) ACTUATOR_STATUS[i] = 0.0f;
    for (int i = 0; i < MAX_WIRES; i++)     WIRE_MAP[i] = 0.0f;
    for (int i = 0; i < MAX_LUT; i++)       LUT_DATA[i] = 0.0f;
    for (int i = 0; i < MAX_ROUTES; i++) {
        ROUTE_TABLE[i].flags = 0;
    }
    for (int i = 0; i < MAX_PARAMS; i++) {
        PARAM_TABLE[i].value_a = 0.0f;
        PARAM_TABLE[i].value_b = 0.0f;
        PARAM_TABLE[i].value_c = 0.0f;
        PARAM_TABLE[i].value_d = 0.0f;
    }
    for (int i = 0; i < MAX_STATES; i++) {
        STATE_TABLE[i].state_a = 0.0f;
        STATE_TABLE[i].state_b = 0.0f;
        STATE_TABLE[i].state_c = 0.0f;
        STATE_TABLE[i].state_d = 0.0f;
    }

    /* ── 运行程序区初始为空，等待 IDE 部署 ── */
    *(volatile uint32_t *)N_ROUTES_ADDR = 0;
    *(volatile uint32_t *)N_PARAMS_ADDR = 0;
    *(volatile uint32_t *)N_STATES_ADDR = 0;
    *(volatile uint32_t *)PROGRAM_MAGIC_ADDR = 0;

    /* (硬编码演示路由已移除 — 由 IDE 部署) */

    } /* ====== init block end: program empty, wait for IDE ====== */

    /* ═══════════════════════════════════════════════════════
     * Step B: GPIOE 输出初始化 (必须最优先, ADC/DMA会干扰AHB4)
     * ═══════════════════════════════════════════════════════ */
    RCC_AHB4ENR |= (1 << 4);  /* GPIOEEN */
    __asm__ volatile("dsb");

    /* MODER: PE0-8,10,12,15=输出; PE9,11,13,14=AF */
    {
        uint32_t moder = GPIOE_MODER;
        /* PE0-7: 通用输出 */
        for (int i = 0; i <= 7; i++) { moder &= ~(3 << (i*2)); moder |= (1 << (i*2)); }
        moder &= ~(3 << 16); moder |= (1 << 16);  /* PE8: 输出 */
        moder &= ~(3 << 18); moder |= (2 << 18);  /* PE9: AF */
        moder &= ~(3 << 20); moder |= (1 << 20);  /* PE10: 输出 */
        moder &= ~(3 << 22); moder |= (2 << 22);  /* PE11: AF */
        moder &= ~(3 << 24); moder |= (1 << 24);  /* PE12: 输出 */
        moder &= ~(3 << 26); moder |= (2 << 26);  /* PE13: AF */
        moder &= ~(3 << 28); moder |= (2 << 28);  /* PE14: AF */
        moder &= ~(3 << 30); moder |= (1 << 30);  /* PE15: 输出 */
        GPIOE_MODER = moder;
    }

    /* AFRH: PE9/11/13/14 �?AF1 (TIM1) */
    {
        uint32_t afrh = GPIOE_AFRH;
        afrh &= ~((0xF << 4) | (0xF << 12) | (0xF << 20) | (0xF << 28));
        afrh |=  (1 << 4) | (1 << 12) | (1 << 20) | (1 << 28);
        GPIOE_AFRH = afrh;
    }

    /* PD0/PD1 �?AF9 (FDCAN1) */
    RCC_AHB4ENR |= (1 << 3); /* GPIODEN */
    GPIOD_MODER &= ~((3<<0)|(3<<2));
    GPIOD_MODER |=  (2<<0)|(2<<2);
    GPIOD_AFRL &= ~0xFF;
    GPIOD_AFRL |=  (9)|(9<<4);

    GPIOE_OSPEEDR = 0xFFFFFFFF;
    GPIOE_ODR = 0x0000;   /* 直写输出寄存器, 不再有SHADOW */

    /* 调试标记: 0xBB=GPIO初始化完�?*/
    *(volatile uint32_t *)(DTCM_BASE + 0x0000) = 0xBB000000;

    /* FDCAN1 + CANopen init */
    #define RCC_APB1HENR  (*(volatile uint32_t *)(RCC_BASE + 0xEC))
    RCC_APB1HENR |= (1 << 8);  /* FDCANEN */
    fdcan_init();
    canopen_state = NMT_INITIALISING;
    canopen_hb_period = 1000; /* 1s心跳 */

    /*
     * �?ADC 输入 �?同步时钟模式 + DMA搬运 + TIM1_TRGO触发
     * 解决两个核心问题�?
     *   1. ADRDY=0: 同步模式(CKMODE=10)不依赖per_ck，避免异步时钟源问题
     *   2. ITCM→AHB4阻断: DMA绕过CPU直访，ISR只读取DTCM零等�?
     */
    #define RCC_D3CCIPR  (*(volatile uint32_t *)(RCC_BASE + 0x138))
    RCC_AHB1ENR |= (1 << 5);          /* ADC12EN */
    __asm__ volatile("dsb");
    RCC_D3CCIPR = (RCC_D3CCIPR & ~(3<<18)) | (0<<18);  /* ADCSEL=sys_ck (同步模式不需�? */
    ADC12_CCR = (2 << 16) | (1<<22);  /* CKMODE=10 (同步AHB/2=136MHz), VREFEN */

    /* 0. 强制禁用: 防止之前失败遗留的挂起�?*/
    if (ADC1_CR & 1) {
        ADC1_CR |= (1<<1);
        { uint32_t t=8000000; while((ADC1_CR&1)&&--t){} }
    }
    /* 1. 退出深度掉�?*/
    ADC1_CR &= ~(1<<29);
    { uint32_t t=8000000; while((ADC1_CR&(1<<29))&&--t){} }
    /* 2. 电压调节�?*/
    ADC1_CR |= (1<<28);
    { uint32_t t=8000000; while(!(ADC1_ISR&(1<<12))&&--t){} } /* LDORDY */
    /* 3. ADC校准 (关键! H7系列必须校准后才能正常工�? */
    ADC1_CR |= (1<<31);               /* ADCAL=1 */
    { uint32_t t=8000000; while((ADC1_CR&(1<<31))&&--t){} } /* 等校准完�?*/
    /* 4. CFGR + CFGR2 + 通道 (必须在ADEN之前, RES锁定后)
     * 火车模型: 站点间留足余量 — 延长ADC采样时间避免与DMA/ISR争夺总线
     * SMP17 = 110 (640.5 ADC clocks × 7.35ns = 4.7μs 采样)
     * 周期100μs: 4.7μs采样 + 转换 + DMA搬运 + ISR ≈ 15μs (留85μs余量) */
    /* CFGR: DMAEN=1 + DMACFG=1(循环) + RES=00(12bit) + EXTSEL=10(TIM1_TRGO) + ALIGN=0 */
    #define ADC1_CFGR2  (*(volatile uint32_t *)(ADC1_BASE + 0x10))
    ADC1_CFGR  = (1 << 0) | (1 << 1) | (0 << 3) | (10 << 10); /* DMAEN + CIRC + 12bit + TIM1_TRGO */
    ADC1_CFGR2 = (1 << 0);   /* EXTEN=01: 上升沿触发 */
    ADC1_PCSEL = (1<<17);    /* 预选通道17 (内部VREFINT, 用于自测) */
    ADC1_SMPR1 |= (6 << 21); /* SMP17 = 110b → 640.5 ADC clocks */
    ADC1_SQR1 = (17<<6);     /* SQ1=通道17, L=0 (1次转换) */
    /* 5. 清ADRDY �?ADEN �?等ADRDY */
    ADC1_ISR = 1;
    ADC1_CR |= (1<<0);
    { uint32_t t=8000000; while(!(ADC1_ISR&1)&&--t){} }
    ADC1_ISR = 1;
    /* 6. ADSTART: 硬件触发 (CH4 OC4REF上升�? */
    ADC1_CR |= (1<<2);

    /* ══════════════════════════════════════════════════════�?
     * DMA2 配置: 仅 Stream1 (ADC → DTCM)
     * Stream1: ADC1_DR → DTCM (ADC_RAW @ ADC_RAW_BASE) 输入
     * GPIO 输出已改为 ISR 直写, 不再需要 Stream5/DMAMUX
     * ══════════════════════════════════════════════════════�?*/
    RCC_AHB1ENR |= (1 << 1) | (1 << 2);   /* DMA2EN + DMAMUX1EN */
    __asm__ volatile("dsb; isb");
    /* 验证 DMA2 时钟确实生效 (H7 要求时钟使能后至少等 2 个时钟周期) */
    { volatile uint32_t chk = RCC_AHB1ENR; (void)chk; }

    /* --- Stream1: ADC �?DTCM --- */
    DMAMUX1_S1CR = DMAMUX_REQ_ADC1;
    DMA2_S1CR = 0;  /* 禁用 */
    { uint32_t tout = 8000000; while ((DMA2_S1CR & 1) && --tout) {} }
    DMA2_LIFCR = 0x00000F7C;  /* 清Stream1标志 */
    __asm__ volatile("dsb; isb");
    DMA2_S1NDTR = 0;  /* 先清NDTR, 解除M0AR写保�?*/
    DMA2_S1PAR  = (uint32_t)&ADC1_DR;
    DMA2_S1M0AR = ADC_RAW_BASE;  /* ADC_RAW in DTCM */
    DMA2_S1NDTR = 1;  /* 1�?2bit */
    DMA2_S1FCR  = 0;  /* DMDIS=0: 直通模�?*/
    { uint32_t cr = (1 << 8) | (2 << 10) | (2 << 12) | (3 << 16);  /* CIRC, P32, M32, PL最�?*/
      DMA2_S1CR = cr; }
    __asm__ volatile("dsb");
    DMA2_S1CR |= 1;  /* EN=1 */
    __asm__ volatile("dsb; isb");
    *(volatile uint32_t *)(DTCM_BASE + 0x0000) = 0xCC000001;  /* DMA Stream1 done */

    /* (旧的 Stream5 配置已删除,新配置见下方 DMAMUX1_CH13CR) */
    /* ═══════════════════════════════════════════════════════════
     * DMA2 Stream5: DTCM(SHADOW @ 0x200000E0) → GPIOE_ODR
     *
     * 触发源: TIM1_CC4 match event (CC4DE in DIER)
     * CCR4 = 11700 (97.5μs @120MHz) — 周期末尾触发
     *
     * 架构原则:
     *   CPU 末尾写 SHADOW (1 cycle)
     *   TIM1 在 CCR4 产生 CC4 match → DMA request
     *   DMA 硬件搬 SHADOW → GPIOE_ODR (4 cycles,零 jitter)
     *   GPIO 翻转时刻锁定在 ~97.5μs,与 CPU 计算时刻无关
     * ═══════════════════════════════════════════════════════════ */
    /* DMA Stream5 配置:SHADOW → GPIOE_ODR,由 TIM1_CC4 触发 */
    DMAMUX1_CH13CR = DMAMUX_REQ_TIM1_CH4;  /* TIM1_CH4 (ID=14)→ DMA2_Stream5 */
    DMA2_S5CR = 0;                         /* 禁用 stream */
    { uint32_t t = 8000000; while ((DMA2_S5CR & 1) && --t) {} }
    DMA2_HIFCR = 0x00000F7C;               /* 清所有 Stream5 标志 (写1清) */
    __asm__ volatile("dsb; isb");
    DMA2_S5NDTR = 0;                       /* 解锁 M0AR */
    DMA2_S5PAR  = (uint32_t)&GPIOE_ODR;     /* 外设地址 */
    DMA2_S5M0AR = DTCM_BASE + 0x00E0;      /* 内存:SHADOW */
    DMA2_S5NDTR = 1;                       /* 1 次传输 */
    DMA2_S5FCR  = 0;                       /* 直通模式 */
    { uint32_t cr = (1 << 6)               /* DIR=01 (M2P) */
                   | (1 << 8)               /* CIRC=1 */
                   | (3 << 16);             /* PL=最高 */
      DMA2_S5CR = cr | 1; }                /* EN=1 */

    

    /* ══════════════════════════════════════════════════════�?
     * TIM1 配置: 100μs周期 + 4路PWM + TRGO(ADC)
     * ══════════════════════════════════════════════════════�?*/
    RCC_APB2ENR |= (1 << 0);           /* TIM1EN */
    /* TIM1 寄存器是 16-bit, �?16-bit volatile �?*/
    *(volatile uint16_t *)(TIM1_BASE + 0x00) = 0;          /* CR1: stop */
    __asm__ volatile("dsb");

    /* CR2: MMS=100 �?OC4REF作为TRGO (CH4比较时触发ADC) */
    *(volatile uint16_t *)(TIM1_BASE + 0x04) = (4 << 4);

    *(volatile uint16_t *)(TIM1_BASE + 0x28) = 0;          /* PSC */
    *(volatile uint16_t *)(TIM1_BASE + 0x2C) = 11999;      /* ARR: 100μs @120MHz */
    *(volatile uint16_t *)(TIM1_BASE + 0x18) = (6 << 4) | (1 << 3) | (6 << 12) | (1 << 11);
    /* CH3=PWM1, CH4=PWM2 (OC4REF上升沿@CNT=CCR4→TRGO→ADC触发) */
    *(volatile uint16_t *)(TIM1_BASE + 0x1C) = (6 << 4) | (1 << 3) | (7 << 12) | (1 << 11);
    *(volatile uint16_t *)(TIM1_BASE + 0x20) = (1 << 0) | (1 << 4) | (1 << 8) | (1 << 12);
    *(volatile uint16_t *)(TIM1_BASE + 0x34) = 0; *(volatile uint16_t *)(TIM1_BASE + 0x38) = 0;
    *(volatile uint16_t *)(TIM1_BASE + 0x3C) = 0;
    /* CH4: CCR4=11700 (97.5μs) — 周期末尾触发 DMA 搬 SHADOW→ODR */
    /* 注意: CH4 从此输出 PWM (PE14),占空比 97.5% */
    *(volatile uint16_t *)(TIM1_BASE + 0x40) = 11700;
    { uint16_t bdtr = *(volatile uint16_t *)(TIM1_BASE + 0x44); bdtr |= (1 << 15);
      *(volatile uint16_t *)(TIM1_BASE + 0x44) = bdtr; }
    /* DIER: UIE(中断) + CC4DE(DMA request on CC4 match @ 97.5μs) */
    /* CC4DE = bit 12 (RM0468 TIM1_DIER) */
    *(volatile uint16_t *)(TIM1_BASE + 0x0C) = (1 << 0) | (1 << 12);
    __asm__ volatile("dsb; isb");
    *(volatile uint16_t *)(TIM1_BASE + 0x00) = 1;          /* CR1: start */
    __asm__ volatile("dsb; isb");
    *(volatile uint32_t *)(DTCM_BASE + 0x0000) = 0xCC000003;  /* TIM1 started */
    /* ADC: TIM1 TRGO硬件触发, ISR仅读取DTCM */
    SCB_CFSR = 0xFFFFFFFFU;  /* 清除所有fault标志 (�?清除) */
    __asm__ volatile("dsb; isb");
    /* ══�?中断优先级配�?(PLC确定性要�? ══�?*/
    /* TIM1_UP = IRQ25, 优先�? (最�? �?保证100μs周期不被抢占 */
    #define NVIC_IPR6  (*(volatile uint32_t *)0xE000E418)
    NVIC_IPR6 = (NVIC_IPR6 & ~(0xFFu << 8)) | (0u << 8);  /* IRQ25 = 优先�? */
    /* FDCAN1_IT0 = IRQ19, 优先�? (次高) �?通信不干扰控�?*/
    #define NVIC_IPR4  (*(volatile uint32_t *)0xE000E410)
    NVIC_IPR4 = (NVIC_IPR4 & ~(0xFFu << 24)) | (1u << 24); /* IRQ19 = 优先�? */
    /* 使能中断 */
    NVIC_ISER0 = (1 << 25);   /* TIM1_UP */
    NVIC_ISER0 = (1 << 19);   /* FDCAN1_IT0 (�?) */
    __asm__ volatile("dsb; isb");

    /* 调试标记: 0xCC=全部初始化完�?*/
    *(volatile uint32_t *)(DTCM_BASE + 0x0000) = 0xCC000000;

    /* IWDG 在主开头(全局函数)已冻结+配置,此处跳过 */

    /* ── USART2 初始�?(UART 通信) ── */
    usart2_init();

    /* ── RTT 初始化 (非侵入式 SWD 监测) ── */
    rtt_init();

    /* ── 告警缓冲区初始化 ── */
    {
        volatile uint32_t *a = (volatile uint32_t *)ALARM_BUF_ADDR;
        a[0] = 0; /* write_idx */
        a[1] = 0; /* overflow_count */
    }

    /* ── 引擎初始化完成: 程序区为空 (N_ROUTES=0), 等待 IDE 部署 ── */
    /* 注意: engine_start() 启动 ISR, 但 N_ROUTES=0 → ISR 不扫描任何路由 */
    engine_start();

    /* 主循环: UART DCL 部署 */
    while (1) {
        uart_poll();
        canopen_poll();
        IWDG_KR = IWDG_KEY_RELOAD;
        *(volatile uint32_t *)(DTCM_BASE + 0x0020) += 1;
    }

}
