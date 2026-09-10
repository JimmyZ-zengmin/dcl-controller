/**
 * clock.h — H723 时钟树配置 (阶段 0 核心)
 *
 * 目标: HSE 25MHz 晶振 → PLL1 → CPU 550MHz, HCLK 275MHz, TIMxCLK 275MHz
 * 完整推导与依据见 MIGRATE-H723.md §3。
 *
 * ★ 所有分频比集中在此头文件 —— 要切换 500/480/400MHz 只改这一段 (见 §3.4)。
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

/* PLL1: 5MHz × 110 = 550MHz VCO, /1 → 550MHz
 * 约束: PLL输入 2-16MHz / VCO 192-836MHz / PLL1P ≤ 550 (VOS0)
 * 推导: 要 CPU=550 → VCO 必须是 550 的整数倍 → 只有 550 (1100 超 VCO 上限) */
/* ★★ 频率落点的代价 —— 550MHz 不是免费的 (2026-09-10 实证, ST 论坛 ST 工程师答复佐证) ★★
 *
 * H723 标称 550MHz, 但 **CPU > 520MHz 必须置 FLASH_OPTSR2 的 CPUFREQ_BOOST 选项字节**,
 * 而该位的真实作用是 **关闭 TCM (ITCM/DTCM) 的 ECC**:
 *     RM0468:  "The ECC is always active except when the CPU frequency boost feature
 *               is used. In that case the ECC is no more active on TCM RAMs."
 *     ST 工程师: "to perform accesses at a CPU speed higher than 520 MHz, one must
 *               disable the ECC on this RAM through the CPUFREQ_BOOST option byte"
 *
 * 不设 boost 硬跑 550MHz 的实测后果: **芯片 BusLock**
 *     (SWD 全部 AP 失联: "No cores were discovered", 只有 connect-under-reset 能救)
 *     —— 本次已亲历一次, 不是文献推测。
 *
 * 所以这是一个**架构权衡**, 不是"越高越好":
 *     550MHz  → 必须 CPUFREQ_BOOST=1 → TCM 失去 ECC (内存位翻转无保护)
 *     ≤520MHz → TCM ECC 保留
 *
 * ★当前默认 520MHz (安全落点, 保 ECC, 且能立即验证 DIVP1EN 修复)。
 *   切 550MHz 需先写选项字节 —— 待拍板 (见 MIGRATE-H723.md §3.5)。
 *   520 = 5MHz × 104, HCLK=260 / APB=130 / TIMxCLK=260, 全链路整数 ✓ */
/* ══════════ PLL1 配置 (改这里即可切换主频) ══════════
 * PLL1:  (HSE / DIVM1) × DIVN1 = VCO,   VCO / DIVP1 = SYSCLK
 *        25MHz / 5 = 5MHz,  × 90 = 450MHz VCO,  / 1 → 450MHz
 *
 * ★★★ 本板实测频率天花板 ≈ 465MHz (2026-09-10, LA 外部测量; 见 h723/sweep_freq.sh)
 *   450MHz  ✅ 完整运行   LA 实测拍周期 99.9975us → 反推 CPU 450.01MHz
 *   460MHz  ✅ 完整运行   LA 实测拍周期 99.9975us → 反推 CPU 460.01MHz
 *   470MHz  ❌ 不运行     PA8 无输出 (1 秒内仅 1 个跳变)
 *   480 / 500 / 520 / 550MHz ❌ 全部不运行
 *   → **远低于数据手册标称的 550MHz**, 且不是配置错误 (见下)。
 *
 * 已排除的原因 (全部是实测, 不是推理):
 *   · 晶振频率   — 450MHz 反推 450.01MHz ⇒ HSE 确为 25.000MHz ✓
 *   · PLL 分频   — 同上, 频率精确到 0.002%
 *   · FLASH 等待态 — WS = 3 / 7 / 15 结果完全相同
 *   · 总线带宽   — 500MHz CPU + HCLK 仅 125MHz (HPRE=/4) 仍然失败
 *   · VOS 电压档 — 写 Scale0 与保持复位默认档, 结果相同
 *   · PLL 输入频率 — 复刻板商原配 M=10/N=220/RGE=1 (2.5MHz 输入) 同样失败
 *   · CPUFREQ_BOOST — 只影响 >520MHz, 解释不了 470MHz 就挂
 *   · DIVP1EN 等输出使能位 — 已修 (修前连 100MHz 都切不过去)
 *
 * ⇒ 剩下最可疑: **VCORE 实际电压没到 VOS0 的水平**, 或板级供电/VCAP 不足。
 *   验证手段: 万用表/示波器量 VCAP 引脚电压。
 *   (AN5419 要求 VCAP 2.2uF / ESR<100mΩ; ★LA Logic 8 无模拟输入, 不能测电压)
 *
 * 当前默认 **400MHz** (测试期固定): 距实测天花板 (~465MHz) 留 65MHz 余量,
 * 且 CPU/HCLK/APB/TIMxCLK = 400/200/100/200 全链路整数 (1us = 400 周期好算)。
 * (440MHz = 5×88 也是全链路整数且余量更大, 可作更保守落点)
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
#define CLK_TIMXCLK_HZ  (CLK_HCLK_HZ)   /* APB 预分频 ≤4 时 TIMxCLK = HCLK */  /* 200 MHz */

/* 拍 (tick) */
#define CLK_TICK_US     100u
#define CLK_TICK_CYCLES ((CLK_CPU_HZ / 1000000UL) * CLK_TICK_US)            /* 40000 周期 */
#define CLK_TICK_TIMCNT ((CLK_TIMXCLK_HZ / 1000000UL) * CLK_TICK_US)        /* 20000 计数 */

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
