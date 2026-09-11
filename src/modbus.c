/*
 * modbus.c — Modbus RTU 从站 (通信域 COMM), ISR 内每拍分摊执行 — H723 移植版
 *
 * 来源: esp32-core0/components/core0/modbus.c (370 行)
 * 移植改动**只有三类** (其余逐字保留, 包括全部设计注释与 WCET 说明):
 *   ① ESP-IDF UART 驱动 (UART_LL_GET_HW / uart_ll_*_fifo / uart_param_config)
 *      → H723 的 USART2 寄存器直读 (USART_ISR/RDR/TDR/BRR)
 *   ② IRAM_ATTR → ATTR_ITCM (热路径进 ITCM 是本项目的成本铁律)
 *   ③ Xtensa `memw` → ARM `dsb`; `memcpy`(热路径) → 显式循环
 *      (本项目铁律: 热路径内存搬运不交给 libc —— newlib nano memcpy 14.8 cyc/字节)
 *
 * 设计四原则 (照搬, 一条未改):
 *  ① 协议优先: 标准 Modbus RTU — CRC16(0xA001)/3.5 字符帧边界/标准异常码/
 *     功能码 03(读保持) 06(写单) 16(写多)。不为了分摊牺牲任何协议语义。
 *  ② ISR 内每拍分摊: RX/TX/★响应组装+CRC 全部按 MB_TICK_BUDGET 字节/拍推进
 *     (原实现在 EXEC 单拍完成整帧解析+组装+CRC, qty=125 时 255 字节单拍 CRC
 *      → 数千 cyc 尖峰; 现改为 BUILD 状态逐字节组装 + 增量 CRC)
 *  ③ 传输层解耦: 字节源 = USART2 FIFO 轮询 或 隧道注入(0x60, 零硬件验证)
 *  ④ 冷启动: mb_reset() 清全部运行态 (由 cold_start_reset 调用)
 *
 * 寄存器映射 (对标 S7-1500: 保持寄存器 = 显式划定的通信数据区):
 *   40001-40064  读区 MB_HOLD: wire[0..63] 工程量 (×100 取整, 只读)
 *   40065-40128  写区 MB_SET : 上位机设定值 (可读写, DSL 显式引用 → SRC_HMI)
 *   ★ 写读区(40001-40064) 返回异常 02 — 唯一写者语义 (同 Force 教训)
 *
 * 每拍 WCET 说明:
 *   RX / TX / BUILD: ≤ MB_TICK_BUDGET(4) 字节 → 有界常数
 *   请求 CRC 校验: 单拍整段 (≤MB_MAX_FRAME-2 = 253 字节; M2 修复后由 126 升) —
 *     "外部请求长度受限"项, 上界随 MB_MAX_FRAME 收敛 (非无界)
 *     ★ H723 上要实测: ITCM 取指下应显著快于 S3 的值 (成本表是代码的函数)
 */

#include "modbus.h"
#include "engine.h"
#include "regs.h"

#define ATTR_ITCM __attribute__((section(".itcm_text"), noinline))

/* SHM 访问一律走显式 base (与 engine_seq_tick 同风格: 可测、无隐藏全局状态)
 * ★ 这些访问器**不加 ATTR_ITCM**: 它们只是指针算术, 内联后是零成本的,
 *   而 ATTR_ITCM 里带 noinline —— 给 inline 函数加 noinline 会直接编译失败
 *   (第一条构建错误就是这个)。真正需要进 ITCM 的是下面那些**大函数**:
 *   mb_tick / mb_parse_frame / mb_crc16 / mb_pull_rx / mb_push_tx。 */
#define MB_PTR(base, off)  ((void *)((base) + (off)))

static inline MbCtrl_t *mb_ctrl(uint8_t *base) { return (MbCtrl_t *)MB_PTR(base, OFF_MB_CTRL); }
static inline uint8_t *mb_rx(uint8_t *base)    { return (uint8_t *)MB_PTR(base, OFF_MB_RX); }
static inline uint8_t *mb_tx(uint8_t *base)    { return (uint8_t *)MB_PTR(base, OFF_MB_TX); }
static inline uint16_t *mb_hold(uint8_t *base) { return (uint16_t *)MB_PTR(base, OFF_MB_HOLD); }
static inline uint16_t *mb_set(uint8_t *base)  { return (uint16_t *)MB_PTR(base, OFF_MB_SET); }

