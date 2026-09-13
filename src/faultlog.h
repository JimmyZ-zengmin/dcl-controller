/**
 * faultlog.h — 统一故障台账 (2026-09-13)
 *
 * ═══════════════════════════════════════════════════════════════════════════
 * 为什么要它 (这一条比代码重要):
 *   485 那次事故的**真正代价不是缺陷本身, 而是"找它的成本"** ——
 *   一个"115200 连续帧下几乎必然触发"的缺陷 (ORE 未清 ⇒ 接收自锁死),
 *   表现为"偶发、收到一点点就没了", 靠 4 轮外部审计 + 一整天排查才定位。
 *   原因不是没人测, 而是:**异常发生时现场没有留下任何案底** ——
 *   我们手里只有"当前状态", 只能靠观测面去**反推**过去发生了什么。
 *
 *   ⇒ 架构上的回答 (本项目"事前留痕胜过事后取证"):
 *     **把"事后拼现场"换成"现场自己留案底"**。
 *     一个统一的台账: 记录**首例**(冻结现场)、**末例**、**分类计数**和**上下文**。
 *
 * ★★ 为什么必须记"首例"而不是只记"最近一次":
 *   第一条异常发生时, 系统状态**还没被后续异常污染** —— 那才是真现场。
 *   只记"最近一次"的话, 你看到的是"已经被搅乱 N 次之后的样子"。
 *   (与汽车 ECU 的 freeze-frame 同理。本次 ORE 排查里, 若有首例记录,
 *    第一眼就能看到 "ORE=1 且 RXNE=0" 那一刻的 ISR/rx_len, 而不是几小时后去猜。)
 *
 * ★★ 台账自己也要能被质疑 (判据必须能失败):
 *   `total == Σcats` 这条自洽式**可以失败** —— 它同时守住"部分写入 / 重入 /
 *   布局漂移"三类问题。PC 侧把它当硬判据读。一个永远为真的校验等于没有校验。
 *
 * ★ 放置与登记纪律 (冻结平台四条):
 *   ① 台账在 **SHM** 内 (DTCM) ⇒ 随 cold_start_reset 的整段 memset 清零,
 *      但**仍显式登记** `fault_init()` —— "天然覆盖"不是"已登记"。
 *   ② 补 `_Static_assert` 布局断言 (见 engine.h)。
 *   ③ 只用已有读路径暴露 (0x22 READ_BURST 读 SHM), **不新增协议命令** ——
 *      少一条命令少一处要同步的东西。
 *   ④ 分类码是**单一真值源**: 所有登记点都引用本文件的 FAULT_* 宏。
 *
 * 读法 (PC):
 *   0x22 READ_BURST(SHM + OFF_FAULT_LOG, 30) —— 30 字 = 120B。
 */
#ifndef DCL_FAULTLOG_H
#define DCL_FAULTLOG_H

#include <stdint.h>
#include "engine.h"     /* OFF_FAULT_LOG —— 本文件依赖 SHM 布局宏; 自包含避免包含顺序陷阱 */

/* ---- 分类码 (单一真值源) ----
 * ★ 分类粒度原则: **按"机制"分, 不按"现象"分** ——
 *   因为排障时要回答的是"哪个机制坏了", 不是"报了什么错"。
 *   (本次教训: "收到一点点就没了"是现象; "ORE 未清锁死"才是机制。) */
