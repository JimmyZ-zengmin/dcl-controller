/*
 * hil.c — HIL 硬件在环 (W5 外设域)。见 hil.h 说明 (语义保留 S3)。
 */
#include "hil.h"
#include "step.h"
#include "itcm.h"   /* ★ ISR 调用树必须住 ITCM —— 见该头文件 */
#include "adc.h"
#include "engine.h"
#include "regs.h"
#include "clock.h"

#define TIM3 TIM3_BASE_ADDR

/* ★ 物理输出面的安全态回调签名统一是 `void (*)(void)` (见 engine.h 的注册表),
 *   带不了 base 参数 ⇒ init 时把基址记一份。它上电后不变, 存静态变量是安全的。 */
static uint8_t *s_hil_base = 0;
/* ★ P1: TIM_ARR+1 的缓存 —— 由 hil_init 写入, 输出臂只读 (见 hil_init 里赋值处的理由)。 */
static uint32_t s_arr1 = 0;
volatile uint32_t g_hil_out_n = 0;   /* hil_out_poll 活性计数 (此前无观测面, 补) */
/* ★★ P1 修复 (2026-09-12): "硬件已初始化" 门。
 *   起因是一次**整机卡死** (排查了很久, 证据链见 main.c 里 s_io_in 那段):
 *   拍中断在 **阶段 8** (`tick_timer_init`) 就开跑了, 而 TIM3 的时钟要到
 *   `hil_init` (**阶段 24**) 才使能 —— 中间这十几毫秒里, 输出臂每拍都会去写
 *   **未使能时钟的 TIM3_CCR1** ⇒ 整机卡进 Default_Handler。
 *   (对照档 `-DDCL_IO_IN_ISR=0` 没事, 因为它的输出臂在主循环里、天然在 init 之后。)
 *   ⇒ 与 adc.c 的 `s_adc_ready` **同一条纪律**:
 *     凡 ISR 可能在 init 之前调用的域, 必须自带"硬件就绪"门 ——
 *     不能依赖"调用顺序正好排在 init 后面"这种隐含前提。 */
static uint32_t s_hil_ready = 0;

static inline float hil_read_f(uint8_t *base, uint32_t off)
{
    return *(volatile float *)(base + off);
}
static inline void hil_write_f(uint8_t *base, uint32_t off, float v)
{
    *(volatile float *)(base + off) = v;
}

void hil_init(uint8_t *base)
{
    s_hil_base = base;                                /* 供 hil_outputs_safe 回写镜像 */
    /* ① PWM 脚 PA6 → AF2 (TIM3_CH1) */
    RCC_AHB4ENR |= (1u << 0);
    uint32_t bit = HIL_PWM_PIN & 15u;                 /* PA6 → bit6, 端口 A */
    uint32_t m = GPIO_MODER(0);
    m &= ~(3u << (bit * 2u));
    m |=  (2u << (bit * 2u));                         /* 10 = AF */
    GPIO_MODER(0) = m;
    uint32_t afr = GPIO_AFRL(0);                      /* PA0..PA7 在 AFRL */
    afr &= ~(0xFu << (bit * 4u));
    afr |=  (2u << (bit * 4u));                       /* AF2 = TIM3 */
    GPIO_AFRL(0) = afr;
    uint32_t pup = GPIO_PUPDR(0);
    pup &= ~(3u << (bit * 2u));                       /* AF 无上下拉 */
    GPIO_PUPDR(0) = pup;
    GPIO_OSPEEDR(0) |= (3u << (bit * 2u));            /* 高速档 (1kHz 方波) */

    /* ② 反馈脚 PA5 → ADC analog (ADC1_INP19) */
    adc_analog_pin(HIL_FB_PIN);

    /* ③ TIM3: PSC 使计数 1MHz, ARR 使周期 1kHz, PWM 模式 1 */
    RCC_APB1LENR |= RCC_APB1LENR_TIM3EN;
    TIM_PSC(TIM3)  = (uint32_t)(CLK_TIMXCLK_HZ / 1000000u) - 1u;   /* → 1MHz */
    TIM_ARR(TIM3)  = (uint32_t)(1000000u / HIL_PWM_HZ) - 1u;       /* → 1kHz */
    TIM_CCR1(TIM3) = 0u;
    TIM_CCMR1(TIM3) = TIM_CCMR1_OC1PE | (6u << TIM_CCMR1_OC1M_SHIFT);  /* PWM1 */
    TIM_CCER(TIM3)  = TIM_CCER_CC1E;
    TIM_CR1(TIM3)   = TIM_CR1_ARPE | TIM_CR1_CEN;
    TIM_EGR(TIM3)   = TIM_EGR_UG;                     /* 立即装载 PSC/ARR */
    /* ★ 存下 ARR+1 给输出臂用 —— **单一来源**: 就是刚写进 TIM_ARR 的那个值的 +1。
     *   输出臂里不再重算 `1000000/HIL_PWM_HZ`: 那会变成"两处算同一个量",
     *   将来改 PWM 频率时只要漏改一处, 就是**静默的占空比错误**(项目已知族)。 */
    s_arr1 = TIM_ARR(TIM3) + 1u;
    s_hil_ready = 1u;      /* ★ 必须在 s_arr1 之后置位: 见 hil_out_apply 的判据顺序 */

    __asm__ volatile("dsb" ::: "memory");
}

