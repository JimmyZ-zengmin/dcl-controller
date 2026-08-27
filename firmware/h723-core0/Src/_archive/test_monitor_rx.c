/**
 * test_monitor_rx.c — 非侵入式串口推送测试
 *
 * 验证目标：确认串口能主动发送数据，PC只接收不干预
 * 方法：在main.c的TIM1_UP_IRQHandler中添加计数，
 *       当计数到10000（=100μs * 10000 = 1s）时，往串口发送帧
 *
 * 这是最小的修改，用于验证物理链路是否正常
 */

/* ── 添加到 main.c 头部的宏定义区域 ── */
/*
 * 新增命令码：0x30 = MONITOR
 * 新增状态码：0x50 = STREAM_DATA (硬件主动推送数据流)
 */
#define CMD_MONITOR      0x40   /* 监控命令：payload=[enable:1B, period_ms:2B] */
#define STS_STREAM_DATA  0x50   /* 数据流：payload=[idx:1B, value:4B float] */

/* ── 添加到 DTCM 变量区域（0x20000040 附近） ── */
/*
 * 放在 TIMING_BASE 之后的路由表之前区域
 */
#define MONITOR_FLAG     (*(volatile uint32_t *)(DTCM_BASE + 0x0048))
#define MONITOR_PERIOD   (*(volatile uint32_t *)(DTCM_BASE + 0x004C))
#define MONITOR_COUNTER  (*(volatile uint32_t *)(DTCM_BASE + 0x0050))
#define MONITOR_N_WIRES  (*(volatile uint32_t *)(DTCM_BASE + 0x0054))
#define MONITOR_WIRES    ((volatile uint8_t *)(DTCM_BASE + 0x0058)) /* 最多32个WIRE索引 */

/* ── UART 发送函数（轮询方式，不用DMA） ── */
/*
 * 放在 uart_poll() 函数之后
 */
static void uart_tx_byte(uint8_t b) {
    /* 等待发送完成 */
    while (!(USART2_ISR & (1 << 7))) { __asm volatile("nop"); }
    USART2_TDR = b;
}

static void uart_tx_frame(uint8_t status, uint8_t *payload, uint16_t len) {
    uint16_t crc;
    uint16_t crc_data;

    /* 发送帧头：marker + status + length */
    uart_tx_byte(FRAME_STS);
    uart_tx_byte(status);
    uart_tx_byte(len & 0xFF);
    uart_tx_byte((len >> 8) & 0xFF);

    /* 计算CRC（覆盖status + length + payload） */
    crc = 0xFFFF;
    /* status */
    crc ^= (status << 8);
    for (int i = 0; i < 8; i++) {
        if (crc & 0x8000) crc = (crc << 1) ^ 0x1021;
        else crc <<= 1;
        crc &= 0xFFFF;
    }
    /* length low */
    crc ^= ((len & 0xFF) << 8);
    for (int i = 0; i < 8; i++) {
        if (crc & 0x8000) crc = (crc << 1) ^ 0x1021;
        else crc <<= 1;
        crc &= 0xFFFF;
    }
    /* length high */
    crc ^= (((len >> 8) & 0xFF) << 8);
    for (int i = 0; i < 8; i++) {
        if (crc & 0x8000) crc = (crc << 1) ^ 0x1021;
        else crc <<= 1;
        crc &= 0xFFFF;
    }
    /* payload */
    for (int i = 0; i < len; i++) {
        crc ^= (payload[i] << 8);
        for (int j = 0; j < 8; j++) {
            if (crc & 0x8000) crc = (crc << 1) ^ 0x1021;
            else crc <<= 1;
            crc &= 0xFFFF;
        }
    }

    /* 发送payload */
    for (int i = 0; i < len; i++) {
        uart_tx_byte(payload[i]);
    }

    /* 发送CRC */
    uart_tx_byte(crc & 0xFF);
    uart_tx_byte((crc >> 8) & 0xFF);
}

/* ── 在 TIM1_UP_IRQHandler 中添加监控采样 ── */
/*
 * 在 ISR 末尾（日志记录之后）添加：
 *
 * if (MONITOR_FLAG && MONITOR_N_WIRES > 0) {
 *     MONITOR_COUNTER++;
 *     if (MONITOR_COUNTER >= MONITOR_PERIOD) {
 *         MONITOR_COUNTER = 0;
 *         // 采样并发送第一个WIRE的值
 *         uint8_t idx = MONITOR_WIRES[0];
 *         float val = WIRE_MAP[idx];
 *         uint8_t payload[5];
 *         payload[0] = idx;
 *         *(float *)(payload + 1) = val;
 *         uart_tx_frame(STS_STREAM_DATA, payload, 5);
 *     }
 * }
 */

/* ── 在 uart_process_byte() 中添加 MONITOR 命令处理 ── */
/*
 * 在 switch(fp_cmd) 中添加：
 *
 * case CMD_MONITOR: {
 *     if (fp_len >= 3) {
 *         uint8_t enable = fp_payload[0];
 *         uint16_t period = fp_payload[1] | (fp_payload[2] << 8);
 *         MONITOR_FLAG = enable;
 *         MONITOR_PERIOD = period;  // 单位：100μs周期数
 *         MONITOR_COUNTER = 0;
 *     }
 *     break;
 * }
 */

/* ── 在 main() 中初始化监控变量 ── */
/*
 * 在 engine_start() 之前添加：
 *
 * MONITOR_FLAG = 0;
 * MONITOR_PERIOD = 10000;  // 默认1s
 * MONITOR_COUNTER = 0;
 * MONITOR_N_WIRES = 1;
 * MONITOR_WIRES[0] = 0;    // 默认监控WIRE[0]
 */
