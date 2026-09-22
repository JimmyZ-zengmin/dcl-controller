/**
 * dcnn_topology.h —— DT-CNN 的编译期拓扑与预算断言
 *
 * ## 这个文件存在的理由
 *   本项目"每拍成本可算"的能力，来自"无数据相关控制流"。一个固定拓扑 CNN 的循环次数
 *   是**编译期常量** ⇒ 同一个性质 ⇒ 可以事前给出最坏界。本文件把那个拓扑写成常量，
 *   让"MAC 数"和"最坏周期"变成**编译期可判**的量。
 *
 * ## ★★ 设计要点：把"未标定的常数"做成**构建失败**
 *   `DC_C_MAC_Q8` 必须由 `cost/measure_mac_cost.py` 上机实测后填入。
 *   未填时**直接 #error** —— 因为一个"猜出来的成本常数"会让所有"可证"变成假宣称，
 *   而假宣称比没有宣称更坏（本项目"宣称=实现"纪律）。
 *
 * ## 与既有预算门的关系
 *   EXEC_BUDGET_CYCLES  = **扫描段**的运行期判据（80% 拍长）
 *   DC_SLICE_CYC        = **推理切片**的预算
 *   ⇒ 两者是**不同的量、不同的名字**（本项目"一个语义两处存放"族的直接推论）
 *
 * 归属：docs/exp-deterministic-cnn/kernels/ —— 集成时复制进 src/ 并进 CMakeLists。
 */
#ifndef DCNN_TOPOLOGY_H
#define DCNN_TOPOLOGY_H

#include <stdint.h>

/* ── 拍参数：优先用引擎的权威值；本文件被单独编译时用回退（并大声标注） ── */
#if defined(__has_include)
#  if __has_include("engine.h")
#    include "engine.h"
#    define DC_TICK_CYC      (CLK_TICK_CYCLES)
#    define DC_BUDGET_CYC    (EXEC_BUDGET_CYCLES)
#    define DC_FROM_ENGINE   1
#  endif
#endif
#ifndef DC_FROM_ENGINE
#  warning "dcnn_topology.h: 未找到 engine.h ⇒ 用回退值。**集成时必须走 engine.h 的权威值。**"
#  define DC_TICK_CYC        40000u   /* 100 µs @400MHz */
#  define DC_BUDGET_CYC      32000u   /* 拍长 × 80% */
#endif

/* ══════════ 拓扑（固定，编译期常量） ══════════
 * 输入 = 跟随误差序列（AS5600 实测位置 − 指令积分位置），10 kHz
 * 选型依据见 ../README.md §3；成本见 ../THEORY.md §5 */
#define DC_L_IN      256u   /* 窗口点数 @10kHz = 25.6 ms */
#define DC_C1         8u    /* conv1 输出通道 */
#define DC_K1         5u    /* conv1 核长 */
#define DC_P1         4u    /* pool1 窗口 */
#define DC_C2        16u    /* conv2 输出通道 */
#define DC_K2         5u    /* conv2 核长 */
#define DC_NCLS       3u    /* 分类数：正常 / 失步 / 机械异常 */

/* ── 派生形状（全部编译期常量） ── */
#define DC_L1   (DC_L_IN - DC_K1 + 1u)        /* 252  valid 卷积 */
#define DC_L1P  (DC_L1 / DC_P1)               /*  63 */
#define DC_L2   (DC_L1P - DC_K2 + 1u)         /*  59 */
#define DC_GAP  (DC_C2)                       /*  16  全局平均池化 */

/* ── ★ MAC 数：**必须显式带 C_in** ──
 * ★ 本文件第一版漏了 conv2 的 C_in（写成 L×C_out×K）⇒ 少算 8 倍。
 *   这不是小事：它让"能不能进一拍"的结论整体偏乐观。留在注释里防复发。 */
#define DC_MAC1   (DC_L1 * DC_C1 * DC_K1 * 1u)          /* 10 080 */
#define DC_MAC2   (DC_L2 * DC_C2 * DC_K2 * DC_C1)       /* 37 760 */
#define DC_MACFC  (DC_C2 * DC_NCLS)                     /*     48 */
#define DC_MAC    (DC_MAC1 + DC_MAC2 + DC_MACFC)        /* 47 888 */