/* ══════════ 输出臂 (两个入口共用) ══════════
 * ★ 抽出来是为了让"主循环版"与"拍内版"**逐字相同地**跑同一段逻辑 ——
 *   这样 A/B 对照时唯一的变量才真的是**执行频率**, 而不是"我顺手又改了什么"。 */
/* ★★★ 2026-09-13: **必须**住 ITCM —— 它是"擦 flash 期间 ISR 卡死"的真凶。
 *   症状链: 落盘(擦 flash) ⇒ 拍 ISR 每拍都要执行本函数 ⇒ 而它的机器码在 **FLASH**,
 *     取指被 stall ⇒ ISR 不返回 ⇒ 喂狗停 ⇒ 200ms 后 IWDG 复位
 *     ⇒ 现场语义"操作员按保存 = 机器重启", 且配置**从未落盘**。
 *   ★ 为什么此前"看不出它是元凶" —— 两层假象, 都是仪器在骗人:
 *     ① 本函数被 GCC **部分内联(IPA)** 拆成 `hil_out_apply.part.0`, 主体落在
 *        FLASH `0x08007a14`; 而 `hil_out_poll` 在 ITCM ⇒ 靠链接器 veneer
 *        (`ldr.w pc,[pc]` → flash) 跳过去。`nm | grep hil_out_apply` 只看得到
 *        `.part.0` 这类名字, 很容易被当成"已经在 ITCM 的那个函数"。
 *     ② 闸门 `gate_isr_itcm.py` 当时有 6 个自身 bug, 输出的是**"无违规"**
 *        (逐条见该脚本注释; 其中"读字面量没做字节序反转"让第②③项判据整体失效)。
 *   ★ 2026-09-12 那句注释"暂时留在 flash —— 曾试过放 ITCM"的由来: 当时确实一放
 *     ITCM 就卡死, 但真因是**向量表落位** (`_vtor_itcm` 只做了 128 对齐, 见下方
 *     hil_out_poll 的完整证据链), 与"本函数放哪"无关 ⇒ 本函数的落位就这么被漏掉了。
 *   ★ 纪律(见 itcm.h): **凡 ISR 可达的函数必须 DCL_ITCM**, 与"它调谁/谁调它"无关。 */
