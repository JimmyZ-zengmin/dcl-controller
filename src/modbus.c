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
#include "faultlog.h"   /* 统一故障台账: 通信域每一类异常都留案底 (见 faultlog.h) */

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

/* ★★ 字节级观测面 (见 engine.h 的 OFF_MB_DIAG 说明)。
 *   起因: frames_rx/err_crc 只统计**完整帧**, 而"1~3 字节被静默丢弃"这条路径不计数
 *   ⇒ "线上死寂" 与 "字节到了但框不成帧" 读数一样, 判据分不清"没坏"与"没跑"。 */
#define MB_DIAG_BYTES   0u   /* 累计从物理口拉到的字节数 */
#define MB_DIAG_MAXRX   1u   /* 见过的最大 rx_len */
#define MB_DIAG_SHORT   2u   /* 因太短(<4B)被丢弃的次数 */
#define MB_DIAG_LASTISH 3u   /* 最近一次 USART2 ISR 原值 */
#define MB_DIAG_ERRACC  4u   /* ISR 错误位累加 (PE|FE|NE|ORE) */
#define MB_DIAG_LASTBYT 5u   /* 最近拉到的字节值 */
#define MB_DIAG_CFGMOD  8u   /* GPIOD MODER 实际读回 */
#define MB_DIAG_CFGAFR  9u   /* GPIOD AFRL 实际读回 */
#define MB_DIAG_CFGPUP 10u   /* GPIOD PUPDR 实际读回 */
#define MB_DIAG_CFGMK  11u   /* 配置读回标记 */
/* ★ 15 是**清错误标志的次数** (2026-09-13 补, 审计建议 §4.3)。
 *   原来只有 ERRACC = "见过哪些错误位" (OR 累加, 只增不减), 于是
 *   "ORE 被反复清掉、接收反复复活" 与 "ORE 从未出现过" 在读数上**完全一样** ——
 *   而这正是本次故障的核心机制。⇒ 一个"该变而没变"的量必须能被单独读走。 */
#define MB_DIAG_ERRCLR 15u
/* ══════════ 响应延迟观测面 (2026-09-13 新增) ══════════
 * ★ 为什么必须补这个: 此前所有"响应时间"的数字都来自 **PC 侧**(经 USB) ——
 *   而 USB/CH340 的开销 (实测 2.2ms) 比被测的改进量还大, 等于用一把能量 1 米的尺
 *   去量 1 毫米的改动。⇒ 把"请求最后一字节到达 → 响应第一字节发出"的**板内**
 *   周期数记下来: 不含 USB、不含线缆, 直接反映固件自身的处理延迟。
 * ★ 量化误差: 请求侧的时刻取自"拉到该字节的那一拍", 故有 ≤1 拍 (100µs) 的量化;
 *   响应侧的时刻是精确的 (写 TDR 那一下)。比较**改前/改后**时量化误差是共模的。
 * ★★ 单位是 **100µs 拍**而不是 CPU 周期 —— 这是**故意的** (2026-09-13 踩到):
 *   第一版用 `DWT_CYCCNT`, 实测 `LAT_N=227` 却 `LAT_LAST=0` —— 因为
 *   **调试器(pyocd)会话会静默停掉 DWT_CYCCNT**(本项目"铁律 0"的既知血证:
 *     "一切正常, 只有时间量是 0")。⇒ 用 DWT 当时间源, 就会把"调试器来过"读成
 *   "延迟为 0"。改成**自己的拍计数**后, 该观测量不再依赖任何可被外部关掉的东西。
 *   (这本身就是铁律 0 的又一次应用: 观测量不能依赖"可能被观测动作改变"的状态。)
 *   ⇒ 读数换算: 1 拍 = 100µs。 */
#define MB_DIAG_LAT_LAST 23u  /* 最近一次: 响应延迟 (CPU 周期) */
#define MB_DIAG_LAT_MIN  24u
#define MB_DIAG_LAT_MAX  25u
#define MB_DIAG_LAT_N    26u  /* 样本数 */
#define MB_DIAG_T_RX     27u  /* 内部: 最近一次拉到字节的 DWT 时刻 */
#define MB_DIAG_FASTOK   28u  /* 内部: 早判帧 (CRC 门) 成功次数 */
#define MB_DIAG_RX_FULL  29u  /* 内部: RX 缓冲被填满的次数 (帧过长/无帧间隔) */

