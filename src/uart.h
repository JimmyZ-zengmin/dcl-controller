/**
 * uart.h — H723 USART1 驱动 (协议物理层)
 *
 * 为什么是 USART1: 板上调试口 H1(2×4P) 已把 **PA9=USART1_TX / PA10=USART1_RX**
 * 引成排针(见 docs/HARDWARE-PINOUT.md §2), 不需要飞线。APB2 = 100 MHz。
 *
 * 分工 (与 esp32-core0 的 S3 版本对齐):
 *   · 发送: **轮询**。响应帧短(ACK 4B / NAK 十几字节), 轮询最简单且无并发问题。
 *   · 接收: **RXNE 中断** → 只做"读 RDR + 塞环形缓冲"两件事(约十几条指令),
 *           解析与命令执行全在**主循环**做 —— ISR 里绝不做可能阻塞的事。
 *   · 中断优先级**必须低于 100μs 拍**(TIM2 = 0)。理由: 拍抖动是命根子,
 *     任何外设中断都不许抢占它。USART 在 115200 下一个字节有 86.8μs 的余量,
 *     排到拍后面完全来得及。
 *
 * ★ 为什么不做"回调"而做"环形缓冲 + pop": 回调会让业务代码跑在中断上下文里。
 *   一旦某天有人在回调里写 flash 或 memcpy 6KB, 拍就被拉长 —— 而那是**运行时
 *   才暴露**的故障。用缓冲把"取字节"和"处理字节"彻底分开, 代价是主循环多一次轮询。
 */
#ifndef DCL_UART_H
#define DCL_UART_H

#include <stdint.h>

/**
 * @brief 初始化 USART1: PA9=TX / PA10=RX, 8N1, 无流控, RXNE 中断
 * @param pclk2_hz  APB2 时钟频率 (H723 本项目 = 100 MHz)
 * @param baud      波特率 (协议固定 115200)
 */
void uart1_init(uint32_t pclk2_hz, uint32_t baud);

/** @brief 轮询发送 n 字节, 返回时保证最后一字节**已完整移出**(等 TC) */
void uart1_write(const uint8_t *p, uint32_t n);

/** @brief 当前 BRR 值 (供外部核对波特率分频: 100MHz/115200 → 0x364) */
uint32_t uart1_brr(void);

/** @brief 取一个已收到的字节; 返回 0 = 缓冲空 (主循环调用) */
int uart1_rx_pop(uint8_t *out);

/** @brief 硬件溢出(ORE)累计次数 —— 非 0 说明主循环排空太慢, 是故障信号 */
uint32_t uart1_ore_count(void);

/** @brief 环形缓冲写满丢弃累计次数 —— 非 0 说明主循环被长时间阻塞 */
uint32_t uart1_drop_count(void);
/* 诊断计数 (排障用, 每字节都能被外部读走) */
uint32_t uart1_isr_count(void);
uint32_t uart1_isr_ore(void);
uint32_t uart1_fe_count(void);
uint32_t uart1_ne_count(void);
uint32_t uart1_push_count(void);
uint32_t uart1_last_isr(void);
uint32_t uart1_last_byte(void);

/**
 * @brief 回读"RXNE 中断真的被使能了吗" —— 1 = 使能
 *
 * ★★ 为什么要有这个函数 (A1 事故的教训, 2026-09-10):
 *   当时 `NVIC_ISER = (1u << 37)` 位移溢出, USART1 中断**从未使能**, 而:
 *     · CR1=UE|TE|RE|RXNEIE 全对  · BRR 精确  · GPIO AF7 正确
 *     · LA 在 PA9 上能抓到真实 UART 波形 (发送路径完全正常)
 *     · 拍中断一切正常 (TIM2 = IRQ 28 < 32, 没踩到坑)
 *   ⇒ 所有"配置类"检查全绿, 只有"上位机发来的字节不触发中断"这一个症状,
 *     而它看起来像"接线没接好"。整条排障方向被带偏了一整轮。
 *   所以:**中断使能必须是一个可以被外部读走的量**, 而不是"写过了就算"。
 *   本函数读的是 NVIC 的 ISER 位本身 (而不是本地缓存一个 bool),
 *   所以它能抓到"写错寄存器"这类失败。
 */
uint32_t uart1_irq_enabled(void);

#endif /* DCL_UART_H */
