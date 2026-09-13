/**
 * manifest.h — 板内诊断资源目录 (自描述) — 2026-09-13
 *
 * ═══════════════════════════════════════════════════════════════════════════
 * 为什么需要它 (它是"管理面"的地基, 不是一个便利功能):
 *
 *  ① **消灭"PC 端硬编码地址"这一整类缺陷。**
 *     今天一天里我就踩了两次: 硬编码 `g_shm = 0x200084A0`(加一个全局就挪成了 0x200084C0,
 *     读出台账 magic=0 的**假故障**), 以及手抄各诊断区的字偏移。
 *     ⇒ 板子把自己的资源目录**说出来**, PC 端按名字读 —— 布局怎么挪都不会错。
 *     这就是操作系统里 `/proc` 的思路: 稳定的、机器可读的内部接口。
 *
 *  ② **回答"哪里出问题读什么"。**
 *     以前排障的路径是"想起哪儿 → 写个脚本 → 手工算地址"; 现在是
 *     "目录里查名字 → 读 → 按 kind 解释"。诊断知识的落点从**人的记忆**搬到**代码里**。
 *
 *  ③ **它是看门狗的前置。**
 *     复位之后要回答的第一个问题是"**为什么复位**"—— 那在 AXI 的 BOOT_AXI 里
 *     (RCC_RSR/RCC_BDCR)。目录 + 只读 AXI 窗让这件事**不需要调试器**就能做。
 *
 * ★★ 纪律 (冻结平台四条里与本文件相关的两条):
 *   · 本表是**唯一真值源**: 新增诊断区就在这里加一行, 不许在 PC 工具里另写一份地址。
 *   · 表里只列**协议可读**的东西 —— 读路径放行范围见 engine.c 的 ENG_AXIDIAGF。
 *     若某区读不到, 那是 bug (不是"文档里写一下就行")。
 */
#ifndef DCL_MANIFEST_H
#define DCL_MANIFEST_H

#include <stdint.h>
#include "engine.h"      /* OFF_* 偏移 (SHM 相对) */

/* kind: PC 侧据此选解析方式 (见 tools/mgmt.py) */
#define MF_K_U32    0u   /* u32 数组 —— 直接十进制 */
#define MF_K_BITS   1u   /* 位图/掩码 —— 建议按十六进制/逐位看 */
#define MF_K_BYTES  2u   /* 字节数组 —— hexdump */
#define MF_K_STRUCT 3u   /* 混合结构 —— PC 侧有专用解析器 (按名字分派) */

/* flags */
#define MF_F_SHM    1u   /* addr 是 **SHM 相对偏移** (真地址 = g_shm + addr); 0 = 绝对地址 */

#define MF_ENTRY(nm, a, w, k, f) \
    { nm, (uint32_t)(a), (uint16_t)(w), (uint8_t)(k), (uint8_t)(f) }

typedef struct __attribute__((packed, aligned(4))) {
    char     name[12];   /* 定长 ASCII, 不足补 0 —— 直接就是帧里那 12 字节 */
    uint32_t addr;       /* flags&MF_F_SHM ? g_shm + addr : addr */
    uint16_t words;      /* 读几个 32 位字 (0x22 的 count) */
    uint8_t  kind;       /* MF_K_* */
    uint8_t  flags;      /* MF_F_* */
} ManifestEnt_t;
_Static_assert(sizeof(ManifestEnt_t) == 20u,
               "ManifestEnt_t must be 20 bytes (PC parses the wire layout)");

/* ★★ 目录表 —— **按"排障时想回答的问题"分组**, 不按内存地址排。
 *   顺序即用途, 读的人从上往下看就能顺着查。 */
