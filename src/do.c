/*
 * do.c — DO 数字量输出面 (P3-A: ACTUATOR → GPIOE, CPU 直写)
 *
 * ★ 本组件的存在意义 (2026-09-12 P3-A): 用户最初要求"输入输出链一起搬"。
 *   输入链 (di/adc) 与 PWM 输出 (hil) 已进拍内, 但 16 路 DO 之前**根本不存在**:
 *   `OFF_CTRL_GPIO_MASK` 只做了定案与观测, `eng_outputs_safe` 对 GPIOE 只计数不清位。
 *   本组件把这条最后的"输出链"补上, 并让安全态第一次真正覆盖 GPIOE。
 *
 * 写法要点: **BSRR 单次 32 位写** (低 16 位=置 1, 高 16 位=清 0), 天然原子 ——
 *   不存在"读-改-写 ODR"的窗口, 也不需要临界区。GPIO_MASK 定案② 的越界检查
 *   (高 16 位非 0 ⇒ g_safe_mask_oob) 在 engine.c 的 eng_outputs_safe 里, 不在本处重复。
 */
#include "do.h"
#include "itcm.h"   /* ★ ISR 调用树必须住 ITCM —— 见该头文件 */
#include "engine.h"
#include "regs.h"
#include "memmap.h"

/* ★ 硬件就绪门: 拍中断从阶段 8 就在跑, 而 GPIOE 时钟/引脚配置在 do_init (阶段 26)。
 *   没有这扇门, 阶段 8~26 之间每一拍都会去配未使能时钟的 GPIOE —— 与 adc.c 的
 *   s_adc_ready / hil.c 的 s_hil_ready 同一条纪律: 凡 ISR 可能在 init 之前调用的域,
 *   必须自带就绪门, 不依赖"调用顺序恰好排在我后面"。 */
static uint8_t *s_do_base = 0;
static uint32_t s_do_ready = 0;
volatile uint32_t g_do_poll_n = 0;    /* 活性计数: 就绪即计 (每拍+1) —— 防空判据 */
volatile uint32_t g_do_write_n = 0;   /* 实际写 BSRR 的次数 (区分"活着"与"在干活") */

/* ══════════ P3-B: 影子 + MDMA 定时锁存链 (2026-09-12) ══════════
 * 数据流: do_poll 打包 → 写 shadow(SHM 尾) → TIM2 上溢(拍边界) → DMAMUX1 C8(TIM2_UP=22)
 *         → DMA2 S0 哑传输(读 TIM2_CNT 快照, 1 字) → TC 脉冲 → MDMA ch0 触发
 *         → 读 shadow(4B) → 写 GPIOE_ODR。CPU 零参与锁存, 输出沿硬件锚定。
 *
 * 触发桥的原因 (ST 官方确认): MDMA 的请求源是**固定表**(DMA1/2 的 TC、LTDC、JPEG、
 * QSPI、DMA2D、SDMMC、软件) —— **没有任何 TIM 请求**。⇒ 用 DMA2 哑传输当桥:
 * TIM2_UP 触发 DMA2 (经 DMAMUX1 C8, 请求号 22), DMA2 完成(TC)恰好是 MDMA 的合法请求。
 * 哑传输源故意指向 **TIM2_CNT** —— 锁存瞬间的定时器计数被顺手抄进内存,
 * 输出沿的实测证据免费白送 (旧项目 SCK1 自检的精神)。
 *
 * 配方来源: h723-core0 `dcl_out_dma_start` (真机验证, <4.17ns), 本处三处适配:
 *   ① 触发请求 TIM1_UP(15) → **TIM2_UP(22)** (9.10 的拍定时器是 TIM2);
 *   ② MDMA 源 = DTCM 的 shadow (h723-core0 因 DMA1/2 不可达 DTCM 而被迫把源放
 *      AXI; **MDMA 经 AHBS 可读 DTCM** —— 这是路线 2 保住"SHM 单总线"公理的钥匙);
 *   ③ 哑传输源 = TIM2_CNT (原为 SRAM 固定字) —— 兼作锁存时刻记录。
 * ★ 坑位备忘 (h723-core0 的血泪, 全部规避): DMA2 时钟位=AHB1ENR bit1(曾错 bit2);
 *   DMAMUX 通道归属 0-7=DMA1 / 8-15=DMA2 (C8 = DMA2 S0); 输出源 M0AR 用 0x24003000
 *   (原 0x30004000 是 H723 reserved 区)。 */
