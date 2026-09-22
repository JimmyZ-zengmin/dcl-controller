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

/* ★★★ 2026-09-22: TX 改成"入队 + 主循环尽量推"（原来逐字节死等 ⇒ 观测拖慢被测对象）。
 * 主循环每圈调 `uart1_tx_pump()`；`uart1_tx_pending()` 暴露残余（可观测、可作判据）。
 * ★ 队列**必须是线性的**（`uart1_write` 保证"下一条写入前先推完残余"）。
 * ★ 大小只需 ≥ 一条最大应答 ⇒ 由 `main.c` 的 `_Static_assert(FRAME_TOTAL_MAX_V2 <= UART_TXQ_SZ)`
 *   守住；帧上限哪天长大超过它，**构建就红**，不会静默截断。 */
#define UART_TXQ_SZ  8192u
/* ★★★ 2026-09-22（第二次修）：一次 `uart1_tx_pump()` 最多在 ISR 外待多少微秒。
 *
 * 为什么需要它（实测）：只"试一次"的 pump 等于**每圈推 1 字节** —— TXE 推完一字节要
 * 86.8 µs 才再置位（115200），而主循环 0.37 ms/圈 ⇒ 吞吐被绑到 0.37 ms/字节
 * ⇒ **1040 B 要 385 ms**（实测 `sub=26` 拉满 64 条 = **230 ms**，按线路时间只该 90 ms）。
 * ⇒ DELTA_RING 的消费能力掉到 ~224 条/s < 产出 230 条/s ⇒ **环被覆盖、drop 涨**。
 * 而全等（原阻塞版）= 90 ms 但 CPU 100% 自旋。
 * ⇒ 折中：限时 1 ms ⇒ 一次推 ~11 字节、1040 B 约 95 次 pump ≈ 35 ms，
 *   且**单次阻塞只有 1 ms**（已知最大阻塞是 SD 落盘 46.7 ms，1 ms 是它的 2%）。
 * ★ 不选 TXE 中断：中断频率 = 11520 Hz > 拍 10 kHz ⇒ 每次 ~2 µs ⇒ 给拍引入 ~2% 抖动。 */
/* ★★★ 2026-09-22 第三次修：**1000 → 10000 µs**（限时 1 ms 仍不够）。
 *
 * 实测依据（长稳分析，`docs/analysis-2026-09-22/report.html`）：
 *   限时 1 ms 之后 1040 B 实测 **198.7 ms**，而上面按同样的推理**预期 35 ms** —— **差 5.7 倍**。
 *   把 198.7 ms 反代进"每圈最多泵 P 毫秒、主循环另有 C 毫秒"的模型：
 *        吞吐 = 11.5·P / (P + C)   [KB/s]      （11.5 KB/s = 115200 的线路极限）
 *        5.2 = 11.5×1/(1+C)  ⇒  **C ≈ 1.21 ms**
 *   ⇒ **主循环周期实际约 1.21 ms，不是 0.37 ms** ⇒ 1 ms 的泵预算被"每圈固定代价"
 *     吃掉一半以上。而按同一模型，P=10 ms 时吞吐 = 11.5×10/(10+1.21) ≈ **10.3 KB/s**
 *     （≈ 线路极限的 90%）。
 *
 * 为什么要动它（它卡住的是**整条上传通路**）：
 *   运动态实测 **产出 419 条/s**，而消费上限 = 轮询率 × `DELTA_READ_MAX`。
 *   单轮里的 `sub=26`（24 条 = 400 B）按 5.2 KB/s 要 **77 ms** ⇒ 轮询率只有 13.7 Hz
 *   ⇒ 上限 **329 条/s < 419** ⇒ **环被覆盖、丢 20%**（实测丢 97 条/s）。
 *   ★ 而"加大批长"这条路已实测堵死（`engine.h`: n=16/24/32 = 281/297/283，非单调；
 *     n=64 单次 198.7 ms ⇒ 只有 322 条/s）—— 原因正是**这里**：大块发送也被同一个
 *     泵速率卡住。⇒ **先把泵速率提上去，批长才可能重新变有效**。
 *
 * 代价核算（必须说清）：主循环**单次阻塞 1 ms → 10 ms**。
 *   · 已知最大阻塞是 SD 落盘 **46.7 ms** ⇒ 10 ms 是它的 21%；
 *   · 实测主循环最大间隔 1135 拍 = **0.11 s**（看门狗阈值 1.2 s，余量 10×）；
 *   · **拍内实时路径已不在主循环**（`step_service_motion()`/`step_tick_isr()` 都在 ISR，
 *     见 step.c）⇒ 主循环阻塞**不影响拍**。
 *   ★ 仍不选 TXE 中断：中断频率 11.5 kHz > 拍 10 kHz ⇒ 会给拍引入 ~2% 抖动
 *     （本项目的红线是拍的确定性，不能为上传让路）。
 *   ★★ **根治方向是 DMA 发送**（零 CPU、零中断、吞吐 = 线路极限）—— 见 PLAN，本次先不动。
 *
 * ★ **改这个值 = 改源码**（不是构建选项）—— 实测过：
 *   `bash build.sh -DUART_TX_PUMP_US=10000` 会被 CMake 报
 *   `Manually-specified variables were not used by the project` ⇒ **宏根本没进编译**。
 *   原因：`build.sh` 虽然直传 `-D`，但 `CMakeLists.txt` 里**没有**这个变量的
 *   `set(... CACHE STRING ...)` 声明，CMake 于是把它当"未使用变量"丢掉。
 *   （对比：`DCL_MB_BUILD_BUDGET` 那类都有 CACHE 声明 ⇒ 才能用 `-D` 做 A/B。）
 *   ⇒ 所以本次是**改源码**生效的；若要把它变成可 A/B 的旋钮，
 *     需先在 `CMakeLists.txt` 加 `set(UART_TX_PUMP_US ... CACHE STRING ...)`
 *     + `add_compile_definitions(...)`。★ 那件事**尚未做**（记在挂账里）。 */
#ifndef UART_TX_PUMP_US
#define UART_TX_PUMP_US  10000u
#endif
void     uart1_tx_pump(void);
uint32_t uart1_tx_pending(void);

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
