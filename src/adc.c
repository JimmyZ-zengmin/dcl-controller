/*
 * adc.c — H723 ADC1 16bit 驱动 + AI 通道 (W5 外设域)
 * 见 adc.h 的移植说明。本文件的关键纪律: **所有位域/偏移抄官方头文件**, 不手推。
 */
#include "adc.h"
#include "itcm.h"   /* ★ ISR 调用树必须住 ITCM —— 见该头文件 */
#include "engine.h"
#include "regs.h"
#include "clock.h"
/* ★ P2: 拍内状态机要写 HIL 反馈槽 ⇒ 需要 HIL_FB_SENSOR / HIL_FB_AVG。
 *   hil.h 不包含 adc.h, 所以无循环包含。 */
#include "hil.h"

#define ADC1  ADC1_BASE

static volatile uint32_t s_adc_ready = 0;

/* 把引脚置 analog 模式 (11) + 无上下拉。ADC 取样前必须做, 否则数字输入缓冲会引入
 * 漏电/振荡 (本项目纪律: "配置全对≠功能可用" —— 这里 MODER 不对就是"全对里的一处")。 */
void adc_analog_pin(uint32_t pin)
{
    uint32_t port = pin >> 4, bit = pin & 15u;
    RCC_AHB4ENR |= (1u << port);
    uint32_t m = GPIO_MODER(port);
    m &= ~(3u << (bit * 2u));
    m |=  (3u << (bit * 2u));          /* 11 = analog */
    GPIO_MODER(port) = m;
    uint32_t p = GPIO_PUPDR(port);
    p &= ~(3u << (bit * 2u));          /* analog 模式: 无上下拉 */
    GPIO_PUPDR(port) = p;
}

static void adc_delay(volatile uint32_t n) { while (n--) { } }