#define MDMA_BASE        0x52000000u   /* ★ H723 的 MDMA 在 0x52000000 (h723-core0 实测);
                                          * 首版误写 0x58000000(H743 地址, H723 reserved) ⇒ 寄存器全 0 */
#define MDMA_CH0_CISR    (MDMA_BASE + 0x40u)
#define MDMA_CH0_CIFCR   (MDMA_BASE + 0x44u)
#define MDMA_CH0_CCR     (MDMA_BASE + 0x4Cu)
#define MDMA_CH0_CTCR    (MDMA_BASE + 0x50u)
#define MDMA_CH0_CBNDTR  (MDMA_BASE + 0x54u)
#define MDMA_CH0_CSAR    (MDMA_BASE + 0x58u)
#define MDMA_CH0_CDAR    (MDMA_BASE + 0x5Cu)
#define MDMA_CH0_CLAR    (MDMA_BASE + 0x64u)
#define MDMA_CH0_CTBR    (MDMA_BASE + 0x68u)
#define DMA2S0_CR     0x40020410u
#define DMA2S0_NDTR   0x40020414u
#define DMA2S0_PAR    0x40020418u
#define DMA2S0_M0AR   0x4002041Cu
#define DMA2S0_FCR    0x40020424u
#define DMA2_LIFCR    0x40020408u
#define DMAMUX1_C8    0x40020820u
#define DMAMUX_REQ_TIM2_UP  22u     /* DMAMUX1 请求 22 = TIM2_UP (三源核对) */
#define MDMA_REQ_DMA2S0_TC  8u      /* MDMA_REQUEST_DMA2_Stream0_TC */
#define LATCH_SNAP_ADDR     AXI_LATCH_SNAP  /* 地址唯一源: src/memmap.h */
#define LNODE_ADDR          AXI_LNODE       /* 地址唯一源: src/memmap.h */

/* PEi = ACTUATOR[i] > 0.5。返回本拍要写进 ODR 的 16 位值 (只含管辖位)。 */
static uint32_t do_pack(uint8_t *base, uint32_t mask)
{
    uint32_t bits = 0;
    for (uint32_t i = 0; i < DO_COUNT; i++) {
        if (!((mask >> i) & 1u)) continue;              /* 非管辖位: 不碰 */
        float v = *(volatile float *)(base + OFF_ACTUATOR_STATUS + i * 4u);
        if (v > 0.5f) bits |= (1u << i);
    }
    return bits;
}

void do_init(uint8_t *base)
{
    s_do_base = base;
    RCC_AHB4ENR |= (1u << DO_GPIO_PORT);                /* GPIOE 时钟 */
    /* PE0..15 推挽输出, 初始 0: MODER=01(输出), ODR 经 BSRR 清一遍 */
    GPIO_MODER(DO_GPIO_PORT) = 0x55555555u;
    GPIO_BSRR(DO_GPIO_PORT) = 0xFFFF0000u;              /* 高 16 位写 1 = 全部清 0 */
    __asm__ volatile("dsb" ::: "memory");
    /* 影子模式: shadow 初值 = 0 (与 PE 电平一致, MDMA 使能后无跳变) */
#if DCL_DO_LATCH
    *(volatile uint32_t *)(base + OFF_DO_SHADOW) = 0u;
    *(volatile uint32_t *)(base + OFF_DO_SHADOW_SEQ) = 0u;
#endif
    __asm__ volatile("dsb" ::: "memory");
    s_do_ready = 1;
}

