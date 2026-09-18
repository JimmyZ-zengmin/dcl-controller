/**
 * clock.h — H723 时钟树配置 (阶段 0 核心)
 *
 * 链路: HSE 25MHz 晶振 → PLL1 → CPU 400MHz, HCLK 200MHz, APB 100MHz, TIMxCLK 200MHz
 *
 * ★★ **本头文件的 CLK_* 宏是当前频率的唯一权威定义**。
 *   本文件与同目录其它文件里出现过的 550/520/465MHz 全部是**历史记录或天花板记录**,
 *   不是当前配置 —— H6 修正前这里并存过五个互相矛盾的频率叙述, 读注释会直接配错。
 *   完整推导见 MIGRATE-H723.md §3, 天花板取证见 docs/REF-frequency-ceiling.md。
 *
 * ★ 所有分频比集中在下面的宏 —— 换主频只改那一段 (注意 CLK_TIMXCLK_HZ 的断言)。
 *
 * ★ 为什么必须用外部晶振 (而不是内部 HSI):
 *   HSI 是 % 级精度且随温度漂移; 100μs 拍的实际长度会随板温变化。
 *   本项目核心承诺是"声明 100μs 就精确是 100μs", 只有晶振 (ppm 级) 能满足。
 */
#ifndef DCL_CLOCK_H
#define DCL_CLOCK_H

#include <stdint.h>

/* ══════════ 频率定义 (改这里即可切换主频) ══════════ */
#define CLK_HSE_HZ      25000000UL    /* 板上 25MHz 晶振 */

/* HCLK(=AXI) 分频码 (H7 规则): 8=/2, 9=/4, 10=/8, 11=/16 */
#define CLK_HPRE_CODE   8u   /* sweep */
#define CLK_APB_CODE    4u   /* 4 → APB = HCLK/2 */

/* FLASH 等待态 (LATENCY | WRHIGHFREQ<<4)。RM0468 Table 16 按 AXI 索引:
 * VOS0: ≤70MHz→(0,0) ≤140→(1,1) ≤210→(2,2) ≤275→(3,3)
 * 复位默认 0x37 (7 WS) — 偏保守但绝对安全 */
/* FLASH 等待态: 低半字节 = LATENCY, 高半字节 = WRHIGHFREQ (=3, VOS0+VDD>=2.7V)
 * LATENCY=3 对应 AXI ≤275MHz @VOS0 (RM0468 Table 16), 与 ST 官方
 * NUCLEO-H723ZG 模板的 FLASH_LATENCY_3 一致。
 * ★实测: 3 / 7 / 15 对"能否运行"完全无影响 (排除等待态为频率天花板的原因) */
#define CLK_FLASH_WS    3u

/* ══════════ 历史记录: 为什么最终落在 400MHz 而不是 550MHz ══════════
 * ★ H6 修正 (2026-09-10): 本文件里原本并存 550/520/450/400/465MHz **五个**
 *   "当前频率"叙述, 读注释会直接得出错误的配置。现已收敛: **当前频率的唯一权威
 *   定义 = 下面的 CLK_PLL1_* 宏**; 下面这段只作为"为什么不选更高频率"的决策记录,
 *   其中的数字**都不是当前配置**, 不要拿来推算。
 *
 * 决策链 (实测, 非文献推测):
 *   ① H723 标称 550MHz, 但 **CPU > 520MHz 必须置 FLASH_OPTSR2 的 CPUFREQ_BOOST**
 *      选项字节, 而该位的真实作用是**关闭 TCM(ITCM/DTCM) 的 ECC**:
 *        RM0468: ECC 一直有效, 除非使用 CPU 频率提升特性 —— 此时 TCM 上 ECC 不再有效。
 *      不设 boost 硬跑 550MHz 的实测后果: **芯片 BusLock** (SWD 全部 AP 失联,
 *      只有 connect-under-reset 能救) —— 本次已亲历一次。
 *   ② 本板**实测天花板 ≈ 465MHz** (LA 外部测量, 见 h723/sweep_freq.sh 与
 *      docs/REF-frequency-ceiling.md): 450/460 可完整运行, 470 起 PA8 无输出。
 *      排除项: 晶振 / PLL 分频 / FLASH 等待态(3=7=15) / 总线带宽 / VOS 写入 /
 *              PLL 输入频率 / CPUFREQ_BOOST / DIVP1EN —— 全部实测排除。
 *      ⇒ 最可疑: VCORE 实际电压未达 VOS0, 或板级 VCAP/供电不足 (待万用表量 VCAP)。
 *   ③ 因此在**测试期固定 400MHz**: 距实测天花板留 65MHz 余量, 且
 *      CPU/HCLK/APB/TIMxCLK = 400/200/100/200 全链路整数 (1μs = 400 周期好算)。
 *  (历史: 曾用过 520MHz 作为"保 ECC 的安全落点"; 它高于本板实测天花板, 已废弃。)
 * ══════════════════════════════════════════════════════════════════════════ */