/* ---- CRC16 (Modbus 多项式 0xA001) ---- */
static uint16_t ATTR_ITCM mb_crc16(const uint8_t *buf, uint16_t len)
{
    uint16_t crc = 0xFFFF;
    for (uint16_t i = 0; i < len; i++) {
        crc ^= buf[i];
        for (int b = 0; b < 8; b++)
            crc = (crc & 1) ? (uint16_t)((crc >> 1) ^ 0xA001) : (uint16_t)(crc >> 1);
    }
    return crc;
}

/* 增量 CRC 单步 (BUILD 状态用: 逐字节累加, 避免整段单拍) */
static inline uint16_t crc16_step(uint16_t crc, uint8_t b)
{
    crc ^= b;
    for (int i = 0; i < 8; i++)
        crc = (crc & 1) ? (uint16_t)((crc >> 1) ^ 0xA001) : (uint16_t)(crc >> 1);
    return crc;
}

/* 寄存器读取: idx<64 读区, ≥64 写区 */
static inline uint16_t mb_reg(uint8_t *base, uint16_t idx)
{
    return (idx < MB_NREG) ? mb_hold(base)[idx] : mb_set(base)[idx - MB_NREG];
}

/* ---- 传输层: 拉字节 (隧道注入 或 USART2 FIFO 轮询) ----
 * ★ 与 S3 的差异: ESP32 能读 `hw->status.rxfifo_cnt` 直接拿到 FIFO 深度;
 *   H7 的 USART 没有"当前 FIFO 字节数"寄存器 (只有 RXFNE 标志)。
 *   ⇒ 改为"最多尝试 max_n 次, 每次收 1 字节, 直到 RXNE 落" —— 语义等价。
 *   ★ 顺带: 不使能 H7 的 FIFO (CR1.FIFOEN=0, 单字节缓冲) 也完全够 ——
 *     115200 @ 100μs/拍 → 每拍最多到达 1.15 字节, 单字节缓冲不会溢出。
 *     不开 FIFO 少一处配置, 确定性更好。 */
static int ATTR_ITCM mb_pull_rx(uint8_t *base, uint8_t *dst, int max_n)
{
    MbCtrl_t *c = mb_ctrl(base);
    if (c->src == 1) {
        /* 隧道模式: 从 SHM RX 缓冲搬 (显式循环, 不走 libc) */
        if (c->rx_pos >= c->rx_len) return 0;
        int n = (int)c->rx_len - (int)c->rx_pos;
        if (n > max_n) n = max_n;
        const uint8_t *src = mb_rx(base) + c->rx_pos;
        for (int i = 0; i < n; i++) dst[i] = src[i];
        c->rx_pos = (uint8_t)(c->rx_pos + n);
        return n;
    }
    /* 物理口: 轮询 (不开中断 —— 见 modbus.h 的说明) */
    int n = 0;
    while (n < max_n) {
        if (!(USART_ISR(USART2_BASE) & USART_ISR_RXNE)) break;
        dst[n++] = (uint8_t)(USART_RDR(USART2_BASE) & 0xFFu);
        /* ★ 读 RDR 同时清 RXNE/ORE; S3 那边由 uart_ll_read_rxfifo 完成同样的事 */
    }
    return n;
}

static int ATTR_ITCM mb_push_tx(uint8_t *base, const uint8_t *src, int n)
{
    MbCtrl_t *c = mb_ctrl(base);
    if (!c->tx_uart) return n;      /* 缓冲模式: 响应留 TX 缓冲供 0x61 读回 */
    int sent = 0;
    while (sent < n) {
        if (!(USART_ISR(USART2_BASE) & USART_ISR_TXE)) break;
        USART_TDR(USART2_BASE) = src[sent++];
    }
    return sent;
}

/* ---- 置异常响应上下文 (不直接构造, 由 BUILD 状态逐字节生成) ---- */
static void ATTR_ITCM mb_set_exc(uint8_t *base, uint8_t func, uint8_t exc)
{
    MbCtrl_t *c = mb_ctrl(base);
    c->b_func = (uint8_t)(func | 0x80);
    c->b_start = 0;
    c->b_qty = exc;
    c->b_len = 3;
    c->b_pos = 0;
    c->crc_acc = 0xFFFF;
    c->err_exc++;
}