void do_latch_init(void)
{
#if !DCL_DO_LATCH
    /* ★★★ A 档（交付档）必须**完全不启动**这条锁存链 —— 否则它会**把 DO 0..7 弄死**。
     * 机理（2026-09-16 读码取证，见 docs/PLAN-dcl-standardization.md §6.5-4）：
     *   ① 节点 CTCR=0x00020000（**byte 尺寸 + 地址固定**）+ CBNDTR=4
     *      ⇒ 4 次字节搬运**全落在 `GPIOE_ODR + 0`** ⇒ **只覆盖 PE0~PE7**；
     *   ② 源 = `OFF_DO_SHADOW`，而 A 档的 `do_poll()` 走 `#else` 分支，**只写 BSRR、
     *      从不写影子** ⇒ **影子恒为 0**（冷启 memset 之后没有任何写者）；
     *   ③ ⇒ 每 100 µs（TIM2_UP）MDMA 把 **0** 锁进 ODR 低字节
     *      ⇒ **CPU 用 BSRR 置起来的 PE0~PE7 会在下一拍被静默清掉** ⇒ DO 0..7 永不可用。
     * ★ 这条是"DCL_DO_LATCH 切到 0 档"的**必要补丁**：当初只算了抖动
     *   （3.6 ns vs 54~60 ns），**没算到"锁存链还在跑、而影子已经没人写"** ——
     *   于是切档顺手把 DO 0..7 弄死了，而且**观测面上完全看不出来**
     *   （寄存器读回全对、`g_do_write_n` 照涨，只是引脚不动）。
     *   ★ 同族：`h_engine_status` 的 `r[40]` / 影子没人写 —— 都是"改了一半"。
     * ⇒ A 档 = 纯 CPU 直写：**不启用 DMA2 / DMAMUX1 / MDMA，也不置 TIM2.DIER.UDE**。
     *   顺带消掉 ~350 万次/秒的 AXI 链表搬运与 AHB4 写。
     * ★ 判据（能失败的）：A 档下写 `GPIO_MASK` 含 PE0~PE7 的位并置 ACTUATOR>0.5，
     *   `GPIOE_IDR` 对应位必须**保持**为 1（配对照：本补丁前它会在 ≤100 µs 内变 0）。 */
    (void)0;
    return;
#else
    /* ── 时钟: MDMA 在 AHB3, DMA2/DMAMUX1 在 AHB1 ── */
    *(volatile uint32_t *)0x580244D4u |= 1u;            /* RCC_AHB3ENR(@0x580244D4).MDMAEN ——
                                          * 地址经 h723-core0 实测代码核实 (0x1C 是 D2CFGR, 首版写错) */
    RCC_AHB1ENR |= (1u << 1) | (1u << 2);               /* DMA2EN(bit1) + DMAMUX1EN(bit2) */
    __asm__ volatile("dsb" ::: "memory");

    /* ── DMA2 S0: TIM2_UP 触发的哑传输 (TC 脉冲 = MDMA 触发源) ── */
    *(volatile uint32_t *)DMAMUX1_C8 = DMAMUX_REQ_TIM2_UP;   /* 请求 22 = TIM2_UP */
    *(volatile uint32_t *)DMA2S0_CR = 0u;                    /* 先禁 */
    __asm__ volatile("dsb; isb");
    *(volatile uint32_t *)DMA2_LIFCR = 0x3Du;                /* 清 S0 全部标志 */
    *(volatile uint32_t *)DMA2S0_PAR  = LATCH_SNAP_ADDR - 4u; /* 源 = AXI 固定字 (SRAM→SRAM 自环,
                                          * **绝不碰外设寄存器**) —— 首版两次踩坑:
                                          * ① DIR=M2P 未改 ⇒ DMA 把 AXI 内容写进 TIM2_CNT ⇒
                                          *   CNT 被写花成 0x663F331D ⇒ 32 位自由计数不再上溢
                                          *   ⇒ UIF 消失 ⇒ 拍中断永久死亡 (tick 冻结 24); */
    *(volatile uint32_t *)DMA2S0_M0AR = LATCH_SNAP_ADDR;     /* 目的 = AXI 快照槽 */
    *(volatile uint32_t *)DMA2S0_NDTR = 1u;                  /* 1 个字 */
    *(volatile uint32_t *)DMA2S0_FCR  = 0u;
    /* TCIE + M2P + CIRC + PSIZE/MSIZE=word (h723-core0 配方; TC 信号硬件连 MDMA, 无需中断) */
    /* TCIE + **DIR=P2M(bit6=0)** + CIRC + word —— DIR=M2P 是首版致命错:
     * 它让哑传输把 AXI 内容写进 TIM2_CNT (见上), 拍定时器被谋杀 */
    *(volatile uint32_t *)DMA2S0_CR = (1u << 4) | (1u << 8)
                                    | (2u << 11) | (2u << 13);
    __asm__ volatile("dsb; isb");
    *(volatile uint32_t *)DMA2S0_CR |= 1u;                   /* EN */
    __asm__ volatile("dsb; isb");

    /* ── MDMA ch0: **链表循环模式** (h723-core0 'LLOK' 同款) ──
     * ★ 为什么必须链表: BUFFER/BLOCK 单发模式传输完成 ⇒ **EN 自动清零** ⇒
     *   之后每次 DMA2 TC 触发都被忽略 (实测 CISR=0x1E 但 ODR 不再更新)。
     *   链表循环模式: **EN 保持**, 每次请求触发整个链表(单节点=4B 锁存), 永续。
     * ★ 节点 8 word (32B 对齐): CTCR/CBNDTR/CSAR/CDAR/CBRUR/CLAR/CTBR/保留。
     *   CTCR=TRGM=FULL(11)<<17 | byte 尺寸 | 地址固定; CLAR **指回节点自身** = 循环。 */
    *(volatile uint32_t *)LNODE_ADDR + 0u;   /* (占位保持行结构) */
    {
        volatile uint32_t *nd = (volatile uint32_t *)LNODE_ADDR;
        nd[0] = 0x00020000u;                 /* CTCR: TRGM=BLOCK(01)<<17 + byte 尺寸 + 地址固定 */
        nd[1] = 4u;                          /* CBNDTR: BNDT=4 字节 */
        nd[2] = (uint32_t)(s_do_base + OFF_DO_SHADOW);   /* CSAR = shadow */
        nd[3] = 0x58021014u;                 /* CDAR = GPIOE_ODR */
        nd[4] = 0u;                          /* CBRUR */
        nd[5] = LNODE_ADDR;                  /* CLAR 指回自身 ⇒ 循环 */
        nd[6] = MDMA_REQ_DMA2S0_TC | (1u << 16);         /* CTBR: TSEL=DMA2_S0_TC + SBUS */
        nd[7] = 0u;                          /* Reserved */
        /* ★★ 节点必须含 **CMAR/CMDR** (Mask 寄存器字段, 偏移 0x20/0x24) ——
         * 节点总大小 = **40 字节** 而非 32B! 首版只给 8 word ⇒ MDMA 加载时把
         * 节点区之外的 .bss 数据当 Mask 读 ⇒ TEMD=1 (写 Mask Data 出错)
         * ⇒ TEIF ⇒ 整链停止。CMAR/CMDR=0 = 不启用数据掩码。 */
        nd[8] = 0u;                          /* CMAR = 0 (mask 地址, 不启用) */
        nd[9] = 0u;                          /* CMDR = 0 (mask 数据) */
    }
    __asm__ volatile("dsb; isb");
    /* MDMA ch0 主寄存器 = 节点同款配置 + CLAR 指向节点 */
    *(volatile uint32_t *)MDMA_CH0_CIFCR = 0x1Fu;
    *(volatile uint32_t *)MDMA_CH0_CTCR  = 0x00020000u;      /* TRGM=BLOCK + byte + 地址固定 */
    *(volatile uint32_t *)MDMA_CH0_CBNDTR = 4u;
    *(volatile uint32_t *)MDMA_CH0_CSAR  = (uint32_t)(s_do_base + OFF_DO_SHADOW);
    *(volatile uint32_t *)MDMA_CH0_CDAR  = 0x58021014u;
    *(volatile uint32_t *)MDMA_CH0_CLAR  = LNODE_ADDR;       /* 链表头 */
    *(volatile uint32_t *)MDMA_CH0_CTBR  = MDMA_REQ_DMA2S0_TC | (1u << 16);
    __asm__ volatile("dsb; isb");
    *(volatile uint32_t *)MDMA_CH0_CCR   = 1u;               /* EN */
    __asm__ volatile("dsb; isb");
    /* CTBR: TSEL=8(DMA2_S0_TC) + SBUS(bit16)=源在 DTCM (走 TCM 端口)
 * ★★ SBUS/DBUS 语义 (软触发实验定谳 2026-09-12):
 *   SBUS(bit16)=源在 DTCM/TCM 路径; DBUS(bit17)=目的在 DTCM/TCM 路径。
 *   首版误写 DBUS(1<<17) ⇒ MDMA 从 TCM 端口去写 GPIOE(AHB4) ⇒ CESR=0x28
 *   (TEA=40=SAR 低 7 位, TED=0=读错误) ⇒ ODR 永远收到 0。
 *   修为 SBUS 后软触发验证: pyocd 写 shadow=0xFFF ⇒ ODR=0xFF ✅ (byte 尺寸锁存低 8 位) */
    *(volatile uint32_t *)MDMA_CH0_CTBR  = MDMA_REQ_DMA2S0_TC | (1u << 16);
    __asm__ volatile("dsb; isb");
    *(volatile uint32_t *)MDMA_CH0_CCR   = 1u;               /* EN */
    __asm__ volatile("dsb; isb");

    /* ── TIM2 DIER |= UDE: 拍上溢发 DMA 请求 (|= 保护已有 UIE) ── */
    TIM_DIER(TIM2_BASE) |= (1u << 8);
    __asm__ volatile("dsb" ::: "memory");
#endif /* DCL_DO_LATCH —— A 档在函数开头就 return 了, 绝不碰上面这些寄存器 */
}