/* ══════════ PLL1 配置 (改这里即可切换主频 —— 这是**唯一**权威定义处) ══════════
 * PLL1:  (HSE / DIVM1) × DIVN1 = VCO,   VCO / DIVP1 = SYSCLK
 *
 * ★★★ 本板实测频率天花板 ≈ 465MHz (2026-09-10, LA 外部测量)
 *   450MHz  ✅ 完整运行   LA 实测拍周期 99.9975us → 反推 CPU 450.01MHz
 *   460MHz  ✅ 完整运行   LA 实测拍周期 99.9975us → 反推 CPU 460.01MHz
 *   470MHz  ❌ 不运行     PA8 无输出 (1 秒内仅 1 个跳变)
 *   → **远低于数据手册标称的 550MHz**; 排除清单见上面的"历史记录"块。
 *
 * 当前落点 **400MHz** (测试期固定, 理由见上)。
 */
#define CLK_PLL1_DIVM1  5u
#define CLK_PLL1_DIVN1  80u          /* 5MHz × 80 = 400MHz (测试期固定落点) */
#define CLK_PLL1_DIVP1  1u           /* VCO 直接作 SYSCLK (整数模式, 禁 sigma-delta) */
/* PLL1RGE 输入频率档: 0=1-2M 1=2-4M 2=4-8M 3=8-16M (必须与实际输入频率匹配) */
#define CLK_PLL1_RGE    2u           /* 5MHz 输入 → 4-8MHz 档 */
#define CLK_PLL1_DIVQ1  4u           /* 400/4 = 100MHz (外设用, 暂未使用) */
#define CLK_PLL1_DIVR1  4u

#define CLK_PLL1_IN_HZ  (CLK_HSE_HZ / CLK_PLL1_DIVM1)                       /*  5 MHz */
#define CLK_VCO_HZ      (CLK_PLL1_IN_HZ * CLK_PLL1_DIVN1)                   /* 400 MHz */
#define CLK_SYSCLK_HZ   (CLK_VCO_HZ / CLK_PLL1_DIVP1)                       /* 400 MHz */

/* 分频: CPU=SYSCLK/1, AXI(HCLK)=CPU/2, APB=HCLK/2
 * ★ HCLK = CPU/2 是 H72x 的架构限制 (AXI 最高 275MHz) —— 访问 AXI 上的对象
 *   每个总线周期要花 2 个 CPU 周期。这是"表必须进 DTCM"的时钟层理由。 */
#define CLK_D1CPRE_DIV  1u
#define CLK_HPRE_DIV    2u
#define CLK_APB_DIV     2u

#define CLK_CPU_HZ      (CLK_SYSCLK_HZ / CLK_D1CPRE_DIV)                    /* 400 MHz */
#define CLK_HCLK_HZ     (CLK_CPU_HZ / CLK_HPRE_DIV)                         /* 200 MHz */
#define CLK_PCLK_HZ     (CLK_HCLK_HZ / CLK_APB_DIV)                         /* 100 MHz */
/* ★ H7 的规则是 timx_ker_ck = **2 × PCLK** (当 DxPPRE ≤ 4 时), 不是"HCLK"。
 *   本配置 (APB=/2) 下 2×100 = 200 = HCLK, 所以旧写法 `= CLK_HCLK_HZ` 恰好也对 ——
 *   但 CLK_APB_CODE 是头文件明列的旋钮, **一旦改成 /4, 旧写法会静默把拍周期算错**,
 *   而原来的断言只检查"TIMxCLK 是整数 MHz"(改 /4 后 100MHz 仍是整数 → 放行)。
 *   所以这里按定义写成 2×PCLK, 并加断言把"改 APB 分频必须重算"钉死。 */
#define CLK_TIMXCLK_HZ  (2u * CLK_PCLK_HZ)                                  /* 200 MHz */
_Static_assert(CLK_TIMXCLK_HZ == CLK_HCLK_HZ,
               "拍周期推导依赖 2×PCLK == HCLK; 改了 CLK_APB_CODE/CLK_APB_DIV "
               "必须重新核对 TIMxCLK 与 CLK_TICK_TIMCNT");
_Static_assert(CLK_APB_DIV == 2u || CLK_APB_DIV == 4u,
               "H7 要求 DxPPRE <= 4 才有 timx_ker_ck = 2×PCLK; 更大的分频 "
               "会变成 timx_ker_ck = PCLK (拍周期差 2 倍)");