DCL_ITCM static void hil_out_apply(uint8_t *base)
{
    /* ★ 硬件未就绪直接返回 —— 见 s_hil_ready 的说明 (少了这一句会整机卡死) */
    if (!s_hil_ready) return;
    /* ★★ 2026-09-15: TIM3_CH1 已交给**步进脉冲源**(step.c) ⇒ 本输出臂必须让出。
     *   否则它每拍都把 TIM_CCR1 改回 HIL 占空比, 步进脉冲被**静默毁掉**
     *   (寄存器写进去了、读回来也对, 但波形不对 —— 本项目的老族)。
     *   与 PA6 上同时挂 HIL PWM 和步进脉冲是**同一个引脚的排他使用**, 必须显式串行化。 */
    if (g_step_owns_tim3) { return; }
    /* 输出臂: WIRE[HIL_U_WIRE] → 占空比 (钳到 [0, RES]) */
    float u = hil_read_f(base, OFF_WIRE_MAP + (uint32_t)HIL_U_WIRE * 4u);
#if HIL_SAFE
    /* ★★ 停机 ⇒ 安全态 (2026-09-11 迁移保真度审查 一级 #1):
     *   引擎停扫后 WIRE[20] 是**冻结的陈旧值**。若无条件回写, STOP 之后 PWM 会一直保持
     *   最后占空比不动 —— "停机 = 进安全态"在真实执行器上不成立。
     *   ⇒ 输出臂受 ENGINE_RUN 门控: 未运行就把 u 压到 0。
     *   ★ 为什么这里要再判一次, 而不只靠 STOP 那一刻的清零:
     *     STOP 的一次性清零只覆盖"走了 0x12 命令"这条路径。安全态应当是**持续成立的性质**
     *     (任何把占空比留在非零的路径都会在一个节拍内被纠回), 而不是"某时刻做过一次动作"。
     *     ⇒ h_stop_w1 的即时清零 (快) + 本处的周期自检 (稳), 两者都要。
     *   ★ 反馈眼 **不**受门控: 停机时仍要能读现场值 (输入面不门控, 输出面才门控)。 */
    if (!*(volatile uint8_t *)(base + OFF_CTRL_ENGINE_RUN)) u = 0.0f;
#else
    /* A/B 对照档 (HIL_SAFE=0) **改前行为**: 无条件回写 ⇒ STOP 后仍保持最后占空比。
     * 这一档存在的唯一目的, 是让"停机安全态"判据能在同一套测量方法下量到 FAIL
     * (否则无法排除"判据本身量不出问题")。交付构建永远不是这一档。 */
#endif
    if (!(u > 0.0f)) u = 0.0f;
    if (u > (float)HIL_PWM_RES) u = (float)HIL_PWM_RES;
    uint32_t duty = (uint32_t)((u / (float)HIL_PWM_RES) * (float)s_arr1);
    TIM_CCR1(TIM3) = duty;
    /* ★ 观测镜像: 把"实际写进 TIM3_CCR1 的值"回写 SHM —— 否则"PWM 按 u 变了"这句
     *   话在协议侧不可核对 (只写进硬件寄存器 = 不可验证的宣称)。PC 用 0x22 读回。 */
    *(volatile uint32_t *)(base + OFF_HIL_DUTY) = duty;
}

/* 主循环版 (A/B 对照档 = 改前行为): 输出臂 + 反馈臂, 每 10ms 一次 */
void hil_tick(uint8_t *base, uint32_t tick_now)
{
    static uint32_t last = 0;
    if ((uint32_t)(tick_now - last) < 100u) return;   /* 100 拍 = 10ms */
    last = tick_now;

    hil_out_apply(base);

#if !IO_IN_ISR
    /* ★ P2: 交付档 (IO_IN_ISR=1) 下**跳过本段** —— 这 16 次阻塞采样
     *   (16 × 259µs ≈ **4.1ms**!) 已由拍内的 adc_poll 状态机接管 (跨拍累加, 非阻塞)。
     *   保留在对照档里, 是为了让 A/B 能打出"搬走前"的那份数据。 */
    /* 反馈眼: ADC 多次平均 (无 RC 时采到方波 → 平均≈占空比×VDDA) → SENSOR[2] (V) */
    uint32_t acc = 0;
    uint16_t last_raw = 0;
    for (int i = 0; i < HIL_FB_AVG; i++) {
        uint16_t r = 0;
        if (adc_read(HIL_FB_CH_PA5, &r) != 0) r = 0;   /* 超时按 0 计 (显式, 不用哨兵值) */
        last_raw = r;
        acc += r;
    }
    /* 排障镜像: 与 0x37 扫描同通道读数对照 (SENSOR[2]=0 而扫描=满幅时, 看这里) */
    *(volatile uint32_t *)(base + OFF_HIL_FB_RAW) = last_raw;
    float v = (float)(acc / (uint32_t)HIL_FB_AVG) * 3.3f / 65535.0f;
    hil_write_f(base, OFF_SENSOR_MAP + (uint32_t)HIL_FB_SENSOR * 4u, v);
#endif
    __asm__ volatile("dsb" ::: "memory");
}