/* ══════════════════════════════════════════════════════════════════════════
 * ★★★ 掩码"归属"的唯一实现处（见 do.h 的 DO_STEP_RESERVED_MASK 长注释）
 *
 * 为什么必须有"生效掩码"这一层：`GPIO_MASK` 的既有语义是
 *   "bit=0 ⇒ **任何路径都不许碰 PEi**"。这对**普通 DO** 是对的，但对
 *   **步进脉冲源的 PE8..PE11** 是错的 —— 那几位是**驱动器的接口**，
 *   "不碰"等于把它停在**非受控电平**（可能使能、可能失能，取决于上一次是谁写的）。
 *   血证：`g_step_ena_mismatch_n` 涨到 327616 后冻结，真因是上位机把掩码换成 0x00FF。
 *
 * ★ 两处必须用**同一个函数**：#define 一个不行 —— `do_poll`（拍内写）与
 *   `do_outputs_safe`（停机清）以及诊断读回，三处若各算一遍就会分叉
 *   （本项目老族："一个绑定跨两个寄存器 ⇒ 必须同一个函数改"）。
 * ══════════════════════════════════════════════════════════════════════════ */
volatile uint32_t g_do_mask_step_drop_n = 0u;   /* 上位机剔除保留位的**次数**(沿) */