/* ---- 响应字节生成器 (pos: 0..b_len-1) ---- */
static uint8_t ATTR_ITCM mb_resp_byte(uint8_t *base, uint16_t pos)
{
    MbCtrl_t *c = mb_ctrl(base);
    uint8_t f = (uint8_t)(c->b_func & 0x7F);
    if (c->b_func & 0x80) {                    /* 异常: [addr][func|80][exc] */
        if (pos == 0) return c->slave_addr;
        if (pos == 1) return c->b_func;
        return (uint8_t)(c->b_qty & 0xFF);
    }
    switch (f) {
    case 0x03:
        if (pos == 0) return c->slave_addr;
        if (pos == 1) return 0x03;
        if (pos == 2) return (uint8_t)(c->b_qty * 2);
        {                                      /* 数据段: 每寄存器 2 字节大端 */
            uint16_t i = (uint16_t)(pos - 3);
            uint16_t v = mb_reg(base, (uint16_t)(c->b_start + (i >> 1)));
            return (i & 1) ? (uint8_t)(v & 0xFF) : (uint8_t)(v >> 8);
        }
    case 0x06:
    case 0x10:                                 /* 回显请求前 6 字节 */
        if (pos == 0) return c->slave_addr;
        if (pos == 1) return f;
        if (pos == 2) return (uint8_t)(c->b_start >> 8);
        if (pos == 3) return (uint8_t)(c->b_start & 0xFF);
        if (pos == 4) return (uint8_t)(c->b_qty >> 8);
        return (uint8_t)(c->b_qty & 0xFF);
    default:
        return 0;
    }
}

/* ---- 解析请求 → 置构建上下文 (轻量, 不组装响应) ----
 * respond=1 → BUILD (正常响应或异常响应); respond=0 → IDLE (静默丢弃) */
static void ATTR_ITCM mb_parse_frame(uint8_t *base)
{
    MbCtrl_t *c = mb_ctrl(base);
    uint8_t *rx = mb_rx(base);
    uint16_t len = c->rx_len;
    int respond = 1;

    c->frames_rx++;

    if (len < 4) {
        respond = 0;                                  /* 太短 */
    } else if (rx[0] == 0 || rx[0] != c->slave_addr) {
        respond = 0;                                  /* 广播 / 非本站 */
    } else {
        /* CRC 校验 (低字节在前)。★WCET: 单拍 ≤253B (M2 修复后), 上界随 MB_MAX_FRAME 收敛 */
        uint16_t crc_recv = (uint16_t)(rx[len - 2] | ((uint16_t)rx[len - 1] << 8));
        if (mb_crc16(rx, (uint16_t)(len - 2)) != crc_recv) {
            c->err_crc++;
            respond = 0;                              /* 坏帧: 丢弃不响应 */
        } else {
            uint8_t func = rx[1];
            switch (func) {
            case 0x03: {    /* 读保持寄存器 */
                if (len < 8) { mb_set_exc(base, func, MB_EX_ILLEGAL_VAL); break; }
                uint16_t start = (uint16_t)(rx[2] << 8 | rx[3]);
                uint16_t qty   = (uint16_t)(rx[4] << 8 | rx[5]);
                if (qty == 0 || qty > 125) { mb_set_exc(base, func, MB_EX_ILLEGAL_VAL); break; }
                uint16_t idx = (start >= 40001) ? (uint16_t)(start - 40001) : 0xFFFF;
                if (start < 40001 || (uint32_t)idx + qty > (uint32_t)(MB_NREG * 2)) {
                    mb_set_exc(base, func, MB_EX_ILLEGAL_ADDR); break;
                }
                c->b_func = 0x03; c->b_start = idx; c->b_qty = qty;
                c->b_len = (uint16_t)(3 + qty * 2);
                break;
            }
            case 0x06: {    /* 写单个保持寄存器 */
                if (len < 8) { mb_set_exc(base, func, MB_EX_ILLEGAL_VAL); break; }
                uint16_t start = (uint16_t)(rx[2] << 8 | rx[3]);
                uint16_t val   = (uint16_t)(rx[4] << 8 | rx[5]);
                uint16_t idx = (start >= 40001) ? (uint16_t)(start - 40001) : 0xFFFF;
                if (start < 40001 || idx >= MB_NREG * 2) {
                    mb_set_exc(base, func, MB_EX_ILLEGAL_ADDR); break;
                }
                if (idx < MB_NREG) { mb_set_exc(base, func, MB_EX_ILLEGAL_ADDR); break; }
                mb_set(base)[idx - MB_NREG] = val;
                c->b_func = 0x06; c->b_start = start; c->b_qty = val;
                c->b_len = 6;
                break;
            }
            case 0x10: {    /* 写多个保持寄存器 */
                if (len < 11) { mb_set_exc(base, func, MB_EX_ILLEGAL_VAL); break; }
                uint16_t start = (uint16_t)(rx[2] << 8 | rx[3]);
                uint16_t qty   = (uint16_t)(rx[4] << 8 | rx[5]);
                uint8_t  bc    = rx[6];
                if (qty == 0 || qty > 123 || bc != qty * 2 || len < (uint16_t)(9 + bc)) {
                    mb_set_exc(base, func, MB_EX_ILLEGAL_VAL); break;
                }
                uint16_t idx = (start >= 40001) ? (uint16_t)(start - 40001) : 0xFFFF;
                if (start < 40001 || (uint32_t)idx + qty > (uint32_t)(MB_NREG * 2)) {
                    mb_set_exc(base, func, MB_EX_ILLEGAL_ADDR); break;
                }
                if (idx < MB_NREG) { mb_set_exc(base, func, MB_EX_ILLEGAL_ADDR); break; }
                for (uint16_t i = 0; i < qty; i++)
                    mb_set(base)[idx + i - MB_NREG] =
                        (uint16_t)(rx[7 + i * 2] << 8 | rx[8 + i * 2]);
                c->b_func = 0x10; c->b_start = start; c->b_qty = qty;
                c->b_len = 6;
                break;
            }
            default:
                mb_set_exc(base, func, MB_EX_ILLEGAL_FUNC);
                break;
            }
            /* 成功路径初始化构建进度 (异常路径已由 mb_set_exc 设好) */
            if (!(c->b_func & 0x80)) { c->b_pos = 0; c->crc_acc = 0xFFFF; }
        }
    }

    c->rx_len = 0; c->rx_pos = 0;
    c->silent = 0;
    c->state = respond ? MB_ST_BUILD : MB_ST_IDLE;
}