#define FAULT_NONE           0u   /* 哨兵: 台账尚未记录过 */
#define FAULT_MB_ORE         1u   /* USART2 溢出/帧错/噪声/校验错 (PE|FE|NE|ORE) */
#define FAULT_MB_CRC         2u   /* 收到的请求 CRC 校验不通过 */
#define FAULT_MB_SHORT       3u   /* 太短帧被丢弃 (<4B) */
#define FAULT_MB_RX_FULL     4u   /* RX 缓冲被填满 (帧过长 / 无帧间隔) */
#define FAULT_MB_EXC         5u   /* 回异常响应 (非法地址/功能码/数量) */
#define FAULT_MB_BUILD_OVF   6u   /* 响应组装越界 (未完成保护触发) */
#define FAULT_ISR_OVER       7u   /* ISR 超拍预算 (超载) */
#define FAULT_SCAN_DIV0      8u   /* 扫描体测到 0 周期 (测量本身坏了) */
#define FAULT_SHM_GUARD      9u   /* SHM 护栏被踩 */
#define FAULT_DEPLOY_REJ    10u   /* 部署被静态校验拒绝 */
#define FAULT_PROTO_NAK     11u   /* 协议命令被拒 (地址/长度/非有限/越界) */
#define FAULT_MACRO_ERR     12u   /* 宏字节码执行错误 */
#define FAULT_CTRL_WHILE_STOP 13u /* 停机态收到需要运行态的控制类命令 */
#define FAULT_TIMEBASE      14u   /* ★ DWT 时基**没在走** (被外部停掉) —— 见下 */
#define FAULT_LOOP_STALL    15u   /* ★ 主循环停滞 (拍照常进但主循环不推进) —— 见下
                                   *   ★ 为什么单独一个码: 看门狗**故意不覆盖主循环**
                                   *     (T26 实测 sector erase 会让主循环阻塞 ~816ms,
                                   *      把主循环拉进门 ⇒ 每次刷盘都误复位)。
                                   *     所以"主循环死了"这件事必须靠**记录**浮出来, 不能靠复位。 */
#define FAULT_WDT_RESET     16u   /* 本轮启动由看门狗复位引起 (由上一轮写进 AXI, 本轮读出) */
#define FAULT_WDT_INIT_FAIL 17u   /* ★ 看门狗**启动失败** (没武装起来) —— 见 wdt.h 的读回校验
                                   *   ★ 为什么必须有这个码: "武装了"若只靠"我写过寄存器了"就是
                                   *     本项目的静默失败族。实测第一次就失败 (返回 -2),
                                   *     而若无此码, 表现只是"看门狗没救场", 查不出为什么。 */
/* ★★ 为什么单独给"时基"一个码 (2026-09-13 实测抓到):
 *   台账上线第一次运行就发现 `SCAN_DIV0` 以"185034 拍里 184043 拍"的频率在报,
 *   上下文 c0 恒为 0 —— 即**引擎扫描段测得恰好 0 周期**。扫描不可能 0 周期,
 *   唯一解释是 **`DWT_CYCCNT` 被停掉了**(调试器会话会静默停它, 见"铁律 0")。
 *   后果: `eng_cyc_*` / `isr_cyc_*` / `pmin/pmax` 这些 DWT 计时统计**全部静默变垃圾**,
 *   而"一切正常, 只有时间量是 0" —— 这正是本项目记录过的、最贵的一类事故。
 *   ⇒ 以前靠人排查才能发现; 现在让**它自己浮出来**: 检测到计数不推进就记一笔。
 *   ★ 为什么不用"两个时刻相减很大"来判暂停: 擦 flash (persist) 本来就会造成
 *     几十 ms 的跨度, 那是**已知且合理**的窗口, 报了就是噪声。
 *     所以只判**"完全不推进"**(delta==0), 无假阳性。 */
/* ★ 加新码时: 只在这里加, 不要在各登记点写裸数字 */
#define FAULT_N_CATS        24u   /* 分类槽位 (留余量; 与 cats[] 尺寸一致) */

#define FAULT_MAGIC         0x464C4F47u   /* "FLOG" —— 台账身份/布局标记 */

/* ---- 台账布局 (packed, 4 对齐; PC 侧按字段偏移解析, 必须钉死) ---- */
typedef struct __attribute__((packed, aligned(4))) {
    uint32_t magic;      /* FAULT_MAGIC; 由 fault_init 写入 ⇒ 用于判"已登记" */
    uint32_t total;      /* 总记录次数 (必须 == Σcats, 见 fault_sane) */
    uint32_t cats[FAULT_N_CATS];   /* [0]=次数(FAULT_NONE 槽不用, 恒 0) */
    /* ---- 首例 (freeze-frame): 最早一次异常的现场 ---- */
    uint32_t f_code;     /* 分类码 */
    uint32_t f_tick;     /* 发生时的拍序号 (调用者提供的时基) */
    uint32_t f_c0;       /* 上下文 0 (调用者定义, 如 ISR / rx_len / NAK 码) */
    uint32_t f_c1;       /* 上下文 1 */
    /* ---- 末例 ---- */
    uint32_t l_code, l_tick, l_c0, l_c1;
} FaultLedger_t;

