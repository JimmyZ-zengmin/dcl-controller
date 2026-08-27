/**
 * UART 通信接口: USART2 (115200,8N1) + 帧协议
 */
#ifndef DCL_UART_H
#define DCL_UART_H

#include <stdint.h>
#include "../registers.h"
#include "../dtcm_layout.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ── 帧格式 ── */
#define FRAME_CMD   0xC0
#define FRAME_STS   0xC1
#define CMD_DEPLOY  0x10
#define CMD_START   0x11
#define CMD_STOP    0x12
#define CMD_RESET   0x13
#define CMD_READ    0x20
#define CMD_WRITE   0x21
#define STS_WIRE_DATA  0x20
#define STS_ACK        0x30
#define STS_ERROR      0x40

#define UART_RX_BUF_SIZE 256

/**
 * 初始化 USART2 + DMA Stream2 接收
 */
void usart2_init(void);

/** 轮询处理接收数据 (在主循环调用) */
void uart_poll(void);

/** 发送状态帧 */
void uart_send_status(uint8_t sts, const uint8_t *payload, uint16_t len);

#ifdef __cplusplus
}
#endif

#endif /* DCL_UART_H */