/* ---- ISR 每拍推进 (核心: 分摊 + 限速) ---- */
void ATTR_ITCM mb_tick(uint8_t *base)
{
    MbCtrl_t *c = mb_ctrl(base);
    if (!c->enabled) return;

    uint8_t budget = c->tick_budget ? c->tick_budget : MB_TICK_BUDGET;
    uint8_t *rx = mb_rx(base);

    switch (c->state) {
    case MB_ST_IDLE:
    case MB_ST_RX: {
        uint8_t tmp[MB_TICK_BUDGET];
        int n = mb_pull_rx(base, tmp, budget);
        if (n > 0) {
            for (int i = 0; i < n && c->rx_len < MB_MAX_FRAME; i++)
                rx[c->rx_len++] = tmp[i];
            c->silent = 0;
            c->state = MB_ST_RX;
        } else if (c->state == MB_ST_RX) {
            /* 隧道注入的帧: rx_pos 已达 rx_len (视为收满) → pull 返回 0 → 累计静默 */
            if (++c->silent >= MB_SILENT_TICKS) {
                if (c->rx_len >= 4) { c->state = MB_ST_EXEC; }
                else { c->rx_len = 0; c->rx_pos = 0; c->silent = 0; c->state = MB_ST_IDLE; }
            }
        }
        break;
    }
    case MB_ST_EXEC:
        mb_parse_frame(base);                   /* 轻量: 置上下文后转 BUILD/IDLE */
        break;

    case MB_ST_BUILD: {                        /* 逐字节组装 + 增量 CRC (不单拍) */
        uint16_t total = (uint16_t)(c->b_len + 2);   /* + CRC16 两字节 (≤255) */
        uint8_t n = 0;
        uint8_t *tx = mb_tx(base);
        /* ★★ 界限用 MB_TX_SIZE(256, 响应缓冲), **不是** MB_MAX_FRAME(128, 请求上限)。
         *   这是外部审计 W4 的 M1 (P1) 的修复本体: 旧写法 `c->b_pos < MB_MAX_FRAME`
         *   让 qty≥62 (total=2qty+5>128) 时 b_pos 永远到不了 total ⇒ 状态死在 BUILD
         *   ⇒ mb_inject 的 `state != IDLE` 守卫恒真 ⇒ 之后所有注入 NAK busy,
         *   一条**完全合法**的读请求即可让通信域永久不可用 (只能 RESET/断电)。
         *   total ≤ 2×125+5 = 255 < 256, 天然安全。 */
        while (n < budget && c->b_pos < total && c->b_pos < MB_TX_SIZE) {
            uint8_t b;
            if (c->b_pos < c->b_len) {
                b = mb_resp_byte(base, c->b_pos);
                c->crc_acc = crc16_step(c->crc_acc, b);
            } else if (c->b_pos == c->b_len) {
                b = (uint8_t)(c->crc_acc & 0xFF);        /* CRC 低字节先发 */
            } else {
                b = (uint8_t)(c->crc_acc >> 8);
            }
            tx[c->b_pos] = b;
            c->b_pos++;
            n++;
        }
        if (c->b_pos >= total) {
            c->tx_len = (uint8_t)total;
            c->tx_sent = 0;
            c->state = MB_ST_TX;
        } else if (c->b_pos >= MB_TX_SIZE) {
            /* ★ 未完成保护 (同 OA17 原则: 宁可丢一帧响应, 也不让通信域永久 busy)。
             *   正常路径**不会**到这里 (total ≤ 255 < 256); 一旦触发说明界限/长度
             *   算法出了新错 —— 静默丢弃该帧并计一次异常, 保持通信域可用,
             *   而不是把状态机永久钉死在 BUILD (那正是 M1 的故障形态)。 */
            c->err_exc++;
            c->tx_len = 0; c->tx_sent = 0;
            c->rx_len = 0; c->rx_pos = 0;
            c->state = MB_ST_IDLE;
        }
        break;
    }
    case MB_ST_TX: {
        uint16_t left = (uint16_t)(c->tx_len - c->tx_sent);
        if (!left) {
            c->frames_tx++;
            if (c->tx_uart) { c->tx_len = 0; c->tx_sent = 0; }
            c->state = MB_ST_IDLE;
            break;
        }
        int n = budget < left ? budget : (int)left;
        int sent = mb_push_tx(base, mb_tx(base) + c->tx_sent, n);
        c->tx_sent = (uint8_t)(c->tx_sent + sent);
        if (c->tx_sent >= c->tx_len) {
            c->frames_tx++;
            if (c->tx_uart) { c->tx_len = 0; c->tx_sent = 0; }
            c->state = MB_ST_IDLE;
        }
        break;
    }
    default:
        c->state = MB_ST_IDLE;
        break;
    }
}