uint32_t do_mask_host(void)
{
    if (!s_do_base) return 0u;
    return SHM_U32(s_do_base, OFF_CTRL_GPIO_MASK) & 0xFFFFu;
}

uint32_t do_mask_effective(void)
{
    uint32_t host = do_mask_host();
#if DCL_DO_MASK_UNION
    return host | DO_STEP_RESERVED_MASK;
#else
    (void)DO_STEP_RESERVED_MASK;
    return host;                    /* ★ A/B 对照档: 改前行为（掩码说了算） */
#endif
}

/* ★ 沿计数（不是每拍 +1）：每拍 +1 会在 10 kHz 下变成不可解释的大数 ——
 *   本轮血证（327616）本身就是"计数器口径不可解释"造成的误判，不能再犯。
 *   ★★ 为什么**内联**在 do_poll 而不单独成一个 DCL_ITCM 函数：**ITCM 已 100% 占满**
 *     （记忆 §5.4），而 do_poll 是 ISR 可达 ⇒ 它调用的每个函数都必须也在 ITCM。
 *     为 4 行账目再开一个 ITCM 符号不划算 ⇒ 直接写在同一函数里。 */
DCL_ITCM void do_poll(uint8_t *base, uint32_t tick_now)
{
    (void)tick_now;          /* 输出面每拍都做, 不需要相位 */
    if (!s_do_ready) return;
    g_do_poll_n++;               /* ★ 活性计数在就绪门之后、mask 判别之前:
                                    第一版放在 mask==0 早退之后 ⇒ 上电 GPIO_MASK=0
                                    时它恒 0, 看起来"DO 没工作"—— 空判据翻车 (项目
                                    已知教训, 自己代码里又踩了一次)。 */
    uint32_t host = SHM_U32(base, OFF_CTRL_GPIO_MASK) & 0xFFFFu;
    /* ★ 记账必须在早退之前（否则"mask==0"这条路径又把它变成空判据） */
    {
        static uint32_t s_prev_drop = 0u;
        uint32_t drop = ((host & DO_STEP_RESERVED_MASK) != DO_STEP_RESERVED_MASK) ? 1u : 0u;
        if (drop != 0u && s_prev_drop == 0u) { g_do_mask_step_drop_n++; }
        s_prev_drop = drop;
    }
    uint32_t mask;
#if DCL_DO_MASK_UNION
    mask = do_mask_effective();             /* = host | DO_STEP_RESERVED_MASK */
#else
    mask = host;                            /* ★ A/B 对照档: 改前行为（掩码说了算） */
#endif
    if (mask == 0u) return;      /* 没登记任何管辖位 ⇒ 整口不碰 (且省 40 cyc) */
    uint32_t bits = do_pack(base, mask);
#if DCL_DO_LATCH
    /* ★★ 影子模式 (P3-B): 只写 shadow, 锁存由 MDMA 在拍边界硬件完成。
     *   非管辖位保持: 读 ODR 现值, 仅改管辖位 —— BSRR 直写模式的"不碰"语义
     *   在这里靠软件保持 (多一次 ODR 读, ~5 cyc, 可忽略)。 */
    uint32_t odr = GPIO_ODR(DO_GPIO_PORT) & 0xFFFFu;
    *(volatile uint32_t *)(base + OFF_DO_SHADOW) = (odr & ~mask) | bits;
    *(volatile uint32_t *)(base + OFF_DO_SHADOW_SEQ) = g_do_poll_n;
#else
    /* BSRR 一次写完成"置位 + 清零", 对 ODR 是原子覆盖; 非管辖位因为 bits 里
     * 相应为 0、mask 相应为 0 ⇒ 写的是 "清 0" —— 但我们**不该清非管辖位**!
     * ⇒ 所以只对管辖位下发: 置位 = bits, 清零 = (mask & ~bits)。 */
    GPIO_BSRR(DO_GPIO_PORT) = bits | ((mask & ~bits) << 16);
#endif
    g_do_write_n++;
}

void do_outputs_safe(void)
{
    if (!s_do_ready) return;
    uint32_t mask = do_mask_effective();   /* ★ 与 do_poll 同一个函数（不许各算一遍） */
#if DCL_DO_LATCH
    /* 影子模式: 清 shadow 的管辖位 (MDMA 下个拍边界锁存 0) —— 与 do_poll 同款保持语义 */
    uint32_t odr = GPIO_ODR(DO_GPIO_PORT) & 0xFFFFu;
    *(volatile uint32_t *)(s_do_base + OFF_DO_SHADOW) = (odr & ~mask);
#else
    GPIO_BSRR(DO_GPIO_PORT) = (mask << 16);
#endif
}
