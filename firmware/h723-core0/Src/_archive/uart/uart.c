/**
 * USART2 通信: 115200 8N1 + DMA 接收 + 帧协议
 *
 * PC→H723: [0xC0] [CMD] [LEN:2B LE] [PAYLOAD] [CRC16:2B LE]
 * H723→PC: [0xC1] [STS] [LEN:2B LE] [PAYLOAD] [CRC16:2B LE]
 * CRC-16/CCITT: poly=0x1021, init=0xFFFF, covers CMD+LEN+PAYLOAD
 */
#include "uart.h"

/* ── 命令处理器声明 (在 main.c 定义) ── */
typedef void (*cmd_handler_t)(const uint8_t *payload, uint16_t len);
extern void handle_deploy(const uint8_t *payload, uint16_t len);
extern void handle_start(void);
extern void handle_stop(void);
extern void handle_reset(void);
extern void handle_read(const uint8_t *payload, uint16_t len);
extern void handle_write(const uint8_t *payload, uint16_t len);

/* ── 模块状态 ── */
static uint8_t uart_rx_buf[UART_RX_BUF_SIZE] __attribute__((aligned(4)));
static uint32_t uart_rx_read_pos;

#define FP_MAX_PAYLOAD 2048
#define FP_IDLE    0
#define FP_CMD     1
#define FP_LEN0    2
#define FP_LEN1    3
#define FP_PAYLOAD 4
#define FP_CRC0    5
#define FP_CRC1    6

static uint8_t  fp_state = FP_IDLE;
static uint8_t  fp_cmd;
static uint16_t fp_len;
static uint16_t fp_pos;
static uint16_t fp_crc_rx;
static uint8_t  fp_payload[FP_MAX_PAYLOAD];

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

/* ── TX ── */
static void uart_send_bytes(const uint8_t *data, uint32_t len) {
    for (uint32_t i = 0; i < len; i++) {
        while (!(USART2_ISR & (1u << 7))) {}
        USART2_TDR = data[i];
    }
    while (!(USART2_ISR & (1u << 6))) {}
}

void uart_send_status(uint8_t sts, const uint8_t *payload, uint16_t len) {
    uint8_t hdr[4] = { FRAME_STS, sts, len & 0xFF, (len >> 8) & 0xFF };
    uart_send_bytes(hdr, 4);
    if (len > 0 && payload)
        uart_send_bytes(payload, len);
    uint16_t crc = crc16_ccitt(&hdr[1], 3);
    if (len > 0 && payload)
        crc = crc16_ccitt_update(crc, payload, len);
    uint8_t crc_buf[2] = { crc & 0xFF, (crc >> 8) & 0xFF };
    uart_send_bytes(crc_buf, 2);
}

/* ── 帧解析 ── */
static void uart_process_byte(uint8_t b) {
    switch (fp_state) {
    case FP_IDLE:
        if (b == FRAME_CMD) fp_state = FP_CMD;
        break;
    case FP_CMD:
        fp_cmd = b; fp_len = 0; fp_pos = 0; fp_state = FP_LEN0; break;
    case FP_LEN0:
        fp_len = b; fp_state = FP_LEN1; break;
    case FP_LEN1:
        fp_len |= ((uint16_t)b << 8);
        if (fp_len == 0) fp_state = FP_CRC0;
        else if (fp_len > FP_MAX_PAYLOAD) fp_state = FP_IDLE;
        else fp_state = FP_PAYLOAD;
        break;
    case FP_PAYLOAD:
        fp_payload[fp_pos++] = b;
        if (fp_pos >= fp_len) fp_state = FP_CRC0;
        break;
    case FP_CRC0:
        fp_crc_rx = b; fp_state = FP_CRC1; break;
    case FP_CRC1: {
        fp_crc_rx |= ((uint16_t)b << 8);
        uint8_t hdr[3] = { fp_cmd, fp_len & 0xFF, (fp_len >> 8) & 0xFF };
        uint16_t crc = crc16_ccitt(hdr, 3);
        if (fp_len > 0) crc = crc16_ccitt_update(crc, fp_payload, fp_len);
        if (crc == fp_crc_rx) {
            switch (fp_cmd) {
            case CMD_DEPLOY: handle_deploy(fp_payload, fp_len); break;
            case CMD_START:  handle_start(); break;
            case CMD_STOP:   handle_stop(); break;
            case CMD_RESET:  handle_reset(); break;
            case CMD_READ:   handle_read(fp_payload, fp_len); break;
            case CMD_WRITE:  handle_write(fp_payload, fp_len); break;
            default: {
                const char *e = "UNKNOWN CMD";
                uart_send_status(STS_ERROR, (const uint8_t *)e, 11);
            }
            }
        }
        fp_state = FP_IDLE;
        break;
    }
    }
}

/* ── 轮询 (主循环调用) ── */
#define UART_POLL_LIMIT 64
void uart_poll(void) {
    uint32_t dma_pos = UART_RX_BUF_SIZE - DMA2_S2NDTR;
    uint32_t cnt = 0;
    while (uart_rx_read_pos != dma_pos && cnt < UART_POLL_LIMIT) {
        uart_process_byte(uart_rx_buf[uart_rx_read_pos]);
        uart_rx_read_pos = (uart_rx_read_pos + 1) % UART_RX_BUF_SIZE;
        dma_pos = UART_RX_BUF_SIZE - DMA2_S2NDTR;
        cnt++;
    }
}

/* ── 初始化 ── */
void usart2_init(void) {
    /* GPIOD: PD5=TX (AF7), PD6=RX (AF7) */
    RCC_AHB4ENR |= (1 << 3);  /* GPIODEN */
    __asm__ volatile("dsb");
    GPIOD_MODER &= ~((3u << 10) | (3u << 12));
    GPIOD_MODER |=  (2u << 10) | (2u << 12);
    GPIOD_AFRL &= ~((0xFu << 20) | (0xFu << 24));
    GPIOD_AFRL |=  (7u << 20) | (7u << 24);

    RCC_APB1LENR |= (1 << 17);  /* USART2EN */
    __asm__ volatile("dsb");

    USART2_CR1 = (1u << 3) | (1u << 2);
    USART2_CR2 = 0;
    USART2_CR3 = (1u << 6);   /* DMAR */
    USART2_PRESC = 0;
    USART2_BRR = (590u << 4) | 4u;
    USART2_CR1 |= (1u << 0);  /* UE */

    /* DMA Stream2: USART2_RDR → uart_rx_buf */
    DMAMUX1_S2CR = DMAMUX_REQ_USART2_RX;
    DMA2_S2CR = 0;
    { uint32_t tout = TIMEOUT; while ((DMA2_S2CR & 1) && --tout) {} }
    DMA2_S2PAR  = (uint32_t)&USART2_RDR;
    DMA2_S2M0AR = (uint32_t)uart_rx_buf;
    DMA2_S2NDTR = UART_RX_BUF_SIZE;
    DMA2_S2FCR  = (1u << 2);
    DMA2_S2CR = (1u << 8) | (1u << 10) | (2u << 16);
    __asm__ volatile("dsb");
    DMA2_S2CR |= 1;
    __asm__ volatile("dsb; isb");

    uart_rx_read_pos = 0;
}