/* 拍 (tick) */
/* ★★★ 2026-09-18（④ 拍长可配）：**拍长从此只有一个定义处**。
 *
 * ## 缺陷（本日扫描发现）
 * 同一个物理常数以前有**两份**：
 *   · 本文件的 `CLK_TICK_US`（决定 TIM2 的 ARR ⇒ **真实拍长**）
 *   · `engine.h` 的 `TICK_PERIOD_US`（派生 dt ⇒ **代码以为的拍长**）
 * 两者都是 `100u`，于是**看不出来**；而 `engine.h` 那处注释写着"★ 规划中此项将变为可配"
 *   ⇒ 谁按那句话去改 `TICK_PERIOD_US`，**TIM2 仍然是 100 µs**，而 `dt` 变成新值
 *   ⇒ **所有声明的秒/毫秒整体缩放**，且没有任何断言会响。
 * ★ 这是"同一个语义两处存放"在**物理常数**上的形态（本日第 5/6/7 处见 E-Q/E-R/RETRACTIONS）。
 *
 * ## 修法
 * 拍长在这里定义一次（可 `-DCLK_TICK_US=...` 覆盖），`engine.h` 的 `TICK_PERIOD_US`
 * **别名到它** ⇒ 两者不可能分叉。三条断言把"可用的拍长范围"钉住（**都能失败**）：
 *   ① `1e6 % 拍长 == 0` ② `1000 % 拍长 == 0` —— 否则 `step.c` 的 拍→秒/毫秒 换算不精确
 *      （例: 150 µs ⇒ 1000/150 非整数 ⇒ **编译失败**）
 *   ③ `TIM2 的 ARR = 200 × 拍长 ≤ 65536` —— 16 位上限 ⇒ 拍长 > 327 µs **编译失败**
 */
#ifndef CLK_TICK_US
#define CLK_TICK_US     100u
#endif
#define CLK_TICK_CYCLES ((CLK_CPU_HZ / 1000000UL) * CLK_TICK_US)            /* 40000 周期 */
#define CLK_TICK_TIMCNT ((CLK_TIMXCLK_HZ / 1000000UL) * CLK_TICK_US)        /* 20000 计数 */
_Static_assert(1000000UL % CLK_TICK_US == 0UL,
               "拍长必须整除 1e6 µs —— 否则 step.c 的「拍→秒」换算不精确（余数被丢）");
_Static_assert(1000UL % CLK_TICK_US == 0UL,
               "拍长必须整除 1000 µs —— 否则 step.c 的「拍→毫秒」换算不精确");
_Static_assert(CLK_TICK_TIMCNT <= 65536UL,
               "TIM2 的 ARR 是 16 位 ⇒ 拍长上限 = 65536/200 = 327 µs");

/* ══════════ 错误码 (clock_init 返回值) ══════════ */
#define CLK_OK               0
#define CLK_ERR_HSE          (-1)   /* HSE 晶振未起振 (查晶振/负载电容/焊接) */
#define CLK_ERR_VOSRDY       (-2)   /* VOS0 未生效 (VOSRDY 超时) */
#define CLK_ERR_VOS_ACTIVE   (-3)   /* ACTVOS 未跟随 (VOS 已写但电压档没切过去) */
#define CLK_ERR_FLASH_RB     (-4)   /* FLASH_ACR 回读不符 (写入被忽略) */
#define CLK_ERR_PLL_LOCK     (-5)   /* PLL1 未锁定 (PLL1RDY 超时 → 分频比/晶振问题) */
#define CLK_ERR_SWITCH       (-6)   /* SYSCLK 切换失败 (SWS 未跟随) */
#define CLK_ERR_PLLCFGR_RB   (-7)   /* PLLCFGR 回读不符 (DIVP1EN/RGE 写入被忽略) */

/**
 * @brief 初始化时钟: HSE → VOS0 → FLASH 等待态 → PLL1 → 分频 → 切 SYSCLK
 * @return CLK_OK 或负错误码
 *
 * 执行顺序遵循 RM0468 铁律: **升性能时先改电压, 再升频率**。
 * 每一步都有超时保护, 失败立即返回错误码而不是死等 (便于排雷)。
 */
int clock_init(void);

/** @brief 当前 AXI(HCLK) 频率, 由 RCC 寄存器实际值反推 (不是常量) */
uint32_t clock_get_hclk_hz(void);

/** @brief 使能 DWT 周期计数 (测量用, 分辨率 = 1/CPU 频率 = 1.82ns) */
void dwt_enable(void);

/** @brief 读 CPU 周期计数 (DWT->CYCCNT, 需先 dwt_enable) */
uint32_t dwt_cyccnt(void);

#endif /* DCL_CLOCK_H */