void adc_init(void)
{
    /* ① 时钟: per_ck = HSE(25MHz), adc_ker_ck = CLKP(per_ck)
     *   ★ 只用 HSE: 不依赖 PLL2/PLL3 (本项目未配置), 25MHz 稳在 16bit fADC 限内。 */
    RCC_D1CCIPR = (RCC_D1CCIPR & ~(3u << RCC_D1CCIPR_CKPERSEL_SHIFT))
                | (2u << RCC_D1CCIPR_CKPERSEL_SHIFT);       /* 10 = HSE */
    RCC_D3CCIPR = (RCC_D3CCIPR & ~(3u << RCC_D3CCIPR_ADCSEL_SHIFT))
                | (2u << RCC_D3CCIPR_ADCSEL_SHIFT);         /* 10 = CLKP */
    RCC_AHB1ENR |= RCC_AHB1ENR_ADC12EN;

    /* ② ADC 时钟模式: CKMODE=00 → 用 adc_ker_ck (异步), PRESC=3 → /8。
     *   ★ 反直觉但已核官方头文件: CKMODE=00 才是异步; 01/10/11 才是 AHB+/1,/2,/4。
     *   ★★ 为什么 /8 (25MHz → 3.125MHz): 实测读回 CR=0x10000101 —— BOOST 只置到
     *      **bit8 (值1 = fADC≤12.5MHz)**, bit9 写不进去(该位在此片可能未实现)。
     *      而我原先用的 adc_ker_ck = HSE 25MHz **超出该 BOOST 档**。
     *      STM32 的规则是"fADC 超出 BOOST 档 ⇒ 转换结果不正确"(不是精度差, 是数不对),
     *      症状正是"读数完全不跟随输入"(本项目 2026-09-11 实测撞到: AI/HIL 都读不到)。
     *      ⇒ 取最保守组合: PRESC=/8 → 3.125MHz, BOOST=0 (≤6.25MHz), 彻底避开该风险。
     *        采样时间 810.5 周期 @3.125MHz ≈ 259µs/次 —— ai_tick 3 次/10ms 完全够。 */
    /* ★★ 2026-09-12 优化: PRESC /8 → /2 ⇒ fADC = 25/2 = **12.5MHz** (BOOST=1 档极限:
     *   H723 的 BOOST 字段只有 bit8 一位, bit9 写不进 ⇒ 上限 12.5MHz, /2 恰好到限)。
     *   单次转换 259µs → **≈4µs** (32.5 周期采样 + 16.5 转换) ⇒ 一拍装下一次转换,
     *   采样孔径从此远离输出沿 (拍内 t≈几 µs 完成, 输出沿在拍边界)。 */
    ADC_CCR = (0u << ADC_CCR_CKMODE_SHIFT) | (1u << ADC_CCR_PRESC_SHIFT);

    /* ③ 上电: 退出深睡眠 + 稳压器 + BOOST + **PCSEL 通道预选** */
    ADC_CR(ADC1) &= ~ADC_CR_DEEPPWD;
    ADC_CR(ADC1) |=  ADC_CR_ADVREGEN;
    ADC_CR(ADC1) = (ADC_CR(ADC1) & ~ADC_CR_BOOST) | ADC_CR_BOOST;  /* BOOST=1 档 (≤12.5MHz) */
    adc_delay(200000u);                                   /* tADC 稳压器稳定 (~ms 级余量) */

    /* ★★ 通道预选 (PCSEL) —— **漏了它 ADC 就看不到任何引脚**。
     * ★★ 位置教训 (2026-09-12): PCSEL 原写在**深睡眠态**(DEEPPWD=1) ⇒ **写不生效**
     *   (读回 PCSEL=0 ⇒ ADC 读数全是"未定义值"~0.18V)。移到退出深睡眠之后才可靠。
     *   全开 20 个通道, 让 0x37 全通道扫描覆盖所有 INP。 */
    ADC_PCSEL(ADC1) = 0x000FFFFFu;

    /* ④ 校准 (照 HAL 口径: 先禁能再校准; ADVREGEN 必须在) */
    ADC_CR(ADC1) |= ADC_CR_ADDIS;
    { uint32_t g = 0; while ((ADC_CR(ADC1) & ADC_CR_ADEN) && ++g < 2000000u) { } }
    ADC_CR(ADC1) |= ADC_CR_ADCAL;
    { uint32_t g = 0; while ((ADC_CR(ADC1) & ADC_CR_ADCAL) && ++g < 20000000u) { } }

    /* ⑤ 配置 (禁用态): 16bit / 单次 / 软件触发; 所有通道给最长采样时间
     *   (810.5 周期 @25MHz ≈ 32µs) —— 本自检用内部 40kΩ 上拉驱动, 高阻源必须长采样。 */
    ADC_CFGR(ADC1)  = 0u;                                 /* RES=000 → 16bit, CONT=0 */
    /* ★ SMP = 32.5 周期(编码4) @12.5MHz = 2.6µs —— 40kΩ 源的 16 位充电需求
     *   (tS ≥ ln(2^18)×(Rsrc+Radc)×Csample ≈ 2.5µs) 刚好满足; 810.5 周期是
     *   百倍保守 (3.125MHz 时代的产物)。每通道 3 位字段 = 100b ⇒ 0x24924924。 */
    ADC_SMPR1(ADC1) = 0x24924924u;                        /* SMP0..SMP9  = 32.5 周期 */
    ADC_SMPR2(ADC1) = 0x24924924u;                        /* SMP10..SMP19= 32.5 周期 */

    /* ⑥ 使能, 等 ADRDY */
    ADC_CR(ADC1) |= ADC_CR_ADEN;
    { uint32_t g = 0; while (!(ADC_ISR(ADC1) & ADC_ISR_ADRDY) && ++g < 2000000u) { } }

    s_adc_ready = 1;
}

int adc_read(uint32_t ch, uint16_t *out)
{
    if (!s_adc_ready) return -1;
    /* ★★ 启动前先清 EOC / OVR。理由 (实测撞出来的不一致): 连读时上一轮若留下 EOC(或 OVR),
     *   本次 ADSTART 后会**立即**看到 EOC=1 ⇒ 读到的是**上一次的 DR**(旧值)。
     *   (旁证: ADC_ISR 实测出现过 0x100B, bit3 = OVR 已置位。) */
    ADC_ISR(ADC1) = (1u << 2) | (1u << 3);                /* 写 1 清 EOC / OVR */
    ADC_SQR1(ADC1) = (uint32_t)((ch & 0x1Fu) << 6);       /* L[3:0]=0 → 1 次转换; SQ1=ch */
    ADC_CR(ADC1) |= ADC_CR_ADSTART;
    uint32_t g = 0;
    while (!(ADC_ISR(ADC1) & ADC_ISR_EOC) && ++g < 5000000u) { }
    if (!(ADC_ISR(ADC1) & ADC_ISR_EOC)) return -1;        /* 超时: 用状态码, 不用哨兵值 */
    *out = (uint16_t)(ADC_DR(ADC1) & 0xFFFFu);            /* 读 DR 同时清 EOC */
    return 0;
}

