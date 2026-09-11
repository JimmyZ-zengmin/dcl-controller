/*
 * modbus.h — Modbus RTU 从站 (通信域 COMM) — H723 移植版
 *
 * 来源: esp32-core0/components/core0/modbus.{h,c} (S3 线)
 * ★ S3 的源码注释里就写着 "SHM 基址本模块自持 …… 便于将来移植到单核/裸机平台时
 *   整块搬走" —— 本次正是那次移植。搬过来时改了三类东西, 其余逐字保留:
 *     ① ESP-IDF 的 UART 驱动 (UART_LL_GET_HW / uart_ll_*_fifo) → H723 寄存器直读
 *     ② IRAM_ATTR → ATTR_ITCM (热路径进 ITCM 是本项目的成本铁律)
 *     ③ memw → dsb (ARM 没有 Xtensa 的 memw)
 *
 * 调用点:
 *   mb_init(base, addr, use_uart)  主循环初始化时调用一次
 *   mb_tick(base)                  ISR 每拍调用 (在路由/SEQ 扫描后、计时统计前)
 *   mb_refresh_hold(base)          刷新读区镜像 (wire→MB_HOLD), 主循环调用
 *   mb_reset(base)                 冷启动复位, 由 cold_start_reset() 调用
 *   mb_inject(base, frame, n)      隧道注入一帧 (0x60 命令, 零硬件验证用)
 *
 * 每拍成本: ≤ MB_TICK_BUDGET(4) 字节 × 状态机开销 (WCET 有上界)
 */
#ifndef DCL_MODBUS_H
#define DCL_MODBUS_H

#include <stdint.h>

/* ── 物理口: USART2 (PA2=TX / PA3=RX) ──
 * ★ 为什么是**独立口**而不是复用协议口 USART1:
 *   ① 两个从站共用一条线无法判定帧归属 (DCL 私有协议 vs Modbus RTU 帧格式不同,
 *      靠"能不能解析"猜归属在噪声/半帧下必然误判);
 *   ② Modbus 用 3.5 字符静默判帧边界 —— 协议口上一条 3KB 的 0x10 deploy 长帧
 *      插进 Modbus 帧中间会直接破坏该语义;
 *   ③ RS485 半双工的方向控制会与"帧归属判断"耦合;
 *   ④ 独立口才能给状态机固定拍预算。
 *   对照 S3: 协议口 UART0(GPIO43/44) / Modbus UART1(GPIO17/18) —— 同为独立口。
 *
 * ★ 为什么**只用轮询不开中断** (与 S3 同): ISR 内直接轮询 FIFO, 确定性最好。
 *   本平台是**单核** (S3 是双核 core0 协议 / core1 ISR), 每多一个中断源就多一份
 *   拍抖动风险; 轮询让 Modbus 的 WCET 有上界, 且不引入新中断。
 */
#define MB_UART_BAUD     115200
#define MB_UART_GPIO_PORT   'A'   /* 仅文档用途 */
#define MB_UART_TX_PIN      2     /* PA2 = USART2_TX */
#define MB_UART_RX_PIN      3     /* PA3 = USART2_RX (本批次不接收, 见下) */

/** @brief 建立通信域配置 (写控制块默认值 + 清零各缓冲)。**不含 UART 初始化**。
 *  @param base      SHM 基址 (显式传, 不依赖全局 —— 与 engine_seq_tick 同风格)
 *  @param slave_addr 从站地址 (0 → 用 MB_DEFAULT_ADDR)
 *  @param use_uart  1 = 默认 RX 走物理口 (src=0); 0 = 默认隧道 (src=1)
 *
 *  ★★ 为什么与 S3 的 mb_init 拆成两个函数 (这是本平台与 S3 的**结构性差异**):
 *     S3 的 cold_start_reset() 是**逐域显式清零**的 (清 RUN/表/wire/SEQ/force,
 *     再显式调 mb_reset 清通信域运行态) ⇒ 通信域的**配置**(slave_addr/src/tx_uart)
 *     天然存活, 所以一次 mb_init 就够。
 *     而 H723 的 cold_start_reset() 是**整段 memset(SHM)** —— 它连通信域配置一起清。
 *     ⇒ 必须在每次 cold_start_reset 之后重建配置, 否则 RESET 一次通信域就死了
 *       (enabled=0, 且 src/slave_addr 全 0)。
 *     ⇒ 于是拆成: mb_config (每次冷启动都调, 重建配置) + mb_uart_enable (只调一次)。
 *     这样"任何清零路径之后, 通信域都自动回到可用的默认状态" —— 与 MAGIC 那条
 *     是同一个思路 (把"必须记得补一句"变成"单一入口自动做")。 */
void mb_config(uint8_t *base, uint8_t slave_addr, int use_uart);

/** @brief 使能 USART2 物理口 (RCC + GPIO AF + BRR + CR1)。**只应调用一次**。
 *  ★ 不开 RXNEIE: 本平台用轮询 (见本文件上方说明), 不引入新中断源。 */
void mb_uart_enable(void);

/** @brief ISR 每拍推进状态机 (RX 收字节 / EXEC 解析 / BUILD 组装 / TX 发送) */
void mb_tick(uint8_t *base);

/** @brief 刷新读区镜像: wire[0..63] 工程量 (×100 取整) → MB_HOLD
 *  ★ 放主循环而不是 ISR: 它是一块 64×u16 的搬运, 每拍做等于给热路径白加成本,
 *    而外部主站读 40001-40064 的频率远低于 100μs。 */
void mb_refresh_hold(uint8_t *base);

/** @brief 冷启动复位 — 清运行态 (state/rx/tx/构建上下文/统计), **保留配置**
 *  (slave_addr/enabled/tick_budget/src/tx_uart)。由 cold_start_reset() 调用。 */
void mb_reset(uint8_t *base);

/** @brief 隧道注入一帧 (0x60 命令的载荷) — 零硬件验证用
 *  @return 0 = 受理, <0 = 拒绝 (忙碌/长度非法) */
int mb_inject(uint8_t *base, const uint8_t *frame, uint16_t n);

#define MB_DEFAULT_ADDR 1

/* 默认是否使能 USART2 物理口。1 = 上电即用物理口; 0 = 纯隧道调试。
 * ★ 这是**编译开关**而非运行期参数: "要不要动 RCC/GPIO" 属板级配置,
 *   不属于运行期可切的东西 (运行期用 0x62 切 src/tx_uart)。
 * ★ 在硬件到位前保持 1 也无害 —— 没接线的 USART2 只是收不到字节,
 *   隧道注入 (0x60) 照样把协议栈验证跑完。 */
#ifndef MB_DEFAULT_USE_UART
#define MB_DEFAULT_USE_UART 1
#endif

#endif /* DCL_MODBUS_H */
