#ifndef STEP_H
#define STEP_H
/* ═══════════ 步进脉冲源 (TB6600) (2026-09-15) ═══════════
 * 接线: PUL←PA6(TIM3_CH1)  DIR←PE8  ENA←PE9  (共阳: 三个正端并到板 +5V, MCU 拉低=光耦导通)
 *
 * ★★★ 上电安全态 (最重要的一条):
 *   `step_init()` 结束时 **通道 CC1E=0 (无脉冲)** 且 PE8..PE11 **全部为高(光耦不导通)**。
 *   ⇒ 接 24V 的瞬间: 没有脉冲 ⇒ 无论 ENA 极性如何电机都不会转; 继电器也不吸合。
 *   ★ "会不会转"只由**有没有脉冲**决定 —— 所以"上电无脉冲"是硬保证, ENA 只是次要层。
 *
 * ★★ 为什么不用"数脉冲"而用"控时间 + 编码器闭环":
 *   TIM3 没有重复计数器(RCR), 软件无法精确数脉冲。而位置由 AS5600 闭环给出,
 *   所以本模块只暴露"**频率**"这一个口子 —— 走多远由闭环决定, 不由脉冲数决定。 */
#include <stdint.h>

#define STEP_DIR_ACT   8u        /* PE8  → ACTUATOR[8] */
#define STEP_ENA_ACT   9u        /* PE9  → ACTUATOR[9] */
/* ★ 把 PE8..PE11 一起纳入 DO 面管辖: 好处是"**主动驱动成确定的电平**"而不是悬空
 *   (继电器模块的输入若悬空, 吸合与否不确定 —— 那才是真危险)。 */
#define STEP_DO_MASK   0x0F00u

void     step_init(uint8_t *base);
void     step_tick(uint32_t tick_now);    /* 主循环调用: 只做"限时截止"检查 (按拍差算) */

void     step_set_rate(uint32_t hz);      /* 0 = 停脉冲 */
void     step_set_dir(uint32_t dir);
void     step_set_ena(uint32_t en);       /* en=1 表示"希望驱动器使能" */
/* ★ ENA 有效电平做成**运行期可切**: TB6600 有"拉低使能"与"拉低失能"两种标注,
 *   实测一次就能定, 不必重烧固件 (省掉一整轮 编译→烧录→复位)。
 *   pol=0: 光耦导通(MCU 拉低) = 使能   pol=1: 光耦不导通(MCU 高) = 使能 */
void     step_set_ena_pol(uint32_t pol);
void     step_set_deadline_ms(uint32_t ms);  /* 0 = 不限时 */
void     step_stop_safe(void);            /* 停脉冲 + 失能 (安全态) */

extern volatile uint32_t g_step_rate_hz, g_step_dir, g_step_ena, g_step_owns_tim3;
extern volatile uint32_t g_step_deadline_tick, g_step_stop_n, g_step_arr, g_step_ccr1;
extern volatile uint32_t g_step_ena_pol;
extern volatile uint32_t g_step_dt_max;   /* step_tick 见过的最大 dt (拍) —— 诊断用 */
#endif