/* ★★ 断言消息**必须 ASCII** —— GCC 7.3.1 会把非 ASCII 打成八进制转义
 *   (实测: 失败时打出 "FaultLedger_t \37777777745\37777777677..." 完全不可读),
 *   这条规矩本项目早前就记过 (见 cross-project 铁律), 这次仍踩了一次。
 *   尺寸 = 4(magic) + 4(total) + 24*4(cats) + 4*4(首例) + 4*4(末例) = 136 */
_Static_assert(sizeof(FaultLedger_t) == 136u,
               "FaultLedger_t must be exactly 136 bytes (PC parses by offset)");

/** 台账指针 (base = SHM 基址) */
static inline FaultLedger_t *fault_ledger(uint8_t *base)
{
    return (FaultLedger_t *)(void *)(base + OFF_FAULT_LOG);
}
static inline const FaultLedger_t *fault_ledger_r(const uint8_t *base)
{
    return (const FaultLedger_t *)(const void *)(base + OFF_FAULT_LOG);
}

/** 登记: 由 cold_start_reset 调用 (新域必须登记到单一入口)。
 *  ★ 不 memset 整块之外的东西 —— 本域在 SHM 内, 整段 memset 已覆盖;
 *    这里只负责**写 magic**, 让"漏登记"变成一个能被读出来的问题。 */
static inline void fault_init(uint8_t *base)
{
    FaultLedger_t *lg = fault_ledger(base);
    lg->magic = FAULT_MAGIC;
}

/** 记一笔故障。可 ISR 调用 (只做几次 store, 无分支外的重活)。
 *  @param tick 调用者时基 (主循环用 g_tick_count, 通信域用 s_mb_tick —— 单位统一 100µs)
 *  @param c0/c1 上下文: 排障时最想问的那两个量 (如 ISR 原值、rx_len、NAK 码) */
static inline void fault_record(uint8_t *base, uint32_t code,
                                uint32_t tick, uint32_t c0, uint32_t c1)
{
    FaultLedger_t *lg = fault_ledger(base);
    if (lg->magic != FAULT_MAGIC) lg->magic = FAULT_MAGIC;   /* 兜底 (未登记也能记账) */
    if (code < FAULT_N_CATS) lg->cats[code]++;
    lg->total++;
    /* 首例: 只在"还没有首例"时写 (FAULT_NONE 哨兵) ——
     * ★ 注意判据是 f_code == FAULT_NONE 而不是 total == 1:
     *   total 可能因为 code 越界而不涨, 用 total 判会让首例被**覆盖**掉。 */
    if (lg->f_code == FAULT_NONE) {
        lg->f_code = code; lg->f_tick = tick; lg->f_c0 = c0; lg->f_c1 = c1;
    }
    lg->l_code = code; lg->l_tick = tick; lg->l_c0 = c0; lg->l_c1 = c1;
}

/** 自洽校验: 返回 0 = 一致, 非 0 = 出问题 (失败时返回"哪一条").
 *  ★ 这是本模块**唯一能失败的判据**, PC 侧当硬判据读。 */
static inline uint32_t fault_sane(const uint8_t *base)
{
    const FaultLedger_t *lg = fault_ledger_r(base);
    if (lg->magic != FAULT_MAGIC) return 1u;        /* 未登记 (漏进 cold_start_reset) */
    uint32_t s = 0u;
    for (uint32_t i = 0u; i < FAULT_N_CATS; i++) s += lg->cats[i];
    if (s != lg->total) return 2u;                  /* 计数与总和不等 ⇒ 部分写入/重入/布局漂移 */
    return 0u;
}

#endif /* DCL_FAULTLOG_H */