/* 通信域自己的拍计数 (每次 mb_tick +1, 单位 100μs)。
 * ★ 为什么不用 DWT_CYCCNT: 见上面 MB_DIAG_LAT_* 的注释 —— 调试器会话会静默把它停掉,
 *   于是"延迟"会被读成 0。自己的计数器不依赖任何可被外部关掉的状态 (铁律 0)。 */
static uint32_t s_mb_tick;
static inline volatile uint32_t *mb_diag(uint8_t *base)
{
    return (volatile uint32_t *)(base + OFF_MB_DIAG);
}

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
 *   ⇒ 改为"最多尝试 max_n 次, 每次收 1 字节, 直到 RXFNE 落" —— 语义等价。
 *
 * ★★★ 这里原来有一段**拿平均值当上界**的推理错误 (2026-09-13 实测推翻):
 *     原文主张 "不使能 FIFO 也完全够 —— 115200 @ 100µs/拍 → 每拍最多到达 1.15 字节"。
 *     "1.15" 是**均值, 不是上界**: 字节间隔 = 10bit/115200 = **86.8µs**,
 *     而拍周期 = **100µs** —— **拍比字节慢**, 相位每字节漂 13.2µs,
 *     于是**周期性出现"一拍内到 2 字节"** ⇒ 单字节 RDR 必然溢出 ⇒ ORE。
 *   实测对照 (tools/h723_485_ore_verify.py):
 *     慢发 (逐字节 2000µs) → **8/8 全收**      ← 链路/引脚/PD6 信号全是好的
 *     快发 (整帧连续)      → **3/8, 必然 ORE** ← 问题纯在接收节奏
 *     开 FIFOEN 后快发     → **32/32 全收 + 整帧 maxrx=8 + 总线上应答正常**
 *   ⇒ 修法: mb_uart_enable() 打开 CR1.FIFOEN (深度 8 吸收拍间积压)。
 *   完整证据链与对照: docs/audit/H723-485-RX-AUDIT.md
 *
 * ★★ 配套: 见 mb_rx_clear_errs() —— ORE 不清会**自锁死**接收。 */
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
    /* 物理口: 轮询 (不开中断 —— 见 modbus.h 的说明)
     * ★ 开 FIFO 之后 bit5 的语义是 RXFNE (FIFO 非空), 而读 RDR 会把 FIFO 逐字节弹出
     *   ⇒ 下面这个"读一次 ISR、只要 RXFNE 还在就继续读 RDR"的循环天然多字节搬运,
     *     一个拍内最多搬 MB_TICK_BUDGET(4) 个 —— '一次只读 1 字节' 的旧顾虑不复存在。 */
    int n = 0;
    volatile uint32_t *d = mb_diag(base);
    while (n < max_n) {
        uint32_t isr = USART_ISR(USART2_BASE);
        /* ★★ 必须先清错误标志, 再判 RXFNE —— 2026-09-13 实测修复:
         *   若只剩 `if (!(isr & RXNE)) break;` —— 一旦 **ORE=1 且 RXNE=0**,
         *   就永远 break、**永不读 RDR** ⇒ ORE 永不清; 而 H7 在 ORE 置位期间
         *   **丢弃所有新收到的字符** ⇒ RXNE 再也不置位 ⇒ **接收自锁死**。
         *   实测: 该状态下灌 12 帧收 0 字节; 写 ICR 清错误后**立刻复活**
         *         (0 → 4 字节)。这就是"偶尔收到一点点、然后就没有了"的真因。
         *   ★ 注意这里**既不 break 也不 continue**: 错误标志与"FIFO 里还有数据"
         *     是两件事, 清完错误要继续往下判 RXNE/读 RDR。 */
        if (isr & (USART_ISR_PE | USART_ISR_FE | USART_ISR_NE | USART_ISR_ORE)) {
            USART_ICR(USART2_BASE) = 0x1FFu;      /* 写 1 清 (实测有效) */
            d[MB_DIAG_ERRACC] |= (isr & (USART_ISR_PE | USART_ISR_FE
                                         | USART_ISR_NE | USART_ISR_ORE));
            /* ★ 审计建议: 原来只记"见过什么错", 不记"清过几次" ——
             *   于是"ORE 被反复清掉"与"ORE 从未出现"在读数上完全一样。
             *   要能分开, 就必须有一个独立计数 (同族: "该变而没变"必须可读)。 */
            d[MB_DIAG_ERRCLR] += 1u;
            /* ★ 台账: 上下文带 isr 原值与当时的 rx_len —— 这正是 485 那次事故里
             *   最想问的两个量 (ORE=1 且 RXNE=0 那一刻的状态)。 */
            fault_record(base, FAULT_MB_ORE, s_mb_tick, isr, (uint32_t)c->rx_len);
        }
        if (!(isr & USART_ISR_RXNE)) break;
        dst[n++] = (uint8_t)(USART_RDR(USART2_BASE) & 0xFFu);
        /* ★★ 每拉到一个字节就记一笔 (见 engine.h 的 OFF_MB_DIAG 说明):
         *   这条路径原来**只体现在 rx_len 上**, 而 rx_len 会在 400us 静默后归零
         *   ⇒ 外部读不到"曾经有字节到过"任何证据。 */
        d[MB_DIAG_BYTES]  += 1u;
        d[MB_DIAG_LASTISH] = isr;
        d[MB_DIAG_LASTBYT] = (uint32_t)dst[n - 1];
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
    /* ★ 响应延迟观测: 本次是这条响应的**第一个**字节 (c->tx_sent 是调用前的偏移)
     *   ⇒ 此刻 − 请求最后一字节到达时刻 = 板内处理延迟 (含静默等待 + 解析 + 组装)。 */
    if (sent > 0 && c->tx_sent == 0u) {
        volatile uint32_t *d = mb_diag(base);
        uint32_t lat = s_mb_tick - d[MB_DIAG_T_RX];      /* 单位: 100µs 拍 */
        d[MB_DIAG_LAT_LAST] = lat;
        if (d[MB_DIAG_LAT_N] == 0u || lat < d[MB_DIAG_LAT_MIN]) d[MB_DIAG_LAT_MIN] = lat;
        if (lat > d[MB_DIAG_LAT_MAX]) d[MB_DIAG_LAT_MAX] = lat;
        d[MB_DIAG_LAT_N] += 1u;
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
    /* ★ 台账: 上下文 = (功能码, 异常码) —— 排障要知道"哪类请求被哪种原因拒了" */
    fault_record(base, FAULT_MB_EXC, s_mb_tick, func, exc);
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
            /* ★ 台账: 上下文 = (收到长度, 收到CRC) —— "哪个长度上开始错"一眼可见 */
            fault_record(base, FAULT_MB_CRC, s_mb_tick, (uint32_t)len, (uint32_t)crc_recv);
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

/* ---- 请求帧的**确定长度** (2026-09-13 早判帧用) ----
 * Modbus RTU 靠 3.5 字符静默划帧, 但**多数请求的长度是可以算出来的**:
 *   0x01..0x08 (读写单个/位操作)  → [addr][func][X2][Y2][crc2] = **8 字节**
 *   0x0F / 0x10 (写多个)          → [addr][func][start2][qty2][bc][data…][crc2]
 *                                   = **9 + bc**, bc = rx[6]
 *   其它功能码                    → 0 (未知 ⇒ 交给静默判帧兜底)
 * ⇒ "长度已到 + **CRC 通过**" 是比"等静默"更强的完整帧判据 (CRC-16 误判 1/65536)。
 *   ★ 返回 0 表示"此刻还判不出来", 调用者必须保持沉默、不要误判。
 * ★ 整个函数被 `#if MB_FAST_FRAME` 包住: 对照档 (FAST_FRAME=0) 下没有任何调用点,
 *   而本工程的 `-Werror` 会把 "defined but not used" 变成编译失败 ——
 *   刚踩过: 对照构建因此**编译失败**, 而**失败的构建会让 pyocd flash 跳过烧录并返回 0**,
 *   于是"对照档"其实还在跑交付档 (症状: 对照读数与交付读数一模一样)。
 *   ⇒ 见下面 verify 步骤: 烧完必须**读回一个只有新固件才有的量**才算数。 */
#if MB_FAST_FRAME
static uint16_t ATTR_ITCM mb_expected_len(const uint8_t *rx, uint16_t len)
{
    switch (rx[1]) {
    case 0x01u: case 0x02u: case 0x03u: case 0x04u:
    case 0x05u: case 0x06u: case 0x07u: case 0x08u:
        return 8u;
    case 0x0Fu: case 0x10u:
        return (len >= 7u) ? (uint16_t)(9u + (uint16_t)rx[6]) : 0u;
    default:
        return 0u;
    }
}
#endif

/* ---- ISR 每拍推进 (核心: 分摊 + 限速) ---- */
void ATTR_ITCM mb_tick(uint8_t *base)
{
    MbCtrl_t *c = mb_ctrl(base);
    if (!c->enabled) return;

    s_mb_tick++;                                /* 本域自己的拍计数 (响应延迟测量用) */
    uint8_t budget = c->tick_budget ? c->tick_budget : MB_TICK_BUDGET;
    uint8_t *rx = mb_rx(base);
    /* ★★ 首个 tick 把 GPIOD 的**实际配置**读回来发布 (一次性)。
     *   起因: 调试器读不了外设, 而"引脚写过了就算"是著名的静默失败族 ——
     *   未开时钟时写入被丢弃 / 后面的 init 整寄存器覆盖, 两者都不报错。
     *   有了它, "PD6 到底是不是 AF7"就是读出来的事实, 不再是推断。 */
    {   volatile uint32_t *d = mb_diag(base);
        if (d[MB_DIAG_CFGMK] != 0xCF600001u) {
            d[MB_DIAG_CFGMOD] = GPIO_MODER(3) & 0xFFFFu;
            d[MB_DIAG_CFGAFR] = GPIO_AFRL(3);
            d[MB_DIAG_CFGPUP] = GPIO_PUPDR(3) & 0xFFFFu;
            /* ★ 连 USART2 自己的寄存器也读回来 —— "引脚对了但外设没使能"是同一族静默失败,
             *   而调试器读不了外设区, 只能靠固件自报。 */
            d[16] = USART_CR1(USART2_BASE);
            d[17] = USART_CR2(USART2_BASE);
            d[18] = USART_CR3(USART2_BASE);
            d[19] = USART_BRR(USART2_BASE);
            d[20] = USART_ISR(USART2_BASE);
            d[21] = USART_PRESC(USART2_BASE);
            d[22] = 0x05E20001u;
            d[MB_DIAG_CFGMK]  = 0xCF600001u;
        }
    }

    /* ══════════ ★★ 每拍无条件清接收错误标志 (2026-09-13 实测修复) ══════════
     * ★ 为什么必须在**这里**(而不是只在 mb_pull_rx 里):
     *   mb_pull_rx 只在 MB_ST_IDLE / MB_ST_RX 两个状态被调用; 状态机走到
     *   EXEC → BUILD → TX 期间**完全不读 RDR**。那段时间一旦来字节就必然 ORE,
     *   而 ORE 未清 ⇒ H7 丢弃后续所有字符 ⇒ 这一拍处理完回到 IDLE 时**接收已经死了**。
     *   (实测症状: 一帧被收下 → 进 BUILD/TX → 之后再也收不到, 只能等复位。)
     * ★ 位置: 在"首拍配置读回"**之后** —— 保留上电瞬间 ISR 的原值当证据, 不去盖掉它。
     * ★ 顺序: 先读 ISR 再写 ICR(写 1 清); 只清**错误位**那一组, 不动 RXNE。
     * ★ 这是"同一族缺陷只修了一半"的收口: USART1 在 uart.c 里已有 FE/NE/ORE 逐个清除,
     *   USART2 漏了 —— 而本次故障恰好就落在没修的那个口上。 */
    {
        uint32_t isr = USART_ISR(USART2_BASE);
        if (isr & (USART_ISR_PE | USART_ISR_FE | USART_ISR_NE | USART_ISR_ORE)) {
            USART_ICR(USART2_BASE) = 0x1FFu;
            volatile uint32_t *d = mb_diag(base);
            d[MB_DIAG_ERRACC] |= (isr & (USART_ISR_PE | USART_ISR_FE
                                         | USART_ISR_NE | USART_ISR_ORE));
            d[MB_DIAG_ERRCLR] += 1u;
            fault_record(base, FAULT_MB_ORE, s_mb_tick, isr, 0xE0u);   /* 0xE0=每拍清这一路 */
        }
    }

    switch (c->state) {
    case MB_ST_IDLE:
    case MB_ST_RX: {
        uint8_t tmp[MB_TICK_BUDGET];
        int n = mb_pull_rx(base, tmp, budget);
        if (n > 0) {
            int put = 0;
            for (int i = 0; i < n && c->rx_len < MB_MAX_FRAME; i++) {
                rx[c->rx_len++] = tmp[i];
                put++;
            }
            /* ★ 台账: 缓冲被填满而还有字节没放下 ⇒ 帧过长 / 无帧间隔。
             *   这条以前**没有任何量反映** —— 485 那次"帧间零间隔"实验里,
             *   板子收到 34739 字节却只结算出 1 帧, 当时没人知道是"缓冲满了"。 */
            if (put < n) {
                mb_diag(base)[MB_DIAG_RX_FULL] += 1u;
                fault_record(base, FAULT_MB_RX_FULL, s_mb_tick,
                             (uint32_t)c->rx_len, (uint32_t)(n - put));
            }
            c->silent = 0;
            c->state = MB_ST_RX;
            {   /* ★ 记下"到过多少字节" —— 这是"字节到了但框不成帧"的唯一指纹 */
                volatile uint32_t *d = mb_diag(base);
                if (c->rx_len > d[MB_DIAG_MAXRX]) d[MB_DIAG_MAXRX] = c->rx_len;
                /* ★ 响应延迟测量的**起点**: 本拍拉到过字节的时刻 (单位: 拍) */
                d[MB_DIAG_T_RX] = s_mb_tick;
            }
#if MB_FAST_FRAME
            /* ══════ ★★ 按长度早判帧 (2026-09-13 优化, A/B 开关 MB_FAST_FRAME) ══════
             * 老实现: 收到字节后必须再等 MB_SILENT_TICKS×100µs (400µs) 静默才敢判帧
             *         ⇒ 每条事务白付 400µs (8B 事务总长才 2.5ms, 占 16%!)。
             * 这里: 长度由功能码算出, 一到就用 **CRC 做闸门**当场判帧, 不等静默。
             * ★ 只在 `rx_len == 期望长度` 的那一拍试一次 —— 失败就退回静默路径,
             *   所以不会每拍重算 CRC (WCET 有界: 每帧最多一次 ≤253B 的 CRC)。
             * ★ 静默路径**原样保留**作兜底 (未知功能码/坏帧/被截断的帧都还得靠它)。 */
            if (c->rx_len >= 4u) {
                uint16_t want = mb_expected_len(rx, c->rx_len);
                if (want != 0u && c->rx_len == want) {
                    uint16_t crc_recv = (uint16_t)(rx[want - 2u]
                                        | ((uint16_t)rx[want - 1u] << 8));
                    if (mb_crc16(rx, (uint16_t)(want - 2u)) == crc_recv) {
                        mb_diag(base)[MB_DIAG_FASTOK] += 1u;
                        c->state = MB_ST_EXEC;      /* 帧完整 ⇒ 立刻解析, 省掉 400µs */
                        break;
                    }
                }
            }
#endif
        } else if (c->state == MB_ST_RX) {
            /* 隧道注入的帧: rx_pos 已达 rx_len (视为收满) → pull 返回 0 → 累计静默 */
            if (++c->silent >= MB_SILENT_TICKS) {
                if (c->rx_len >= 4) { c->state = MB_ST_EXEC; }
                else {
                    /* ★ 这条"太短就丢"的路径原来**不计数** ⇒ 判据里是个盲区 */
                    if (c->rx_len > 0u) {
                        mb_diag(base)[MB_DIAG_SHORT] += 1u;
                        fault_record(base, FAULT_MB_SHORT, s_mb_tick,
                                     (uint32_t)c->rx_len, 0u);
                    }
                    c->rx_len = 0; c->rx_pos = 0; c->silent = 0; c->state = MB_ST_IDLE;
                }
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
        while (n < MB_BUILD_BUDGET && c->b_pos < total && c->b_pos < MB_TX_SIZE) {
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
            /* ★ 台账: "组装越界保护被触发" = 界限/长度算法出了新错, 必须留案底 */
            fault_record(base, FAULT_MB_BUILD_OVF, s_mb_tick,
                         (uint32_t)c->b_pos, (uint32_t)total);
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

/* ---- 物理口引脚选择 (2026-09-13) ----------------------------------------
 * 同一路 USART2, 只是换一对脚 —— 用来切开"PA2/PA3 本身有问题"这个可能
 * (引脚损伤 / 板上丝印认错 / 被别的片上功能占用)。两档都是 **AF7**,
 * 所以波特率 / 协议语义 / 状态机**一个字都不用改**。
 *   1 = PD5(TX) / PD6(RX)  —— 交付默认 (备用脚)
 *   0 = PA2(TX) / PA3(RX)  —— 原方案 (对照档)
 * 依据: 官方手册 DS13313 Rev 5 第 68 页 AF7 列 ——
 *   PD3=USART2_CTS, PD4=USART2_RTS, **PD5=USART2_TX, PD6=USART2_RX**, PD7=USART2_CK;
 *   PA2=USART2_TX / PA3=USART2_RX (同手册 p60, FT_ha = 5V 容忍)。 */
#ifndef MB_UART_ALT
#define MB_UART_ALT 1
#endif
#if MB_UART_ALT
#  define MB_UART_GPIO_PORT 3u   /* GPIOD */
#  define MB_UART_TX_BIT    5u   /* PD5 */
#  define MB_UART_RX_BIT    6u   /* PD6 */
#else
#  define MB_UART_GPIO_PORT 0u   /* GPIOA */
#  define MB_UART_TX_BIT    2u   /* PA2 */
#  define MB_UART_RX_BIT    3u   /* PA3 */
#endif

/* ---- 排障用: "这根线到底接在哪个脚上?" 的口线检 (2026-09-13) --------------
 * 原理: 被**外部推挽驱动**的脚, 即使片内开下拉也仍读 1; 而**没接东西**的脚会被下拉成 0。
 *   ⇒ 把整口设成"输入 + 下拉", 读一次 IDR: **位图里为 1 的位就是被外部驱动着的脚**。
 *   这恰好回答"模块 TXD 接在哪个脚上" —— 它空闲时就是 5V 推挽高。
 * 动机: 485 联机排障里, "板子侧代码/引脚全部有证据, 但对方那根线插在哪"始终只能靠猜,
 *   来回换脚位试了多次。**能用一次读数解决的, 不该靠试。**
 * 用法: 调试器预写 SD_CFG[10]=1 + 魔数 [15] (AXI 不跨复位, 沿用既有机制),
 *   主循环取走后执行一次; 结果写进 MbDiag[6] = 位图, [7] = 0xC0DEF00D 完成标记。
 * ★ 测完**原样恢复**该口全部寄存器 (MODER/PUPDR/AFRL/AFRH), 不留副作用。 */
void mb_line_test(uint8_t *base)
{
    volatile uint32_t *d = mb_diag(base);
    uint32_t m0 = GPIO_MODER(3), p0 = GPIO_PUPDR(3);
    uint32_t a0 = GPIO_AFRL(3),  h0 = GPIO_AFRH(3);
    RCC_AHB4ENR |= (1u << 3);                 /* 确保 GPIOD 时钟在 (写入才不被丢弃) */
    GPIO_MODER(3) = 0u;                       /* 全部输入 */
    GPIO_PUPDR(3) = 0xAAAAAAAAu;              /* 每脚 2 位 = 10b ⇒ 全部下拉 */
    __asm__ volatile("dsb" ::: "memory");
    { volatile uint32_t i = 60000u; while (i--) { } }    /* 等电平建立 (~1ms @240MHz) */
    d[6] = GPIO_IDR(3);                       /* ★ 位图: 1 = 被外部驱动为高 */
    /* ★★ 复原顺序 (2026-09-13 由 min_uart485.c 抓出的真缺陷):
     *   原来写的是 `MODER = m0; PUPDR = p0;` —— 即先接回 AF7 而 PUPDR 仍停在下拉,
     *   那一瞬 RX 脚被下拉成低 = 一个 **break 条件** ⇒ USART 必然收到一个 0x00 + FE。
     *   症状: 每跑一次本检, `erracc` 就多一个 FE、字节计数也多 1 —— **观测动作自己
     *   造出了一个"到过字节"的假证据**, 而它看起来正像"真有数据来了"。
     *   (干净固件里同一处错序被直接看到: 每轮扫描固定多 1 字节 0x00 + 1 次 FE。)
     *   ⇒ 正确顺序: 先 AF 选择 → 再 PUPDR 回上拉 → **最后**才接 AF7。 */
    GPIO_AFRL(3)  = a0;
    GPIO_AFRH(3)  = h0;
    GPIO_PUPDR(3) = p0;
    GPIO_MODER(3) = m0;
    __asm__ volatile("dsb" ::: "memory");
    d[7] = 0xC0DEF00Du;                       /* 完成标记 (读的人凭它判断结果有效) */
}

/* ---- 排障用: 直接在 PD6 上"量波形" —— 固件当示波器 (2026-09-13) ------------
 * 起因: 485 联机里出现了一个真矛盾 ——
 *   引脚配置**读回确认**是 AF7、线检确认 PD6 上挂着外部驱动、状态机也确认在 IDLE 轮询,
 *   可 UART 就是收不到。此时只剩一种问法: **PD6 上到底有没有在动的信号?**
 *   LA 要在插着线的排针脚上夹探针很别扭 ⇒ 那就让固件自己采样。
 * 做法: 把 PD6 临时配成"输入 + 上拉", 紧循环采样其 IDR 位, 统计**低电平次数**;
 *   - lowCount > 0 ⇒ 线上确实有**在翻转**的信号 (对方在发数据)
 *   - lowCount == 0 ⇒ 一直是高 ⇒ 对方要么没发、要么根本没接到这根线上
 * 用完**立即恢复 PD6 为 AF7**, 不留副作用。结果写 MbDiag[12..14]。 */
void mb_line_probe(uint8_t *base)
{
    volatile uint32_t *d = mb_diag(base);
    uint32_t m0 = GPIO_MODER(3), p0 = GPIO_PUPDR(3);
    uint32_t a0 = GPIO_AFRL(3);
    uint32_t low = 0, n = 0;
    RCC_AHB4ENR |= (1u << 3);                       /* 确保 GPIOD 时钟在 */
    GPIO_MODER(3) = (m0 & ~(3u << (6u * 2u)));      /* 只把 PD6 改成输入, 别的不动 */
    GPIO_PUPDR(3) = (p0 & ~(3u << (6u * 2u))) | (1u << (6u * 2u));  /* PD6 上拉 */
    __asm__ volatile("dsb" ::: "memory");
    for (uint32_t i = 0; i < 400000u; i++) {        /* ~几十 ms 的采样窗 */
        if ((GPIO_IDR(3) & (1u << 6)) == 0u) low++;
        n++;
    }
    /* ★ 立即恢复 —— 顺序与 mb_line_test 同一条纪律 (先 PUPDR 再 MODER):
     *   本函数把 PD6 设成"输入+上拉", 原来恢复时先写 MODER(→AF7) 时 PUPDR 还停在
     *   上拉 —— 这一处恰好**不会**造出 break (上拉下接 AF 是高位, 与空闲态一致),
     *   但为免"两处顺序不一致"在下一次改动时变成坑, 统一成同一顺序。 */
    GPIO_AFRL(3)  = a0;
    GPIO_PUPDR(3) = p0;
    GPIO_MODER(3) = m0;
    __asm__ volatile("dsb" ::: "memory");
    d[12] = low; d[13] = n; d[14] = 0xA5A50001u;    /* 12=低电平次数 13=总采样 14=完成标记 */
}

/* ---- 物理口使能 (USART2: AF7, 8N1, 轮询) ---- */
void mb_uart_enable(void)
{
    /* ① 时钟: **USART2 本体 + 它自己那对 GPIO 的时钟**。
     *   ★★ 为什么必须自己开 GPIO 时钟: 对**未开时钟的外设**写入会被硬件**静默丢弃**,
     *     寄存器读回 0, 不报任何错 —— 现象就是"通信完全不通"。
     *     原方案(PA2/PA3)碰巧成立: GPIOA 的时钟被 `pin_out_init(PA8)` 在 main.c:2228
     *     就打开了。而 **GPIOD 的时钟只在 sd_init 里开, sd_init 在 mb_uart_enable
     *     之后** ⇒ 换到 PD5/PD6 后不自己开就必然失败。这种坑不该靠"碰巧"。 */
    RCC_APB1LENR |= RCC_APB1LENR_USART2EN;
    RCC_AHB4ENR  |= (1u << MB_UART_GPIO_PORT);

    /* ② GPIO → AF (读-改-写, 绝不整寄存器赋值 —— uart.c 的既有纪律:
     *    同一个 MODER 里还有别的脚, 整赋值会把它们打掉) */
    uint32_t mod = GPIO_MODER(MB_UART_GPIO_PORT);
    mod &= ~(3u << (MB_UART_TX_BIT * 2u));  mod |= (2u << (MB_UART_TX_BIT * 2u));
    mod &= ~(3u << (MB_UART_RX_BIT * 2u));  mod |= (2u << (MB_UART_RX_BIT * 2u));
    GPIO_MODER(MB_UART_GPIO_PORT) = mod;

    /* ★ RX 加上拉: 线上没有驱动时读成**空闲(高)**, 而不是悬空拾噪。
     *   (与 uart.c 对 USART1_RX=PA10 的处理同口径; TX 是推挽输出, 不需要。) */
    uint32_t pup = GPIO_PUPDR(MB_UART_GPIO_PORT);
    pup &= ~(3u << (MB_UART_RX_BIT * 2u));  pup |= (1u << (MB_UART_RX_BIT * 2u));
    GPIO_PUPDR(MB_UART_GPIO_PORT) = pup;

    /* AFRL 管 pin0..7, AFRH 管 pin8..15 —— 条件在编译期定死, 无运行期开销 */
    {   uint32_t tx = MB_UART_TX_BIT, rx = MB_UART_RX_BIT;
        if (tx < 8u && rx < 8u) {
            uint32_t a = GPIO_AFRL(MB_UART_GPIO_PORT);
            a &= ~(0xFu << (tx * 4u));  a |= (7u << (tx * 4u));
            a &= ~(0xFu << (rx * 4u));  a |= (7u << (rx * 4u));
            GPIO_AFRL(MB_UART_GPIO_PORT) = a;
        }
    }

    /* ③ 波特率: BRR = PCLK1/baud = 100e6/115200 = 868 (OVER8=0)
     *    ★ 不照抄常数 —— 见 regs.h 里 USART2_BRR_115200 的 16 倍错教训 */
    USART_BRR(USART2_BASE) = USART2_BRR_115200;
    USART_PRESC(USART2_BASE) = 0;                       /* 不分频 (BRR 已按 PCLK1 算) */

    /* ④ 使能: UE | TE | RE | ★ FIFOEN —— **不开 RXNEIE** (轮询, 见 modbus.h 说明)
     * ★★★ FIFOEN 是 2026-09-13 实测修复 (docs/audit/H723-485-RX-AUDIT.md):
     *   RDR 只有 1 字节深, 而拍周期(100µs) > 字节间隔(86.8µs) ⇒ 相位漂移 ⇒
     *   **周期性"一拍内到 2 字节"** ⇒ 单字节 RDR 必然 ORE。原注释"每拍最多 1.15 字节"
     *   是**平均值不是上界**, 正是它把这条缺陷藏了一整轮排查。
     *   实测: 不开 FIFO 快发 8 字节只收 3; 开 FIFO 后 **32/32 全收、maxrx=8(整帧)、
     *         总线上应答 CRC 正确**。FIFO 深度 8 吸收拍间积压。
     * ★ H7 要求 FIFOEN 在 **UE=0** 时配置: 本函数是"先整写 CR1"的写法, 天然满足;
     *   若将来改成增量改位, 必须先清 UE。 */
    USART_CR1(USART2_BASE) = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE
                           | USART_CR1_FIFOEN;
    (void)USART_ISR(USART2_BASE);                       /* 读一次清初值 */
    USART_ICR(USART2_BASE) = 0x1FFu;                    /* ★ 顺手清掉上电残留的错误位 */
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
    /* ★★ TX 默认改成**物理口** (2026-09-12)。
     *   原默认 0 = "响应只留缓冲, 等 0x61 读回" —— 那是**零硬件隧道测试**的便利,
     *   但一个真的 Modbus 从站必须在总线上应答, 出厂默认就该能直接对话。
     *   ★ 改它**不破坏**原有测试: `mb_push_bytes()` 只决定"要不要**额外**推到 USART2",
     *     TX 缓冲照样被填满 ⇒ 0x61 依旧读得到响应 (只是不再替 PC 清缓冲)。
     *   ★ 为什么必须在**编译期**定而不是靠 0x62: 实测本机 CMSIS-DAP 每次 pyocd 会话
     *     结束都会复位板子 ⇒ 运行期写 SHM 的配置活不到下一次会话 (上电 cold_start_reset
     *     会 memset SHM)。0x62 仍可随时切回缓冲模式做隧道测试。 */
    c->tx_uart = 1;

    uint8_t *rx = mb_rx(base), *tx = mb_tx(base);
    for (int i = 0; i < MB_MAX_FRAME; i++) { rx[i] = 0; tx[i] = 0; }
    uint16_t *h = mb_hold(base), *s = mb_set(base);
    for (int i = 0; i < MB_NREG; i++) { h[i] = 0; s[i] = 0; }

    /* ★ 不在这里调 mb_uart_enable(): 它要动 RCC/GPIO, 不该随每次冷启动重复执行,
     *   且 cold_start_reset 可能在 UART 尚未初始化时被调用 (上电早期)。
     *   UART 使能由 main() 显式调一次 (见 modbus.h)。 */
}