/* ══════════ AI: 3 路模拟量 → SENSOR[8..10] (V) ══════════ */
static const uint32_t AI_PINS[AI_NCH] = { AI_PIN_PA0, AI_PIN_PA1, AI_PIN_PA4 };
static const uint32_t AI_CHS[AI_NCH]  = { AI_CH_PA0,  AI_CH_PA1,  AI_CH_PA4  };

static inline float ai_raw_to_volt(uint16_t raw)
{
    return (float)raw * 3.3f / 65535.0f;                  /* VDDA 假设 3.3V (见 adc.h) */
}

void ai_init(uint8_t *base)
{
    for (int i = 0; i < AI_NCH; i++) adc_analog_pin(AI_PINS[i]);
    for (int i = 0; i < AI_NCH; i++)
        *(volatile float *)(base + OFF_SENSOR_MAP + (uint32_t)(AI_SENSOR_BASE + i) * 4u) = 0.0f;
    __asm__ volatile("dsb" ::: "memory");
}

void ai_tick(uint8_t *base, uint32_t tick_now)
{
    static uint32_t last = 0;
    if ((uint32_t)(tick_now - last) < 100u) return;       /* 100 拍 = 10ms (同 S3 周期) */
    last = tick_now;
    for (int i = 0; i < AI_NCH; i++) {
        uint16_t raw = 0;
        if (adc_read(AI_CHS[i], &raw) != 0) raw = 0;      /* 超时按 0V 处理 (显式) */
        *(volatile float *)(base + OFF_SENSOR_MAP + (uint32_t)(AI_SENSOR_BASE + i) * 4u) =
            ai_raw_to_volt(raw);
    }
    __asm__ volatile("dsb" ::: "memory");
}

void ai_selftest(uint8_t *base, uint32_t method, uint16_t *out)
{
    (void)base;
    for (int i = 0; i < AI_NCH; i++) {
        uint32_t pin = AI_PINS[i], port = pin >> 4, bit = pin & 15u;
        for (int k = 0; k < 2; k++) {                     /* k=0 上拉, k=1 下拉 */
            uint32_t m = GPIO_MODER(port);
            m &= ~(3u << (bit * 2u));
            if (method == 0u) m |= (3u << (bit * 2u));    /* method0: analog */
            GPIO_MODER(port) = m;                         /* method1: 保持 00=input */
            uint32_t p = GPIO_PUPDR(port);
            p &= ~(3u << (bit * 2u));
            p |= ((k == 0) ? 1u : 2u) << (bit * 2u);      /* 01=上拉 10=下拉 */
            GPIO_PUPDR(port) = p;
            adc_delay(200000u);                           /* 建立时间 (40kΩ 拉的 RC) */
            { uint16_t v = 0; if (adc_read(AI_CHS[i], &v) != 0) v = 0;
              out[i * 2 + k] = v; }
        }
    }
    for (int i = 0; i < AI_NCH; i++) adc_analog_pin(AI_PINS[i]);   /* 恢复 */
}

