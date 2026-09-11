/*
 * hil.c — HIL 硬件在环 (W5 外设域)。见 hil.h 说明 (语义保留 S3)。
 */
#include "hil.h"
#include "adc.h"
#include "engine.h"
#include "regs.h"
#include "clock.h"

#define TIM3 TIM3_BASE_ADDR

/* ★ 物理输出面的安全态回调签名统一是 `void (*)(void)` (见 engine.h 的注册表),
 *   带不了 base 参数 ⇒ init 时把基址记一份。它上电后不变, 存静态变量是安全的。 */
static uint8_t *s_hil_base = 0;

static inline float hil_read_f(uint8_t *base, uint32_t off)
{
    return *(volatile float *)(base + off);
}
static inline void hil_write_f(uint8_t *base, uint32_t off, float v)
{
    *(volatile float *)(base + off) = v;
}

void hil_init(uint8_t *base)
{
    s_hil_base = base;                                /* 供 hil_outputs_safe 回写镜像 */
    /* ① PWM 脚 PA6 → AF2 (TIM3_CH1) */
    RCC_AHB4ENR |= (1u << 0);
    uint32_t bit = HIL_PWM_PIN & 15u;                 /* PA6 → bit6, 端口 A */
    uint32_t m = GPIO_MODER(0);
    m &= ~(3u << (bit * 2u));
    m |=  (2u << (bit * 2u));                         /* 10 = AF */
    GPIO_MODER(0) = m;
    uint32_t afr = GPIO_AFRL(0);                      /* PA0..PA7 在 AFRL */
    afr &= ~(0xFu << (bit * 4u));
    afr |=  (2u << (bit * 4u));                       /* AF2 = TIM3 */
    GPIO_AFRL(0) = afr;
    uint32_t pup = GPIO_PUPDR(0);
    pup &= ~(3u << (bit * 2u));                       /* AF 无上下拉 */
    GPIO_PUPDR(0) = pup;
    GPIO_OSPEEDR(0) |= (3u << (bit * 2u));            /* 高速档 (1kHz 方波) */

    /* ② 反馈脚 PA5 → ADC analog (ADC1_INP19) */
    adc_analog_pin(HIL_FB_PIN);

    /* ③ TIM3: PSC 使计数 1MHz, ARR 使周期 1kHz, PWM 模式 1 */
    RCC_APB1LENR |= RCC_APB1LENR_TIM3EN;
    TIM_PSC(TIM3)  = (uint32_t)(CLK_TIMXCLK_HZ / 1000000u) - 1u;   /* → 1MHz */
    TIM_ARR(TIM3)  = (uint32_t)(1000000u / HIL_PWM_HZ) - 1u;       /* → 1kHz */
    TIM_CCR1(TIM3) = 0u;
    TIM_CCMR1(TIM3) = TIM_CCMR1_OC1PE | (6u << TIM_CCMR1_OC1M_SHIFT);  /* PWM1 */
    TIM_CCER(TIM3)  = TIM_CCER_CC1E;
    TIM_CR1(TIM3)   = TIM_CR1_ARPE | TIM_CR1_CEN;
    TIM_EGR(TIM3)   = TIM_EGR_UG;                     /* 立即装载 PSC/ARR */

    __asm__ volatile("dsb" ::: "memory");
}

