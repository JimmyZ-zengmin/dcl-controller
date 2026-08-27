/**
 * CANopen 协议栈: FDCAN1 (500kbps) + NMT/SDO/Heartbeat
 */
#include "canopen.h"
#include "../dtcm_layout.h"

/* ── 模块状态 ── */
static uint8_t  canopen_state;
static uint32_t canopen_hb_timer;
static uint16_t canopen_hb_period = 1000; /* ms */
uint32_t canopen_ticks;

void canopen_send_heartbeat(void) {
    uint8_t d[1] = { canopen_state };
    can_send(COB_HEARTBEAT + NODE_ID, d, 1);
}

static void canopen_handle_nmt(uint8_t *data) {
    uint8_t cmd = data[0], node = data[1];
    if (node != 0 && node != NODE_ID) return;
    switch (cmd) {
    case 1:  canopen_state = NMT_OPERATIONAL;  break;
    case 2:  canopen_state = NMT_STOPPED;      break;
    case 128: canopen_state = NMT_PREOP;       break;
    case 129: canopen_state = NMT_INITIALISING; break;
    case 130: canopen_state = NMT_INITIALISING; break;
    }
}

static void canopen_handle_sdo(uint32_t cob_id, uint8_t *data) {
    (void)cob_id;
    uint8_t rsp[8] = {0};
    uint16_t idx = (data[1] << 8) | data[2];
    uint8_t  sub = data[3];

    if ((data[0] & 0xE0) == 0x40) {
        rsp[0] = 0x4F | ((data[0] & 3) << 2);
        rsp[1] = data[1]; rsp[2] = data[2]; rsp[3] = sub;
        if (idx == OD_DEVICE_TYPE && sub == 0) {
            uint32_t v = 0x00010191;
            rsp[4]=v; rsp[5]=v>>8; rsp[6]=v>>16; rsp[7]=v>>24;
        } else if (idx == OD_HEARTBEAT_TIME && sub == 0) {
            rsp[4]=canopen_hb_period; rsp[5]=canopen_hb_period>>8;
        } else { rsp[0] = 0x80; }
    } else { rsp[0] = 0x80; }
    can_send(COB_TSDO + NODE_ID, rsp, 8);
}

void canopen_poll(void) {
    uint32_t cob_id; uint8_t data[8];
    int len = can_recv(&cob_id, data);
    if (len > 0) {
        if (cob_id == COB_NMT) {
            canopen_handle_nmt(data);
        } else if ((cob_id & 0xFF80) == (COB_RSDO & 0xFF80)) {
            if (len >= 4) canopen_handle_sdo(cob_id, data);
        }
    }

    /* Heartbeat */
    if (canopen_ticks >= canopen_hb_period) {
        canopen_ticks = 0;
        canopen_send_heartbeat();
    }
}

/* ── FDCAN1 底层 ── */
int can_send(uint32_t id, uint8_t *data, uint8_t len) {
    if (!(FDCAN1_TXFQS & (1<<5))) return -1;
    uint32_t *ram = (uint32_t *)(FDCAN1_MSGRAM + FDCAN1_TX_FIFO_OFFSET);
    uint32_t w0 = (id << 18) | (len << 16) | (1<<15);
    uint32_t w1 = (data[0]<<24)|(data[1]<<16)|(data[2]<<8)|data[3];
    ram[0] = w0; ram[1] = w1;
    if (len > 4) {
        uint32_t w2 = (data[4]<<24)|(data[5]<<16)|(data[6]<<8)|data[7];
        ram[2] = w2;
    }
    FDCAN1_TXBAR = (1 << 0);
    return 0;
}

int can_recv(uint32_t *id, uint8_t *data) {
    if (!(FDCAN1_RXF0S & 1)) return 0;
    uint32_t *ram = (uint32_t *)(FDCAN1_MSGRAM + FDCAN1_RX_FIFO0_OFFSET);
    uint32_t w0 = ram[0], w1 = ram[1];
    *id = (w0 >> 18) & 0x7FF;
    uint8_t len = (w0 >> 16) & 0x0F;
    data[0] = w1>>24; data[1] = w1>>16; data[2] = w1>>8; data[3] = w1;
    if (len > 4) { uint32_t w2 = ram[2];
        data[4]=w2>>24; data[5]=w2>>16; data[6]=w2>>8; data[7]=w2; }
    FDCAN1_RXF0A = 0;
    return len;
}

void fdcan_init(void) {
    /* 1. 使能时钟: GPIOD + FDCAN */
    RCC_AHB4ENR |= (1 << 3);   /* GPIODEN */
    RCC_APB1HENR |= (1 << 8);  /* FDCANEN */
    __asm__ volatile("dsb; isb");
    for (volatile int i = 0; i < 8; i++) {};

    /* 2. 配置 PD0(RX) / PD1(TX) 为 AF9 (FDCAN1) */
    /*    MODER: PD0=AF(10), PD1=AF(10) */
    *(volatile uint32_t *)GPIOD_MODER = (*(volatile uint32_t *)GPIOD_MODER & ~0xF) | 0xA;
    /*    AFRL: PD0=AF9, PD1=AF9 */
    *(volatile uint32_t *)GPIOD_AFRL = (*(volatile uint32_t *)GPIOD_AFRL & ~0xFF) | 0x99;

    /* 3. FDCAN1 进入初始化模式 */
    FDCAN1_CCCR = FDCAN_CCCR_INIT | FDCAN_CCCR_CCE;
    { uint32_t t=TIMEOUT; while(!(FDCAN1_CCCR & FDCAN_CCCR_INIT)&&--t){} }

    /* 4. 位时序配置 */
    FDCAN1_NBTP = (105 << 16) | (20 << 8) | (10 << 0) | (1<<25);
    FDCAN1_DBTP = 0;

    /* 5. 退出初始化模式 */
    FDCAN1_CCCR = 0;
    { uint32_t t=TIMEOUT; while((FDCAN1_CCCR & FDCAN_CCCR_INIT)&&--t){} }

    /* 6. 配置消息 RAM */
    FDCAN1_RXF0C = (1<<31) | (FDCAN1_RX_FIFO0_OFFSET / 4);
    FDCAN1_RXF0A = 0;
    FDCAN1_TXBC = (FDCAN1_TX_FIFO_OFFSET / 4);

    uint32_t *sram = (uint32_t *)FDCAN1_MSGRAM;
    sram[0x800/4] = 0;
    sram[0x800/4 + 1] = (1<<27);

    /* 7. 中断配置 */
    FDCAN1_IE = (1 << 0);
    FDCAN1_ILS = 0;
    FDCAN1_ILE = (1 << 0);

    canopen_state = NMT_INITIALISING;
    canopen_ticks = 0;
}
