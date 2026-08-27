/**
 * CANopen 接口: FDCAN1 + NMT/SDO/Heartbeat (500kbps)
 */
#ifndef DCL_CANOPEN_H
#define DCL_CANOPEN_H

#include <stdint.h>
#include "../registers.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ── NMT 状态 ── */
#define NMT_INITIALISING   0
#define NMT_PREOP          127
#define NMT_OPERATIONAL    5
#define NMT_STOPPED        4

/* ── COB-ID ── */
#define COB_NMT      0x000
#define COB_SYNC     0x080
#define COB_TSDO     0x580
#define COB_RSDO     0x600
#define COB_HEARTBEAT 0x700

#define NODE_ID      1

/* 对象字典 */
#define OD_DEVICE_TYPE      0x1000
#define OD_ERROR_REGISTER   0x1001
#define OD_HEARTBEAT_TIME   0x1017

/**
 * 初始化 FDCAN1 (500kbps @ 68MHz APB1)
 * - 位时序: 136 tq (NTSEG1=105, NTSEG2=20, NSJW=10)
 * - RX FIFO0: 4 elements
 * - TX FIFO: 4 elements
 */
void fdcan_init(void);

/** 发送 CAN 帧, 成功返回 0, 失败返回 -1 */
int can_send(uint32_t id, uint8_t *data, uint8_t len);

/** 接收 CAN 帧, 有消息返回长度, 无消息返回 0 */
int can_recv(uint32_t *id, uint8_t *data);

/** CANopen 主循环处理 (调用间隔 ~1ms) */
void canopen_poll(void);

/** 100us tick counter (ISR 递增, canopen 读取) */
extern uint32_t canopen_ticks;

#ifdef __cplusplus
}
#endif

#endif /* DCL_CANOPEN_H */
