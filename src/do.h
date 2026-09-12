/*
 * do.h — DO 数字量输出面 (P3-A, 路径 A: CPU 直写) — H723
 *
 * 链路: 扫描/seq 写 ACTUATOR[0..15] (float) → 拍内 do_poll 打包 → GPIOE_ODR。
 * 与 adc/di/hil 同构: 独立组件、拍内驱动、安全态登记、就绪门、可观测。
 *
 * 语义 (与 OFF_CTRL_GPIO_MASK 定案② 一一对应, 见 engine.h):
 *   · ACTUATOR[i] > 0.5 ⇒ PEi = 1, 否则 PEi = 0 (h723-core0 同款阈值);
 *   · GPIO_MASK bit i = 1 ⇒ PEi **归引擎管** (拍内按 ACTUATOR 驱动, 停机清 0);
 *     bit i = 0 ⇒ **任何路径都不许碰 PEi** (包括本组件)。
 *
 * ★ 为什么放拍内输出段: 输出臂读的是本拍刚算出的 WIRE, DO 读的是本拍刚算出的
 *   ACTUATOR —— 同属"扫描之后、拍结束之前"的输出段 (位置理由见 main.c ISR 注释)。
 *
 * ★ 与 macro VM 的关系: macro 的 M 原语也能写 GPIO ODR。**不要让 macro 写 PE**
 *   (与 DO 面抢同一个物理口) —— 两家驱动者写同一个引脚, 谁后写谁赢, 确定性破产。
 */
#ifndef DCL_DO_H
#define DCL_DO_H

#include <stdint.h>

#define DO_COUNT        16
#define DO_GPIO_PORT    4        /* GPIOE (端口编码: 0..15 = PA..PK, 与 di/adc 同约定) */

/** @brief GPIOE 时钟使能 + PE0..15 推挽输出、初始电平 0。调一次。 */
void do_init(uint8_t *base);

/** @brief 拍内输出: 按 GPIO_MASK 管辖位, ACTUATOR[0..15] → GPIOE (BSRR 单次原子写)。
 *  ISR 每拍调用; 内部有 s_do_ready 就绪门 (do_init 之前调用直接返回,
 *  与 adc.c 的 s_adc_ready 同一条纪律 —— ISR 从阶段 8 就在跑, 外设 init 在阶段 26)。 */
void do_poll(uint8_t *base, uint32_t tick_now);

/** @brief 安全态: 把引擎管辖位清 0 (登记进 eng_outputs_safe 的回调)。
 *  受 HIL_SAFE 同一个编译开关控制 (HIL_SAFE=0 的对照档不登记, 复现"停机不清输出")。 */
void do_outputs_safe(void);

extern volatile uint32_t g_do_poll_n;   /* do_poll 执行过的拍数 (obs_anchor 必须登记) */

#endif /* DCL_DO_H */
