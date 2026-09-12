/*
 * hil.h — HIL 硬件在环组件 (W5 外设域) — H723 移植版
 *
 * 来源: esp32-core0/components/hil/hil.h (S3)。语义保留:
 *   1. 输出臂: 读 WIRE_MAP[HIL_U_WIRE] 的 u (引擎 PID 输出) → 输出 PWM
 *   2. 反馈眼: PWM 经外部回路回来 → ADC 采样 → SENSOR_MAP[HIL_FB_SENSOR]
 * 交互全经共享内存, 引擎本体零改动 (路由侧 SRC_SENSOR/SRC_WIRE 天然支持)。
 *
 * 移植差异:
 *   · S3 用 LEDC 硬件 PWM (GPIO5) → H723 **TIM3_CH1 硬件 PWM (PA6, AF2)**。
 *     PWM 由定时器硬件产生, 引擎被暂停时波形照走 (同一设计意图)。
 *   · 反馈 ADC: S3 GPIO4 (ADC1_CH3) → H723 **PA5 = ADC1_INP19** (16bit, 精度升级)。
 *   · 采样/更新由主循环 10ms 驱动 (S3 是 1kHz Core0 任务)。
 * ★ 回环验证 (需 1 根线): PA6(PWM) 直连 PA5(反馈)。无 RC 滤波时 ADC 采到的是方波,
 *   固件按多次平均估占空比; 有 1kΩ+1µF 串成 RC 更干净 (见 docs)。
 */
#ifndef DCL_HIL_H
#define DCL_HIL_H

#include <stdint.h>

#define HIL_PWM_PIN     6      /* PA6 = TIM3_CH1 (AF2), 端口编码 6 = PA6 */
#define HIL_FB_PIN      5      /* PA5 = ADC1_INP19 (adc.c 里配 analog) */
#define HIL_FB_SENSOR   2      /* 反馈 → SENSOR_MAP[2] (与 S3 同: 0/1 原 DHT22) */
#define HIL_U_WIRE      20     /* u 源 → WIRE_MAP[20] (引擎 PID 输出写这里, 与 S3 同) */
#define HIL_PWM_HZ      1000   /* PWM 频率 1kHz */
#define HIL_PWM_RES     1024   /* 分辨率 10bit (1024 级, 与 S3 LEDC 同) */
#define HIL_FB_AVG      16     /* 无 RC 时反馈取 16 次平均 (方波占空比估计) */

void hil_init(uint8_t *base);
void hil_tick(uint8_t *base, uint32_t tick_now);   /* 主循环版 (A/B 对照档 / 兼容保留) */

/** @brief ★ P1 拍内输出臂: 由 ISR **每拍**调用, 读 WIRE[HIL_U_WIRE] → 写 TIM3_CCR1。
 *  ★ 与 hil_tick 的输出臂**逻辑完全相同** (共用 hil_out_apply()), 区别只在执行频率:
 *      hil_tick     : 主循环, 每 10ms 一次
 *      hil_out_poll : ISR, 每 100µs 一次
 *  ★ 为什么更快是对的: TIM3_CCR1 已配 OC1PE (预装载) ⇒ 写入在**下一个 PWM 更新事件**
 *    才生效 (1kHz ⇒ 1ms)。两种频率的生效时刻几乎一样, 但"每拍写"保证预装载寄存器里
 *    **永远有一个新鲜值**; "每 10ms 写"则会在两次更新之间留下陈旧窗口。
 *  ★ 安全态语义**不变**: 仍受 ENGINE_RUN 门控 (HIL_SAFE=1), STOP 后仍归零。
 *  ★ 反馈眼已从本入口**剥离** —— 它由 ADC 拍内状态机负责 (见 adc.h)。对照档仍走 hil_tick。
 *  ★★ `long_call` = **正确性要求**: ITCM 里的 ISR 到本函数 (flash) 相距 128MB,
 *     超出 Thumb `BL` 的 ±16MB ⇒ 必须走 BLX (详见 main.c 里 s_io_in 的证据链)。 */
__attribute__((long_call)) void hil_out_poll(uint8_t *base, uint32_t tick_now);

extern volatile uint32_t g_hil_out_n;   /* hil_out_poll 活性计数 (obs_anchor 登记) */

/** @brief HIL 物理输出的**安全态** —— 由 engine 的物理输出面注册表在 STOP/RESET 时调用。
 *  ★ 语义: 把 PWM 占空比压到 0 (输出归零), 并回写 `OFF_HIL_DUTY` 镜像。
 *  ★ 为什么必须回写镜像: `OFF_HIL_DUTY` 的契约是"最近写入 TIM3_CCR1 的值"。
 *    只清寄存器不回写 ⇒ 任何读镜像做的停机安全判据都看不到安全态,
 *    "停机已进安全态"就变成一句不可核对的宣称 (本项目"宣称必须等于实现")。 */
void hil_outputs_safe(void);

#endif /* DCL_HIL_H */