/* ---- 读区刷新: wire[0..63] 工程量 (×100 取整) → MB_HOLD ---- */
void mb_refresh_hold(uint8_t *base)
{
    uint16_t *hold = mb_hold(base);
    const float *wm = (const float *)MB_PTR(base, OFF_WIRE_MAP);
    for (int i = 0; i < MB_NREG; i++) {
        float v = wm[i] * 100.0f;
        if (!(v > 0.0f)) v = 0.0f;           /* 非有限/负 → 0 */
        if (v > 65535.0f) v = 65535.0f;
        hold[i] = (uint16_t)v;
    }
}

/* ---- 冷启动复位 (清运行态 + 统计, 保留配置) ---- */
void mb_reset(uint8_t *base)
{
    MbCtrl_t *c = mb_ctrl(base);
    c->state = MB_ST_IDLE;
    c->rx_len = 0; c->rx_pos = 0;
    c->tx_len = 0; c->tx_sent = 0;
    c->silent = 0;
    c->b_func = 0; c->b_start = 0; c->b_qty = 0;
    c->b_pos = 0; c->b_len = 0; c->crc_acc = 0xFFFF;
    c->frames_rx = 0; c->frames_tx = 0;
    c->err_crc = 0; c->err_exc = 0;
    __asm__ volatile("dsb" ::: "memory");
}

/* ---- 隧道注入 (0x60 的载荷) ---- */
int mb_inject(uint8_t *base, const uint8_t *frame, uint16_t n)
{
    if (n < 4 || n > MB_MAX_FRAME) return -1;
    MbCtrl_t *c = mb_ctrl(base);
    if (c->state != MB_ST_IDLE || c->rx_len) return -2;    /* 忙碌 */
    uint8_t *rx = mb_rx(base);
    for (uint16_t i = 0; i < n; i++) rx[i] = frame[i];
    c->src = 1;              /* 隧道模式 */
    c->rx_len = (uint8_t)n;
    c->rx_pos = (uint8_t)n;  /* ★ 隧道: 视为**已收满** (pull 返回 0 → 静默判定 → EXEC)。
                              *   不能置 0, 否则状态机会把缓冲里的帧再读一遍追加到尾部
                              *   (rx_len 翻倍 → CRC 校验失败)。这是 S3 踩过的坑, 原样保留。 */
    c->silent = 0;
    c->state = MB_ST_RX;     /* 收完 rx_len 后自然进入静默判定 → EXEC */
    __asm__ volatile("dsb" ::: "memory");
    return 0;
}