/* ── 非 MAC 逐元素操作（池化比较 + GAP 累加 + 激活/量化） ── */
#define DC_NONMAC ((DC_L1 * DC_C1) + (DC_L1P * DC_C2) + (DC_C2 * DC_L2))
#define DC_NLAYER 4u                                    /* conv1/pool1/conv2/gap+fc */

/* ── 参数与缓冲 ── */
#define DC_PARAMS ((DC_C1*DC_K1 + DC_C1) + (DC_C2*DC_K2*DC_C1 + DC_C2) + (DC_C2*DC_NCLS + DC_NCLS))
#define DC_BUF_IN     (DC_L_IN)
#define DC_BUF_CONV1  (DC_L1 * DC_C1)
#define DC_BUF_POOL1  (DC_L1P * DC_C1)
#define DC_BUF_CONV2  (DC_L2 * DC_C2)
#define DC_ARENA_MIN  (2u * DC_BUF_CONV2)   /* 双缓冲下界（int8，单位字节） */
#define DC_WEIGHT     (DC_PARAMS)           /* int8 ⇒ 字节数 == 参数量 */

/* ══════════ ★★ 成本常数：未标定即构建失败 ══════════
 * 填入方式：跑 cost/measure_mac_cost.py 上机测出 cycles/MAC，
 *          然后 -DDC_C_MAC_Q8=<cyc × 8>（Q3.5 定点，避免浮点常量比较）。 */
#ifndef DC_C_MAC_Q8
#  error "DC_C_MAC_Q8 未标定！先跑 cost/measure_mac_cost.py 上机测出 cycles/MAC，再 -DDC_C_MAC_Q8=<cyc*8>。**不许猜** —— 一个猜的成本常数会让『可证』变成假宣称。"
#endif
#ifndef DC_K_LAYER_CYC
#  error "DC_K_LAYER_CYC 未标定！层开销必须实测（用 1 层 vs 2 层网络对照）。"
#endif
#ifndef DC_T_FIXED_CYC
#  error "DC_T_FIXED_CYC 未标定！推理固有开销必须实测（N=0 基线，形制照 C_other）。"
#endif

/* 单片推理切片（分给 CNN 的**每拍**周期）。 */
#ifndef DC_SLICE_CYC
#  define DC_SLICE_CYC 12000u
#endif

/* ★★★ 引擎的**声明额度**（每拍、整段 ISR 里属于引擎那部分）。
 *   默认 18 000 = 实测最坏（PID×128 div0 = 17 896 cyc）上取整。
 *   ★ 引擎与推理**共享**报警线，没有谁"天生占 80%"。 */
#ifndef DC_ENGINE_CYC
#  define DC_ENGINE_CYC 18000u
#endif

/* ★★ 承诺周期：**允许多少拍完成**。这是设计参数，不是 1。
 *   见 ../README.md §0：1 拍 / 10 拍(1 ms) / 100 拍(10 ms) / 1000 拍(100 ms) 都合法。
 *   一旦定下，"推理一定在这个周期内结束"就是承诺值。 */
#ifndef DC_COMMIT_TICKS
#  define DC_COMMIT_TICKS 1u
#endif

/* 非 MAC 逐元素操作的成本（池化比较 / GAP 累加 / 激活 / 量化）。
 * ★ 不是承重常数（MAC 占了总量的绝大部分），给默认值即可；有实测再覆盖。 */
#ifndef DC_C_NONMAC_CYC
#  define DC_C_NONMAC_CYC 1u
#endif

/* ── 最坏周期（定点，Q3.5） ── */
#define DC_T_MAC_Q8    ((uint64_t)DC_MAC * (uint64_t)DC_C_MAC_Q8)
#define DC_T_NONMAC    ((uint64_t)DC_NONMAC * (uint64_t)DC_C_NONMAC_CYC)
#define DC_T_LAYER     ((uint64_t)DC_NLAYER * (uint64_t)DC_K_LAYER_CYC)
#define DC_T_WORST     ((uint64_t)DC_T_FIXED_CYC + DC_T_MAC_Q8 / 8u + DC_T_NONMAC + DC_T_LAYER)