/* ══════════════════════════════════════════════════════════════════
 * ★★ P2 (2026-09-12): 拍内非阻塞 ADC 状态机
 * ══════════════════════════════════════════════════════════════════
 * 为什么必须是非阻塞的 (本文件最有分量的一个数字):
 *   `adc_read()` 单次转换 ≈ **259µs** —— `SMP=810.5 周期 @ adc_ker_ck=3.125MHz`
 *   (见 adc_init 对 /8 与 BOOST=0 的推导)。而拍长只有 **100µs**。
 *   ⇒ 一次转换 = **2.6 个拍**。在 ISR 里自旋等 EOC 会把拍周期直接撑成 ISR 时长
 *     (即项目已知的"超载"形态: 拍周期由 ISR 时长决定, 不再是 100µs)。
 *   ⇒ 唯一的出路是**把等待摊到多个拍上**: 一拍启动, 若干拍后再取结果。
 *
 * 通道轮询与更新率 (实测推算, 见 docs/PLAN-io-into-engine.md §2.5):
 *   序列 AI0 → AI1 → AI2 → HIL_FB → AI0 → …
 *   每通道占用 ADC_SM_START_WAIT 拍 (启动那拍 + 等待), 4 通道 ⇒ 一轮 16 拍 = 1.6ms
 *     · AI 3 路: 每 **1.6ms** 更新 (原来是 10ms —— 快了 6 倍)
 *     · HIL 反馈: 累加 HIL_FB_AVG(=16) 次 ⇒ 16 轮 = **25.6ms** 出一个平均值
 *       (原来是 10ms 窗口内阻塞 16 次; 功能等价 —— 平均值的正确性不依赖窗口长度,
 *        方波周期 1ms, 两个窗口都跨多个完整周期。代价只是**反馈跟随变慢**,
 *        而反馈量本身是慢变量 ⇒ 已知且有意的取舍, 不是副作用。)
 *
 * 每拍代价: 无事可做时 = 一次自增 + 一次比较 + return (约 5 cyc);
 *           取结果那拍多一次 DR 读 + 一次 float 存储 (约 25 cyc)。
 *           平均 ≈ 9 cyc/拍 ⇒ 占拍预算 0.02%。
 *
 * ★ 与 `adc_read` 的关系: 两者**共存**。adc_read 留给"自检/一次性读"用 (ai_selftest
 *   需要"立刻拿到本次转换结果", 状态机做不到)。生产路径走本状态机。
 *
 * ★★ 超时必须有自己的计数 (不能静默):
 *   正常情况下 259µs 后 EOC 必到 (4 拍远大于 2.6 拍)。若一直不到, 说明 ADC 配置坏了
 *   或外设挂了 —— 这类"本该发生却没发生"的事件按项目纪律**必须可被外部读走**,
 *   否则就是下一个"一切正常, 只有 X 是 0"的静默失效。
 *   ⇒ `g_adc_sm_timeout` 独立计数 (应恒 0), `g_adc_sm_done` 作为**正向证据**
 *     (它必须随运行时间单调增 —— 与 timeout 成对, 才能区分"没坏"与"根本没跑")。
 */
#define ADC_SM_NCH        (AI_NCH + 1)   /* AI 3 路 + HIL 反馈 1 路 */
#define ADC_SM_START_WAIT 3u             /* 启动后先等 3 拍再查 EOC (259µs ≈ 2.6 拍) */
#define ADC_SM_TIMEOUT    8u             /* 超过 8 拍仍无 EOC ⇒ 放弃本次 (远大于 2.6 拍, 只在真故障时触发) */

static struct {
    uint8_t  idx;       /* 下一个要启动的通道索引 (0..ADC_SM_NCH-1) */
    uint8_t  pend;      /* 1 = 已启动, 等结果 */
    uint8_t  wait;      /* 已等待的拍数 */
    uint32_t fb_acc;    /* HIL 反馈累加 (原始码) */
    uint32_t fb_n;      /* HIL 反馈已累加次数 */
} s_sm;

volatile uint32_t g_adc_sm_done    = 0;   /* 完成转换次数 (正向证据: 必须单调增) */
volatile uint32_t g_adc_sm_timeout = 0;   /* 超时次数 (应恒 0) */

/* 取回的原始码按通道落槽。idx < AI_NCH ⇒ AI 通道; 否则 = HIL 反馈 (累加后出平均)。 */
static void adc_sm_store(uint32_t idx, uint16_t raw, uint8_t *base)
{
    if (idx < (uint32_t)AI_NCH) {
        *(volatile float *)(base + OFF_SENSOR_MAP
                            + (uint32_t)(AI_SENSOR_BASE + (int)idx) * 4u) = ai_raw_to_volt(raw);
    } else {
        s_sm.fb_acc += (uint32_t)raw;
        if (++s_sm.fb_n >= (uint32_t)HIL_FB_AVG) {
            float v = (float)(s_sm.fb_acc / (uint32_t)HIL_FB_AVG) * 3.3f / 65535.0f;
            *(volatile float *)(base + OFF_SENSOR_MAP
                                + (uint32_t)HIL_FB_SENSOR * 4u) = v;
            /* 排障镜像: 与 hil.c 同契约 (最近一次的原始码), 便于对照 0x37 扫描读数 */
            *(volatile uint32_t *)(base + OFF_HIL_FB_RAW) = (uint32_t)raw;
            s_sm.fb_acc = 0u;
            s_sm.fb_n   = 0u;
        }
    }
}