void hil_tick(uint8_t *base, uint32_t tick_now)
{
    static uint32_t last = 0;
    if ((uint32_t)(tick_now - last) < 100u) return;   /* 100 拍 = 10ms */
    last = tick_now;

    /* 输出臂: WIRE[HIL_U_WIRE] → 占空比 (钳到 [0, RES]) */
    float u = hil_read_f(base, OFF_WIRE_MAP + (uint32_t)HIL_U_WIRE * 4u);
#if HIL_SAFE
    /* ★★ 停机 ⇒ 安全态 (2026-09-11 迁移保真度审查 一级 #1):
     *   引擎停扫后 WIRE[20] 是**冻结的陈旧值**。若无条件回写, STOP 之后 PWM 会一直保持
     *   最后占空比不动 —— "停机 = 进安全态"在真实执行器上不成立。
     *   ⇒ 输出臂受 ENGINE_RUN 门控: 未运行就把 u 压到 0。
     *   ★ 为什么这里要再判一次, 而不只靠 STOP 那一刻的清零:
     *     STOP 的一次性清零只覆盖"走了 0x12 命令"这条路径。安全态应当是**持续成立的性质**
     *     (任何把占空比留在非零的路径都会在 ≤10ms 内被纠回), 而不是"某时刻做过一次动作"。
     *     ⇒ h_stop_w1 的即时清零 (快) + 本处的周期自检 (稳), 两者都要。
     *   ★ 反馈眼 (下面的 ADC 采样) **不**受门控: 停机时仍要能读现场值。 */
    if (!*(volatile uint8_t *)(base + OFF_CTRL_ENGINE_RUN)) u = 0.0f;
#else
    /* A/B 对照档 (HIL_SAFE=0) **改前行为**: 无条件回写 ⇒ STOP 后仍保持最后占空比。
     * 这一档存在的唯一目的, 是让"停机安全态"判据能在同一套测量方法下量到 FAIL
     * (否则无法排除"判据本身量不出问题")。交付构建永远不是这一档。 */
#endif
    if (!(u > 0.0f)) u = 0.0f;
    if (u > (float)HIL_PWM_RES) u = (float)HIL_PWM_RES;
    uint32_t arr1 = TIM_ARR(TIM3) + 1u;
    uint32_t duty = (uint32_t)((u / (float)HIL_PWM_RES) * (float)arr1);
    TIM_CCR1(TIM3) = duty;
    /* ★ 观测镜像: 把"实际写进 TIM3_CCR1 的值"回写 SHM —— 否则"PWM 按 u 变了"这句
     *   话在协议侧不可核对 (只写进硬件寄存器 = 不可验证的宣称)。PC 用 0x22 读回。 */
    *(volatile uint32_t *)(base + OFF_HIL_DUTY) = duty;

    /* 反馈眼: ADC 多次平均 (无 RC 时采到方波 → 平均≈占空比×VDDA) → SENSOR[2] (V) */
    uint32_t acc = 0;
    uint16_t last_raw = 0;
    for (int i = 0; i < HIL_FB_AVG; i++) {
        uint16_t r = 0;
        if (adc_read(HIL_FB_CH_PA5, &r) != 0) r = 0;   /* 超时按 0 计 (显式, 不用哨兵值) */
        last_raw = r;
        acc += r;
    }
    /* 排障镜像: 与 0x37 扫描同通道读数对照 (SENSOR[2]=0 而扫描=满幅时, 看这里) */
    *(volatile uint32_t *)(base + OFF_HIL_FB_RAW) = last_raw;
    float v = (float)(acc / (uint32_t)HIL_FB_AVG) * 3.3f / 65535.0f;
    hil_write_f(base, OFF_SENSOR_MAP + (uint32_t)HIL_FB_SENSOR * 4u, v);
    __asm__ volatile("dsb" ::: "memory");
}

/* ---- 物理输出面安全态 (注册到 engine 的输出面表; STOP/RESET 时被调用) ----
 * 见 engine.h "物理输出面注册" 与 hil.h 的 hil_outputs_safe 说明。
 * ★ 幂等: 反复调用只是反复写 0, 无副作用 (安全态必须幂等 —— 它可能被并发路径多次进入)。 */
void hil_outputs_safe(void)
{
    if (!s_hil_base) return;              /* init 之前被调: 硬件未配, 无事可做 */
    TIM_CCR1(TIM3) = 0u;                  /* 输出臂归零 (OC1PE: 下一个更新事件生效, ≤1 个 PWM 周期) */
    /* ★ 镜像必须跟着写, 否则"停机已进安全态"读不出来 (契约见 hil.h)。 */
    *(volatile uint32_t *)(s_hil_base + OFF_HIL_DUTY) = 0u;
    __asm__ volatile("dsb" ::: "memory");
}