/* ══════════ 断言（★ 每条都配了"怎么让它在变异下失败"） ══════════ */

/* 1. 结构自洽：三层的 MAC 非 0、通道数单调 */
_Static_assert(DC_MAC1 > 0u && DC_MAC2 > 0u && DC_MACFC > 0u,
               "DC-CNN: 拓扑常量算出了 0 个 MAC —— 形状常量写错了");
_Static_assert(DC_L1 > DC_L2 && DC_L2 > 0u,
               "DC-CNN: 层间长度不单调 —— 核长/池化窗口与输入长度不匹配");

/* 2. ★ MAC 数必须显式含 C_in（防复发；把结论钉在编译期）
 *    若有人把 DC_MAC2 改成漏掉 C1 的写法，这里立刻红。 */
_Static_assert(DC_MAC2 == DC_L2 * DC_C2 * DC_K2 * DC_C1,
               "DC-CNN: conv2 的 MAC 必须含输入通道数 C_in（漏了会少算 C1 倍）");

/* 3. 参数与缓冲能装下（ITCM 放权重、DTCM 放 arena） */
_Static_assert(DC_WEIGHT <= 4096u,  "DC-CNN: int8 权重超 4 KB —— 超出本课题的 ITCM 预算假设");
_Static_assert(DC_ARENA_MIN <= 16384u, "DC-CNN: arena 超 16 KB —— 会吃掉 DTCM 余量（实测余 ~76 KB）");

/* 4. ★★ 每拍：引擎额度 + 推理切片 ≤ **报警线**（拍长 × 80%）
 *    ★★★ 本条修过一次（2026-09-21，用户指出）。原写法是
 *        `DC_SLICE_CYC + DC_BUDGET_CYC <= DC_TICK_CYC` —— 那是**双重记账**：
 *        `DC_BUDGET_CYC`（32 000）是**整段 ISR** 的天花板
 *        （代码里比的是 `di = t1 - t0`；t0 在 ISR 入口 main.c:970、
 *          t1 在 mb_tick 之后 main.c:1471），**不是"引擎专用额度"**。
 *        原式会放行"引擎 32 000 + 推理 8 000"，而那把整段 ISR 顶到 40 000，
 *        ⇒ `g_isr_overrun` 会一直涨（判据名存实亡）。
 *    ⇒ 正确形式：**两者共享**报警线，谁也没有"天生占 80%"。 */
_Static_assert(DC_ENGINE_CYC + DC_SLICE_CYC <= DC_BUDGET_CYC,
               "DC-CNN: 引擎额度 + 推理切片 超过整段 ISR 的报警线(拍长×80%) ⇒ ov 会一直涨");

/* 5. ★★ 硬线：整段 ISR（引擎 + 推理）必须小于拍长 */
_Static_assert(DC_ENGINE_CYC + DC_SLICE_CYC <= DC_TICK_CYC,
               "DC-CNN: 整段 ISR 超过拍长 —— 拍会被拉长");

/* 6. ★★★ 承重：最坏推理必须在**承诺周期**内结束（README §0 的"承诺值"）
 *    变异对照：把 DC_COMMIT_TICKS 降到 DC_T_WORST/DC_SLICE_CYC 以下 ⇒ 这里必须红。 */
_Static_assert(DC_T_WORST <= (uint64_t)DC_COMMIT_TICKS * (uint64_t)DC_SLICE_CYC,
               "DC-CNN: 最坏推理周期超过承诺周期 ⇒ 加 DC_COMMIT_TICKS，或缩网络");

/* 7. 定点表示不溢出 */
_Static_assert((DC_T_MAC_Q8 / 8u) < (1ull << 40), "DC-CNN: 计算周期数溢出 40 位 —— 常量写错了");

#endif /* DCNN_TOPOLOGY_H */
