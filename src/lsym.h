/**
 * lsym.h — 安全读取**链接脚本符号**的地址 (DCL H723)
 *
 * ★★ 为什么需要这个宏 (H4 事故, 已在真机上复现并被审计确认):
 *
 *   C 标准保证「两个不同对象的地址必定不同」。当我们写
 *
 *       extern uint8_t _shm_start[];           // ← 声明成了一个**数组对象**
 *       if ((uintptr_t)g_shm != (uintptr_t)_shm_start) return 0;
 *
 *   时, GCC 可以**在不比较任何数值的情况下**断定 `&g_shm != &_shm_start` 恒真,
 *   于是把整个自检折叠成:
 *
 *       08000580 <shm_layout_ok>:
 *         8000580:  2000   movs r0, #0
 *         8000582:  4770   bx   lr          ← 恒返回 0, 零警告
 *
 *   真机表现: `g_shm_ok` 恒为 0, 而 `g_shm_addr = 0x200000E0` 明明正确 ——
 *   自检报了一个**假故障**, 让人去查根本不存在的落位问题。
 *
 *   这不是"一处 bug"而是一**类**: 凡是"把 linker symbol 当地址、又与 C 对象地址
 *   比较"的地方都会中招 —— `_shm_end` / `_sitcm` / `_eitcm` / `_estack` /
 *   `_sidata` / `_edata` …。所以固化成宏, 而不是在每处手写 volatile 中转。
 *
 * 机制: 用一条**空的内联汇编**把符号地址强制物化到寄存器。GCC 无法再把该值当成
 *       "编译期已知的对象地址"参与常量折叠, 于是比较变成真实的内存/寄存器比较。
 *
 * 用法 (只对**比较/算术**有意义; 单纯把符号当地基址去访问内存不受此问题影响):
 *
 *      if ((uintptr_t)g_shm != LSYM_ADDR(_shm_start)) ...;
 *
 * ★ 提醒: 本宏只解决"被折叠"。**权威比对仍应交给外部工具**(pyocd 读回
 *   g_shm_start_addr / g_shm_end_addr 与 nm 输出的符号地址比对) —— 固件自报
 *   永远可能被编译器优化掉, 外部比对不会。
 */
#ifndef DCL_LSYM_H
#define DCL_LSYM_H

#include <stdint.h>

#define LSYM_ADDR(sym)                                                        \
    ({                                                                        \
        extern uint8_t sym[];                                                 \
        uintptr_t v_lsym_;                                                    \
        __asm__ volatile ("" : "=r"(v_lsym_) : "0"((uintptr_t)(void *)sym));   \
        v_lsym_;                                                              \
    })

#endif /* DCL_LSYM_H */