static void adc_sm_start(uint32_t ch)
{
    /* ★ 启动前先清 EOC/OVR —— 与 adc_read 同款, 同一个实测陷阱:
     *   连读时上一轮若留下 EOC(或 OVR), 本次 ADSTART 后会**立即**看到 EOC=1
     *   ⇒ 读到的是**上一次的 DR**(旧值)。状态机同样是"连读", 所以这一步不能省。 */
    ADC_ISR(ADC1) = (1u << 2) | (1u << 3);
    ADC_SQR1(ADC1) = (uint32_t)((ch & 0x1Fu) << 6);      /* L[3:0]=0 → 1 次转换; SQ1=ch */
    ADC_CR(ADC1) |= ADC_CR_ADSTART;
}

/* ══════════ P3-C (2026-09-12): adc_poll 拆两半 —— 回收(拍头)/启动(拍尾) ══════════
 * ★ 为什么拆: 采样孔径(转换期间)不能与输出沿重叠 —— 输出沿的 di/dt(地弹/串扰)
 *   会混进采样。启动挪到 **ISR 末尾**(拍尾) ⇒ 孔径从拍尾开始, 距上一个输出沿
 *   (拍头 MDMA 锁存)已隔 ~97µs —— 采样开关动作不再与输出沿同瞬。
 * ★ 拆分后节奏加快: 转换 4µs << 拍长 100µs ⇒ 每 2 拍完成一个通道
 *   (拍尾 kick → 下拍头 reclaim ⇒ 同拍尾 kick 下一通道), 4 通道 = 8 拍 = 0.8ms
 *   (原 16 拍 = 1.6ms, 快一倍; HIL 反馈均值跨度 25.6ms → 12.8ms)。
 * ★ 握手: pend=1 表示"有转换在跑"; kick 只在 pend==0 时启动 (否则 ADC busy)。
 *   对照档 (IO_IN_ISR=0) 走 adc_poll() 整体 (reclaim+kick 顺序调), 语义不变。 */
DCL_ITCM void adc_poll_reclaim(uint8_t *base)
{
    if (!s_adc_ready) return;
    if (!s_sm.pend) return;    /* 无转换在跑 (上拍尾没 kick) ⇒ 无事 */
    if (s_sm.wait < ADC_SM_START_WAIT) { s_sm.wait++; return; }   /* 起跑阶段: 不查 */
    if (ADC_ISR(ADC1) & ADC_ISR_EOC) {
        uint16_t raw = (uint16_t)(ADC_DR(ADC1) & 0xFFFFu);        /* 读 DR 同时清 EOC */
        adc_sm_store(s_sm.idx, raw, base);
        s_sm.pend = 0;
        g_adc_sm_done++;
    } else {
        s_sm.wait++;
        if (s_sm.wait >= ADC_SM_TIMEOUT) {
            g_adc_sm_timeout++;     /* ★ 显式计数, 不静默 (见上方长注释) */
            s_sm.pend = 0;
        }
        /* 还在等: 本拍不回收 (下拍头再查) */
    }
}

DCL_ITCM void adc_poll_kick(void)
{
    if (!s_adc_ready) return;
    if (s_sm.pend) return;     /* 还有转换在跑 ⇒ 不启动 (等回收), ADC 单次模式 busy */
    uint32_t next = (uint32_t)s_sm.idx;
    uint32_t ch   = (next < (uint32_t)AI_NCH) ? AI_CHS[next] : HIL_FB_CH_PA5;
    adc_sm_start(ch);
    s_sm.idx++;
    if (s_sm.idx >= (uint8_t)ADC_SM_NCH) s_sm.idx = 0;
    s_sm.pend = 1;
    s_sm.wait = 0;
}

/* 完整版 (对照档/兼容): 回收+启动顺序调 —— 与拆分前行为逐拍一致 */
void adc_poll(uint8_t *base, uint32_t tick_now)
{
    (void)tick_now;
    adc_poll_reclaim(base);
    adc_poll_kick();
}
