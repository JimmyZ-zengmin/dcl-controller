/**
 * transport.c — DCL 帧协议: CRC16-CCITT + 帧解析状态机
 *
 * ★ 逐字沿用 esp32-core0/components/transport/uart_protocol.c —— 除 include 名外
 *   **一个字符都没改**。协议不变是阶段 3 的硬验收条件(上位机脚本零改动)。
 *
 * 为什么搬它而不是重写: 这份状态机被 S3 的 20 套回归 + Modbus LA 外部验证跑过,
 * 是**已被验证过的资产**。重写 = 引入新缺陷面, 零收益。
 */
#include "transport.h"
#include <string.h>

/* CRC16-CCITT (poly=0x1021) */
uint16_t crc16_ccitt_seg(uint16_t crc, const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (int j = 0; j < 8; j++)
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
    return crc;
}

uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
    return crc16_ccitt_seg(0xFFFF, data, len);
}

/* 帧解析状态机 */
void fp_init(FrameParser_t *fp) { memset(fp, 0, sizeof(*fp)); }

int fp_feed(FrameParser_t *fp, uint8_t byte) {
    switch (fp->state) {
        case 0: /* WAIT_SYNC */
            /* ★ v2 由 SYNC 字节区分（老上位机永远发 0xC0 ⇒ 行为一字不变）*/
            if (byte == FRAME_SYNC_PC2MCU)    { fp->v2 = 0; fp->state = 1; fp->payload_idx = 0; }
            else if (byte == FRAME_SYNC_PC2MCU_V2) { fp->v2 = 1; fp->state = 1; fp->payload_idx = 0; }
            return 0;
        case 1: /* CMD */
            fp->cmd = byte; fp->state = fp->v2 ? 7 : 2; return 0;
        case 7: /* ★ V2_SEQ: v2 请求的序号（应答原样回显）*/
            fp->seq = byte; fp->state = 2; return 0;
        case 2: /* LEN_LO */
            fp->payload_len = byte; fp->state = 3; return 0;
        case 3: /* LEN_HI */
            fp->payload_len |= (byte << 8);
            if (fp->payload_len > FRAME_PAYLOAD_MAX) { fp->state = 0; return -1; }
            fp->state = (fp->payload_len == 0) ? 5 : 4;
            fp->payload_idx = 0;
            return 0;
        case 4: /* PAYLOAD */
            fp->payload[fp->payload_idx++] = byte;
            if (fp->payload_idx >= fp->payload_len) fp->state = 5;
            return 0;
        case 5: /* CRC_LO */
            fp->crc_lo = byte; fp->state = 6; return 0;
        case 6: { /* CRC_HI — F1: 分块 CRC, 消除栈上 FRAME_PAYLOAD_MAX 缓冲 (6KB 会爆栈) */
            uint16_t rx = fp->crc_lo | ((uint16_t)byte << 8);
            uint16_t ex = crc16_ccitt_seg(0xFFFF, (const uint8_t[]){fp->cmd,
                        (uint8_t)(fp->payload_len & 0xFF), (uint8_t)((fp->payload_len >> 8) & 0xFF)}, 3);
            if (fp->payload_len) ex = crc16_ccitt_seg(ex, fp->payload, fp->payload_len);
            /* ★ v2: CRC 还要覆盖 SEQ（它是帧的一部分, 不覆盖就等于给了篡改的余地）
             *   —— 用**独立重算**而不是"改上面那 3 字节数组", 因为 CRC 是**分段链式**的:
             *      必须按实际发送顺序 [CMD][SEQ][LEN][payload] 喂进去。 */
            if (fp->v2) {
                ex = crc16_ccitt_seg(0xFFFF, (const uint8_t[]){fp->cmd, fp->seq,
                            (uint8_t)(fp->payload_len & 0xFF), (uint8_t)((fp->payload_len >> 8) & 0xFF)}, 4);
                if (fp->payload_len) ex = crc16_ccitt_seg(ex, fp->payload, fp->payload_len);
            }
            fp->state = 0;
            return (rx == ex) ? 1 : -1;
        }
        default: fp->state = 0; return 0;
    }
}