/* ---- 物理输出面安全态 (注册到 engine 的输出面表; STOP/RESET 时被调用) ----
 * 见 engine.h "物理输出面注册" 与 hil.h 的 hil_outputs_safe 说明。
 * ★ 幂等: 反复调用只是反复写 0, 无副作用 (安全态必须幂等 —— 它可能被并发路径多次进入)。 */
void hil_outputs_safe(void)
{
    if (!s_hil_base) return;              /* init 之前被调: 硬件未配, 无事可做 */
    TIM_CCR1(TIM3) = 0u;                  /* 输出臂归零 (OC1PE: 下一个更新事件生效, ≤1 个 PWM 周期) */
    /* ★ 镜像必须跟着写, 否则"停机已进安全态"读不出来 (契约见 hil.h)。 */
    *(volatile uint32_t *)(s_hil_base + OFF_HIL_DUTY) = 0u;
    __asm__ volatile("dsb" ::: "memory");
}

/* ★ P1 拍内输出臂 (交付): 由 ISR **每拍**调用一次。
 * ★ 为什么"每拍写一次 CCR"比"每 10ms 写一次"更对:
 *     TIM3_CCR1 配了 OC1PE (预装载) ⇒ 写入的值在**下一个 PWM 更新事件**才搬进影子寄存器
 *     (1kHz ⇒ 每 1ms 一次)。所以两种频率的**生效时刻**几乎一样 —— 但
 *       · 每 10ms 写: 相邻两次更新之间, 有多次更新用的是**同一个陈旧值**
 *       · 每拍写   : 预装载寄存器里**永远有一个最新值**待生效
 *     对"输出跟随输入"的确定性来说, 后者才是想要的性质。代价: 一次 float 读 +
 *     两次比较 + 一次寄存器写 (+ 一次镜像写) —— 实测增量见 PLAN-io-into-engine.md §7。
 * ★ 安全态语义**不变**: 仍走 hil_out_apply() ⇒ 仍受 ENGINE_RUN 门控 (HIL_SAFE=1 时),
 *   STOP 后仍归零。换的只是"多久看一次", 不是"看不看"。 */
/* ★★ 2026-09-12 **已解决** —— 它曾经"一加进 ISR 就整机卡死", 查了十几轮,
 *   最后定位到**真因不在它**: 是**向量表的落位** (`_vtor_itcm = 0x1880` 时卡死,
 *   0x1800 / 0x1900 都正常)。完整证据链见 main.c 里"输出段"那段注释;
 *   修法是 ld/STM32H723ZG_FLASH.ld 的 `.itcm_vectors` 对齐 128 → **256**。
 *   ★ 保留 `long_call` (见 hil.h): 本函数在 flash 而 ISR 在 ITCM, 相距 128MB ⇒
 *     必须走 BLX。这**不是**本次卡死的原因, 但确实是真实的跨区调用要求。 */
DCL_ITCM void hil_out_poll(uint8_t *base, uint32_t tick_now)
{
    (void)tick_now;          /* 输出臂每拍都做, 不需要相位 */
    g_hil_out_n++;           /* 活性计数 (每拍+1; 防空判据) */
    hil_out_apply(base);
}
