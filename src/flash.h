/**
 * flash.h — STM32H723 裸 Flash 驱动 (只为本项目的 persist 用途)
 *
 * ★ 为什么不用 HAL: 本工程全程不依赖 CubeHAL (CubeIDE 1.5.1 未附 H7 设备头),
 *   且 HAL_FLASH 会把"等待/超时/错误"包装起来, 掩盖我们**必须能观测**的量。
 *   这里 4 个函数, 每个都返回可判定的错误码, 并且把关键寄存器回读值暴露出去。
 *
 * ★★ 本文件里最容易出错、也最值钱的三条 (全部有权威出处, 不是推测):
 *
 * ① **寄存器偏移**: KEYR1=0x04 / CR1=0x0C / SR1=0x10 / CCR1=0x14 —— 来自 ST 官方
 *    `stm32h723xx.h` 的 FLASH_TypeDef 结构。网上示例常写成 0x0C/0x14/0x18
 *    (那是 bank2 段或 H743 双 bank 的记法), 照抄会**静默操作到 OPTCR 上**。
 *
 * ② **编程粒度 = 256 bit = 32 字节 ("flash word")**, 由 **8 次 32 位连续写**完成。
 *    起始地址必须 32 字节对齐。依据:
 *      · OpenOCD `stm32h7x.c` 源码注释引 RM 原文:
 *          "Standard programming: 1. Check QW bit in FLASH_SR  2. Set PG bit
 *           3. 8 x Word access (or Force Write FW)  4. Wait for completion"
 *      · 该驱动 H72x/H73x 的 `block_size = 32` 字节, 且
 *        `bank->write_start_alignment = block_size` (assert 强制 32B 对齐)
 *      · ST H7 系列 "Flash 最少写入单元 = 256-bit" (与 F4 的 32-bit 完全不同)
 *    ⇒ 写一个不满 32B 的尾块必须先补 0xFF 到 32B 边界 (擦除态是 0xFF)。
 *
 * ③ **完成判据是 SR1.QW (bit 2), 不是 BSY (bit 0)**。
 *    H7 的 flash 控制器有写队列: BSY 会在队列**仍有内容**时提前落 0, 只看 BSY
 *    会在擦除/编程真正结束前放行下一步 ⇒ 后续操作被 PGSERR 拒。
 *    依据: OpenOCD `stm32h7_wait_flash_op_queue()` 等的就是 `(status & FLASH_QW) == 0`。
 *
 * ★ 错误标志清除: 向 **CCR1** 写 1 (偏移 0x14), 不是向 SR1 写。
 *   (OpenOCD: `write_flash_reg(STM32_FLASH_ICR_CCR_INDEX, status)`)
 *
 * ★★ 铁律 (RM0468 §4.3.9): **编程/擦除期间不得从 Flash 取指**。
 *   本项目的热代码全在 ITCM, 且 persist 只在 STOP 窗口调用 —— 天然满足。
 *   但调用方**仍必须**保证: 不得在 ISR 或拍内调用本模块任何函数。
 *   擦除 128KB 扇区约 1~4 秒, 是拍长 (100μs) 的 1~4 万倍。
 */
#ifndef DCL_FLASH_H
#define DCL_FLASH_H

#include <stdint.h>
#include <stddef.h>

#include "regs.h"   /* flash_sector_base 需要 FLASH_BANK1_BASE / FLASH_SECTOR_SIZE */

/* 错误码 (全部为负, 0 = 成功; 与 clock.h 的 CLK_ERR_* 同风格) */
#define FL_OK              0
#define FL_ERR_TIMEOUT   (-1)   /* 等 QW 超时 (硬件未在预期内完成) */
#define FL_ERR_LOCKED    (-2)   /* 解锁后 LOCK 位仍为 1 (密钥写失败) */
#define FL_ERR_SRERROR   (-3)   /* SR1 里有错误标志 (WRPERR/PGSERR/...) */
#define FL_ERR_ALIGN     (-4)   /* 地址/长度不满足 32B 对齐 */
#define FL_ERR_RANGE     (-5)   /* 不在 0x08000000 + 1MB 范围, 或扇区号越界 */
#define FL_ERR_NOSECTOR  (-6)   /* 扇区号 >= FLASH_SECTOR_TOTAL */

