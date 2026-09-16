#ifndef STEP_H
#define STEP_H
/* ═══════════ 步进脉冲源 (TB6600) (2026-09-15 建, 2026-09-17 修订) ═══════════
 * 接线: PUL←PA6(TIM3_CH1)  DIR←PE8  ENA←PE9
 *   共阳: 三个光耦**正端**并到板 **3.3 V**（★ 不是 5 V），MCU 拉低 = 光耦导通。
 *   ★★ 为什么必须 3.3 V 而不是 5 V：3.3 V 推挽的"高"**关不断** 5 V 光耦
 *     （链上仍有 5−3.3 = 1.7 V > LED 的 Vf）；**开漏也救不了** —— PE8/PE9/PA6 在
 *     `DS13313 Rev 5` Table 7 里是 `TT_ha`（**非 FT**），高阻会被外部拉到 ~4.5 V，
 *     **超绝对最大额定值**。⇒ 正端改接 3.3 V ⇒ 压差 0 ⇒ 彻底关断，且 MCU 只做 sink。
 *     （现场证据：`docs/audit/H723-MOTION-QUALITY-AUDIT.md` §8.7；细则见技能 `mcu-opto-input-bringup`）
 *
 * ★★★ 极性必须**显式声明**，没有"隐藏默认值"：
 *   `DCL_STEP_ENA_POL` 三态（-1 未配置 / 0 / 1）。TB6600 的 ENA 标注两种都有 ⇒
 *   未声明时 **fail-closed：拒绝使能 + 保持物理失能**，而不是"猜一个值"。
 *   ★ 血证（2026-09-16，现场"电机响但轴不动"）：隐藏默认 `0` 与实机接线相反 ⇒
 *     `ena(1)` 实际是**失能**，而 `ena(0)` / `step_stop_safe()` / "上电态"实际是**通电**
 *     ⇒ 「使能 / 失能 / 上电安全态」**三处同时反相**，现象与"驱动器坏了"完全同形。
 *     （A/B 与三处反相表：`H723-MOTION-QUALITY-AUDIT.md` §16）
 *   ★ 工业做法：极性是**接线属性**，用**板上跳线/DIP** 或**装置配置**显式给出
 *     （雷赛 DMC2210 有"板卡跳线设置"专章；ADLINK 限位 NO/NC 用板上 DIP）。
 *     本文件的编译期声明 + 运行期 `step_set_ena_pol()` 就是这两档的软件等价物。
 *
 * ★★★ 上电/停止态 = `DCL_STEP_STOP_HOLD` 决定，与极性**正交**：
 *   保力矩(1) ⇒ 通电；去使能(0) ⇒ 断电。**垂直轴去使能 = 掉力滑车** ⇒ 默认 1。
 *   ★ 旧注释曾写"PE8..PE11 全高 = 上电安全态" —— **那句与电气后果相反**：
 *     在 pol=1 下"高" = 光耦不导通 = **物理使能** ⇒ 上电即通电。
 *     真正与极性无关的硬保证只有一条：**`CC1E=0`（无脉冲）** ——
 *     没有脉冲，无论 ENA 怎么接、极性对不对，电机都不会转。
 *
 * ★★ 为什么不用"数脉冲"而用"控时间 + 编码器闭环"：
 *   TIM3 没有重复计数器(RCR)，软件无法精确数脉冲。而位置由 AS5600 闭环给出，
 *   所以本模块只暴露"**频率**"这一个口子 —— 走多远由闭环决定，不由脉冲数决定。 */
#include <stdint.h>

#define STEP_DIR_ACT   8u        /* PE8  → ACTUATOR[8] */
#define STEP_ENA_ACT   9u        /* PE9  → ACTUATOR[9] */
/* ★ 把 PE8..PE11 一起纳入 DO 面管辖: 好处是"**主动驱动成确定的电平**"而不是悬空
 *   (继电器模块的输入若悬空, 吸合与否不确定 —— 那才是真危险)。 */
#define STEP_DO_MASK   0x0F00u

/* ---- `step_set_ena()` 的返回码（对外可读；**不要**用哨兵值） ---- */
#define STEP_RC_OK            0u   /* 已执行 */
#define STEP_RC_ENAPOL_UNSET  1u   /* ★ fail-closed: ENA 极性未声明 ⇒ 拒绝使能，且保持物理失能 */

void     step_init(uint8_t *base);
void     step_tick(uint32_t tick_now);    /* 主循环调用: 限时截止 + "指令 vs 引脚实读"核对 */

void     step_set_rate(uint32_t hz);      /* 0 = 停脉冲 */
void     step_set_dir(uint32_t dir);
/* en=1 = "希望驱动器使能"。★ 未声明极性时 en=1 会被**拒绝**(返回 STEP_RC_ENAPOL_UNSET)；
 *   en=0（去使能）**永远允许** —— 拒绝一个"停机"请求没有任何安全收益。 */
uint32_t step_set_ena(uint32_t en);
uint32_t step_ena_pol_is_set(void);       /* 0 = 极性未声明（此时一律不许使能） */

/* ★ ENA 有效电平做成**显式声明**（调用本函数即视为"已声明"，相当于"插跳线"这个动作）:
 *   pol=0: 光耦导通(MCU 拉低) = 使能   pol=1: 光耦不导通(MCU 高) = 使能
 *   ★ 实测一次就能定，不必重烧固件；但**不设**时不许使能（fail-closed）。 */
void     step_set_ena_pol(uint32_t pol);
/* 停止/上电时是否**保持力矩**（按轴）。★ 垂直轴必须为 1（去使能 = 掉力滑车）。 */
void     step_set_stop_hold(uint32_t hold);
void     step_set_deadline_ms(uint32_t ms);  /* 0 = 不限时 */
/* 停脉冲 + 按 `stop_hold` 决定静止态（保力矩 ⇒ 通电 / 否则 ⇒ 物理去使能）。 */
void     step_stop_safe(void);

extern volatile uint32_t g_step_rate_hz, g_step_dir, g_step_ena, g_step_owns_tim3;
extern volatile uint32_t g_step_deadline_tick, g_step_stop_n, g_step_arr, g_step_ccr1;
extern volatile uint32_t g_step_ena_pol;
extern volatile uint32_t g_step_dt_max;   /* step_tick 见过的最大 dt (拍) —— 诊断用 */

/* ★★★ 2026-09-17 新增：让"极性有没有声明 / 指令和实际对不对得上"变成**可读的量**。
 *   动机：本次事故里 `ena`(逻辑位) 与 `PE9`(引脚实读) 都在应答里，但**没有任何判据用它**
 *   ⇒ 逻辑位说"使能"、物理上却是"失能"，而两者都不是恒 0/1 ⇒ 不满足"空判据"特征 ⇒ 没人怀疑。
 *   ⇒ 现在把"不一致"本身变成计数器（PID 界的说法：这是 **fail-loud**，不是 fail-silent）。*/
extern volatile uint32_t g_step_ena_pol_set;     /* 0 = 极性未声明 */
extern volatile uint32_t g_step_stop_hold;       /* 1 = 停止/上电保持力矩 */
extern volatile uint32_t g_step_ena_rc;          /* 最近一次 step_set_ena 的返回码 */
extern volatile uint32_t g_step_ena_rej_n;       /* 被 fail-closed 拒绝的次数 */
extern volatile uint32_t g_step_ena_mismatch_n;  /* "指令 != 引脚实读" 的次数 */
extern volatile uint32_t g_step_ena_pin_intent;  /* 我们**意图**的 PE9 电平 (0/1) */
#endif
