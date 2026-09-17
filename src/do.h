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

/** @brief 锁存链初始化 (P3-B): DMA2 哑传输(TIM2_UP 触发) + MDMA(shadow→GPIOE_ODR)。
 *  依赖 do_init 先行 (用 s_do_base)。调用后 do_poll 走影子模式 (需 DCL_DO_LATCH=1)。 */
void do_latch_init(void);

/* ★ 影子模式开关 (默认开): =1 时 do_poll 只写 shadow, GPIOE_ODR 由 MDMA 在拍边界
 *   硬件锁存 (输出沿与计算时长解耦); =0 时 do_poll 直接写 BSRR (P3-A 行为, 对照档)。
 *   放这里用 #ifndef 兜底, 可被 CMake -DDCL_DO_LATCH=0 覆盖。 */
#ifndef DCL_DO_LATCH
#define DCL_DO_LATCH 0   /* ★★ 2026-09-15 改交付档: CPU 一次 32 位 BSRR 直写。
                            *  理由有二: ① 影子+MDMA 锁存**只覆盖 ODR 低字节** ⇒ PE8~PE15 死;
                            *  而 DIR/ENA 接在 PE8/PE9(高字节), 用锁存档根本驱动不了;
                            *  ② 实测 CPU 直写抖动 3.6ns vs 锁存 54~60ns, 且是原子 16 位写。
                            *  详见 docs/ASSESS-architecture-as-controller-2026-09-14.md */
#endif

extern volatile uint32_t g_do_poll_n;    /* 活性计数 (就绪门后每拍+1) */
extern volatile uint32_t g_do_write_n;   /* 实际写 BSRR 次数 (mask≠0 时) */

/* ══════════════════════════════════════════════════════════════════════════
 * ★★★ 2026-09-17: **步进脉冲源的固有管辖位**（PE8..PE11）—— 掩码不得剔除
 *
 * 起因（一个被计数器掩盖了真因的问题）：`g_step_ena_mismatch_n` 曾涨到 **327616** 后冻结，
 *   当时被解读为"有别的代码在驱 PE9"。实测真因是**上位机把 GPIO_MASK 换成 0x00FF
 *   （`h723_do_hold_test.py`）或 0x7FC00000（`h723_w1.py` 的越界注入）** ⇒
 *   `do_poll` 的 `mask==0` 早退/不含 bit9 ⇒ **PE9 从此没人驱动**，冻结在旧电平 ⇒
 *   与 `g_step_ena_pin_intent` 永久不一致 ⇒ 主循环每圈 +1。
 *
 * ⇒ 结构结论：**PE8..PE11 不是"上位机可以决定要不要管"的位，而是步进脉冲源的固有接口**
 *   （`STEP_DIR/ENA/PUL` 与预留一位）。它们归 DO 面驱动，但**归谁管不由上位的掩码决定** ——
 *   否则"一位都不驱"就等于把驱动器停在**非受控电平**上（安全相关，不是显示问题）。
 *
 * ★ 为什么把常量放在 do.h 而不是 step.h：**门必须住在资源的定义处**（本项目纪律）——
 *   真正写 GPIOE 的是 DO 面，所以"哪些位不能被剔"必须由 DO 面定义。
 *   ★ 而这**不破坏单写者**：PE8..PE11 的**值**仍来自 ACTUATOR[8..11]，DO 面是唯一写者；
 *     这里只是保证"管辖范围"不被裁剪掉。
 *
 * ★ A/B 对照开关 `DCL_DO_MASK_UNION`（默认 1）：置 0 复现**改前行为**（掩码说了算），
 *   用来证明"这条判据能失败"——见 `tools/h723_do_mask_owner_test.py`。
 * ══════════════════════════════════════════════════════════════════════════ */
#define DO_STEP_RESERVED_MASK 0x0F00u   /* PE8..PE11 = 步进 DIR/PUL/ENA + 预留 */

#ifndef DCL_DO_MASK_UNION
#define DCL_DO_MASK_UNION 1   /* ★ 默认开 = 保留位不可被上位机掩码剔除（交付语义）。
                               *   =0 ⇒ **改前行为**（掩码说了算）的对照档，唯一目的是让
                               *   "PE9 被合法丢在半空"这条判据能在同一套测量下量到 FAIL。 */
#endif

/** @brief 上位机写在 SHM 里的**原始**掩码（低 16 位）。诊断用，**只读**。 */
uint32_t do_mask_host(void);

/** @brief **实际生效**的掩码 = host | DO_STEP_RESERVED_MASK（`DCL_DO_MASK_UNION=0` 时 = host）。
 *  ★ 与 `do_poll` 用的是**同一个函数** ⇒ "读回来的"和"用上去的"不可能分叉。 */
uint32_t do_mask_effective(void);

/** @brief 上位机把保留位剔出掩码的**次数（沿计数，不是每拍 +1）**。
 *  ★ 沿计数：若按每拍 +1，10 kHz 下这个数会变成不可解释的大数（本轮血证的翻版）。 */
extern volatile uint32_t g_do_mask_step_drop_n;

#endif /* DCL_DO_H */