/* ---- 物理口使能 (USART2: PA2=TX / PA3=RX, 8N1, 轮询) ---- */
void mb_uart_enable(void)
{
    /* ① 时钟 */
    RCC_APB1LENR |= RCC_APB1LENR_USART2EN;

    /* ② GPIO: PA2/PA3 → AF7 (读-改-写, 绝不整寄存器赋值 —— uart.c 的既有纪律:
     *    PA8(拍输出) 就在同一个 MODER 里, 整赋值会把拍输出打掉) */
    uint32_t mod = GPIO_MODER(0);
    mod &= ~(3u << (2 * 2));  mod |= (2u << (2 * 2));   /* PA2 = AF (10b) */
    mod &= ~(3u << (3 * 2));  mod |= (2u << (3 * 2));   /* PA3 = AF (10b) */
    GPIO_MODER(0) = mod;

    uint32_t afr = GPIO_AFRL(0);                        /* PA0..PA7 在 AFRL */
    afr &= ~(0xFu << (2 * 4));  afr |= (7u << (2 * 4)); /* PA2 → AF7 (USART2) */
    afr &= ~(0xFu << (3 * 4));  afr |= (7u << (3 * 4)); /* PA3 → AF7 (USART2) */
    GPIO_AFRL(0) = afr;

    /* ③ 波特率: BRR = PCLK1/baud = 100e6/115200 = 868 (OVER8=0)
     *    ★ 不照抄常数 —— 见 regs.h 里 USART2_BRR_115200 的 16 倍错教训 */
    USART_BRR(USART2_BASE) = USART2_BRR_115200;
    USART_PRESC(USART2_BASE) = 0;                       /* 不分频 (BRR 已按 PCLK1 算) */

    /* ④ 使能: UE | TE | RE —— **不开 RXNEIE** (轮询, 见 modbus.h 说明) */
    USART_CR1(USART2_BASE) = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
    (void)USART_ISR(USART2_BASE);                       /* 读一次清初值 */
    __asm__ volatile("dsb" ::: "memory");
}

/* ---- 建立配置 (由 cold_start_reset 每次调用) ----
 * 见 modbus.h 里"为什么要拆成两个函数"的说明。 */
void mb_config(uint8_t *base, uint8_t slave_addr, int use_uart)
{
    MbCtrl_t *c = mb_ctrl(base);
    /* ★ 清零控制块: 用显式循环而非 memset(sizeof) —— 结构体是 packed 40B,
     *   libc memset 在这里没有优势, 而"热路径外就能随便用 libc"是一种滑坡。 */
    uint8_t *cb = (uint8_t *)c;
    for (uint32_t i = 0; i < sizeof(MbCtrl_t); i++) cb[i] = 0;

    c->slave_addr = slave_addr ? slave_addr : MB_DEFAULT_ADDR;
    c->tick_budget = MB_TICK_BUDGET;
    c->enabled = 1;
    c->state = MB_ST_IDLE;
    c->crc_acc = 0xFFFF;
    c->src = use_uart ? 0 : 1;      /* RX: 0=USART2 FIFO, 1=隧道 (默认隧道: 零硬件可测) */
    c->tx_uart = 0;                 /* TX: 默认留缓冲 (0x61 可读); 0x62 可切物理口 */

    uint8_t *rx = mb_rx(base), *tx = mb_tx(base);
    for (int i = 0; i < MB_MAX_FRAME; i++) { rx[i] = 0; tx[i] = 0; }
    uint16_t *h = mb_hold(base), *s = mb_set(base);
    for (int i = 0; i < MB_NREG; i++) { h[i] = 0; s[i] = 0; }

    /* ★ 不在这里调 mb_uart_enable(): 它要动 RCC/GPIO, 不该随每次冷启动重复执行,
     *   且 cold_start_reset 可能在 UART 尚未初始化时被调用 (上电早期)。
     *   UART 使能由 main() 显式调一次 (见 modbus.h)。 */
}