/** @brief 解 flash 锁 (写 KEY1/KEY2 到 KEYR1, 回读 CR1.LOCK 确认)
 *  ★ 幂等: 已解锁时直接返回 OK (重复写密钥会触发总线错误)。
 *  @return FL_OK / FL_ERR_LOCKED */
int flash_unlock(void);

/** @brief 重新上锁 (写 CR1.LOCK=1)。持久化完成**必须**调用 —— 否则
 *         任何后续误写都可能改掉 Flash 内容 (本项目其余代码都不碰 Flash)。 */
void flash_lock(void);

/** @brief 当前 CR1 原值 (供工具核对解锁/上锁真的生效, 而不是相信返回值) */
uint32_t flash_cr1(void);

/** @brief 当前 SR1 原值 (诊断用; 注意 QW/BSY 是**瞬时**量, 调用时机决定读数) */
uint32_t flash_sr1(void);

/** @brief 擦除一个扇区 (128KB, 耗时约 1~4 秒 —— 绝不在 ISR/拍内调用)
 *  @param sector 0..7
 *  @return FL_OK / FL_ERR_NOSECTOR / FL_ERR_LOCKED / FL_ERR_TIMEOUT / FL_ERR_SRERROR
 *
 *  ★ 擦除后必须回读验证全为 0xFF —— 硬件不保证"命令返回就说擦干净了"。
 *    (本函数只做擦除; 回读由调用方/persist 层做, 因为那属于"数据完整性"判据。) */
int flash_erase_sector(uint32_t sector);

/** @brief 编程 32 字节对齐的一段 (长度必须是 32 的整数倍)
 *  @param addr  目标地址 (必须 32B 对齐, 且在 0x08000000+1MB 内)
 *  @param src   源数据 (RAM)
 *  @param len   字节数 (必须是 32 的倍数)
 *  @return FL_OK / FL_ERR_ALIGN / FL_ERR_RANGE / FL_ERR_LOCKED / FL_ERR_TIMEOUT / FL_ERR_SRERROR
 *
 *  ★ H7 语义: 只能**把 1 写成 0**。目标区必须先擦除 (全 0xFF), 否则写入结果
 *    是"新旧按位与", 静默给出错误数据 —— 这正是 persist 必须先擦后写的原因。 */
int flash_write(uint32_t addr, const void *src, size_t len);

/* ══════════════ 错误诊断面 (观测) ══════════════
 * ★ 为什么必须有: FL_ERR_SRERROR 只说"SR1 里有错位", 但 ERR_Msk 有 5 个位
 *   (WRPERR/WRPERR/PGSERR/STRBERR/INCERR/OPERR), **哪一个**决定了完全不同的
 *   排障方向 (写保护? 时序? 地址?)。只返回 -3 就只能靠猜。
 *   本项目铁律: 凡"写过了就算"的状态, 必须补一个能被外部读走的量。 */
extern volatile uint32_t g_fl_err_stage;  /* 1=擦 2=写 3=前置等待 */
extern volatile uint32_t g_fl_err_sr1;    /* 出错时的原始 SR1 */
extern volatile uint32_t g_fl_err_cr1;    /* 出错时的原始 CR1 */
extern volatile uint32_t g_fl_err_cnt;    /* 累计出错次数 */

/** @brief 判定某地址是否落在本函数可操作的 Flash 区 (供 persist 做范围断言) */
int flash_addr_ok(uint32_t addr, size_t len);

/** @brief 扇区号 → 基址 (供 persist 用, 避免各处手算 0x08000000 + n*0x20000) */
static inline uint32_t flash_sector_base(uint32_t sector)
{
    return FLASH_BANK1_BASE + sector * FLASH_SECTOR_SIZE;
}

#endif /* DCL_FLASH_H */