static const ManifestEnt_t g_manifest[] = {
    /* ── ① 板子活着吗 / 在跑什么 (最先该看的) ── */
    MF_ENTRY("SHM_CTRL",  0x0000u,  16u, MF_K_U32,    MF_F_SHM),
    /*   SHM+0x00.. : MAGIC / VERSION / HEARTBEAT(0x08, 每拍+1) / ENGINE_RUN(0x0D)
     *   / N_ROUTES(0x0E) / APPLIED_SEQ ... 判活: HEARTBEAT 两次读必须变大。 */

    /* ── ② 通信域 (Modbus/485 出问题看这里) ── */
    MF_ENTRY("MB_DIAG",   0x4A10u,  32u, MF_K_U32,    MF_F_SHM),
    /*   [0]bytes [1]maxrx [2]short [4]erracc [5]last_byte [15]ERRCLR
     *   [16..22]USART2 CR1/CR2/CR3/BRR/ISR/PRESC/reg_mk
     *   [23..26]响应延迟 last/min/max/n (100µs 拍) [28]FASTOK [29]RX_FULL */
    MF_ENTRY("MB_CTRL",   0x4B20u,  10u, MF_K_STRUCT, MF_F_SHM),
    /*   MbCtrl_t(40B): state/slave_addr/rx_len/rx_pos/silent/enabled/src/tx_uart
     *   frames_rx/frames_tx/err_crc/err_exc */
    MF_ENTRY("MB_RX",     0x4B60u,  64u, MF_K_BYTES,  MF_F_SHM),
    /*   收到的原始字节 (证明"到底到了什么"的最硬证据) */
    MF_ENTRY("MB_TX",     0x4C60u,  64u, MF_K_BYTES,  MF_F_SHM),

    /* ── ③ 故障台账 (本次新增: 异常自己留的案底) ── */
    MF_ENTRY("FAULTLOG",  0x7020u,  34u, MF_K_STRUCT, MF_F_SHM),
    /*   magic"FLOG" total 24类计数 首例(code,tick,c0,c1) 末例
     *   ★ total == Σcats 是自洽式 (能失败) ⇒ 不等就是台账自己坏了 */

    /* ── ④ 时间与事件 ── */
    MF_ENTRY("RTC_DIAG",  0x4A00u,   4u, MF_K_U32,    MF_F_SHM),
    /*   [0]RCC_BDCR [1]RTC_ISR [2]TR 启动时 [3]状态判定 (1=日历已可信) */
    MF_ENTRY("EVT_BUF",   0x6E20u,  64u, MF_K_STRUCT, MF_F_SHM),
    /*   32 条 × [亚秒, 事件码] 环形缓冲 (RTC 1024Hz ⇒ ~1ms 分辨率) */

    /* ── ⑤ 存储 / 黑匣子 (AXI —— 走只读窗才读得到) ── */
    MF_ENTRY("SD_DIAG",   0x24000200u, 64u, MF_K_U32, 0u),
    /*   SD 初始化/日志/吞吐诊断; [17]=台账快照刷新次数 */
    MF_ENTRY("BB_DIAG",   0x24000300u, 64u, MF_K_U32, 0u),
    /*   [36]映射表绑到几槽(应=60) [37]映射表 FNV 校验和 (与 PC 端对账) */
    MF_ENTRY("SD_CFG",    0x24000400u, 16u, MF_K_U32, 0u),
    /*   ★ 调试钩子配置字 (**只读可达; 写仍被拒** —— 一条帧不许改调试钩子) */

    /* ── ⑥ 为什么复位了 (看门狗前置: 复位归因) ── */
    MF_ENTRY("BOOT_AXI",  0x24000500u,  6u, MF_K_STRUCT, 0u),
    /*   [0]启动次数(跨复位单调) [1]"RCLK"标记 [2]RCC_RSR 复位原因 [3]RCC_BDCR
     *   ⇒ 判"是我按的复位/看门狗/掉电/软件"不用调试器。 */
};

#define MANIFEST_N ((uint32_t)(sizeof(g_manifest) / sizeof(g_manifest[0])))

#endif /* DCL_MANIFEST_H */
