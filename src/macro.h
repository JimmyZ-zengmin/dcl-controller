/*
 * macro.h — MACRO 字节码 VM (W5 外设域) — H723 移植版
 *
 * 来源: esp32-core0/components/macro/{macro_loop.h, macro_exec.h} (S3 线)
 *
 * ★ 移植改动 (三类, 其余语义逐字保留):
 *   ① 存储: S3 = 64KB flash 分区 (esp_partition) + FreeRTOS 任务循环
 *           → H723 = **4KB 驻 SHM (RAM)**, 由**主循环**按 loop_ms 间隔驱动
 *      (裸机无 FreeRTOS; macro 是 ms 级慢动作, 不该占 ISR 拍 —— 与 S3 的
 *       "10ms 任务"同一设计意图, 只换调度器)。
 *   ② 平台: gpio_config/gpio_set_level → GPIOx MODER/OTYPER/PUPDR/BSRR/IDR;
 *           rsr.ccount → DWT->CYCCNT; vTaskDelay → DWT 忙等
 *   ③ 排除: SPI 字节码 (0x20-0x23) **不迁移** —— S3 里它只服务 ST7735 屏
 *      (macro_get_spi 是给显示代码共享的), 而 H723 无 TFT 且 display 明确不迁
 *      (MIGRATE §4)。VM 遇到这些 op 返回错误码, 而不是静默跳过 (静默=宣称>实现)。
 *
 * 平台差异 (写进本文件的理由, 不让后来人踩):
 *   · GPIO 引脚编码: S3 用 ESP 的 GPIO_NUM_xx (0..48 全局编号)。H723 改为
 *     **端口编码**: pin 0..15 = PA0..15, 16..31 = PB0..15, 32..47 = PC0..15,
 *     48..63 = PD0..15。PC 侧组态按此编码。改编码 = 改宏 M_PORT_OF/M_BIT_OF。
 *   · 裸地址访问 (0x10 load / 0x11 store): 裸机**无 MMU**, 越界访问 = HardFault
 *     = 整机卡死。故 H723 **限定在 SHM 窗口内** (base .. base+SHM_SIZE, 4B 对齐),
 *     窗口外返回错误码。这是相对 S3 的**有意加固**, 不是遗漏 (S3 允许任意地址)。
 */
#ifndef DCL_MACRO_H
#define DCL_MACRO_H

#include <stdint.h>

#define MACRO_STACK_DEPTH 16

/* 一次性执行 (0x40): code/clen → 栈内容写入 out (最多 MACRO_STACK_DEPTH 个),
 * *outn = 栈深。返回 0 = 成功; <0 = 错误 (见 macro.c 的返回码表)。 */
int macro_exec(uint8_t *base, const uint8_t *code, uint16_t clen,
               uint32_t *out, uint16_t *outn);

/* 主循环每轮调用: run 且到达 loop_ms 间隔时, 执行 SHM 内字节码一次。
 * tick_now = g_tick_count (100μs/拍)。返回 1 = 本轮真的执行了一次 (供观测面计数),
 * 0 = 未到间隔 / 未运行 / 出错自停。 */
int macro_tick(uint8_t *base, uint32_t tick_now);

/* 冷启动复位: 清控制块 (由 cold_start_reset 调用 —— 新域登记入口)。
 * ★ 注: cold_start_reset 已整段 memset(SHM), 本调用是**显式登记** +
 *   为将来可能出现的非零默认值留落点 (同 mb_config 的思路)。 */
void macro_reset(uint8_t *base);

/* 上传字节码 (0x41): [loop_ms u16][code...] → SHM。返回 0 = 成功, <0 = 过长。 */
int macro_upload(uint8_t *base, uint16_t loop_ms, const uint8_t *code, uint16_t clen);

/* 控制 (0x42): action 0=stop 1=start。返回 0 = 成功, <0 = 非法 (无程序/坏 action)。 */
int macro_ctrl(uint8_t *base, uint8_t action);

#endif /* DCL_MACRO_H */
