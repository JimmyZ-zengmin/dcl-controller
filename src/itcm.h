/**
 * itcm.h — ★★ ITCM 落位纪律（2026-09-13，一次实测缺陷的产物）
 *
 * ## 不变量
 *   **任何可能被"拍 ISR"间接调用的函数，必须与 ISR 一起住在 ITCM。**
 *
 * ## 为什么（真因记录，别再犯）
 *   拍 ISR 每 100µs 调一批"每拍 poll 一下"的功能：`di_poll` / `adc_poll_kick` /
 *   `adc_poll_reclaim` / `do_poll` / `hil_out_poll` / `rtc_latch` / `bb_kick`。
 *   它们原本都落在 **FLASH（.text）**。而**擦/写内部 flash 期间，flash 取指会被 stall**
 *   （这条项目早就在 VTOR/ITCM 那次记录过，但当时只把**向量表 + ISR**搬进 ITCM）。
 *   于是：落盘 → 擦除 → 拍 ISR 走进这些函数 → **取指 stall → ISR 卡住不返回**
 *   → 后续拍不再触发 → **喂狗停 → 200ms 后看门狗复位**。
 *
 *   现场语义是：**"操作员按保存 → 机器重启"，而且配置永远存不下去**
 *   （实测：每次 `0x43[1]` 启动次数 +1、RSR=IWDG1、`PERSIST_STAT` 的
 *   `writes/erase_ok` 恒 0）。
 *
 * ## 它为什么能躲过所有检查（这才是重点）
 *   这条不变量**此前没有任何机制保证，全靠人记得**。而此后每加一个"在 ISR 里 poll 一下"
 *   的新功能（DI / ADC / DO / HIL / RTC / blackbox），**它就又被悄悄破坏一次** ——
 *   直到某天以"落盘就重启"的形式爆出来。**这是标准的集成回归，而且必然复发。**
 *   ⇒ 纪律：加"ISR 内调用的函数"时，**必须**加 `DCL_ITCM`；
 *     构建期的 ISR 调用树闸门（见 `tools/gate_isr_itcm.py`）会把"忘了"变成"构建不过"。
 *
 * ## 两条纪律（第二条最容易被忽略）
 *   ① 被 ISR 调用的函数 → 加 `DCL_ITCM`。
 *   ② ★ **"代码进 ITCM" ≠ "ISR 不碰 flash"**：若该函数读 `.rodata`（常量表 / LUT /
 *      字符串），那次读**同样会 stall**。大表要么内联成立即数，要么一起放进 ITCM 段。
 *   ⇒ 判据不是"函数地址在 ITCM"，而是"**从 ISR 出发可达的代码与常量都在 ITCM**"。
 */
#ifndef DCL_ITCM_H
#define DCL_ITCM_H

#if defined(DCL_ITCM_ENABLE) && (DCL_ITCM_ENABLE == 0)
/* 对照构建用（`-DDCL_ITCM_ENABLE=0`）：故意把热函数留在 flash，用来复现缺陷。 */
#define DCL_ITCM
#else
#define DCL_ITCM __attribute__((section(".itcm_text"), noinline))
#endif

#endif /* DCL_ITCM_H */
