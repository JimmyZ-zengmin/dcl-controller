/**
 * main.c — DCL 引擎 H723 平台 · 阶段 2
 *
 * 阶段 0: 时钟树 (HSE 25MHz → VOS0 → PLL1 → 400MHz) + LA 外部验证
 * 阶段 1: 100μs 拍 + 空拍骨架 + DWT 拍开销/抖动测量
 * 阶段 2 (本文件): ★ ITCM / DTCM 落位 + 路由扫描移植 + **同镜像 A/B 实验**
 *
 * ══════════════════════════════════════════════════════════════════
 * 阶段 2 要回答的问题 (来自 STAGE1-REPORT §2 的观察)
 * ══════════════════════════════════════════════════════════════════
 * 阶段 1 发现: 同一份 ISR, 删掉 10 条指令反而更慢 (98 vs 95 cyc) ——
 * flash 常驻代码的 WCET 被"对齐/取指"主导, ±10% 不可预测。
 * 于是提了个**假设**: 把热代码搬进 ITCM 就能治。
 *
 * 本文件把这个假设做成可测的实验。为了排除"换固件 = 换环境"的干扰,
 * 用 **单一镜像 + 运行期选择器** 完成 4 组对比:
 *
 *   ① 空拍骨架          (engine_gate=0)                 → 外壳成本
 *   ② 全表扫 · FLASH 版 (gate=1, sel=0, profile=0)      → flash 取指成本
 *   ③ 全表扫 · ITCM  版 (gate=1, sel=1, profile=0)      → ITCM 取指成本
 *   ④ 混合程序 · 两版   (profile=1)                     → 真实程序成本谱
 *
 * ── 运行期选择器 (由 pyocd 写, 见 tools/h723_stage2_read.py) ──
 *   g_engine_gate    0=跳过扫描(只跑骨架) 1=扫描
 *   g_engine_sel     0=扫描走 FLASH 版    1=扫描走 ITCM 版
 *   g_n_routes       本拍扫描条数 (1..128) —— 两点法测斜率用
 *   g_table_profile  0=全DIRECT 1=19原语轮转 2=全PID
 *   g_reinit         写 1 → 主循环重填表 (填表期间自动关 gate, 防撕裂)
 *   g_stat_reset     写 1 → ISR 清空统计 (切配置时用)
 *   g_pa9_enable     1=PA9 每 32 拍翻转 (CH1 线路验证; ★仅 PA9_MODE=0 时有效 ——
 *                      阶段 3.1 起 PA9 默认归 USART1_TX, 见下)
 *
 * ── 编译期开关 ──
 *   ISR_ITCM  0=中断外壳留在 FLASH  1=外壳也进 ITCM (默认)
 *   PA9_MODE  0=PA9 方波(阶段1线路验证) 1=PA9=USART1_TX (默认, 阶段 3.1 起)
 *   (扫描体两份实例**总是同时存在** —— 那是运行期 A/B 的前提)
 *
 * 接线: LA CH4 ← PA8 (拍输出 5kHz) / LA CH1 ← PA9
 *       (PA9_MODE=0 时是 3.2ms 方波; =1 时是 115200 的协议 UART 波形)
 *
 * ── 阶段 3.1 新增: 协议层 (transport 帧 + USART1) ──
 *   帧格式逐字沿用 esp32-core0; 能力位图只声明**真跑通了**的位 (见 transport.h)。
 *   UART 中断优先级 0x80 < 拍(0) —— 外设永远不许抢拍。
 */
#include <stdint.h>
#include <string.h>
#include "regs.h"
#include "clock.h"
#include "engine.h"
#include "transport.h"
#include "uart.h"
#include "flash.h"
#include "persist.h"
#include "modbus.h"
#include "macro.h"
#include "lsym.h"
#include "adc.h"
#include "di.h"
#include "hil.h"
#include "do.h"
#include "rtc.h"
#include "blackbox.h"
#include "sd.h"

#ifndef ISR_ITCM
#define ISR_ITCM 1
#endif

/* ══════════ 启动默认配置 (bench 用, 编译期) ══════════
 * ★ 为什么需要它 (审计途中发现): 运行期选择器再方便, 也**只在 pyocd 会话内有效** ——
 *   实测 pyocd 断开后目标核心被 HALT, 且 `connect_mode=under-reset` 的每次连接
 *   都会**复位目标** (工位经验: 会话内写、会话内读才可靠)。
 *   所以"要用外部仪器(LA)测某个非默认配置"就必须把它**编进固件**。
 * ★ W1 调整 (2026-09-11): 默认从"骨架态(0/0/1)"改为"**跑引擎态(0/1/1)**" ——
 *   骨架态是阶段 1/2 的 bench 默认 (上电安全、不跑引擎)。进入 W1 后引擎配置
 *   已能由协议 (0x10 deploy + 0x11 START) 控制, "上电不跑"不再是安全考虑,
 *   反而会掩盖问题 (PC 发 START 却看不出引擎有没有真动)。
 *   ENGINE_RUN 初始 = 1: 上电即跑空转 (表是 profile 0 全 DIRECT, 无副作用)。
 *   bench 复现阶段 1/2 基线时显式传 -DBOOT_GATE=0。 */
#ifndef BOOT_PROFILE
#define BOOT_PROFILE 0
#endif
#ifndef BOOT_GATE
#define BOOT_GATE    1
#endif
#ifndef BOOT_SEL
#define BOOT_SEL     1
#endif
#ifndef BOOT_SCAN_MODE
#define BOOT_SCAN_MODE 0     /* 0 = 全表扫 / 1 = 分档调度 (阶段 3) */
#endif

/* ── 阶段 3.1 协议层开关 ── */
#ifndef PA9_MODE
#define PA9_MODE 1           /* 0 = PA9 作 GPIO 方波 (阶段 1 线路验证, 保留可复跑)
                              * 1 = PA9 作 USART1_TX (协议; 默认)
                              * ★ 只能是编译期: 引脚复用不是运行期可切的状态,
                              *   做成运行期旋钮就会变成"宣称能做到其实做不到"。 */
#endif
#ifndef BOOT_BANNER
#define BOOT_BANNER 1        /* 上电主动发一帧版本响应 —— 让 LA **不需要 PC 接线**
                              * 就能拿到协议级外部证据 (PA9 上真实波形) */
#endif
#ifndef BANNER_PERIOD_MS
#define BANNER_PERIOD_MS 0   /* >0: 每 N 毫秒重播一次横幅 (LA 抓取用);
                              * 生产固件为 0 —— 持续占总线会干扰 PC 通信 */
#endif
#ifndef UART_SELFTEST
#define UART_SELFTEST 0      /* 1 = 上电做回环自检 (需 PA9↔PA10 短接);
                              * 证明 RX 通路 + CRC 校验 + 解析器真的工作 */
#endif
#ifndef DEPLOY_SELFTEST
#define DEPLOY_SELFTEST 0    /* 1 = 上电跑 deploy 自检 (9 例合法/非法载荷);
                              * 结果在 g_dst_case[] (1=符合预期 2=不符) 与 g_dst_done */
#endif
#ifndef UART_BAUD
#define UART_BAUD 115200u
#endif
#ifndef UART_PCLK2
#define UART_PCLK2 100000000u   /* APB2 = HCLK(200MHz)/2; 见 clock.h CLK_PCLK_HZ */
#endif

/* ══════════ 输出脚 ══════════ */
#define TICK_PORT       0u
#define TICK_BIT        8u          /* PA8: 拍输出 */
#define UARTT_PORT      0u
#define UARTT_BIT       9u          /* PA9: 线路验证 (CH1) */

#define REG8(a)         (*(volatile uint8_t *)(a))
#define NVIC_IP(n)      REG8(0xE000E400UL + (n))

#if ISR_ITCM
#define ISR_PLACE       __attribute__((section(".itcm_text"), noinline, used))
#else
#define ISR_PLACE       __attribute__((noinline, used))
#endif

/* ══════════ 全局可观测 (供 pyocd 读) ══════════
 * ★ 坑位 (2026-09-10 实测两次):
 *   - 只有**静态初值**、固件内无人读也无人写的全局, 会被 `-fdata-sections`
 *     单独开段, 再被 `--gc-sections` 整段回收 → 从符号表消失, 外部读不到。
 *   - 例: 阶段 1 的 g_isr_mode; 阶段 2 的 g_isr_itcm (第一版只在声明处初始化)。
 *   - `__attribute__((used))` 只挡 GCC 层的丢弃, **挡不住链接器的 --gc-sections**。
 *   - 真正可靠的办法: 让变量在代码里被读或被写 (g_isr_itcm 已在 main 里赋值)。
 *   所以下面每个变量都保证被代码触碰过 —— 这是与外部调试器的接口契约。 */
#define OBS   volatile __attribute__((used))

OBS int      g_boot_status = 0;    /* clock_init 返回值, 0=OK */
OBS uint32_t g_stage       = 0;    /* 执行进度 / 运行证据 (7 = ISR 在跑) */
OBS uint32_t g_tick_count  = 0;
OBS uint32_t g_clock_hclk  = 0;
OBS uint32_t g_isr_itcm    = ISR_ITCM;

/* ── 落位自检 ── */
OBS uint32_t g_shm_ok      = 0;    /* SHM 是否真落在 .dtcm_shm / DTCM 域 */
OBS uint32_t g_shm_addr    = 0;    /* g_shm 实际地址 (应为 0x2000xxxx) */
OBS uint32_t g_scan_itcm_addr = 0; /* ITCM 版扫描函数地址 (应 < 0x10000) */
OBS uint32_t g_scan_flash_addr= 0; /* FLASH 版扫描函数地址 (应 ≥ 0x08000000) */

/* ── 向量表落位 (2026-09-11 T26 配套修复) ──
 * 读回 SCB->VTOR。判据: 启动后应 = _vtor_itcm (ITCM 域, < 0x10000),
 * 而**不是** 0x08000000 (flash)。外部工具用这个量证明"擦除期间拍不丢"的
 * 前提条件成立 —— 与 g_isr_itcm 同族的"承诺必须能被读走"。 */
OBS uint32_t g_vtor          = 0;
OBS uint32_t g_vtor_want     = 0;  /* 期望值 = _vtor_itcm 链接符号, 供外部比对 */

/* ── 运行期选择器 (pyocd 写) ── */
OBS uint32_t g_engine_gate    = 0;
OBS uint32_t g_engine_sel     = 0;
OBS uint32_t g_n_routes       = 128;
OBS uint32_t g_table_profile  = 0;
OBS uint32_t g_reinit         = 0;   /* 写 1 → 主循环重填表 */
OBS uint32_t g_stat_reset     = 0;   /* 写 1 → ISR 清统计 */
OBS uint32_t g_pa9_enable     = 0;
OBS uint32_t g_reinit_done    = 0;   /* 证据: 重填表真的发生了几次 */
OBS uint32_t g_table_ck       = 0;   /* ★整表校验和 (工具在 Python 里独立重算比对) */
OBS uint32_t g_bucket_ck      = 0;   /* ★桶表校验和 (同上, 覆盖 220×u16) */
OBS uint32_t g_active_routes  = 0;   /* 表内 ACTIVE 条数 (期望 = n_routes) */
OBS uint32_t g_guard_ok       = 0;   /* 栈哨兵: 1 = SHM 顶上的魔术字完好 */
OBS uint32_t g_guard_bad_off  = 0xFFFFFFFFu;  /* 被踩的第几个字 (诊断用) */

/* ── 阶段 3: 档桶调度 ── */
OBS uint32_t g_scan_mode      = 0;   /* 0 = 全表扫 (阶段 2 基线) / 1 = 分档调度 */
OBS uint32_t g_eng_routes_last = 0;  /* 本拍**实际执行**的路由条数 (正向证据) */
OBS uint64_t g_eng_routes_total = 0; /* 累计执行条数 (与外部预测求和比对) */
OBS uint32_t g_eng_ticks      = 0;   /* 参与累计的拍数 */
OBS uint32_t g_bucket_zero_slots = 0;/* 桶表 64..99 槽非零个数 (期望 0; H9 断言) */

/* ── 引擎扫描: 执行证据 ── */
OBS uint32_t g_eng_ck       = 0;     /* 最近一拍的扫描校验和 */
OBS uint32_t g_eng_sel_used = 0xFFFFFFFFu;  /* 最近一拍实际走的实例 (0/1) */
OBS uint32_t g_eng_n_used   = 0;     /* 最近一拍实际扫的条数 */

/* ★★ H5: 首样本单独留痕 —— 本项目的权威口径是 **max** (WCET), 而 min 极可能
 *   只是"上电后第一拍"这种非稳态样本 (stats_reset 后 min 从 0xFFFFFFFF 开始, 第一个
 *   样本必然成为 min)。旧报告用 `min - overhead` 当"净成本" = 报的是**最乐观值** ——
 *   对一个主打确定性的项目方向正好反了。留痕之后, 外部工具能判断
 *   "min == first ⇒ 这个 min 不可当稳态最小值用"。 */
OBS uint32_t g_eng_cyc_first = 0;   /* 清统计后第一个样本 (0 = 还没有样本) */

/* ══════════ W1: 运行控制 + SHM 读写 的观测面 ══════════
 * 纪律: 每个计数器都要有"能被外部读走"的落点, 且判据必须能失败 ——
 * 只数成功会让"全部被拒"看起来像"没跑过"; 只数失败会让"全部放行"看起来像"很严格"。
 * ⇒ 成功/拒绝**分开计数**, 并且记录最后一次拒绝的原因码。 */
OBS uint32_t g_shm_rd_ok   = 0;   /* 0x20/0x22 成功次数 */
OBS uint32_t g_shm_rd_nak  = 0;   /* 0x20/0x22 被拒次数 */
OBS uint32_t g_shm_wr_ok   = 0;   /* 0x21/0x23 成功次数 */
OBS uint32_t g_shm_wr_nak  = 0;   /* 0x21/0x23 被拒次数 */
OBS uint32_t g_nak_last    = 0;   /* 最后一次拒绝的原因码 (见 NAKRH_* 枚举) */
OBS uint32_t g_start_ok    = 0;
OBS uint32_t g_start_nak   = 0;   /* ★ 与 S3 的 F11 兜底联动: 毒药表 START 必须走这条 */
OBS uint32_t g_stop_ok     = 0;
OBS uint32_t g_reset_ok    = 0;
OBS uint32_t g_safe_calls  = 0;   /* eng_outputs_safe() 被调用次数 (证明安全态真跑过) */
OBS uint32_t g_safe_gpio_mask = 0;/* 最近一次安全态用的掩码 (判据: 非零才算真清过) */
OBS uint32_t g_out_surfaces  = 0; /* 已注册的**物理输出面**个数 (审计 #1: 必须 ≥1) */
OBS uint32_t g_timing_resets  = 0;/* timing_stats_reset 次数 (OA13 幂等判据) */
OBS uint32_t g_engine_run_seen = 0;/* ISR 观察到的 ENGINE_RUN 值快照 */
OBS uint32_t g_isr_cyc_first = 0;

/* ══════════ W2: Force 的观测面 ══════════
 * ★ 判据要点: 强制**非零值**才算真验证过 (OA9 事故: 全用 val=0 的用例里
 *   "只写 WIRE_MAP 不写 FORCE_VAL" 这个 bug 与期望完全重合 → 13/13 全绿却错)。
 *   所以这里记的是"最近一次强制值", 工具必须断言它非零。 */
OBS uint32_t g_force_set     = 0;   /* 成功强制次数 */
OBS uint32_t g_force_rel     = 0;   /* 成功释放次数 */
OBS uint32_t g_force_nak     = 0;   /* 被拒次数 (idx OOR / bad mode / non-finite) */
OBS uint32_t g_force_last_idx = 0;  /* 最近一次成功强制的 wire 号 (0xFFFFFFFF = 无) */
OBS uint32_t g_force_last_val = 0;  /* 最近一次成功强制的值 (f32 位模式, 工具断言非零) */
OBS uint32_t g_force_clears  = 0;   /* eng_force_clear 被调用次数 (deploy/RESET 联动) */

/* ══════════ W2.4: persist (裸 Flash 双副本) 的观测面 ══════════
 * ★ 判据要点: 不能只看"save 返回 0"。要让工具能读到
 *   ① 到底写了哪份 (A/B), ② 序号, ③ 擦除/写/回读各步的结果 —— 这样"掉电判据"
 *   才能成立 (工具要知道"上一份"在哪, 才能断言"新的一笔没毁掉它")。
 * ★ 这些量**必须**被 obs_anchor 读到一次, 否则被 --gc-sections 回收。 */
OBS uint32_t g_persist_cmds       = 0;   /* 0x43 被调用次数 */
OBS uint32_t g_persist_saves      = 0;   /* 受理的 save 请求数 (含因 RUN 跳过) */
OBS uint32_t g_persist_skip_run   = 0;   /* 因引擎 RUN 而跳过落盘 (PERSISTENT 语义门) */
OBS uint32_t g_persist_nak        = 0;   /* 0x43/落盘相关拒绝次数 */
OBS uint32_t g_persist_ab_valid   = 0;   /* 最近一次探测: A/B 有效位图 (1=A 2=B 3=both) */
OBS uint32_t g_persist_seq_a      = 0;   /* 副本 A 序号 */
OBS uint32_t g_persist_seq_b      = 0;   /* 副本 B 序号 */
OBS uint32_t g_persist_crc_a      = 0;   /* 副本 A 实测 CRC32 (0=无效) */
OBS uint32_t g_persist_crc_b      = 0;
OBS uint32_t g_persist_loaded_n   = 0;   /* 上电恢复了多少条 (0 = 空配置) */
OBS uint32_t g_persist_loaded_sec = 0xFFFFFFFFu;  /* 从哪个扇区恢复 (6=A/7=B) */
OBS uint32_t g_persist_loads      = 0;   /* persist_load 调用次数 */

/* ★★ 免串口的落盘触发点 (测试专用, 但在发布固件里也保留):
 *   为什么需要它: persist 的**核心判据是"擦除中掉电也不丢配置"** —— 而"在擦除
 *   进行中打断"这件事, 只有外部调试器能做到 (串口命令做不到: 命令本身要先被
 *   主循环收完, 而主循环正在阻塞等擦除)。所以固件必须提供一个**pyocd 可直接
 *   写、主循环会轮询**的落盘请求标志。
 *   ★ 这与"宣称=实现"的关系: 若只在工具里假装触发, 测的就是工具自己;
 *     这个标志让**固件的真实落盘路径** (persist_save → flash_erase/write) 被走到。
 *   ★ 副作用纪律: 它是 volatile 且被主循环真读 —— 不会被 --gc-sections 回收,
 *     也不依赖 obs_anchor (但保险起见仍登记)。 */
OBS volatile uint32_t g_persist_req     = 0;   /* 写 1 → 主循环执行一次 persist_save */
OBS uint32_t          g_persist_req_cnt = 0;   /* 实际受理的请求数 (与 writes 对账) */

/* ── ★ 空闲窗口自动落盘 (T15/T26 修复, 2026-09-11) ──
 * 两段式 (登记 / 裁决分开, 这样每一段都可被外部读回):
 *   登记 (h_persist_w2): 收到**载荷为空**的 0x43 (纯查询) 且 dirty==1 → g_persist_auto=1
 *   裁决 (主循环):       引擎 RUN? → g_persist_auto_gate++ (放弃, dirty 保持)
 *                        否则     → persist_save() (g_persist_auto_runs++)
 *
 * ★ 为什么"由上位机问出来"而不是固件自己找窗口 (前三次 ①②③ 失败的根因):
 *   裸机上唯一知道"现在能安全阻塞 ~1.5s"的是上位机 —— 它问 0x43 就是在等这个
 *   结果 (S3 的 wait_flush 每 20ms 轮询一次)。固件自己按时间猜窗口 = 在别的
 *   用例正忙着收发的时刻插进 1.5s 失聪 ⇒ 实测把套件从 21/30 打到 5/30。
 *   ★ 本方案**不做任何自由重试**: 没被问就绝不落盘; 被问一次最多落一次
 *     (成功后 dirty 清零, 后续查询不再触发)。影响面 = 套件里全部 5 个 0x43 调用点。
 *
 * ★ 与"不能阻塞引擎"的关系: ENGINE_RUN==0 是**硬前提** —— 落盘只发生在引擎不跑的
 *   窗口, 所以不存在"落盘把拍卡住"这回事。另外向量表已搬进 ITCM (见 main() 开头),
 *   即使 ISR 在这 1.5s 里被触发也取得到向量 (g_vtor 提供可读回的判据)。
 * ★ 实测 (2026-09-11, tools/h723_tick_erase.py, 每 100ms 采拍计数 / 标称 1000 拍每槽):
 *   停引擎 → 请求落盘 → 全程无 Δ=0, 丢 **0** 拍;
 *   同一方法打 -DDCL_VTOR_ITCM=0 (向量表留 flash) 的对照 → 连续 8 槽 Δ=0, 丢 **8153** 拍。
 *   ⇒ "零缺口"是**对照出来的**, 不是"我记得改前是什么"。 */
OBS volatile uint32_t g_persist_auto      = 0;  /* 1 = 待裁决的自动落盘请求 (h_persist_w2 置) */
OBS uint32_t          g_persist_auto_runs = 0;  /* 自动落盘实际执行次数 */
OBS uint32_t          g_persist_auto_gate = 0;  /* 因 RUN 而放弃的次数 (T26 "RUN 中被问" 的证据) */
/* ★★★ 自动落盘 (S3 `persist_task` 语义) —— **已试三次, 全部回退, 别急着重做** (2026-09-11):
 *   需求: S3 的语义是 "deploy 只登记 dirty → 引擎停机窗口由**后台**落盘"。H723 原先只有
 *   0x43 显式落盘与 g_persist_req 两条路 ⇒ S3 套件 T15/T26 "deploy 后等 dirty 被清" 超时。
 *
 *   三次尝试与**实测结论**:
 *     ① 主循环"每 0.5s 重试" → 套件 21/30 → 5/30
 *     ② 闩锁(每 dirty 周期只试一次, RUN 不复位) → 仍 5/30
 *     ③ 闩锁 + 可观测计数 (g_persist_auto_*) + 干净基线(先 wipe) 重跑 → **仍 5/30**
 *        ⇒ 排除了"重试风暴"与"基线不干净"两个假设。
 *
 *   ★★ 最小复现 (deploy → STOP → 立刻连续问 0x01): 板子**哑约 1.55s 后恢复**, 且 dirty 已清
 *      (flags=0x00) —— 即**功能是对的, 代价是每次落盘一个 ~1.5s 的"失聪"窗口**。
 *   ★★ 机制 (结构性, 不是策略问题): **sector erase 会 stall 从 flash 取指**, 而本平台只有
 *      "热代码"(ISR/引擎) 在 ITCM (见 flash.h 前言第 5 条), **命令处理路径在 flash 里** ⇒
 *      擦除期间整机听不见; 单主循环下无第二个线程可顶上。S3 靠 FreeRTOS 后台任务 + 2s 超时
 *      把它盖住; H723 套件的节奏(几十次 deploy/STOP)会把 1.5s×N 叠成级联失败。
 *   ⇒ 要拿下 T15/T26, 得先解决**结构性**问题 (择一): (a) 把 flash 擦写改成 **非阻塞状态机**
 *     且命令服务路径进 ITCM; (b) 让 flush 只在"确认长时间无命令"的窗口发生; (c) 明确接受
 *     "落盘期间失聪"并把 PC 侧 timeout 提高 (但 S3 脚本零改动是硬条件, 所以 (c) 不可行)。 */

/* ★★ 免串口的**协议帧**触发点 (W3 新增, 与 g_persist_req 同族但更通用):
 *   为什么需要它: seq 的验收核心是"引擎按语义推进并驱动译码路由输出", 而这条
 *   链路只有**真的走一遍 `proto_dispatch`** 才算被测到 (直写路由表是无效的 ——
 *   引擎扫的是**桶表**, 而桶表由 deploy 路径里的 engine_build_buckets 产出;
 *   直写表对引擎不可见, 症状是"表看起来对, 但引擎输出的是上一个程序的值")。
 *   而 PC 侧串口此刻未接线 (W1 旁路记录) ⇒ 必须给调试器一个能触发**真实命令
 *   处理路径**的入口。
 *
 *   ★★ 设计要点 (为什么不是一个"直接调 h_seq_deploy"的特权入口):
 *     · 特权入口测的是"h_seq_deploy 这个函数", 测不到"命令怎么被路由进来、
 *       ACK/NAK 怎么回去"。而 PC 侧真正依赖的是**后两者**。
 *     · 这里让 pyocd 把**完整的协议帧载荷** (cmd + payload) 写进 SHM 尾部暂存区,
 *       主循环把它当成"刚从串口收到的一帧"交给 proto_dispatch ⇒ 命令分发、
 *       校验器、ACK/NAK、观测面计数**全部被真实走到**。
 *     · 与 g_persist_req 同族: volatile 且被主循环真读, 不会被 gc-sections 回收。
 *
 *   ★ 暂存区位置 (W4 起): SHM 的 **OFF_CMD_REQ (0x4DE0)**, 紧随通信域之后。
 *     该常量与尺寸 DEPLOY_REQ_MAX 都定义在 engine.h (布局断言需要它们),
 *     不在本文件里另立一份 —— "同一个尺寸两处定义"是本项目反复踩过的坑。
 *     ★ W3 时它落在 0x4B20, 而 W4 的 MB_CTRL 正好要用那一段 ⇒ 已让位。
 */
OBS volatile uint32_t g_cmd_req      = 0;   /* 写 1 → 主循环执行一次暂存帧 */
OBS volatile uint32_t g_cmd_req_len  = 0;   /* 暂存帧的字节数 (由工具写) */
OBS uint32_t          g_cmd_req_cnt  = 0;   /* 实际受理的请求数 */
OBS uint32_t          g_cmd_req_last = 0;   /* 最近一次被处理的命令码 (核对用) */

/* ══════════ W3: Sequencer 的观测面 ══════════
 * ★ 判据要点: "步号动过"不能只看 wire 的终值 —— 终值可能是别的写者写的。
 *   必须有 ① 本拍真的推进了几次 ② 累计推进次数 ③ 0x44 受理/拒绝计数。
 *   这样"顺序域在跑"才是可被外部核对的事实, 而不是"我看到 wire 变了"。 */
OBS uint32_t g_seq_deploys    = 0;   /* 0x44 成功受理次数 */
OBS uint32_t g_seq_nak        = 0;   /* 0x44 被拒次数 */
OBS uint32_t g_seq_steps_sum  = 0;   /* 累计成功推进的步数 (每次 +1) */
OBS uint32_t g_seq_writes     = 0;   /* 步号镜像写入次数 (受 force 屏蔽影响) */
OBS uint32_t g_seq_last_cur   = 0;   /* 最近一次推进后的步号 (0-based, 供核对) */
OBS uint32_t g_seq_ticks      = 0;   /* seq 段被调用的拍数 */
OBS uint32_t g_seq_wrote_last = 0;   /* 最近一拍写入次数 (engine_seq_tick 返回值) */
OBS uint32_t g_seq_max_cur    = 0;   /* 历史最大步号 (证明真的走到过末步) */
/* ★ W3: 累计被 arm 的顺序实例数 (每次 START 对每个实例 +1)。
 *   用途: ① 证明 START 真的走到过 seq arm 段 (而不是"run 位是别处置的");
 *        ② 与 g_start_ok 对账: arm 数应 == start_ok × n_seq, 不等就说明
 *           START 中途改了 N_SEQ (或 arm 段被跳过)。
 *   ★ 语义修正如实记: 第一版它被当成"是否已 arm 过"的布尔 (用来区分 START 的
 *     两个分支)。后来对照 S3 发现 S3 的 START 是**无条件**重置全部实例
 *     (run=1, step_cur=0, step_tick=0, 镜像=1.0) —— 那才是被移植的语义。
 *     于是删掉了自创的"已 RUN 不清零"分支 (它会让 PC 补发的 0x11 把正在执行的
 *     顺序程序打回第一步, 与 S3 语义不符), 这个量随之改成纯计数器。 */
OBS uint32_t g_seq_armed      = 0;
/* ══════════ W4: 通信域观测面 ══════════
 * ★ 判据要点: "注入了"与"响应对了"是两件事 —— 必须分开计数, 否则
 *   "全部被拒"与"全部受理"在看总数时无法区分 (同 0x44 观测面的设计理由)。 */
OBS uint32_t g_mb_inject_ok   = 0;   /* 0x60 成功受理次数 */
OBS uint32_t g_mb_nak         = 0;   /* 0x60/0x62 被拒次数 */
OBS uint32_t g_mb_ticks       = 0;   /* mb_tick 被调用拍数 (证明状态机在推进) */

/* ── W5: macro VM 观测面 (拆开计数: "被拒"与"执行成功"必须可区分) ── */
OBS uint32_t g_macro_exec_ok   = 0;  /* 0x40 一次性执行成功次数 */
OBS uint32_t g_macro_upload_ok = 0;  /* 0x41 上传成功次数 */
OBS uint32_t g_macro_nak       = 0;  /* 0x40/0x41/0x42 被拒次数 */
OBS uint32_t g_macro_ticks     = 0;  /* macro_tick 实际执行的轮次 (证明循环在跑) */

/* ── W5: 外设域观测面 ── */
OBS uint32_t g_w5_ready        = 0;  /* 外设域初始化完成 (adc/ai/di/hil 都 init 过) */
OBS uint32_t g_pin_selftest_n  = 0;  /* 0x36 零接线自检被调用次数 */

/* ── 引擎扫描: 统计 (CPU 周期) ── */
OBS uint32_t g_eng_cyc_last = 0;
OBS uint32_t g_eng_cyc_min  = 0xFFFFFFFFu;
OBS uint32_t g_eng_cyc_max  = 0;
OBS uint64_t g_eng_cyc_sum  = 0;
OBS uint32_t g_eng_n        = 0;
OBS uint32_t g_eng_div0     = 0;

/* ── 整个 ISR: 统计 ── */
OBS uint32_t g_isr_cyc_last = 0;
OBS uint32_t g_isr_cyc_min  = 0xFFFFFFFFu;
OBS uint32_t g_isr_cyc_max  = 0;
OBS uint64_t g_isr_cyc_sum  = 0;
OBS uint32_t g_isr_n        = 0;
OBS uint32_t g_isr_overrun  = 0;   /* 本 RUN 段内 ISR 超预算(EXEC_BUDGET_CYCLES)的次数。
                                    * ★ 审查二级 #5: 它此前"不存在"——0x38 里直接填 0。
                                    *   权威值在 DTCM, 主循环镜像到 SHM 0x3850 (与范本同址)。 */

/* ── 拍周期 (相邻 ISR 入口 CYCCNT 差) ── */
OBS uint32_t g_per_cyc_last = 0;
OBS uint32_t g_per_cyc_min  = 0xFFFFFFFFu;
OBS uint32_t g_per_cyc_max  = 0;
OBS uint32_t g_per_prev     = 0;
OBS uint32_t g_per_glitch_n = 0;   /* 时钟不连续 (CYCCNT 回绕/被清零) 导致的无效样本数 */

/* ── 阶段 3.2: deploy / 热重载 观测量 ──
 * ★ 必须声明在 ISR **之前** (ISR 里要用) —— 这几个量构成 deploy 的可失败判据:
 *   受理成功与被拒**分别**计数 (只数成功会让"全部被拒"看起来像"没部署过");
 *   applied_seq 与 reload_lat 证明"真的生效了", 而不是只"受理了"。 */
OBS uint32_t g_deploy_ok       = 0;   /* 受理成功的 deploy 次数 */
OBS uint32_t g_deploy_nak      = 0;   /* 被拒次数 (校验 / 预算门拦下的) */
OBS uint32_t g_deploy_routes   = 0;   /* 最近一次实际写入 ACTIVE 的路由数 */
OBS uint32_t g_deploy_budget   = 0;   /* 最近一次预算 (cyc/拍, 对照实测占拍) */
OBS uint32_t g_deploy_seq      = 0;   /* 固件侧受理序号 (每次成功 deploy +1) */
OBS uint32_t g_applied_seq     = 0;   /* ★ ISR 已切换生效的序号 (== deploy_seq 即已生效) */
OBS uint32_t g_reload_count    = 0;   /* ISR 实际执行 ACTIVE 切换的次数 */
OBS uint32_t g_reload_cyc      = 0;   /* 最近一次热重载本身的耗时 (DWT, cyc) */
OBS uint32_t g_reload_lat      = 0;   /* 从置 RELOAD 到生效完成跨了几拍 */
OBS uint32_t g_deploy_set_tick = 0;   /* 置 RELOAD 时的拍号 (供 ISR 算延迟) */

/* ── DWT 标定 ── */
OBS uint32_t g_dwt_overhead = 0;
OBS uint32_t g_cal_n1000    = 0;
OBS uint32_t g_pa9_div      = 0;

/* ── L1 I-cache 状态 (H7 复位默认 **关闭**; 不开它 flash 版是被"冤枉"的) ──
 * 这个开关是为了把实验做完整: FLASH 版在 I-cache 关 / 开 两种状态下的成本,
 * 对照 ITCM 版 (ITCM 不经 cache, 应该完全不受影响)。 */
OBS uint32_t g_icache_req = 0;   /* 写 1 → 主循环使能 I-cache (单向, 不可逆) */
OBS uint32_t g_icache_on  = 0;   /* 实际状态 */
OBS uint32_t g_ccr_before = 0;
OBS uint32_t g_ccr_after  = 0;

/* ══════════ GPIO ══════════ */
static void pin_out_init(uint32_t port, uint32_t bit)
{
    if (port == 0u) RCC_AHB4ENR |= (1u << 0);

    uint32_t mod = GPIO_MODER(port);
    mod &= ~(3u << (bit * 2u));
    mod |=  (1u << (bit * 2u));
    GPIO_MODER(port) = mod;

    GPIO_OTYPER(port)  &= ~(1u << bit);
    GPIO_PUPDR(port)   &= ~(3u << (bit * 2u));

    uint32_t spd = GPIO_OSPEEDR(port);
    spd &= ~(3u << (bit * 2u));
    spd |=  (3u << (bit * 2u));
    GPIO_OSPEEDR(port) = spd;
}

static inline void pin_set(uint32_t port, uint32_t bit, int hi)
{
    GPIO_BSRR(port) = hi ? (1u << bit) : (1u << (bit + 16u));
}

/* ══════════ DWT 标定 (阶段 1 沿用) ══════════ */
__attribute__((noinline))
static uint32_t calib_nop(volatile uint32_t n)
{
    uint32_t t0 = DWT_CYCCNT;
    for (volatile uint32_t i = 0; i < n; i++) { __asm__ volatile("nop"); }
    return DWT_CYCCNT - t0;
}

static void calibrate(void)
{
    uint32_t a = DWT_CYCCNT, b = DWT_CYCCNT;
    g_dwt_overhead = b - a;
    g_cal_n1000 = calib_nop(1000u);
}

/* ══════════ TIM2: 精确 100μs 拍 (TIMxCLK 200MHz, ARR 20000-1) ══════════ */
static void tick_timer_init(void)
{
    RCC_APB1LENR |= (1u << 0);                 /* TIM2EN */

    TIM_CR1(TIM2_BASE) = 0;
    TIM_PSC(TIM2_BASE) = 0;
    TIM_ARR(TIM2_BASE) = CLK_TICK_TIMCNT - 1u;
    TIM_EGR(TIM2_BASE) = TIM_EGR_UG;
    TIM_SR(TIM2_BASE)  = 0;

    NVIC_IP(IRQ_TIM2)  = 0;                    /* 最高抢占优先级 */
    nvic_enable_irq(IRQ_TIM2);                 /* ★ 一律走宏: 裸移位在 IRQ≥32 时静默失效 */

    TIM_DIER(TIM2_BASE) = TIM_DIER_UIE;
    TIM_CR1(TIM2_BASE)  = TIM_CR1_CEN;
}

/* ══════════ L1 I-cache 使能 (实验用; 生产固件不用 —— 见下方说明) ══════════
 * ★ 为什么不默认开: cache 是**确定性**的敌人 —— 命中/未命中取决于程序历史,
 *   同一个循环在冷/热两种状态下执行时间不同。本项目的做法是"热代码进 ITCM,
 *   大缓冲留在 non-cacheable 的地址域", 而不是靠 cache 撞运气。
 *   这里的开关只为了把 A/B 实验做完整 (证伪"ITCM 只是碰巧比没开 cache 快")。
 * 序列按 ARM ARM: ICIALLU → DSB → ISB → 置 CCR.IC → DSB → ISB */
static void scb_enable_icache(void)
{
    g_ccr_before = SCB_CCR;
    SCB_ICIALLU = 0;
    __asm__ volatile("dsb; isb" ::: "memory");
    SCB_CCR |= SCB_CCR_IC;
    __asm__ volatile("dsb; isb" ::: "memory");
    SCB_ICIALLU = 0;
    __asm__ volatile("dsb; isb" ::: "memory");
    g_ccr_after = SCB_CCR;
}

/* ══════════ 清统计 ══════════ */
static inline void stats_reset(void)
{
    g_eng_cyc_last  = 0;
    g_eng_cyc_first = 0;      /* ★ 0 = "还没有样本", 与"样本恰好为 0"区分开 */
    g_eng_cyc_min  = 0xFFFFFFFFu;
    g_eng_cyc_max  = 0;
    g_eng_cyc_sum  = 0;
    g_eng_n        = 0;
    g_eng_div0     = 0;

    g_isr_cyc_last  = 0;
    g_isr_cyc_first = 0;
    g_isr_cyc_min  = 0xFFFFFFFFu;
    g_isr_cyc_max  = 0;
    g_isr_cyc_sum  = 0;
    g_isr_n        = 0;
    g_isr_overrun  = 0;   /* ★ 与范本同语义: 超预算计数只反映**本次 RUN 段**
                           *   (S3 在 core0_engine_start 里清 OVERRUN, 这边在 stats_reset 清) */

    g_eng_routes_last  = 0;
    g_eng_routes_total = 0;
    g_eng_ticks        = 0;

    /* 拍周期也复位 (★ 保留 g_per_prev —— 它保证"本轮内"复位后第一个样本仍然有效;
     *  ★ 但**跨轮**(冷启动) 处调用方必须自己把 g_per_prev 也清 0, 因为 dwt_enable
     *    刚把 CYCCNT 清零 —— 见 main() 里 "计时统计的冷启动初始化" 那段实测记录) */
    g_per_cyc_last = 0;
    g_per_cyc_min  = 0xFFFFFFFFu;
    g_per_cyc_max  = 0;
}

/* ══════════ 拍中断 ══════════ */

#if IO_IN_ISR
/* ★★★ P1/P2 (2026-09-12): 拍内 I/O 必须走**间接调用 (BLX)**, 不能直接 BL。
 *
 * 事故与证据链 (别把它简化成"加个函数指针就好了"):
 *   ISR 在 **ITCM (0x0)**, 而 di_poll/adc_poll/hil_out_poll 在 **FLASH (0x0800xxxx)** ——
 *   两者相距 **128MB**, 远超 Thumb `BL` 的 ±16MB 编码范围。
 *   · 链接器确实**插了 veneer** (`__di_poll_veneer@0x17F8` 等 4 个, 见 .map),
 *     且**板上逐字节核对过**: 0x17FC=0x0800534D / 0x1804=0x08004F45 / 0x180C=0x080051B1,
 *     与 elf 完全一致 ⇒ veneer 本身没被漏拷、没被 gc 掉。
 *   · 但整机**卡死在 `Default_Handler`** (PC=0x080057C4), 三条独立判据确认
 *     **TIM2_IRQHandler 从未被执行**: ① 断点 0x0 未命中(核心仍 RUNNING)
 *     ② `g_stage` 从未被写成 7 (ISR 的第一件事) ③ `g_tick_count` 恒 0。
 *     (对照构建 `-DDCL_IO_IN_ISR=0` 一切正常 ⇒ 变量就是这三个调用。)
 *   · veneer 机制**为何失效尚未查明**(已列为待查项), 但**规律是清楚的**:
 *     本工程的既有代码里, ISR 内的**跨区**调用**一律走函数指针**
 *     —— 见 `engine_tick(g_shm, tick, sel ? engine_scan_itcm : engine_scan_flash, ...)`,
 *     那条路生成的是 `BLX` (寄存器间接, **无距离限制**), 所以从来没事;
 *     而 ISR 里的**同区**(ITCM→ITCM)调用 `mb_tick` / `engine_*` 才用直接 BL。
 *   ⇒ 本次照**同一条纪律**走: 用函数指针 ⇒ 生成 BLX, 不从 veneer 走。
 *   ★ 教训 (值得写进 ARCH): **在 ITCM/flash 分离的工程里, "把一个函数从 ITCM 挪到
 *     flash"不是零风险重构** —— 它会把该函数的所有调用点从"同区 BL"变成"跨区调用",
 *     而链接器**不会报错**。 */
/* ★ 注: 这里**不能**用"函数指针数组"来绕开跨区调用 —— 试过了, 会被 `-O2` 的
 *   **常量传播**打回原形: 数组是 `static const`, GCC 直接把它折成直接 BL。
 *   现象极具误导性 —— 源码明明改了、编译零警告, 但 `pyocd flash` 报
 *   `programmed 0 bytes, identical N bytes` (产物一模一样), 板子行为纹丝不动。
 *   ⇒ 改用**声明上的 `long_call` 属性** (见 di.h / adc.h / hil.h): 它强制生成
 *     BLX (寄存器间接), 不受常量传播影响。所以 ISR 里仍是普通的直接调用写法。 */
#endif

ISR_PLACE void TIM2_IRQHandler(void)
{
    uint32_t t0 = DWT_CYCCNT;

    if (TIM_SR(TIM2_BASE) & TIM_SR_UIF) {
        TIM_SR(TIM2_BASE) = ~TIM_SR_UIF;
        g_stage = 7;

        if (g_stat_reset) {            /* 切配置用 (不清拍周期统计, 保连续性) */
            g_stat_reset = 0;
            stats_reset();
            /* ★★★ 必须同时清"上一拍基准" g_per_prev —— 否则会折进一个**假样本**。
             *   机制 (2026-09-11 容量实测时抓到, pmin 报 5999 cyc = 15μs):
             *     `DWT_CYCCNT` 只数**核心周期**, 调试暂停期间它是**冻结**的。
             *     所以"暂停 → 复位统计 → 恢复"这条路径上, 恢复后的第一个 tick 算出的
             *     p = t0 − g_per_prev(暂停前的值) = **一次暂停的残余时间**, 是个任意的正小数
             *     (实测 5999), 被 `stats_reset()` 之后的第一拍折进 min ⇒ **pmin 恒被污染**。
             *   ★ 为什么 `stats_reset()` 本身保留 g_per_prev 是对的、这里却必须清:
             *     它保留是为了"轮内 0x13 RESET 后第一个样本仍有效" (那时钟没断, 相减有意义);
             *     而**调试暂停把钟断了** —— 断过的两个读数相减没有物理意义 (与早上修的
             *     "跨纪元样本"是同一条: **钟一旦不连续, 基准就必须作废**)。
             *   ⇒ 清掉 = 丢弃一个样本 (≤100μs), 换来 pmin/pmax **不再撒谎**。
             *     这是"判据必须能失败"的前置: 一个会被自己的测量方式污染的观测量,
             *     会让任何抖动判据变成假象。 */
            g_per_prev = 0;
        }

        if (g_tick_count & 1u) pin_set(TICK_PORT, TICK_BIT, 1);
        else                   pin_set(TICK_PORT, TICK_BIT, 0);
        g_tick_count++;

        if (g_pa9_enable && ++g_pa9_div >= 32u) {
            g_pa9_div = 0;
            pin_set(UARTT_PORT, UARTT_BIT,
                    (GPIO_ODR(UARTT_PORT) & (1u << UARTT_BIT)) ? 0 : 1);
        }

        /* ---- 热重载 (阶段 3.2): STAGING → ACTIVE ----
         * ★ 位置在扫描**之前**: 本拍就用新表跑完, 不出现"已受理但这一拍还在用旧表"
         *   的中间态。切换过程对 ISR 是原子的 (单字节 RELOAD 标志进入这里)。
         * ★ 代价: 这一拍 ISR 会长出 memcpy 的成本 (实测见 g_reload_cyc)。
         *   只要总时长仍 < 拍长, 拍周期就完全不受影响 —— 只有 isr_cyc_max 会记下
         *   这个尖峰。这是"部署瞬间有一拍变长"的**已知且有界**代价, 不是抖动。 */
        if (SHM_U8(g_shm, OFF_CTRL_RELOAD)) {
            uint32_t tr0 = DWT_CYCCNT;
            engine_reload_active(g_shm);
            g_reload_cyc = DWT_CYCCNT - tr0;
            g_n_routes   = SHM_U16(g_shm, OFF_CTRL_N_ROUTES);   /* 扫描条数随新表走 */
            /* ★ 部署的程序**必须走分档调度**: 全表扫会把 div1/div2 路由也每拍跑一遍
             *   —— 等于静默忽略档位语义 (程序"能跑"但时序全错)。所以这里强制置 1,
             *   并让它留在 g_scan_mode 这个可观测量里, 而不是藏在暗处。 */
            g_scan_mode  = 1;
            g_applied_seq = SHM_U16(g_shm, OFF_CTRL_APPLIED_SEQ);
            g_reload_lat  = g_tick_count - g_deploy_set_tick;
            SHM_U16(g_shm, OFF_CTRL_APPLIED_LAT) = (uint16_t)g_reload_lat;
            SHM_U8(g_shm, OFF_CTRL_RELOAD) = 0;
            __asm__ volatile("dsb" ::: "memory");   /* ARM: dsb (S3 的 Xtensa `memw` 在 ARM 上不存在) */
            g_reload_count++;
        }

        /* ---- 引擎扫描 (ta..tb 只包住扫描体本身) ----
         * ★ W1 (0x11/0x12): 门的条件是 **两个** ——
         *     g_engine_gate  = bench 运行期旋钮 (pyocd 用, 阶段 2 的 A/B 实验入口)
         *     ENGINE_RUN     = 协议运行控制 (PC 用, 0x11 置 / 0x12 清)
         *   为什么保留两个: g_engine_gate 属于"实验仪器", ENGINE_RUN 属于"产品语义"。
         *   把它们合成一个会让 bench 组 (gate=1, RUN=0) 无法复现阶段 2 的基线数字。
         *   ★ 但 ISR **不能**用 gate 单独开跑 —— 那意味着"上电即跑引擎"而 PC 无法停,
         *   所以真实运行条件是 (gate && RUN)。这同时让 0x12 STOP 成为**可失败判据**:
         *   STOP 后 routes_total 必须停止增长 (见 tools/h723_runctrl.py)。 */
        /* ★★★ #2 修复 (2026-09-11 迁移保真度审查): HEARTBEAT 的语义必须是
         *   "**每拍无条件**" = CPU + 定时器存活, 与范本 OA14 **一字不差**:
         *     范本 `core0_isr.c:355` 把 `HEARTBEAT += 1` 写在 `if (!run) return`
         *     **之前**, 那条注释原文是:
         *       "OA14 (P2, 审计): 心跳必须在 run 门外无条件翻 — 停机也翻
         *        (外部可观测 CPU+定时器存活)。上一版把 return 放心跳前 → STOP 心跳停,
         *        与注释相反, 且 LA 的 STOP/RUN 抖动对照场景无法复现。"
         *   ★ 而 H723 原来把它放在 `gate && RUN` 门**内** ⇒ 与**同一张表里 0x18**
         *     的 SAMPLES 语义**完全对调**。同址反义是最危险的一类静默读错:
         *     按"心跳看 CPU 存活"写的上位机会把"引擎已停机"读成"CPU 死了";
         *     按"samples 看本次运行段"写的会把"停机后的空拍"算进统计段。
         *   ★ 为什么放这里: 在 `g_tick_count++` 之后、**任何门之前** ——
         *     只要这一拍进来了就翻一次, 与引擎跑不跑无关。 */
        SHM_U32(g_shm, OFF_CTRL_HEARTBEAT)++;

        /* ══════════ ★ P1/P2: 拍内**输入段** (2026-09-12) ══════════
         * ★ 位置三条理由:
         *   ① 必须在扫描门**之前** —— 扫描要读 SENSOR, 而 DI/AI 都写那里;
         *   ② **不受 gate/RUN 门控** —— 停机时现场输入仍必须可读 (HMI 要看现场值),
         *      与 mb_tick 放门外同类理由: **输入面不门控, 输出面才门控**;
         *   ③ 在 HEARTBEAT **之后** —— 最便宜的存活信号先落袋: 即使输入段出问题,
         *      心跳也已经翻过了 (可观测性的排序原则)。
         * ★ A/B 对照 (IO_IN_ISR=0): 本段整段不编译, 输入回主循环的 ai_tick/di_tick
         *   (改前行为)。两档共用同一套采样体 ⇒ 对照里唯一的变量是**驱动位置**。 */
#if IO_IN_ISR
        /* 这两个函数声明带 `long_call` ⇒ 编译器生成 BLX, 不走 veneer (见上方长注释) */
        di_poll(g_shm, g_tick_count);    /* DI: 每 100 拍相位锚定采样 + 去抖 → SENSOR[3..6] */
        adc_poll_reclaim(g_shm);         /* ★ P3-C: 只回收 (上拍尾启动的转换已完成 ⇒ 孔径
                                          *   与输出沿隔了 ~97µs); 启动挪到 ISR 末尾 */
#endif

        g_engine_run_seen = SHM_U8(g_shm, OFF_CTRL_ENGINE_RUN);
        if (g_engine_gate && g_engine_run_seen) {
            uint32_t ta = DWT_CYCCNT;
            uint32_t sel = g_engine_sel;
            uint32_t ck;
            uint32_t nrun = 0;

            /* ★★ W2.2 拍首 Force 覆写 —— 必须在**两条扫描路径之外**统一调用。
             *   第一版把它写在 engine_tick() 内, 于是 BOOT_SCAN_MODE=0 的板子上
             *   (走全表扫分支) force 完全不生效, 而 SHM 里 fmask/fval 全是对的值 —
             *   症状是"配置全对、效果为零", 与 A1 事故同族。
             *   ⇒ 凡"每拍都必须发生"的动作, 不能挂在某条分支里。 */
            engine_force_apply(g_shm);

            if (g_scan_mode) {
                /* 阶段 3: 分档调度 —— 本拍只跑 [div0]+[div1 本拍桶]+[div2 本拍桶] */
                ck = engine_tick(g_shm, g_tick_count, sel ? engine_scan_itcm
                                                          : engine_scan_flash, &nrun);
            } else {
                /* 阶段 2 基线: 全表扫 (n = g_n_routes) */
                uint32_t n = g_n_routes;
                if (n > MAX_ROUTES) n = MAX_ROUTES;
                ck = sel ? engine_scan_itcm(g_shm, 0, n)
                         : engine_scan_flash(g_shm, 0, n);
                nrun = n;
            }
            uint32_t tb = DWT_CYCCNT;

            g_eng_sel_used = sel;
            g_eng_n_used   = g_n_routes;
            g_eng_ck       = ck;
            g_eng_routes_last = nrun;
            g_eng_routes_total += nrun;
            g_eng_ticks++;

            uint32_t d = tb - ta;
            g_eng_cyc_last = d;
            if (!g_eng_cyc_first) g_eng_cyc_first = d;     /* ★ 首样本留痕 (H5) */
            if (d < g_eng_cyc_min) g_eng_cyc_min = d;
            if (d > g_eng_cyc_max) g_eng_cyc_max = d;
            g_eng_cyc_sum += d;
            g_eng_n++;
            if (d == 0u) g_eng_div0++;      /* 防御: "零成本"一定是测量坏了 */
        }

        /* ══════════ W3: 顺序域扫描段 (Sequencer) ══════════
         * ★★ 位置三条理由 (顺序不能动):
         *   ① 在**路由扫描之后**: seq 条件读的 wire/SENSOR 是本拍最新值, 且
         *      "步号 → CMP 译码路由 → 输出"能在**同一拍内**闭合 (输出不晚一拍)。
         *   ② 在**计时统计之前**: seq 成本天然计入 isr_cyc (无需新开一个统计面),
         *      于是"seq 把拍撑爆"这件事会被既有的 gate 当场抓住。
         *   ③ 在 **if (gate && RUN) 门**内: 与路由同门 —— STOP 后 seq 也停,
         *      步号冻结保持 (不是清零)。"停机时状态可见"是 PLC 的基本要求。
         *
         * ★★ 必须在**路由门之外**再判 n_routes: 纯顺序程序 (n_routes=0, 只有
         *   步进器 + 译码路由) 是合法且常见的一类机器控制程序 —— 上面那个 if
         *   一旦因为 n_routes=0 而跳过整段, 这类程序会静默不推进。
         *   本实现把 seq 放在同一个 if 里但**不依赖 n_routes**, 所以两条路都通。 */
        if (g_engine_gate && g_engine_run_seen) {
            /* ★★ #2 修复: HEARTBEAT 的递增**已移到上面门之外** (语义 = 每拍无条件)。
             *   这里原来放的就是它 —— 那个位置正好把语义写反了, 见上面的长注释。
             *   ★ 换过来之后"引擎停了没"依然可观测, 只是两个量对调了 (S3 契约):
             *       HEARTBEAT↑ samples↑   → 引擎在跑
             *       HEARTBEAT↑ samples=停 → ISR 在跑但引擎已 STOP (正常停机态)
             *       HEARTBEAT=停          → ISR 都没了 (固件死了/未启动) */
            g_seq_ticks++;
            uint32_t sw = engine_seq_tick(g_shm, g_tick_count);
            g_seq_wrote_last = sw;
            if (sw) {
                g_seq_writes += sw;
                /* 累计推进步数 + 记录步号: 步号只在"推进"时变, 所以这里对每个
                 * 实例读一遍 step_cur 取最大 (实例数 ≤ 8, 成本可忽略)。 */
                uint32_t nq = *(const volatile uint8_t *)(g_shm + OFF_CTRL_N_SEQ);
                if (nq > MAX_SEQ_INST) nq = MAX_SEQ_INST;
                for (uint32_t k = 0; k < nq; k++) {
                    uint32_t c = *(const volatile uint16_t *)
                        (g_shm + OFF_SEQ_CTRL + k * 16u + 4u);   /* SeqCtrl_t.step_cur */
                    if (c > g_seq_max_cur) g_seq_max_cur = c;
                    g_seq_last_cur = c;
                }
                g_seq_steps_sum += sw;
            }
        }

        /* ══════════ W4: 通信域 Modbus (ISR 每拍推进) ══════════
         * ★★★ 必须放在 **run 门之外** —— 这是 S3 的事故修复 (OA17), 必须照搬:
         *   原实现把它放在 `if (run)` 门内, 后果是 **STOP 态注入一帧 → 状态机
         *   卡在 RX 无法推进 → 通信域永久 busy** (后续 0x60 全部 NAK, 只能靠
         *   START 或断电恢复) —— 一条从"停机"通往"死锁"的路径。
         *   移到门外之后: STOP 态也能收帧/响应。"停机时 HMI 仍可通信"(读状态/
         *   写配方) 是 PLC 的标准能力, 不是附加功能。
         * ★ 确定性影响: STOP 态本就不做计时统计; RUN 态它仍在 t1 之前,
         *   所以 mb_tick 的开销**照常计入 isr_cyc_max** —— 对外可见, 不隐藏。
         *   (本项目铁律: 热路径成本必须可观测, 不能靠"放在计时之外"来装便宜。) */
        mb_tick(g_shm);
        g_mb_ticks++;

#if IO_IN_ISR
        /* ★★★ TEMP-DIAG-F (2026-09-12): **等长替代** —— 用 `di_poll` 的调用**顶替**
         *   `hil_out_poll` 的调用。两者在调用点的机器码体积几乎相同 (都是
         *   `blx Rn` + 一个常量池项), 而 `di_poll` 自带相位门
         *   (`tick % 100 == 0`) ⇒ 绝大多数拍**立即返回** ⇒ 基本无副作用。
         *
         *   这是本轮最能**定方向**的一个实验, 判据二选一、互斥:
         *     · 这样**也卡**  ⇒ 与 `hil_out_poll` 这个符号/它的目标地址**无关**,
         *                      根因是"ISR 里多出一处跨区调用"这件事**本身** ——
         *                      即 ISR 的**体积/布局**, 正好对上项目已知的家族问题
         *                      (ARCH-H723.md §0.3 "本平台对代码体积敏感";
         *                       T26 期间也记过"加 200 字节就改变上电行为")。
         *     · 这样**不卡**  ⇒ 与 `hil_out_poll` 这个符号或它指向的代码有关。 */
        /* ══════════ ★ P1: 拍内**输出段** (2026-09-12) ══════════
         * ★ 位置: 扫描与顺序域**之后** —— 输出臂读的是 WIRE[HIL_U_WIRE],
         *   必须用**本拍刚算出来**的值。放扫描前会输出上一拍的结果, 白白多一拍延迟。
         * ★ 安全态语义不变: hil_out_poll → hil_out_apply 内部仍受 ENGINE_RUN 门控,
         *   STOP 后仍归零 (HIL_SAFE=1)。门控放在**域内部**而不是在 ISR 外面再包一层,
         *   这样"哪些输出受门控"由各域自己声明, ISR 里不必硬编码一张清单 ——
         *   否则将来每加一个输出面都得回来改 ISR, 正是"改一处忘一处"的老路。
         *
         * ★★ 它曾经"一加进 ISR 就整机卡死", 查了十几轮才定位到**真因不在它**:
         *   卡死的根因是**向量表的落位** —— `_vtor_itcm = 0x1880` (128 对齐但不是
         *   256 对齐) 时整机卡进 Default_Handler。完整证据链 (全部实测):
         *     · 把本调用换成**等长**的 `di_poll` 调用        → 仍卡
         *     · 换成 **4 条 nop** (8 字节, 完全无调用)         → 仍卡
         *     · 把 ISR 体积一次推过阈值 (+200B nop, 向量表随之落到 0x1900) → **正常**
         *     · 重现那 8 字节体积, 但把向量表改成 **256 对齐** (落到 0x1900) → **正常**
         *   ⇒ 与"调用谁 / 函数在哪 / 函数体做什么"全都无关, 只与**表落哪**有关。
         *   修法: ld/STM32H723ZG_FLASH.ld 的 `.itcm_vectors` 对齐 128 → **256**。
         *   ★ 这条值得记住: **ARMv7-M 只要求 VTOR 128 对齐**, 所以 256 是**实测**出来的
         *     经验值, 不是 spec 要求 —— 换板子/换型号要重新验。 */
        hil_out_poll(g_shm, g_tick_count);
        do_poll(g_shm, g_tick_count);        /* ★ P3-A: DO 输出面 ACTUATOR → GPIOE (BSRR 原子写) */
#endif

        uint32_t t1 = DWT_CYCCNT;
        uint32_t di = t1 - t0;
        g_isr_cyc_last = di;
        if (!g_isr_cyc_first) g_isr_cyc_first = di;    /* ★ 首样本留痕 (H5) */
        /* ★ 同族保护 (见下方 g_per_* 处的长注释): `di == 0` 意味着"整段 ISR 期间
         *   CYCCNT 一个数都没走" —— 对真实 ISR 是不可能的, 只可能是**时钟不连续**
         *   (CYCCNT 在本次 ISR 中间被清零/跨纪元)。并进 min 会让 emin 恒为 0
         *   (实测就是这样), 即"最小 ISR 时长"这个量在撒谎。⇒ 单独计数。 */
        if (di == 0u) {
            g_per_glitch_n++;
        } else {
            if (di < g_isr_cyc_min) g_isr_cyc_min = di;
            if (di > g_isr_cyc_max) g_isr_cyc_max = di;
            g_isr_cyc_sum += di;
        }
        /* ★★ 审查二级 #5: 超预算计数 (原实现是"在 0x38 里直接填 0"冒充"没超预算")。
         *   为什么必须有它: S3 套件 T9 的判据含 `ov == 0` —— 而"恒 0"的字段让它
         *   **不可能失败** ⇒ 那半条是**空判据**。一个永远为 0 的观测量看起来像
         *   "这个分支很干净", 实际是"从没被走到"(本项目对空判据的既有教训)。
         *   ★ 判据与常量: EXEC_BUDGET_CYCLES=32000 (拍长 40000 的 80%)。
         *     它与 EXEC_DEPLOY_BUDGET(26000, 下载期静态门) 是**两个语义不同的量**,
         *     刻意分开命名 —— 见 engine.h 里那段"一常量两用"的说明。
         *   ★ 成本: 一次比较 + 极少发生的自增 ⇒ 热路径可忽略。 */
        if (di > EXEC_BUDGET_CYCLES) g_isr_overrun++;
        /* ★★ #2 修复: samples (g_isr_n) = **仅 RUN 拍** (范本语义)。
         *   范本 `core0_isr.c:358` 的 `SAMPLES += 1` 写在 `if (!run) return` **之后**
         *   —— 即 OA13 "统计只反映本次 RUN 段"; 而且 0x38 的 samples 正是取自它
         *   (`esp32-core0/main/main.c:242`), 所以两侧必须同口径。
         *   ⇒ 原来这里**每拍都加** = 与 0x08 的语义完全对调 (见上方的长注释)。
         *   ★ 计数条件与扫描门用**同一个** (gate && RUN) ⇒ samples 的增量恰好等于
         *     "引擎真的推进过的拍数", 而不是"中断进来过几次"。
         *   ★ 副作用(正面): STOP 后 samples 冻结 —— 这正是"0x12 STOP 可失败判据"的一半。 */
        if (g_engine_gate && g_engine_run_seen) g_isr_n++;
        /* ★★ P3-C: 拍尾启动新采样 —— 采样孔径从拍尾开始, 距本拍头的输出沿
         *   已隔 ~97µs (建立时间), 采样开关动作不再与输出沿同瞬 (自导自演消除)。
         *   成本计入 isr 统计 (在 t1 之前)。 */
        adc_poll_kick();
        bb_kick(g_tick_count);       /* ★ 黑匣子: 拍尾快照 → AXI 环形缓冲 (MDMA 后台搬运) */

        if (g_per_prev) {
            uint32_t p = t0 - g_per_prev;
            /* ★★★ 时钟不连续保护 (2026-09-11 实测缺陷修复):
             *   这批量住在 **DTCM, 跨复位不丢**, 而 `dwt_enable()` 会把 **CYCCNT 清零**
             *   ⇒ 新"纪元"的第一个样本 = `t0(≈0) - g_per_prev(上一纪元的值)` = **负增量**
             *   (无符号看是个十亿级的巨值)。原实现直接并进 max ⇒ **统计被永久污染**。
             *   实测指纹 (冷启动后直接读 0x38, 没发任何命令):
             *     pmin = 39992 (正常)  而  pmax = 0xFD68B1BF (= -43550622, 垃圾)
             *   —— "pmin 正常而 pmax 垃圾"这个组合就是它。
             *   ★ 判据: **负增量 = 时钟不连续, 不是"拍变长了"**。合法的拍变长一定是
             *     正数 (超载也只在几十万 cyc 量级, 见 budget 表)。所以负的单独计数,
             *     不并入 min/max —— 保留了"异常发生过"的证据, 又不让它冒充测量结果。 */
            if (p & 0x80000000u) {
                g_per_glitch_n++;
            } else {
                g_per_cyc_last = p;
                if (p < g_per_cyc_min) g_per_cyc_min = p;
                if (p > g_per_cyc_max) g_per_cyc_max = p;
            }
        }
        g_per_prev = t0;
    }
}

/* ══════════════════════════════════════════════════════════════════
 * 阶段 3.1 — 协议层 (transport 帧 + USART1)
 * ══════════════════════════════════════════════════════════════════
 * 分工严格:
 *   USART1 IRQ  → 只把字节塞进环形缓冲 (uart.c)
 *   主循环      → proto_poll() 排空缓冲 → fp_feed 解析 → 分发命令
 * 这样 ISR 时长与"帧有多长"无关, 拍周期不受 PC 通信影响。
 */
static FrameParser_t  s_parser;
static uint8_t        s_txbuf[FRAME_TOTAL_MAX];
static volatile uint32_t s_selftest_active = 0;

OBS uint32_t g_uart_brr      = 0;    /* BRR 实测值 (100MHz/115200 → 0x3641) */
/* ★★ A1 事故的直接判据: USART1 的 NVIC 使能位 (读的是 ISER 位本身, 不是本地缓存)。
 *   期望恒为 1。若为 0 → 中断从未生效, 上位机发什么都不会被收到 ——
 *   而 CR1/BRR/GPIO 会全部看起来正确 (这就是上一轮排障被带偏的原因)。
 *   有了它, A1 这类问题在**上电后第一次读回**就会暴露, 不必等到"串口没反应"。 */
OBS uint32_t g_uart_irq_en   = 0;
OBS uint32_t g_uart_rx_bytes = 0;
OBS uint32_t g_uart_tx_bytes = 0;
OBS uint32_t g_uart_ore      = 0;    /* 硬件溢出: 非 0 = 主循环排空太慢 (故障信号) */
/* ── UART 接收诊断 (排 "只收到 1 个字节" 这类故障必备) ── */
OBS uint32_t g_uart_isr_n    = 0;   /* ISR 进入次数 */
OBS uint32_t g_uart_isr_ore  = 0;   /* ISR 里看到 ORE 的次数 */
OBS uint32_t g_uart_fe       = 0;   /* 帧错误 (线上波形不对) */
OBS uint32_t g_uart_ne       = 0;   /* 噪声错误 */
OBS uint32_t g_uart_push     = 0;   /* 真正入环形缓冲的字节数 */
OBS uint32_t g_uart_last_isr = 0;   /* 最近一次 ISR 原值 */
OBS uint32_t g_uart_last_byte= 0;   /* 最近一次收到的字节 */
OBS uint32_t g_uart_drop     = 0;    /* 环形缓冲写满丢弃 */
OBS uint32_t g_frame_ok      = 0;    /* CRC 通过的完整帧数 —— 解析链路的正向证据 */
OBS uint32_t g_frame_bad     = 0;    /* 超长 / CRC 不符 */
OBS uint32_t g_cmd_count     = 0;
OBS uint32_t g_cmd_last      = 0xFFFFFFFFu;
OBS uint32_t g_nak_count     = 0;
OBS uint32_t g_banner_count  = 0;
/* ★ 组帧自检结果 (1=通过): 用独立实现算出的期望值校验 CRC 覆盖范围。
 *   "覆盖长度写错"这类 bug 不会崩、不会报警, 只会让**每一帧都被对端判为 CRC 错**
 *   —— 所以必须有一个能被外部读走的量。详见 frame_build_selftest()。 */
OBS uint32_t g_frame_selftest = 0;
OBS uint32_t g_selftest_state  = 3;  /* 0=待跑 1=通过 2=失败 3=未启用 */
OBS uint32_t g_selftest_frames = 0;

/* 组帧: [0xC1][sts][len:2 LE][payload][crc:2 LE]
 * CRC 覆盖 [sts][len_lo][len_hi][payload] = **3 + n** 字节 (与 S3 的
 * `crc16_ccitt(cb, 3 + n)` 逐字一致, 也与 PC 侧 h723_proto.py 的
 * `body = frame[1:-2]` 一致)。
 *
 * ★★ 这里踩过一个 P1 级 bug (2026-09-10 自查发现, 审计未覆盖):
 *   原实现写的是 `crc16_ccitt(s_txbuf + 1, 2u + n)` —— **少算了一个字节**:
 *   它只覆盖到 `payload[n-2]`, **载荷最后一个字节不在 CRC 保护范围内**。
 *   症状: PC 侧按 7 字节校验 → 每一帧都被判成 CRCBAD → 协议层即使接线正确、
 *   NVIC 中断已使能, 也**永远收不到一帧合法响应**。
 *   为什么一直没被发现: ① 无人真正收到过帧 (接线 + NVIC 两重故障先拦住了)
 *   ② 阶段 3.1 报告里那个"手算好的横幅帧 …D8 AC" 是**按错误假设手算的**,
 *      从未在线上验证过 —— 属于"宣称 > 实现", 已随本次修复一并纠正。
 *   防护: 下面 build_frame_into() 把"覆盖长度"收敛成一个常量 FRAME_CRC_COVER(n),
 *   并配 frame_build_selftest() 用**独立实现算出的期望值** 0xC9C9 做可失败判据。 */
#define FRAME_CRC_COVER(n)  (3u + (n))    /* [sts][len_lo][len_hi][payload...] */

static uint32_t build_frame_into(uint8_t *out, uint8_t sts, const uint8_t *p, uint32_t n)
{
    if (n > FRAME_PAYLOAD_MAX) n = FRAME_PAYLOAD_MAX;
    uint32_t pos = 0;
    out[pos++] = FRAME_SYNC_MCU2PC;
    out[pos++] = sts;
    out[pos++] = (uint8_t)(n & 0xFFu);
    out[pos++] = (uint8_t)((n >> 8) & 0xFFu);
    for (uint32_t i = 0; i < n; i++) out[pos++] = p[i];
    uint16_t crc = crc16_ccitt(out + 1, FRAME_CRC_COVER(n));   /* ★ 不含 SYNC */
    out[pos++] = (uint8_t)(crc & 0xFFu);
    out[pos++] = (uint8_t)(crc >> 8);
    return pos;
}

/**
 * @brief 组帧自检: 用**独立实现**(PC 侧 Python CRC16-CCITT)算出的期望值校验组帧。
 *
 * 期望值来源 (可手算复核):
 *   crc16_ccitt([0x00][0x04][0x00][0x00 0x02 0x33 0x00]) = 0xC9C9
 *   即: sts=ACK, n=4, payload=[fw_lo fw_hi cap_lo cap_hi]=[00 02 33 00]
 *   ⇒ 帧应为 C1 00 04 00 00 02 33 00 C9 C9
 *
 * ★ 这是"覆盖长度写错"的可失败判据: 若有人再写成 `2u + n`(漏最后一个载荷字节),
 *   帧尾会变成 44 E7 而不是 C9 C9, 本函数立刻返回 0。
 *   2026-09-10 实测: 修复前 = 44 E7 (对应覆盖 6 字节), 修复后 = C9 C9 ✓
 */
static uint32_t frame_build_selftest(void)
{
    static const uint8_t pl[4] = {0x00, 0x02, 0x33, 0x00};
    uint8_t f[16];
    uint32_t pos = build_frame_into(f, STS_ACK, pl, 4);
    if (pos != 10) return 0;
    if (f[0] != FRAME_SYNC_MCU2PC || f[1] != STS_ACK) return 0;
    if (f[2] != 4 || f[3] != 0) return 0;
    if (f[4] != 0x00 || f[5] != 0x02 || f[6] != 0x33 || f[7] != 0x00) return 0;
    return (f[8] == 0xC9u && f[9] == 0xC9u) ? 1u : 0u;
}

static void send_response(uint8_t sts, const uint8_t *p, uint32_t n)
{
    uint32_t pos = build_frame_into(s_txbuf, sts, p, n);
    uart1_write(s_txbuf, pos);
    g_uart_tx_bytes += pos;
}

static void ack(const uint8_t *p, uint32_t n) { send_response(STS_ACK, p, n); }

static void nak(const char *m)
{
    uint32_t n = 0;
    while (m && m[n]) n++;
    g_nak_count++;
    send_response(STS_NAK, (const uint8_t *)m, n);
}

/* 版本/能力协商: 载荷 [fw:u16 LE][cap:u16 LE] —— 与 S3 逐字节同构 */
static void h_get_version(void)
{
    uint8_t r[4];
    uint16_t v = DCL_FW_VERSION_H723;
    uint16_t c = DCL_CAP_H723_IMPL;
    r[0] = (uint8_t)(v & 0xFFu); r[1] = (uint8_t)(v >> 8);
    r[2] = (uint8_t)(c & 0xFFu); r[3] = (uint8_t)(c >> 8);
    ack(r, 4);
}

/* 上电横幅: 主动发一帧版本响应。两个作用:
 *   ① LA 端**不需要 PC 接线**就能抓到真实协议波形 → 外部证据
 *   ② PC 端连上时能立刻看到"设备还活着" */
static void proto_banner(void) { h_get_version(); g_banner_count++; }

/* ══════════ 阶段 3.2 — deploy (0x10) 与引擎状态 (0x38) ══════════ */

static inline void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v); p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
static inline uint16_t get16(const uint8_t *p) { return (uint16_t)(p[0] | ((uint16_t)p[1] << 8)); }
static inline uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* 0x10 DEPLOY — 载荷: [nr:u16][np:u16][ns:u16][routes nr×16B][params np×16B][states ns×16B]
 *
 * 与 S3 的差异 (都是"把债还掉", 不是"另搞一套"):
 *  ① ACK **带载荷** [seq:u16][budget:u32] —— S3 的 ACK 是空的, 上位机只能知道
 *     "受理了"; 现在知道"受理了第几号、预算多少 cyc"。旧上位机读前两个字节之外
 *     的内容会被忽略, 所以是向后兼容的追加 (S3 自己就用这个惯例: 0x43 从 8B 扩到 11B)。
 *  ② **生效确认**: seq 会被 ISR 在真正切换完 ACTIVE 表时写进 APPLIED_SEQ,
 *     0x38 的尾部扩展会把它带回来 ⇒ "已生效"变成可观测, 不再是假设。
 *  ③ 校验里 SRC_HMI 直接拒 (H723 未实现该源, 放行 = 静默给恒 0 的假信号)。
 *  ④ 预算模型的除数 div2 用 **64** 而不是 100 (H9)。 */
static void h_deploy(const uint8_t *p, uint32_t n)
{
    if (n < 6) { g_deploy_nak++; nak("short"); return; }
    uint16_t nr = get16(p), np = get16(p + 2), ns = get16(p + 4);
    if (nr > MAX_ROUTES || np > MAX_PARAMS || ns > MAX_STATES) {
        g_deploy_nak++; nak("counts exceed max"); return;
    }
    uint32_t need = 6u + ((uint32_t)nr + np + ns) * 16u;
    if (n < need) { g_deploy_nak++; nak("payload short"); return; }
    const uint8_t *d = p + 6;
    const uint8_t *pd = d + (size_t)nr * 16u;

    /* ① 参数有限性: NaN/Inf 会经 DIRECT/SCALE/积分直接传播成 NaN 输出。
     *    只查每字的高 8 位全 1 (即指数 0xFF) —— 不用浮点比较, 也不依赖 FPU 状态。 */
    for (uint16_t i = 0; i < (uint16_t)(np * 4u); i++) {
        if (((get32(pd + (size_t)i * 4u) >> 23) & 0xFFu) == 0xFFu) {
            g_deploy_nak++; nak("param not finite"); return;
        }
    }

    /* ② 逐条校验 + dst 唯一写者 (两条路由写同一个 wire = 结果取决于表序, 非确定性) */
    uint64_t dst_seen[2] = { 0, 0 };
    for (uint16_t i = 0; i < nr; i++) {
        RouteEntry_t r;
        memcpy(&r, d + (size_t)i * 16u, 16u);
        if (!(r.flags & ROUTE_FLAG_ACTIVE)) continue;
        const char *err = engine_route_validate(&r);
        if (err) { g_deploy_nak++; nak(err); return; }
        if (r.op == OP_LPF) {                       /* v0.2: LPF 参数是时间常数 τ 秒, 必须 > 0 */
            uint32_t tb = get32(pd + (size_t)r.param_idx * 16u);
            if ((tb & 0x7FFFFFFFu) == 0u) { g_deploy_nak++; nak("lpf tau must be >0"); return; }
        }
        uint64_t bit = 1ULL << (r.dst_channel & 63u);
        if (dst_seen[r.dst_channel >> 6] & bit) { g_deploy_nak++; nak("dst conflict"); return; }
        dst_seen[r.dst_channel >> 6] |= bit;
    }

    /* ★★ 跨档速率检查 (S3 语义, 由 S3 回归套件 T18 发现 H723 **漏了这一步**):
     *   慢消费者读快生产者 = **欠采样** → 混叠/发散。判据: 生产者 div_idx < 消费者 div_idx 即拒。
     *   ★ 这是**程序级**检查 —— 单看一条路由看不出来, 必须先知道"那个 wire 的生产者是谁"。
     *   ★ 与 S3 的唯一差别是报错串更短 (S3 带 wire 号); 语义逐字一致。 */
    {
        int8_t prod_div[MAX_WIRES];              /* 各 wire 的生产者档位; -1 = 该 wire 无生产者 */
        for (int i = 0; i < MAX_WIRES; i++) prod_div[i] = -1;
        for (uint16_t i = 0; i < nr; i++) {
            RouteEntry_t rr;
            memcpy(&rr, d + (size_t)i * 16u, 16u);
            if (!(rr.flags & ROUTE_FLAG_ACTIVE)) continue;
            prod_div[rr.dst_channel] = (int8_t)(rr.period & PERIOD_DIV_MASK);
        }
        for (uint16_t i = 0; i < nr; i++) {
            RouteEntry_t rr;
            memcpy(&rr, d + (size_t)i * 16u, 16u);
            if (!(rr.flags & ROUTE_FLAG_ACTIVE)) continue;
            if (rr.src_type != SRC_WIRE) continue;      /* 只有跨路由的 wire 依赖才可能欠采样 */
            int8_t pdiv = prod_div[rr.src_index];
            int8_t cdiv = (int8_t)(rr.period & PERIOD_DIV_MASK);
            if (pdiv >= 0 && pdiv < cdiv) { g_deploy_nak++; nak("rate mismatch"); return; }
        }
    }

    /* ③ 预算门: 条数限制 ≠ 成本限制 —— 128 条 PID 是 128 条, 成本却是 DIRECT 的 2.6 倍。
     *    Σ ceil((op_cost+src_cost)/div倍率) 必须 ≤ EXEC_DEPLOY_BUDGET,
     *    否则放行的程序会把拍吃掉 (阶段 2 审计真的踩到过一次 102.9% 超载)。 */
    uint32_t budget = engine_prog_budget(d, nr);
    g_deploy_budget = budget;
    if (budget > EXEC_DEPLOY_BUDGET) { g_deploy_nak++; nak("exec budget exceeded"); return; }

    /* ④ 装载 STAGING (不碰 ACTIVE) → 置 RELOAD 让 ISR 在下一拍原子切换 */
    uint16_t nw = engine_stage_program(g_shm, d, nr, np, ns);
    g_deploy_routes = nw;
    /* ★ W2: 新程序 = 新语义, 旧的 force 点位可能指向新程序里根本不存在的 wire。
     *   留着它会在下一拍把无关 wire 钉住, 而 PC 完全看不到 (MASK 位还在但程序换了)。
     *   位置在装载**之后**: 此刻表已就绪, 清 force 与下一拍的扫描无竞争窗口。*/
    eng_force_clear(g_shm);
    g_force_clears++;
    g_deploy_seq++;
    SHM_U16(g_shm, OFF_CTRL_DEPLOY_SEQ) = (uint16_t)g_deploy_seq;
    g_deploy_set_tick = g_tick_count;
    __asm__ volatile("dsb" ::: "memory");   /* ARM: dsb (S3 的 Xtensa `memw` 在 ARM 上不存在) */
    SHM_U8(g_shm, OFF_CTRL_RELOAD) = 1;             /* 单字节写 = 原子 */
    __asm__ volatile("dsb" ::: "memory");   /* ARM: dsb (S3 的 Xtensa `memw` 在 ARM 上不存在) */

    /* ★ W2.4: 标 dirty —— **不在 deploy 里落盘**。理由两条:
     *   ① deploy 通常发生在引擎 RUN 时; 擦一个扇区 1~4 秒 = 拍长的上万倍,
     *      会彻底破坏确定性 (S3 的 PERSISTENT 语义门同款: 运行期 0 flash 操作)
     *   ② 裸机上"什么时候能阻塞 1~4 秒"只有 PC 知道 —— 它才发 0x12 STOP。
     *      deploy 只登记, 由 PC 在 STOP 后用 0x43 mode=1 显式触发落盘。
     *   (S3 用后台 persist_task 异步 flush; 裸机没有后台任务, 所以变成显式请求。
     *    这不是简化, 是"让停顿的时刻可被上位机控制"—— 更确定, 不是更差。) */
    g_persist_dirty = 1;
    g_deploy_ok++;

    uint8_t r[6];
    r[0] = (uint8_t)(g_deploy_seq); r[1] = (uint8_t)(g_deploy_seq >> 8);
    put32(r + 2, budget);
    ack(r, 6);
}

/* ══════════ W3 — 0x44 SEQ_DEPLOY (顺序域 Sequencer v0) ══════════
 * 帧: [n_seq:u8][n_steps_total:u16]
 *     [目录 n_seq×6B {n_steps:u8 out_wire:u8 period:u8 pad:u8 step_off:u16 LE}]
 *     [步表 n_steps_total×16B (SeqStepEntry_t, 按实例连续)]
 *
 * ★ 与 S3 的关系: **校验规则逐条搬** (S3 main/main.c:650 h_seq_deploy), 但有三处
 *   H723 侧必须不同的地方, 不是"简化"而是"本平台的事实":
 *   ① 落盘触发: S3 在命令内直接 persist_save()(它挂后台任务, flash 操作不占协议线程)。
 *      H7 裸机没有后台任务, 擦 128KB 扇区 ≈ 1~4s = 拍长的上万倍 —— 在命令里同步
 *      落盘会**阻塞主循环**并彻底破坏确定性。⇒ 与 0x10 同款: 只标 dirty,
 *      由 PC 在 STOP 后用 0x43 mode=1 显式触发。**语义更确定, 不是更差**。
 *   ② Xtensa 的 `memw` → ARM 的 `dsb`。
 *   ③ 校验错误用本项目逐字同构的 err 字符串 (S3 的 snprintf 动态串也保留一处)。
 *
 * ★ 语义定死 (三件"不做", 每条都有理由):
 *   ① **必须 STOP 态部署**。seq 无 staging (直接写 ACTIVE 表), RUN 态部署 = ISR
 *      可能读到半写表 → 半新半旧步表 = 任一因果都无法归因。0x13 先停是上位机流程。
 *   ② **不热重载**: 直接写 ACTIVE + ctrl.run=0, START 时从 step0 开始。
 *   ③ **不做跨命令 dst 冲突的静态表外校验**——但 **OA3 的 seq↔route 冲突必须查**
 *      (步号镜像 wire 同时被某条路由写 = 双写者, 镜像被每拍覆盖静默失效)。
 */
static void h_seq_deploy(const uint8_t *p, uint32_t n)
{
    if (n < 3) { g_seq_nak++; nak("seq: short frame"); return; }
    uint8_t  n_seq   = p[0];
    uint16_t n_steps = get16(p + 1);
    if (n_seq == 0 || n_seq > MAX_SEQ_INST) { g_seq_nak++; nak("seq: bad n_seq"); return; }
    if (n_steps == 0 || n_steps > MAX_SEQ_STEPS) { g_seq_nak++; nak("seq: bad n_steps"); return; }
    uint32_t need = 3u + (uint32_t)n_seq * 6u + (uint32_t)n_steps * 16u;
    if (need != n) { g_seq_nak++; nak("seq: length mismatch"); return; }
    /* OA7: RUN 态部署 = ISR 可能读半写表。定死必须 STOP 态。 */
    if (SHM_U8(g_shm, OFF_CTRL_ENGINE_RUN)) { g_seq_nak++; nak("seq: stop engine first"); return; }

    const uint8_t *dir = p + 3;
    const uint8_t *tbl = dir + (uint32_t)n_seq * 6u;

    /* ── OA3: seq out_wire 是**新的写者类别**, 0x10 的 dst_seen 位图管不到它 ──
     * seq 的"步号镜像"直接写 WIRE_MAP[out_wire], 与路由的 dst_channel 写同一格。
     * 若撞槽: 每拍"路由写 → seq 覆盖"(或反之), 谁赢取决于段序 —— 步号镜像静默失效。
     * 编译期已拦 (DSL 侧), 协议裸用必须兜底。
     * ★ 只扫**生效条数** (OFF_CTRL_N_ROUTES): 表体在 count 之后是残留内容,
     *   RESET 只清计数不清表 → 全表扫会把上次的残留条目误判冲突 (S3 T29 实测)。
     * ★ ACTIVE + STAGING 两表都扫: deploy(0x10) 后 ISR 未及热切的窗口里,
     *   新路由还在 STAGING —— 只扫 ACTIVE 会漏掉这个时序。 */
    uint64_t rt_bm[2] = { 0u, 0u };
    uint64_t sq_bm[2] = { 0u, 0u };
    uint16_t nr_act = SHM_U16(g_shm, OFF_CTRL_N_ROUTES);
    if (nr_act > MAX_ROUTES) nr_act = MAX_ROUTES;
    const uint8_t *rt_tabs[2] = {
        (const uint8_t *)(g_shm + OFF_ROUTE_TABLE),
        (const uint8_t *)(g_shm + OFF_ROUTE_STAGING)
    };
    for (int t = 0; t < 2; t++) {
        for (uint16_t i = 0; i < nr_act; i++) {
            RouteEntry_t r;
            memcpy(&r, rt_tabs[t] + (uint32_t)i * 16u, 16u);
            if (!(r.flags & ROUTE_FLAG_ACTIVE)) continue;
            if (r.dst_channel < MAX_WIRES)
                rt_bm[r.dst_channel >> 6] |= 1ULL << (r.dst_channel & 63u);
        }
    }

    /* ── pass1: 目录校验 (结构边界 + 唯一写者交叉) ── */
    uint32_t off_sum = 0;
    for (uint32_t i = 0; i < n_seq; i++) {
        const uint8_t *d  = dir + i * 6u;
        uint16_t ns = d[0];
        uint16_t ow = d[1];
        uint8_t  pd = d[2];
        uint16_t off = get16(d + 4);
        if (ns == 0) { g_seq_nak++; nak("seq: inst n_steps=0"); return; }
        if (off != off_sum) { g_seq_nak++; nak("seq: step_off not contiguous"); return; }
        if (ow >= MAX_WIRES) { g_seq_nak++; nak("seq: out_wire OOR"); return; }
        if (ow == 0) { g_seq_nak++; nak("seq: out_wire 0 reserved"); return; }  /* 哨兵 (OA3) */
        uint64_t bit = 1ULL << (ow & 63u);
        if ((rt_bm[ow >> 6] & bit) || (sq_bm[ow >> 6] & bit)) {
            /* 这条必须报出**具体 wire 号**, 否则 PC 侧只知道"冲突了"却不知道是哪条
             * (128 条路由里找一个撞槽 = 大海捞针)。
             * ★ 手工拼十进制, **不用 snprintf** —— 理由两条:
             *   ① 裸机链接的是 nano.specs, snprintf 会拖进整条格式化链
             *      (这个工程 FLASH 才 23KB; 引一个 snprintf 就是几百字节 + 栈开销);
             *   ② 这条 NAK 消息是**协议的一部分**, 手工拼的字节能被 PC 端逐字断言,
             *      比"依赖 libc 版本"稳定。NAK 串是 const 字面量, 走 s_txbuf 发出。 */
            char m[48];
            const char *pre = "seq: out_wire ";
            uint32_t k = 0;
            while (pre[k]) { m[k] = pre[k]; k++; }
            char dig[5]; uint32_t nd = 0;
            if (ow == 0u) dig[nd++] = '0';
            uint32_t tmp = ow;
            while (tmp) { dig[nd++] = (char)('0' + (tmp % 10u)); tmp /= 10u; }
            while (nd) m[k++] = dig[--nd];
            const char *suf = " conflicts writer";
            for (uint32_t j = 0; suf[j]; j++) m[k++] = suf[j];
            /* NAK 发送靠 NUL 定长, 所以这里必须补终止符 (手工拼字符串的经典坑) */
            m[k] = '\0';
            g_seq_nak++; nak(m);
            return;
        }
        sq_bm[ow >> 6] |= bit;
        if ((pd & PERIOD_DIV_MASK) > PERIOD_DIV_IDX_SLOW) { g_seq_nak++; nak("seq: bad div"); return; }
        off_sum += ns;
    }
    if (off_sum != n_steps) { g_seq_nak++; nak("seq: dir steps mismatch"); return; }

    /* ── pass2: 步表参数校验 (route_validate 哲学: 下载期显式拒绝, 不留给运行期) ── */
    for (uint32_t i = 0; i < n_steps; i++) {
        SeqStepEntry_t e;
        memcpy(&e, tbl + i * 16u, 16u);
        if (e.param_idx >= MAX_PARAMS) { g_seq_nak++; nak("seq: param_idx OOR"); return; }
        if (e.cond_type > 2u) { g_seq_nak++; nak("seq: bad cond_type"); return; }
        /* OA6: cond_type=2 (纯超时步, 无条件源) 若不使能 timeout → 无条件可判 又 无
         * 超时计数 ⇒ 引擎永远停在这一步 (卡步), 而 PC 侧只看到"程序不动"。
         * DSL 不会产出这种程序, 协议裸用必须拒。 */
        if (e.cond_type == 2u && !(e.flags & 2u)) {
            g_seq_nak++; nak("seq: timeout step needs timeout_en"); return;
        }
        if (e.cond_type == 0u && e.cond_idx >= MAX_SENSORS) { g_seq_nak++; nak("seq: cond sensor OOR"); return; }
        if (e.cond_type == 1u && e.cond_idx >= MAX_WIRES)   { g_seq_nak++; nak("seq: cond wire OOR"); return; }
        if (e.flags & 2u) {   /* timeout_en: value_b(超时秒) 必须 > 0, 否则超时永不触发 */
            uint32_t tb;
            memcpy(&tb, (const uint8_t *)(g_shm + OFF_PARAM_TABLE) + (uint32_t)e.param_idx * 16u + 4u, 4u);
            if ((tb & 0x7FFFFFFFu) == 0u) { g_seq_nak++; nak("seq: timeout must be >0"); return; }
        }
    }

    /* ── 写表 + 控制块 (此时引擎 STOP, 无竞争窗口) ── */
    memcpy((void *)(g_shm + OFF_SEQ_TABLE), tbl, (uint32_t)n_steps * 16u);
    uint32_t o2 = 0;
    uint8_t phase_cnt[3] = { 0u, 0u, 0u };
    for (uint32_t i = 0; i < n_seq; i++) {
        const uint8_t *d = dir + i * 6u;
        uint8_t pd = (uint8_t)(d[2] & PERIOD_DIV_MASK);
        /* phase = 同 div 组内序号 —— 错开防同拍积压 (与 0x10 的路由相位分配同策略)。
         * ★ 用 & PERIOD_PHASE_MASK 保护: 同组实例数 < 8 永远进不了这个分支, 但仍
         *   不能依赖"输入合法"来做移位安全 —— 6 位域一旦被高位污染就是未定义行为。 */
        uint8_t phase = (uint8_t)(phase_cnt[pd] & PERIOD_PHASE_MASK);
        phase_cnt[pd]++;
        uint8_t *c = (uint8_t *)(g_shm + OFF_SEQ_CTRL) + i * 16u;
        uint16_t ns = d[0];
        /* 逐字段写 volatile SHM (不用结构体整体赋值: 打包结构体在 volatile 上
         * 会被展开成逐字节访问, 语义正确但在这里不必要 —— 用定点写更清楚) */
        *(volatile uint16_t *)(c + 0) = (uint16_t)o2;
        *(volatile uint16_t *)(c + 2) = ns;
        *(volatile uint16_t *)(c + 4) = 0u;                 /* step_cur = 0 */
        *(volatile uint16_t *)(c + 6) = (uint16_t)d[1];     /* out_wire */
        c[8]  = (uint8_t)(pd | (phase << PERIOD_PHASE_SHIFT));  /* period */
        c[9]  = 0u;                                         /* run = 0 (START 置位) */
        *(volatile uint16_t *)(c + 10) = 0u;                /* reserved */
        *(volatile uint32_t *)(c + 12) = 0u;                /* step_tick (u32, OA5) */
        o2 += ns;
    }
    SHM_U8(g_shm, OFF_CTRL_N_SEQ) = n_seq;
    __asm__ volatile("dsb" ::: "memory");

    /* 与 0x10 同: 只标 dirty, 不在此处落盘 (理由见函数头 ①) */
    g_persist_dirty = 1;
    g_seq_deploys++;
    ack(NULL, 0);
}

/* 首次采样前的"无数据"哨兵 → 对外一律呈现 0。
 * ★ 为什么要统一: g_*_min 在第一次采样前是 0xFFFFFFFF (表示"还没有样本")。
 *   若直接写进 SHM, PC 侧会把它当成一个**极大的测量值**(4294967295 cyc),
 *   而不是"尚无数值" —— 这正是"哨兵语义必须显式"的那一族问题。
 *   ★ 与 h_engine_status 的 r[4]/r[12] 口径**必须一致**, 所以抽成一个函数,
 *     避免两处各写一遍 `== 0xFFFFFFFFu ? 0u :` 而后某天只改了一处。 */
static inline uint32_t pn_or_0(uint32_t v) { return (v == 0xFFFFFFFFu) ? 0u : v; }

/* ══════════ W4: 通信域 Modbus (0x60 / 0x61 / 0x62) ══════════
 * 三条命令的载荷格式与语义**逐字照搬 S3** (main.c:588/608/630), 因为 PC 侧工具
 * (verify_modbus.py / verify_hmi.py) 按这个格式写, 改了就等于改了协议。
 *
 * ★ 隧道模式的战略意义 (S3 原设计, 这里原样保留):
 *   0x60 把一帧原始 Modbus RTU 请求**注入 SHM 的 RX 缓冲**, 状态机照常跑完整
 *   协议栈 (CRC 校验/功能码分发/响应组装/异常码) —— 只是"字节来源"不同。
 *   于是 **协议栈可以完全脱离物理层验证** (零硬件)。硬件到位只换字节源 (0x62 的 src)。
 *   ★ 本次的硬件状况正是它要解决的: TTL 转 485 模块已到、USB 转 485 还在路上 ⇒
 *     协议栈先跑通, 物理层后接, 互不阻塞。
 *
 * 状态机推进在 ISR 里 (mb_tick), 本文件只负责"注入/读回/配置"三个入口。 */

/* 0x60 MB_INJECT — 帧: [Modbus RTU 请求原始字节] (含 CRC)
 * → 响应进 TX 缓冲 → PC 用 0x61 读回。
 * 状态机/协议栈与真实 UART 路径完全相同 (见 modbus.c 的 src 分支)。*/
static void h_mb_inject(const uint8_t *p, uint32_t n)
{
    if (n < 4 || n > MB_MAX_FRAME) { g_mb_nak++; nak("mb: bad frame len"); return; }
    int rc = mb_inject(g_shm, p, (uint16_t)n);
    if (rc != 0) {
        /* ★ 把"为什么拒"分开报: 忙碌与长度非法是两种完全不同的现场问题
         *   (忙碌 = 上一帧还没处理完, 长度 = PC 发错了)。合并成一句会让排查失去方向。 */
        g_mb_nak++;
        nak(rc == -2 ? "mb: busy" : "mb: bad frame");
        return;
    }
    g_mb_inject_ok++;
    ack(NULL, 0);
}

/* 0x61 MB_RESP — 读响应帧 + 通信域状态
 * 响应: [state u8][tx_len u8][tx 字节…][frames_rx u32][frames_tx u32]
 *       [err_crc u32][err_exc u32] */
static void h_mb_resp(void)
{
    MbCtrl_t *c = (MbCtrl_t *)SHM_PTR(g_shm, OFF_MB_CTRL);
    /* ★★ 缓冲必须按 **MB_TX_SIZE**(256) 开, 不能按 MB_MAX_FRAME(128):
     *   M1 修复后 tx_len 可达 255 (0x03 qty=125 → 3+250+2), 下面的拷贝循环
     *   `for (i < c->tx_len) r[2+i] = tx[i]` 会**写穿栈** —— 这是 M1 修复的
     *   连带项 (审计只点了 modbus.c, 但读取端不一起改就是拿栈溢出换卡死)。
     *   2 + 255 + 16 = 273 ≤ 2 + 256 + 16 = 274 ✓ */
    uint8_t r[2 + MB_TX_SIZE + 16];
    r[0] = c->state;
    r[1] = c->tx_len;
    if (c->tx_len) {
        const uint8_t *tx = (const uint8_t *)SHM_PTR(g_shm, OFF_MB_TX);
        for (uint32_t i = 0; i < c->tx_len; i++) r[2 + i] = tx[i];
    }
    uint32_t off = 2u + c->tx_len;
    put32(r + off, c->frames_rx); off += 4;
    put32(r + off, c->frames_tx); off += 4;
    put32(r + off, c->err_crc);   off += 4;
    put32(r + off, c->err_exc);   off += 4;
    ack(r, off);
    /* 缓冲模式: 响应已被 PC 取走 → 清缓冲 (物理口模式发完已清, 无需处理) */
    if (!c->tx_uart) { c->tx_len = 0; c->tx_sent = 0; }
}

/* 0x62 MB_CFG — 配置通信域: [src u8][tx_uart u8][budget u8] (后两字节可选)
 *   src    : 0=RX 走物理口 FIFO, 1=RX 走隧道注入(0x60)
 *   tx_uart: 0=响应留缓冲(0x61 读回), 1=响应从物理口发 (★ LA 可抓)
 * 典型 LA 验证: 0x62 [1][1] → 注入走隧道、响应从 PA2 真发 → 抓 USART2_TX 波形。 */
static void h_mb_cfg(const uint8_t *p, uint32_t n)
{
    if (n < 1) { g_mb_nak++; nak("mb cfg: need src"); return; }
    MbCtrl_t *c = (MbCtrl_t *)SHM_PTR(g_shm, OFF_MB_CTRL);
    c->src = p[0];
    if (n >= 2) c->tx_uart = p[1];
    if (n >= 3 && p[2]) c->tick_budget = p[2];
    uint8_t r[3];
    r[0] = c->src; r[1] = c->tx_uart; r[2] = c->tick_budget;
    __asm__ volatile("dsb" ::: "memory");
    ack(r, 3);
}

/* ══════════ W5: macro 字节码 VM (0x40 / 0x41 / 0x42) ══════════
 * 语义逐字对齐 S3 (S3 main.c 的 handle_macro / h_macro_upload / h_macro_ctrl):
 *   0x40 = **一次性执行** payload 里的字节码, ACK 返回栈内容 (每值 4B 小端)
 *   0x41 = 上传 [loop_ms u16][code...] → SHM (H723 驻 RAM, 不落 flash, 见 macro.h)
 *   0x42 = 控制 [action] 0=stop 1=start
 * ★ 一次性执行 (0x40) 与循环执行 (macro_tick) **共用同一个 macro_exec** ——
 *   保证"能单跑"与"能循环跑"是同一份语义, 不会长出两套行为。 */
static void h_macro(const uint8_t *p, uint32_t n)
{
    if (n == 0 || n > MACRO_MAX_CODE) { g_macro_nak++; nak("bad macro len"); return; }
    uint32_t out[MACRO_STACK_DEPTH];
    uint16_t on = 0;
    int rc = macro_exec(g_shm, p, (uint16_t)n, out, &on);
    if (rc != 0) { g_macro_nak++; nak("macro exec failed"); return; }
    uint8_t r[MACRO_STACK_DEPTH * 4];
    for (uint16_t i = 0; i < on; i++) {
        r[i * 4 + 0] = (uint8_t)(out[i] & 0xFF);
        r[i * 4 + 1] = (uint8_t)((out[i] >> 8) & 0xFF);
        r[i * 4 + 2] = (uint8_t)((out[i] >> 16) & 0xFF);
        r[i * 4 + 3] = (uint8_t)((out[i] >> 24) & 0xFF);
    }
    g_macro_exec_ok++;
    ack(r, (uint32_t)on * 4u);
}

static void h_macro_upload(const uint8_t *p, uint32_t n)
{
    if (n < 2) { g_macro_nak++; nak("need loop_ms"); return; }
    uint16_t loop_ms = (uint16_t)(p[0] | (p[1] << 8));
    const uint8_t *code = p + 2;
    uint16_t code_len = (uint16_t)(n - 2);
    if (macro_upload(g_shm, loop_ms, code, code_len) != 0) {
        g_macro_nak++; nak("macro too long"); return;
    }
    uint8_t r[4];
    r[0] = (uint8_t)(code_len & 0xFF); r[1] = (uint8_t)(code_len >> 8);
    r[2] = (uint8_t)(loop_ms & 0xFF);  r[3] = (uint8_t)(loop_ms >> 8);
    g_macro_upload_ok++;
    ack(r, 4);
}

static void h_macro_ctrl(const uint8_t *p, uint32_t n)
{
    if (n < 1) { g_macro_nak++; nak("need action"); return; }
    int rc = macro_ctrl(g_shm, p[0]);
    if (rc != 0) { g_macro_nak++; nak(rc == -1 ? "macro: no prog" : "bad action"); return; }
    ack(NULL, 0);
}

/* ══════════ W5: 0x36 PIN_SELFTEST — **零接线自检** ══════════
 * 用 GPIO 内部上拉/下拉把引脚拉到已知电平, 验证:
 *   · ADC 输入通路 (AI 3 路, 读原始码): 上拉应接近满量程, 下拉应接近 0
 *   · DI 输入通路 (4 路, 读电平): 上拉应=1, 下拉应=0
 * 载荷 [method u8]: ADC 用 (0 = 引脚置 analog + 上/下拉; 1 = 置 input + 上/下拉)
 * 响应: [AI 3ch × 2 raw u16 = 12B][DI 4ch × 2 电平 u8 = 8B] = 20B
 * ★ 这条判据**能失败**: 若引脚配置/取样路径错, 上拉与下拉读数会相同 (常量),
 *   而"同一个值"正是"没采到这个脚"的特征 —— 比"读到一个数字"强得多。 */
static void h_pin_selftest(const uint8_t *p, uint32_t n)
{
    uint32_t method = (n >= 1u) ? p[0] : 0u;
    uint16_t ai[AI_NCH * 2];
    uint8_t  di[DI_COUNT * 2];
    ai_selftest(g_shm, method, ai);
    di_selftest(di);
    uint8_t r[AI_NCH * 2 * 2 + DI_COUNT * 2];
    uint32_t o = 0;
    for (int i = 0; i < AI_NCH * 2; i++) { r[o++] = (uint8_t)(ai[i] & 0xFFu); r[o++] = (uint8_t)(ai[i] >> 8); }
    for (int i = 0; i < DI_COUNT * 2; i++) r[o++] = di[i];
    g_pin_selftest_n++;
    ack(r, o);
}

/* 0x37 ADC_SCAN — 扫描 ADC1 通道原始码 (接线/映射排障用), 见 transport.h 说明。
 * 载荷 [ch_start u8][count u8] → ACK: count × u16 LE (原始码) */
static void h_adc_scan(const uint8_t *p, uint32_t n)
{
    uint32_t ch0 = (n >= 1u) ? p[0] : 0u;
    uint32_t cnt = (n >= 2u) ? p[1] : 20u;
    if (ch0 > 19u) ch0 = 0u;
    if (cnt == 0u || cnt > 20u || ch0 + cnt > 20u) { nak("bad adc scan"); return; }
    uint8_t r[40];
    for (uint32_t i = 0; i < cnt; i++) {
        uint16_t v = 0;
        (void)adc_read(ch0 + i, &v);
        r[i * 2]     = (uint8_t)(v & 0xFFu);
        r[i * 2 + 1] = (uint8_t)(v >> 8);
    }
    ack(r, cnt * 2u);
}

/* 0x38 ENGINE_STATUS — **前 31 字节与 S3 同布局 _且同语义_** (上位机脚本零改动),
 * 尾部追加 H723 扩展 7B (沿用 S3 的"尾部追加保前段兼容"惯例)。
 *
 * ★★ P3 修复 (外部审计): 之前 r[22] 放的是 `g_engine_gate`, 而 S3 同位置是 **run** ——
 *    "布局同、语义不同"比"布局不同"更坏: 按 run 解析的旧上位机会**静默拿到错值**。
 *    现 r[22] 恢复 S3 语义 (引擎是否在跑), H723 特有的 gate 挪到尾部 r[37]。
 *
 * ★ 数据源: 本平台的统计量住在 DTCM 的 C 全局里 (g_*), 不在 SHM 计时区 ——
 *   所以这里**按需打包**, 而不是让 ISR 每拍去维护第二份 SHM 计时块。
 *   理由: 每拍多写 8~10 个 SHM 字段会给 ISR 加成本, 而 ISR 成本是阶段 1/2
 *   基线的一部分, 不该为一个"被轮询才需要"的视图付每拍的代价。 */
static void h_engine_status(void)
{
    uint8_t r[40];
    uint32_t pn = (g_per_cyc_min == 0xFFFFFFFFu) ? 0u : g_per_cyc_min;
    uint32_t en = (g_isr_cyc_min == 0xFFFFFFFFu) ? 0u : g_isr_cyc_min;
    /* ★★ 审计发现 D 修复: 原来读的是 C 全局 `g_active_routes`, 而它只在启动/reinit
     *   更新 —— **deploy 路径漏了**, 于是 deploy 8 条之后这里仍报 128 (实测)。
     *   根因是"同一个量有两个来源": SHM 的 `N_ROUTES`(ISR 真正扫的) 与 C 全局的
     *   `g_active_routes`(只在两处更新)。它们的更新时机不同步 = 迟早不一致。
     *   ⇒ 统一以 **SHM `N_ROUTES` 为唯一权威** (它才是 ISR 真正使用的那个,
     *     由 engine_stage_program / reload / fill_tables 在切换表时写入)。
     *     C 全局 `g_active_routes` 降级为**纯观测面** (供 pyocd 侧旁证),
     *     且补上 deploy 路径的同步 (见 h_deploy 末尾) —— 使两者最终一致,
     *     但**判据只认 SHM**。
     *   ★ 这正是本项目在 engine.c:650 修过的同一类缺陷的另一半
     *     (当时修的是 SHM 侧, C 全局这一半漏了)。 */
    uint16_t nr = SHM_U16(g_shm, OFF_CTRL_N_ROUTES);
    put32(r + 0,  g_isr_n);      /* samples */
    put32(r + 4,  pn);           /* period_min */
    put32(r + 8,  g_per_cyc_max);/* period_max */
    put32(r + 12, en);           /* exec_min   */
    put32(r + 16, g_isr_cyc_max);/* exec_max   */
    r[20] = (uint8_t)(nr); r[21] = (uint8_t)(nr >> 8);
    /* ★ P3 修复 (外部审计): 这里原先是 `g_engine_gate` —— 而 S3 的同位置是 **run**。
     *   头部宣称"前 31 字节与 S3 逐字节同布局", 于是"布局同、语义不同"就成了**会静默
     *   骗人**的陷阱: 按 run 解析的旧上位机会拿到 gate 的值。⇒ 本位置恢复 S3 语义 =
     *   **引擎是否在跑**; 而 H723 特有的 gate 移到尾部扩展 (一个都不丢)。 */
    r[22] = SHM_U8(g_shm, OFF_CTRL_ENGINE_RUN) ? 1u : 0u;   /* = S3 的 `run` */
    put32(r + 23, g_shm_addr);   /* SHM 地址 (供上位机发现) */
    put32(r + 27, g_isr_overrun); /* ★ 审查二级 #5: 超预算次数 —— 真计数。
                                   * 原实现是 `0u` + 注释"暂未实现", 使 S3 套件 T9 的
                                   * `ov == 0` 那半条**不可能失败** (空判据)。
                                   * 现在配了能失败的对照: FLASH 取指 + 全表扫 ⇒ 会超。 */
    /* ---- H723 尾部扩展 (byte 31 起; 前 31 字节布局**与语义**均与 S3 一致) ---- */
    r[31] = (uint8_t)(g_deploy_seq);  r[32] = (uint8_t)(g_deploy_seq >> 8);
    r[33] = (uint8_t)(g_applied_seq); r[34] = (uint8_t)(g_applied_seq >> 8);
    r[35] = (uint8_t)(g_reload_lat);  r[36] = (uint8_t)(g_reload_lat >> 8);
    r[37] = (uint8_t)g_engine_gate;   /* H723 扩展: 扫描门 (S3 无此量, 故挪到尾部) */
    /* ★ 审计 #1: 物理输出面**登记数** —— 让"停机安全态契约覆盖了几个面"可被外部核对。
     *   判据: 必须 ≥1 (当前 = HIL 的 PWM 面)。若为 0 ⇒ 注册步骤没跑到 ⇒ 停机不清任何
     *   物理输出。★ 为什么单靠"读 OFF_HIL_DUTY 是不是 0"不够: 登记数为 0 时 duty
     *   恰好也是 0 (没人写它), 于是"没登记"与"已进安全态"在读数上**完全一样** ——
     *   必须有一个独立的量把这两件事分开。 */
    r[38] = (uint8_t)g_out_surfaces;
    ack(r, 39);
}

/* ══════════ W1: 运行控制 + SHM 读写 (0x11/0x12/0x13 + 0x20-0x23) ══════════
 * 这 7 条命令的意义: 把引擎的配置态从**编译期旋钮**搬到**运行期协议**。
 * 在此之前"改一条路由"要重新编译+烧录; 之后 PC 一条帧就能读写。
 *
 * ★ 与 S3 的关系: 语义逐字搬, 地址表必须重写 (见 engine.h 守卫段说明)。
 * ★ 四个安全守卫必须搬全 (S3 main.c:83-140), 少一个就是一个可利用的洞:
 *     valid_addr / valid_range / write_allowed(NaN 防护) / outputs_safe
 */

/* 拒绝原因码 —— 让"为什么被拒"可被外部读走, 而不是只有一串 NAK 文本。
 * 文本对人类友好, 码对**脚本判据**友好 (脚本比对字符串太脆, 改一个字就失效)。 */
#define NAKRH_ADDR     1u   /* 地址非法 / 不对齐 */
#define NAKRH_RANGE    2u   /* burst 区间非法 / 越界 / 跨禁区 */
#define NAKRH_COUNT    3u   /* count 为 0 或 > 256 */
#define NAKRH_SHORT    4u   /* 载荷长度不足 */
#define NAKRH_NONFIN   5u   /* 写 float 区但值非有限 (NaN/Inf) */
#define NAKRH_BUDGET   6u   /* START 时发现程序超预算 (F11 毒药表兜底) */
#define NAKRH_FIDX     7u   /* force: wire 号越界 */
#define NAKRH_FMODE    8u   /* force: mode 不是 0/1 */
#define NAKRH_FFIN     9u   /* force: 强制值为 NaN/Inf */
#define NAKRH_PMODE   10u   /* persist: mode 不是 0/1 */

/* 0x20 READ [addr:u32] → ACK [val:u32] */
static void h_read_w1(const uint8_t *p, uint32_t n)
{
    if (n < 4) { g_shm_rd_nak++; g_nak_last = NAKRH_SHORT; nak("need addr"); return; }
    uint32_t a = get32(p);
    if (!eng_valid_addr(a)) { g_shm_rd_nak++; g_nak_last = NAKRH_ADDR; nak("bad addr"); return; }
    uint32_t v = *(volatile uint32_t *)(uintptr_t)a;
    uint8_t r[4]; put32(r, v);
    g_shm_rd_ok++;
    ack(r, 4);
}

/* 0x22 READ_BURST [addr:u32][count:u16] → ACK [count×u32] */
static void h_read_burst_w1(const uint8_t *p, uint32_t n)
{
    if (n < 6) { g_shm_rd_nak++; g_nak_last = NAKRH_SHORT; nak("need addr+count"); return; }
    uint32_t a = get32(p);
    uint16_t c = get16(p + 4);
    if (!c || c > 256u) { g_shm_rd_nak++; g_nak_last = NAKRH_COUNT; nak("bad count"); return; }
    if (!eng_valid_range(a, (uint32_t)c * 4u)) {
        g_shm_rd_nak++; g_nak_last = NAKRH_RANGE; nak("bad range"); return;
    }
    /* 256×4 = 1024B ≤ FRAME_PAYLOAD_MAX(6150) —— 单帧放得下, S3 T19 同口径 */
    static uint8_t r[1024];
    for (uint32_t i = 0; i < c; i++) {
        uint32_t v = *(volatile uint32_t *)(uintptr_t)(a + i * 4u);
        put32(r + i * 4u, v);
    }
    g_shm_rd_ok++;
    ack(r, (uint32_t)c * 4u);
}

/* 0x21 WRITE [addr:u32][value:u32] → ACK [addr:u32] (回显地址, 便于脚本确认) */
static void h_write_w1(const uint8_t *p, uint32_t n)
{
    if (n < 8) { g_shm_wr_nak++; g_nak_last = NAKRH_SHORT; nak("need addr+value"); return; }
    uint32_t a = get32(p), v = get32(p + 4);
    if (!eng_valid_addr(a)) { g_shm_wr_nak++; g_nak_last = NAKRH_ADDR; nak("bad addr"); return; }
    if (!eng_write_allowed(a, v)) {
        g_shm_wr_nak++; g_nak_last = NAKRH_NONFIN; nak("non-finite rejected"); return;
    }
    *(volatile uint32_t *)(uintptr_t)a = v;
    __asm__ volatile("dsb" ::: "memory");
    uint8_t r[4]; put32(r, a);
    g_shm_wr_ok++;
    ack(r, 4);
}

/* 0x23 WRITE_BURST [addr:u32][count:u16][count×u32] → ACK [addr:u32]
 * ★ P1b 语义: **先全量预检, 再落笔** —— 否则写到第 5 个字发现 NaN 时,
 *   前 4 个字已经写进去了, 表处于半新半旧的破状态 (比"整体拒绝"危险得多)。 */
static void h_write_burst_w1(const uint8_t *p, uint32_t n)
{
    if (n < 8) { g_shm_wr_nak++; g_nak_last = NAKRH_SHORT; nak("need addr+count+data"); return; }
    uint32_t a = get32(p);
    uint16_t c = get16(p + 4);
    if (!c || c > 256u) { g_shm_wr_nak++; g_nak_last = NAKRH_COUNT; nak("bad count"); return; }
    if (n < 6u + (uint32_t)c * 4u) { g_shm_wr_nak++; g_nak_last = NAKRH_SHORT; nak("short"); return; }
    if (!eng_valid_range(a, (uint32_t)c * 4u)) {
        g_shm_wr_nak++; g_nak_last = NAKRH_RANGE; nak("bad range"); return;
    }
    const uint8_t *d = p + 6;
    for (uint32_t i = 0; i < c; i++) {
        uint32_t v = get32(d + i * 4u);
        if (!eng_write_allowed(a + i * 4u, v)) {
            g_shm_wr_nak++; g_nak_last = NAKRH_NONFIN; nak("non-finite rejected"); return;
        }
    }
    for (uint32_t i = 0; i < c; i++) {
        *(volatile uint32_t *)(uintptr_t)(a + i * 4u) = get32(d + i * 4u);
    }
    __asm__ volatile("dsb" ::: "memory");
    uint8_t r[4]; put32(r, a);
    g_shm_wr_ok++;
    ack(r, 4);
}

/* 0x11 START — 三处语义必须保留 (S3 main.c:895-921) */
static void h_start_w1(void)
{
    /* ① F11 兜底: persist 恢复的历史毒药表 (超预算程序) 必须拒 START,
     *    否则上电即进"锁死死循环"—— 而 PC 侧只会看到引擎卡住, 查不到原因。 */
    uint16_t nr = SHM_U16(g_shm, OFF_CTRL_N_ROUTES);
    if (nr) {
        uint32_t b = engine_prog_budget((const uint8_t *)(g_shm + OFF_ROUTE_TABLE), nr);
        if (b > EXEC_DEPLOY_BUDGET) {
            g_start_nak++; g_nak_last = NAKRH_BUDGET;
            nak("prog exceeds budget; redeploy");
            return;
        }
    }
    /* ② OA13 幂等: 只在 STOP→RUN 转变时清统计。
     *    已 RUN 再收 START (热重载 deploy 默认补发一条 0x11) 必须**不清零**,
     *    否则 T4 "心跳连续" 判据 (samples 单调增长证明未停机) 被误伤。 */
    uint8_t was_run = SHM_U8(g_shm, OFF_CTRL_ENGINE_RUN);
    if (!was_run) {
        stats_reset();
        g_timing_resets++;
    }

    /* ③ ★ W3: 顺序域 arm —— **必须在 ENGINE_RUN=1 之前做完**。
     *   ★★ 顺序理由 (单核 H723 与 S3 双核的差别, 必须说清):
     *     S3 是 core0 写 / core1 扫, 靠 `memw` 排序, 顺序是"先置 RUN 再写 ctrl"。
     *     H723 是**单核**: 主循环置 RUN 的下一拍 ISR 就会扫 seq —— 若先置 RUN,
     *     ISR 可能在本函数还没写完 ctrl 时就推进了步号, 随后本函数又把
     *     `step_cur=0` / 镜像 `1.0` 覆盖回去 ⇒ **一次真实的推进被静默吞掉**。
     *     ⇒ 本平台改为"先把 ctrl 与镜像备好, 最后一步才开 RUN 门", 竞争窗口归零。
     *     (这是"照搬语义、不照搬时序"——语义逐字同 S3, 时序按单核事实重排。)
     *
     *   ★★★ 移植遗漏修正 (对照 S3 main/main.c:914-920 发现的**真缺陷**):
     *     S3 在这里**还要把步号镜像 wire 写成 1.0**:
     *         if (sc[i].out_wire < MAX_WIRES) wm[sc[i].out_wire] = 1.0f;
     *     第一版 H723 移植漏了这句, 后果是: 停在 step0 期间, out_wire 上是
     *     **上一个程序留下的残值** (实测是 boot profile 的 0.01)。
     *     危害两层:
     *       · 语义层: "步号镜像"宣称反映当前步号, 实际显示别的程序的垃圾 ——
     *         正是本项目"宣称≠实现"铁律要消灭的那类东西;
     *       · 功能层: 译码路由若写 `CMP(步号 < 1.5)` 或 DIRECT, 会在第一步期间
     *         读到残值并输出错误的执行器信号 (而 CMP(>1.5) 只是**恰好**正确)。
     *     ★ 这个缺陷之所以能被抓到, 全靠 T28.B1 的"起点快照"判据 (期望 1.0,
     *       实测 0.01) —— 如果只测"推进后对不对", 它会一直藏着。 */
    {
        uint8_t  nq = SHM_U8(g_shm, OFF_CTRL_N_SEQ);
        if (nq > MAX_SEQ_INST) nq = MAX_SEQ_INST;
        volatile float *wm = (volatile float *)(void *)(g_shm + OFF_WIRE_MAP);
        for (uint8_t k = 0; k < nq; k++) {
            uint8_t *c = (uint8_t *)(g_shm + OFF_SEQ_CTRL) + (uint32_t)k * 16u;
            *(volatile uint16_t *)(c + 4) = 0u;     /* step_cur  = 0 (从第 1 步起) */
            *(volatile uint32_t *)(c + 12) = 0u;    /* step_tick = 0 */
            c[9] = 1u;                              /* run = 1 */
            uint16_t ow = *(volatile uint16_t *)(c + 6);
            if (ow < MAX_WIRES) {
                /* 镜像语义: 停在 step0 ⇒ 对外值 1.0 (步号 1 起) */
                uint32_t msk = SHM_U32(g_shm, OFF_FORCE_MASK + ((uint32_t)ow >> 5) * 4u);
                if (!(msk & (1u << ((uint32_t)ow & 31u)))) wm[ow] = 1.0f;
            }
            g_seq_armed++;                          /* 观测: 实际 arm 了几个实例 */
        }
    }

    SHM_U8(g_shm, OFF_CTRL_ENGINE_RUN) = 1;
    __asm__ volatile("dsb" ::: "memory");
    g_start_ok++;
    ack(NULL, 0);
}

/* 0x12 STOP — 停机进安全态 (P1-2): 停机 ≠ 保持最后一拍输出 */
static void h_stop_w1(void)
{
    SHM_U8(g_shm, OFF_CTRL_ENGINE_RUN) = 0;
    /* ★ W3: 步号**冻结保持** (不清零, 不推进) —— 与 ISR 的 `if (gate && RUN)` 门一致。
     *   PLC 停机时"当前在第几步"必须是可读的现场信息; 清零会把它变成不可恢复的丢失。
     *   恢复靠下一次 START (那里做 arm: step_cur=0 + 镜像=1.0)。 */
    __asm__ volatile("dsb" ::: "memory");
    g_safe_gpio_mask = SHM_U32(g_shm, OFF_CTRL_GPIO_MASK);
    eng_outputs_safe();
    g_safe_calls++;
    g_stop_ok++;
    ack(NULL, 0);
}

/* 0x13 RESET — 收敛的冷启动清理 (新增域必须登记到 cold_start_reset) */
static void h_reset_w1(void)
{
    cold_start_reset();          /* 整段 memset, 天然覆盖 FORCE_MASK/VAL (W2.1) */
    g_force_clears++;            /* ★ 用同一计数证明 RESET 真的清过 force */
    /* ★ W3: RESET 清空了 N_SEQ 与整张 ctrl 块 → "已 arm"标记必须一起失效,
     *   否则下一次 START 会走"已 RUN 分支"(不清步号)—— 而步号此刻已经不存在了。
     *   这是"运行态标记必须与它描述的数据同生命周期"的又一例。 */
    g_seq_armed = 0;
    /* ★ W2.4: RESET 清空运行表 → 未落盘的快照语义上已失效。
     *   不清会导致"下次 0x43 落盘"把 RESET 之前的旧配置写进 flash 并恢复 ——
     *   (S3 persist_clear_dirty 的同款理由)。Flash 里已有的数据**不动**
     *   (RESET 是运行态复位, 不是擦除持久化配置; 要清持久化得显式重新 deploy 空程序)。 */
    g_persist_dirty = 0;
    /* ★★ 移植遗漏修正 (由 S3 回归 T9 实测发现): S3 的 RESET 会**清 timing 统计**
     *   (OA13 语义: "统计只反映本次 RUN 段")。H723 的统计量住在 DTCM 的 C 全局里,
     *   而 cold_start_reset 只 memset SHM ⇒ 不清它的话 `samples` 会**跨 RESET 继续增长**
     *   (实测 176437 → 180039), T9 的"样本复位"判据直接失败。
     *   ⇒ RESET 必须显式补一次 stats_reset()。 */
    stats_reset();
    g_timing_resets++;
    /* ★★ 审计 #1: RESET 也必须进安全态 —— 与 0x12 STOP 同理, 而且更隐蔽:
     *   cold_start_reset() 把 SHM 整段清零 (含 ENGINE_RUN=0 与 HIL 的 duty 镜像),
     *   但**物理寄存器 TIM3_CCR1 不在 SHM 里** —— 它保留着上一次的占空比。
     *   ⇒ 只清状态不清输出 = "看起来停机了, 执行器还在动"。
     *   ★ 这条是"变量归零 ≠ 物理输出归零"的标本: 两者住在不同地址空间
     *     (SHM/DTCM vs 外设寄存器), 必须各自显式处理 —— 所以才有
     *     eng_outputs_safe() 这个统一入口, 而不是在每个清零路径里各写一遍。 */
    eng_outputs_safe();
    g_safe_calls++;              /* 契约: 本计数 = eng_outputs_safe 被调用次数 (含 STOP 与 RESET) */
    g_reset_ok++;
    ack(NULL, 0);
}

/* ══════════ W2.3 — 0x24 FORCE (强制/释放 wire) ══════════
 * 载荷 [idx:u16][mode:u8][val:f32]   mode 0=释放 1=强制
 * ★ 逐字搬 S3 的语义 (main.c:204-232), 三处必须保留:
 *    ① mode==1 时必须校验 val 有限 (Inf 会被拍首覆写**每拍**钉进 WIRE_MAP,
 *       无法像一次性写那样被后续计算自愈 —— 比 0x21 的同类检查更要紧)
 *    ② 强制必须**同时写** FORCE_VAL 与 WIRE_MAP (OA9): 只写 WIRE_MAP 会在
 *       下一拍拍首被 FORCE_VAL(0) 抹掉, 而"强制 0"恰好是常见用例 → 判据盲区
 *    ③ 释放时清 FORCE_VAL 残留 (防下次置位前的陈旧值)
 * ★ 这里是**立即生效**的: 拍首覆写下一拍就会用上新值, 不需要 deploy/重载。 */
static void h_force_w2(const uint8_t *p, uint32_t n)
{
    if (n < 7) { g_force_nak++; g_nak_last = NAKRH_SHORT; nak("force: short"); return; }
    uint16_t idx; uint8_t mode; uint32_t vb;
    memcpy(&idx, p, 2);
    mode = p[2];
    vb = get32(p + 3);
    if (idx >= MAX_WIRES)   { g_force_nak++; g_nak_last = NAKRH_FIDX;  nak("force: wire OOR"); return; }
    if (mode > 1)           { g_force_nak++; g_nak_last = NAKRH_FMODE; nak("force: bad mode"); return; }
    if (mode == 1 && !is_finite_bits(vb)) {
        g_force_nak++; g_nak_last = NAKRH_FFIN; nak("force: non-finite"); return;
    }

    volatile uint32_t *fm = (volatile uint32_t *)(void *)(g_shm + OFF_FORCE_MASK);
    volatile float    *fv = (volatile float    *)(void *)(g_shm + OFF_FORCE_VAL);
    volatile float    *wm = (volatile float    *)(void *)(g_shm + OFF_WIRE_MAP);

    if (mode == 1) {
        float f; memcpy(&f, &vb, 4);
        fm[idx >> 5] |= (1u << (idx & 31u));
        fv[idx] = f;              /* ★ OA9: 这一行才是"强制值真的生效"的原因 */
        wm[idx] = f;              /* 立即写一次, 使"下一拍前"读到的也是新值 */
        g_force_last_idx = idx;
        g_force_last_val = vb;
        g_force_set++;
    } else {
        fm[idx >> 5] &= ~(1u << (idx & 31u));
        fv[idx] = 0.0f;           /* 清残留 */
        g_force_rel++;
    }
    __asm__ volatile("dsb" ::: "memory");
    ack(NULL, 0);
}

/* ══════════ W2.4 — 0x43 PERSIST_STATUS (查询/落盘) ══════════
 * 载荷: 空 = 只查询; [mode:u8] mode=1 = 查询并**尝试落盘当前表**
 *
 * ★ 与 S3 的 0x43 差异 (必须说清楚, 否则"逐字沿用"是假的):
 *   S3 的 0x43 是纯查询 —— 因为 S3 的落盘是**后台任务**(N1: deploy 登记 dirty,
 *   persist_task 异步 flush)。H723 是裸机单循环, 没有后台任务, 所以:
 *     · 查询语义保留 (前 8 字节布局也保留: ok / nr / np / ns / flags)
 *     · **落盘变成显式请求** (载荷带 mode=1) —— 由 PC 在引擎 STOP 后主动触发。
 *   这不是偷懒: 裸机上"什么时候可以阻塞 1~4 秒"只有 PC 知道 (它才是发 0x12 STOP
 *   的那一方)。让固件自己找窗口反而会引入"什么时候会停顿"的不确定性。
 *
 * 响应布局 (前 8B 与 S3 同, 尾部按 H723 需要追加):
 *   [0]    ok        (1 = 至少一份副本有效)
 *   [1:2]  n_routes  (u16)
 *   [3:4]  n_params  (u16)
 *   [5:6]  n_states  (u16)
 *   [7]    flags     (bit0 = dirty 有未落盘配置 / bit1 = 本次尝试落盘成功)
 *   [8:11] seq       (u32: 当前有效副本的最大序号)
 *   [12]   ab_valid  (位图 1=A 2=B)
 *   [13]   active    (0=下次写 A / 1=下次写 B)
 *   [14:15] last_err (u16: persist/flash 错误码, 0=无)
 *   [16:19] crc      (u32: 当前有效副本的 CRC32; 供工具与独立计算比对)
 *   [20:23] writes   (u32: 累计成功落盘次数)
 *   = 24 字节
 * ★ 载荷 [0] 的位置放 mode 而不是 ok: 查询用空载荷, 老 PC 不会误触落盘。 */
static void h_persist_w2(const uint8_t *p, uint32_t n)
{
    g_persist_cmds++;
    uint8_t mode = (n >= 1u) ? p[0] : 0u;
    int save_rc = 2;   /* 2 = 本次未请求落盘 */

    if (mode == 1) {
        g_persist_saves++;
        save_rc = persist_save(g_shm);
        if (save_rc == 1) g_persist_skip_run++;      /* 因 RUN 跳过 (非错误) */
        else if (save_rc < 0) g_persist_nak++;
    } else if (mode > 1u) {
        g_persist_nak++;
        g_nak_last = NAKRH_PMODE;
        nak("persist: bad mode");
        return;
    }

    PersistInfo_t info;
    persist_probe(&info);
    g_persist_ab_valid = info.ab_valid;
    g_persist_seq_a = info.seq_a;
    g_persist_seq_b = info.seq_b;
    g_persist_crc_a = info.crc_a;
    g_persist_crc_b = info.crc_b;

    uint32_t seq = (info.seq_a > info.seq_b) ? info.seq_a : info.seq_b;
    uint32_t crc = (info.seq_a >= info.seq_b) ? info.crc_a : info.crc_b;

    uint8_t r[24];
    memset(r, 0, sizeof(r));
    r[0] = (info.ab_valid != PERSIST_AB_NONE) ? 1u : 0u;
    /* ★★ 审计发现 D 修复: 这里原本报的是 `g_active_routes` / SHM 的 N_PARAMS/N_STATES
     *   —— 也就是**当前 ACTIVE 表**的条数。但那不是 0x43 的语义: 0x43 是
     *   PERSIST_STATUS, 它该回答的是"**flash 里持久化了几条**"。
     *   两者在"deploy 了新程序但还没落盘"时会**合法地不同** —— 报当前值等于
     *   谎报持久化状态 (PC 会以为新程序已经存好了)。
     *   ★ 更糟的是 `g_active_routes` 当时还是**陈旧**的 (只在启动/reinit 更新,
     *     deploy 路径漏了) —— 实测 deploy 8 条后它仍报 128。两处错叠在一起。
     *   ⇒ 改用 persist_probe 已经填好的 `info.*` —— 它直接来自 flash 头,
     *     是"持久化内容"的**唯一权威来源**, 不依赖任何内存全局的新鲜度。 */
    r[1] = (uint8_t)(info.n_routes & 0xFFu);
    r[2] = (uint8_t)((info.n_routes >> 8) & 0xFFu);
    r[3] = (uint8_t)(info.n_params & 0xFFu);
    r[4] = (uint8_t)((info.n_params >> 8) & 0xFFu);
    r[5] = (uint8_t)(info.n_states & 0xFFu);
    r[6] = (uint8_t)((info.n_states >> 8) & 0xFFu);
    r[7] = (uint8_t)((g_persist_dirty ? 1u : 0u) | (save_rc == 0 ? 2u : 0u));
    put32(r + 8,  seq);
    r[12] = info.ab_valid;
    r[13] = (uint8_t)info.active;
    r[14] = (uint8_t)(g_persist_last_err & 0xFFu);
    r[15] = (uint8_t)((g_persist_last_err >> 8) & 0xFFu);
    put32(r + 16, crc);
    put32(r + 20, g_persist_writes);

    /* ★ 空闲窗口自动落盘 (T15/T26): 上位机**纯查询**(mode==0)且报了 dirty ⇒ 登记一次
     *   "落盘请求"; 真正的**裁决**在主循环 (那里才知道引擎跑不跑)。
     *   ★★ 刻意**不在登记处**就排除 RUN 态 (第一版这么写, 结果 g_persist_auto_gate
     *     恒为 0 —— 一个**不可能失败**的判据, 等于没测)。把裁决留给主循环之后,
     *     T26 "RUN 中被问两次都放弃" 这件事本身就有了可读回的证据。
     *   ★ 顺序是硬要求: 只登记, 不落盘 —— 本 ACK 必须**先完整移出**
     *     (uart1_write 等 TC 才返回), 主循环随后才动手。反过来的顺序 (先擦再答)
     *     会把这帧 ACK 卡在 erase 的 1.5s 里 → PC 侧第一帧就是丢帧, wait_flush 直接失败。 */
    if (mode == 0u && g_persist_dirty) {
        g_persist_auto = 1;
    }
    ack(r, 24);
}

/* ══════════ deploy 自检 (阶段 3.2) ══════════
 * 为什么需要它: CH340 还没接线, 无法从 PC 侧验证 deploy。自检把**同一个 h_deploy**
 * 用合成载荷驱动一遍 —— 验证的是真代码路径, 不是复制一份逻辑出来单独测。
 * ★ 分工要说清楚: 自检**证不了**"帧能收对"(那是 transport 的事, 已单独验证);
 *   它证的是"载荷合法时能部署、载荷错误时**逐类**被正确拒绝"。
 * ★ 判据全部可失败: 每例都对比调用前后的 (ok, nak) 计数器 —— 只数成功会让
 *   "全部被拒"看起来像"没部署过"; 只数失败会让"全部放行"看起来像"很严格"。 */
#define DSELFTEST_CASES 9
OBS uint32_t g_dst_case[DSELFTEST_CASES];   /* 每例: 0=未跑 1=符合预期 2=不符 */
OBS uint32_t g_dst_done = 0;

static inline void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }

static void put_route(uint8_t *d, uint8_t op, uint8_t st, uint8_t si, uint8_t dch,
                      uint8_t div, uint16_t pidx, uint16_t soff, uint16_t w2, uint8_t flags)
{
    memset(d, 0, 16);
    d[0] = st;  d[1] = si;  d[2] = DST_WIRE;  d[3] = dch;
    d[4] = op;  d[5] = flags;
    put16(d + 6, pidx);  put16(d + 8, soff);  put16(d + 10, 0);  put16(d + 12, w2);
    d[14] = div;  d[15] = 0;
}

/* 构造"例 0 的合法载荷": NR 条三档 DIRECT + NP×4 个有限浮点参数。 */
static void ds_build_valid(uint8_t *buf, uint16_t NR, uint16_t NP, uint16_t NS)
{
    memset(buf, 0, 6u + ((size_t)NR + NP + NS) * 16u);
    put16(buf, NR); put16(buf + 2, NP); put16(buf + 4, NS);
    for (uint16_t i = 0; i < NR; i++)
        put_route(buf + 6 + (size_t)i * 16u, OP_DIRECT,
                  (uint8_t)(i % 3), (uint8_t)(i % 64), (uint8_t)(i % MAX_WIRES),
                  (uint8_t)(i % 3), (uint16_t)(i % NP), 0, 0, ROUTE_FLAG_ACTIVE);
    for (int i = 0; i < NP * 4; i++)
        put32(buf + 6 + (size_t)NR * 16u + (size_t)i * 4u, 0x3F800000u);   /* 1.0f */
}

/* deploy 自检: 用合成载荷驱动**同一个** h_deploy 代码路径 (9 例, 每例都要能失败)。
 * ★ unused 属性同上: 只在 -DDCL_DEPLOY_SELFTEST=1 时被调用。 */
__attribute__((unused))
static void deploy_selftest(void)
{
    static uint8_t buf[6 + (MAX_ROUTES + 8 + 8) * 16u];    /* 2310 B, 静态区不占栈 */
    const uint16_t NR = MAX_ROUTES, NP = 8, NS = 8;
    const uint32_t len = 6u + ((uint32_t)NR + NP + NS) * 16u;
    uint32_t ok0, nak0;

    for (int i = 0; i < DSELFTEST_CASES; i++) g_dst_case[i] = 0;

    /* ---- 例 0: 合法三档程序 → 必须受理 ---- */
    ds_build_valid(buf, NR, NP, NS);
    ok0 = g_deploy_ok; nak0 = g_deploy_nak;
    h_deploy(buf, len);
    g_dst_case[0] = (g_deploy_ok == ok0 + 1u && g_deploy_nak == nak0) ? 1u : 2u;

    /* ---- 例 1: dst 冲突 (路由1 的 dst_channel 改成与路由0 相同) ----
     * 两条路由写同一个 wire → 结果取决于表序 = 非确定性, 必须下载期拒绝 */
    ds_build_valid(buf, NR, NP, NS);
    buf[6u + 16u + 3u] = buf[6u + 3u];
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, len);
    g_dst_case[1] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 2: SRC_HMI → 必须拒 (H723 未实现该源, 放行 = 静默给恒 0 的假信号) ---- */
    ds_build_valid(buf, NR, NP, NS);
    buf[6u + 0u] = (uint8_t)SRC_HMI;
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, len);
    g_dst_case[2] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 3: PID(stateful) 挂 0 号 state 槽 (= "无槽") → 必须拒 (ISR 会传 NULL) ---- */
    ds_build_valid(buf, NR, NP, NS);
    buf[6u + 4u] = (uint8_t)OP_PID;          /* op */
    put16(buf + 6u + 8u, 0u);                /* state_offset = 0 */
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, len);
    g_dst_case[3] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 4: div=3 (掩码外) → 必须拒 ---- */
    ds_build_valid(buf, NR, NP, NS);
    buf[6u + 14u] = 3u;
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, len);
    g_dst_case[4] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 5: 非法 op (0x1F) → 必须拒 ---- */
    ds_build_valid(buf, NR, NP, NS);
    buf[6u + 4u] = 0x1Fu;
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, len);
    g_dst_case[5] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 6: 参数非有限 (指数全 1) → 必须拒 (NaN 会经运算传播成坏输出) ---- */
    ds_build_valid(buf, NR, NP, NS);
    put32(buf + 6u + (size_t)NR * 16u, 0x7F800000u);   /* +Inf */
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, len);
    g_dst_case[6] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 7: 载荷长度不足 (声称 128 条却只给 4 字节) → 必须拒 ---- */
    ds_build_valid(buf, NR, NP, NS);
    ok0 = g_deploy_ok; nak0 = g_deploy_nak; h_deploy(buf, 10u);
    g_dst_case[7] = (g_deploy_nak == nak0 + 1u && g_deploy_ok == ok0) ? 1u : 2u;

    /* ---- 例 8: 全 NONACTIVE → 受理, 但写入 **0 条**
     *   (边界: "空程序"是合法的 —— 是停止运行的手段, 不该被当成错误) ---- */
    ds_build_valid(buf, NR, 0u, 0u);
    for (uint16_t i = 0; i < NR; i++) buf[6u + (size_t)i * 16u + 5u] = 0u;   /* flags 清 ACTIVE */
    ok0 = g_deploy_ok; nak0 = g_deploy_nak;
    h_deploy(buf, 6u + (uint32_t)NR * 16u);
    g_dst_case[8] = (g_deploy_ok == ok0 + 1u && g_deploy_nak == nak0 && g_deploy_routes == 0u)
                    ? 1u : 2u;

    g_dst_done = 1;
}

static void proto_dispatch(uint8_t cmd, const uint8_t *p, uint32_t n)
{
    g_cmd_count++;
    g_cmd_last = cmd;
    switch (cmd) {
        case CMD_GET_VERSION:   h_get_version(); break;
        case CMD_DEPLOY:        h_deploy(p, n); break;
        case CMD_ENGINE_STATUS: h_engine_status(); break;
        /* ---- W1: 运行控制 ---- */
        case CMD_START:         h_start_w1(); break;
        case CMD_STOP:          h_stop_w1(); break;
        case CMD_RESET:         h_reset_w1(); break;
        /* ---- W1: SHM 读写 ---- */
        case CMD_READ:          h_read_w1(p, n); break;
        case CMD_READ_BURST:    h_read_burst_w1(p, n); break;
        case CMD_WRITE:         h_write_w1(p, n); break;
        case CMD_WRITE_BURST:   h_write_burst_w1(p, n); break;
        /* ---- W2: Force ---- */
        case CMD_FORCE:         h_force_w2(p, n); break;
        /* ---- W2.4: persist ---- */
        case CMD_PERSIST:       h_persist_w2(p, n); break;
        /* ---- W3: 顺序域 ---- */
        case CMD_SEQ_DEPLOY:    h_seq_deploy(p, n); break;
        /* ---- W4: 通信域 (Modbus RTU 从站) ---- */
        case CMD_MB_INJECT:     h_mb_inject(p, n); break;
        case CMD_MB_RESP:       h_mb_resp(); break;
        case CMD_MB_CFG:        h_mb_cfg(p, n); break;
        /* ---- W5: macro 字节码 VM ---- */
        case CMD_MACRO:         h_macro(p, n); break;
        case CMD_MACRO_UPLOAD:  h_macro_upload(p, n); break;
        case CMD_MACRO_CTRL:    h_macro_ctrl(p, n); break;
        /* ---- W5: 外设域零接线自检 ---- */
        case CMD_PIN_SELFTEST:  h_pin_selftest(p, n); break;
        case CMD_ADC_SCAN:      h_adc_scan(p, n); break;
        /* ★ 未实现的命令**显式拒绝**(NAK 带原因), 而不是静默丢弃或假装成功。
         *   静默丢弃的后果是 PC 端只能看到 TIMEOUT —— 分不清"固件挂了"还是
         *   "这命令没实现", 正是 S3 审计里 N2 记录过的那类缺陷。 */
        default: nak("bad cmd"); break;
    }
}

/* 主循环调用: 排空串口 → 喂解析器 → 完整帧则分发 */
static void proto_poll(void)
{
    uint8_t b;
    while (uart1_rx_pop(&b)) {
        g_uart_rx_bytes++;
        int r = fp_feed(&s_parser, b);
        if (r == 1) {
            g_frame_ok++;
            /* 自检期只计数**不分发** —— 否则回环收到的请求会被再次应答,
             * TX→RX 再 TX… 变成回声风暴 */
            if (s_selftest_active) { g_selftest_frames++; continue; }
            proto_dispatch(s_parser.cmd, s_parser.payload, s_parser.payload_len);
        } else if (r < 0) {
            g_frame_bad++;
        }
    }
    g_uart_ore    = uart1_ore_count();
    g_uart_drop   = uart1_drop_count();
    g_uart_isr_n     = uart1_isr_count();
    g_uart_isr_ore   = uart1_isr_ore();
    g_uart_fe        = uart1_fe_count();
    g_uart_ne        = uart1_ne_count();
    g_uart_push      = uart1_push_count();
    g_uart_last_isr  = uart1_last_isr();
    g_uart_last_byte = uart1_last_byte();
    g_uart_irq_en = uart1_irq_enabled();   /* ★ 中断使能位必须每轮刷新 (A1 判据) */
}

/* 回环自检 (需 PA9↔PA10 短接): 发一帧 PC→MCU 请求, 看它能不能从 RX 回来并被
 * CRC 校验通过。这是**唯一能证明 RX 通路 + CRC + 解析器真的工作**的内部手段;
 * 配合 LA 抓 TX 波形, 形成"外部确证发出去的字节 + 内部确证收回来的字节"闭环。
 * ★ unused 属性: 本函数只在 -DDCL_UART_SELFTEST=1 时被调用; 不加属性会在默认构建里
 *   产生一条 -Wunused-function —— 而"噪声警告"正是把 A1 那条真警告埋掉的原因
 *   (25 条警告里 20 条是这种无害的), 所以本项目对"条件使用"的函数一律显式标注。 */
__attribute__((unused))
static void proto_selftest(void)
{
    uint8_t req[6];
    req[0] = FRAME_SYNC_PC2MCU;
    req[1] = CMD_GET_VERSION;
    req[2] = 0; req[3] = 0;                        /* 无载荷 */
    uint16_t crc = crc16_ccitt(req + 1, 3);
    req[4] = (uint8_t)(crc & 0xFFu);
    req[5] = (uint8_t)(crc >> 8);

    s_selftest_active = 1;
    uart1_write(req, 6);
    uint32_t t0 = g_tick_count;                    /* 6B@115200 ≈ 0.52ms → 3ms 上限足够 */
    while ((g_tick_count - t0) < 30u) {
        proto_poll();
        if (g_selftest_frames) break;
    }
    s_selftest_active = 0;
    g_selftest_state = g_selftest_frames ? 1u : 2u;
}

/* ══════════ 观测变量锚定 —— 结构性防"被回收" ══════════
 * ★ 已踩过**三次**的同一个坑: 只被静态初始化、代码里无人读也无人写的全局,
 *   会被 -fdata-sections + --gc-sections 整段回收 → 从符号表消失 → 外部读不到。
 *     ① 阶段 2: g_isr_itcm     (第一版只在声明处初始化)
 *     ② 阶段 3.1: g_selftest_state (UART_SELFTEST=0 时无人写)
 *     ③ 阶段 3.1: g_banner_count   (BOOT_BANNER=0 && 周期=0 时无人写)
 *   逐个人工绕不可持续 —— 这里做**一次统一锚定**: 把每个观测变量读一遍。
 *   读操作是 volatile 的, 链接器看得见引用, 于是一个都不会被回收。
 *   ★ 纪律: **新增观测变量必须在这里加一行**, 否则它可能悄悄从符号表消失。 */
static void obs_anchor(void)
{
    volatile uint32_t sink = 0;
    sink ^= (uint32_t)g_boot_status;      sink ^= (uint32_t)g_stage;
    sink ^= g_tick_count;                 sink ^= g_clock_hclk;
    sink ^= g_isr_itcm;                   sink ^= g_shm_ok;
    sink ^= g_shm_addr;                   sink ^= g_scan_itcm_addr;
    sink ^= g_scan_flash_addr;            sink ^= g_reinit_done;
    sink ^= g_vtor;                       sink ^= g_vtor_want;
    sink ^= g_table_ck;                   sink ^= g_active_routes;
    sink ^= g_guard_ok;                   sink ^= g_guard_bad_off;
    sink ^= g_bucket_ck;                  sink ^= g_bucket_zero_slots;
    sink ^= g_engine_gate;                sink ^= g_engine_sel;
    sink ^= g_n_routes;                   sink ^= g_table_profile;
    sink ^= g_reinit;                     sink ^= g_stat_reset;
    sink ^= g_pa9_enable;                 sink ^= g_pa9_div;
    sink ^= g_eng_ck;                     sink ^= g_eng_sel_used;
    sink ^= g_eng_n_used;                 sink ^= g_eng_cyc_last;
    sink ^= g_eng_cyc_min;                sink ^= g_eng_cyc_max;
    sink ^= (uint32_t)g_eng_cyc_sum;      sink ^= g_eng_n;
    sink ^= g_eng_div0;                   sink ^= g_eng_routes_last;
    sink ^= (uint32_t)g_eng_routes_total; sink ^= g_eng_ticks;
    sink ^= g_isr_cyc_last;               sink ^= g_isr_cyc_min;
    sink ^= g_isr_cyc_max;                sink ^= (uint32_t)g_isr_cyc_sum;
    sink ^= g_isr_n;                      sink ^= g_per_cyc_last;
    sink ^= g_isr_overrun;                /* 审计二级 #5 的观测量 (不读会被回收) */
    sink ^= g_per_cyc_min;                sink ^= g_per_cyc_max;
    sink ^= g_per_prev;                   sink ^= g_dwt_overhead;
    sink ^= g_per_glitch_n;
    sink ^= g_cal_n1000;                  sink ^= g_icache_req;
    sink ^= g_icache_on;                  sink ^= g_ccr_before;
    sink ^= g_ccr_after;                  sink ^= g_scan_mode;
    sink ^= g_uart_brr;                   sink ^= g_uart_rx_bytes;
    sink ^= g_uart_irq_en;                sink ^= g_uart_tx_bytes;
    sink ^= g_eng_cyc_first;              sink ^= g_isr_cyc_first;
    sink ^= g_frame_selftest;             sink ^= g_uart_ore;
    sink ^= g_uart_drop;                  sink ^= g_frame_ok;
    sink ^= g_frame_bad;                  sink ^= g_cmd_count;
    sink ^= g_cmd_last;                   sink ^= g_nak_count;
    sink ^= g_banner_count;               sink ^= g_selftest_state;
    sink ^= g_uart_isr_n;   sink ^= g_uart_isr_ore;  sink ^= g_uart_fe;
    sink ^= g_uart_ne;      sink ^= g_uart_push;     sink ^= g_uart_last_isr;
    sink ^= g_uart_last_byte;
    sink ^= g_selftest_frames;
    sink ^= g_deploy_ok;                  sink ^= g_deploy_nak;
    sink ^= g_deploy_routes;              sink ^= g_deploy_budget;
    sink ^= g_deploy_seq;                 sink ^= g_applied_seq;
    sink ^= g_reload_count;               sink ^= g_reload_cyc;
    sink ^= g_reload_lat;                 sink ^= g_deploy_set_tick;
    sink ^= g_dst_case[0];                sink ^= g_dst_case[DSELFTEST_CASES - 1];
    sink ^= g_dst_done;
    /* W1 观测面 */
    sink ^= g_shm_rd_ok;   sink ^= g_shm_rd_nak;
    sink ^= g_shm_wr_ok;   sink ^= g_shm_wr_nak;
    sink ^= g_nak_last;    sink ^= g_start_ok;
    sink ^= g_start_nak;   sink ^= g_stop_ok;
    sink ^= g_reset_ok;    sink ^= g_safe_calls;
    sink ^= g_safe_gpio_mask; sink ^= g_timing_resets;
    sink ^= g_engine_run_seen;
    /* W2 Force 观测面 */
    sink ^= g_force_set;   sink ^= g_force_rel;
    sink ^= g_force_nak;   sink ^= g_force_last_idx;
    sink ^= g_force_last_val; sink ^= g_force_clears;
    /* W2.4 persist 观测面 (main.c 侧) */
    sink ^= g_persist_cmds;    sink ^= g_persist_saves;
    sink ^= g_persist_skip_run; sink ^= g_persist_nak;
    sink ^= g_persist_ab_valid; sink ^= g_persist_seq_a; sink ^= g_persist_seq_b;
    sink ^= g_persist_crc_a;   sink ^= g_persist_crc_b;
    sink ^= g_persist_loaded_n; sink ^= g_persist_loaded_sec;
    sink ^= g_persist_loads;
    sink ^= g_persist_req;      sink ^= g_persist_req_cnt;
    sink ^= g_persist_auto;     sink ^= g_persist_auto_runs;
    sink ^= g_persist_auto_gate;
    sink ^= g_fl_err_stage;     sink ^= g_fl_err_sr1;
    sink ^= g_fl_err_cr1;       sink ^= g_fl_err_cnt;
    /* W2.4 persist 观测面 (persist.c 侧 —— 这些是 extern, 不读会被 gc-sections 回收) */
    sink ^= g_persist_save_ok;  sink ^= g_persist_save_fail;
    sink ^= g_persist_load_ok;  sink ^= g_persist_load_fail;
    sink ^= g_persist_load_seq; sink ^= g_persist_last_err;
    sink ^= g_persist_writes;   sink ^= g_persist_dirty;
    sink ^= g_persist_target;   sink ^= g_persist_erase_ok;
    sink ^= g_persist_erase_fail;
    /* W3 Sequencer 观测面 (不登记必被 --gc-sections 回收 → nm 找不到符号) */
    sink ^= g_seq_deploys;     sink ^= g_seq_nak;
    sink ^= g_seq_steps_sum;   sink ^= g_seq_writes;
    sink ^= g_seq_last_cur;    sink ^= g_seq_ticks;
    sink ^= g_seq_wrote_last;  sink ^= g_seq_max_cur;
    sink ^= g_seq_armed;
    /* W3 免串口协议帧钩子 (不登记会被 --gc-sections 回收) */
    sink ^= g_cmd_req;         sink ^= g_cmd_req_len;
    sink ^= g_cmd_req_cnt;     sink ^= g_cmd_req_last;
    /* 审计发现 H 的观测面 (engine.c 侧, 不读会被回收) */
    sink ^= g_safe_mask_nonzero;
    /* ★ P1/P2 拍内 I/O 观测面 (不读会被 --gc-sections 回收 —— 本项目已知族:
     *   第一版 g_selftest_state / g_isr_itcm 都这么从符号表里消失过)。
     *   g_safe_mask_oob  : GPIO_MASK 越界写次数 (定案② 的违规判据, 应恒 0)
     *   g_adc_sm_done    : 状态机完成转换数 (正向证据, 必须单调增)
     *   g_adc_sm_timeout : 状态机超时数 (应恒 0; 与上面那个成对读才分得清"没坏"与"没跑") */
    sink ^= g_safe_mask_oob;
    sink ^= g_adc_sm_done;     sink ^= g_adc_sm_timeout;
    /* ★ P3-A DO 输出面观测面 (不读会被 --gc-sections 回收) */
    sink ^= g_do_poll_n;     sink ^= g_do_write_n;
    sink ^= g_hil_out_n;
    /* 审计 #1 的观测面: 物理输出面 登记数 / 上次实际执行数 */
    sink ^= g_out_surfaces;    sink ^= g_safe_surfaces_ran;
    /* W4 通信域 */
    sink ^= g_mb_inject_ok;    sink ^= g_mb_nak;
    sink ^= g_mb_ticks;
    /* W5 macro VM */
    sink ^= g_macro_exec_ok;   sink ^= g_macro_upload_ok;
    sink ^= g_macro_nak;       sink ^= g_macro_ticks;
    /* W5 外设域 */
    sink ^= g_w5_ready;        sink ^= g_pin_selftest_n;
    (void)sink;                            /* 只要求"被引用", 不要求有意义的和 */
}

/* ══════════ 失败指示 ══════════ */
static void blink_error(int err)
{
    if (err < 0) err = -err;
    if (err == 0) err = 1;
    for (;;) {
        for (int i = 0; i < err; i++) {
            pin_set(TICK_PORT, TICK_BIT, 1);
            for (volatile uint32_t d = 0; d < 400000u; d++) { }
            pin_set(TICK_PORT, TICK_BIT, 0);
            for (volatile uint32_t d = 0; d < 400000u; d++) { }
        }
        for (volatile uint32_t d = 0; d < 3000000u; d++) { }
    }
}

void SystemInit(void)
{
    SCB_CPACR |= (0xFu << 20);                 /* FPU */
    __asm__ volatile("dsb; isb");
}

int main(void)
{
    /* ★★★ 向量表搬进 ITCM + VTOR 指过去 —— **必须在使能任何中断之前** (见 ld 里的说明)。
     *   动机 (2026-09-11 实测): VTOR 原值 = 0x08000000 (flash), 而 **sector erase 会 stall
     *   flash 取指** ⇒ 擦除期间**中断根本进不来** —— 100μs 拍被整段吞掉。
     *   "ISR 代码已在 ITCM"不够: **取向量这一步本身也要过 flash**。
     *   改法: 把 flash 里的向量表 (.isr_vector, 由 _siv/_eiv 界定) 拷进 ITCM 副本区
     *   (.itcm_vectors), 再把 SCB->VTOR 指过去 ⇒ 取向量零等待, 引擎的拍与 flash 操作解耦。
     *
     * ★ -DVTOR_ITCM=0 是**对照构建** (留在 flash, 即改前行为), 只为证明
     *   "擦除期间丢拍"这个判据真能失败 —— 详见 tools/h723_t26.py 头注释里的实测数字。
     *   交付默认恒为 1 (build.sh 每次显式传)。 */
    pin_out_init(TICK_PORT, TICK_BIT);   /* ★ 放在 #if 之外: 两份构建都要用它点灯报错,
                                          *   放进去会让对照组报 -Wunused-function */
#if VTOR_ITCM
    {
        uint32_t n4 = (uint32_t)((LSYM_ADDR(_eiv) - LSYM_ADDR(_siv)) / 4u);
        uint32_t cap4 = (uint32_t)((LSYM_ADDR(_evtor_itcm) - LSYM_ADDR(_vtor_itcm)) / 4u);
        volatile uint32_t *src = (volatile uint32_t *)LSYM_ADDR(_siv);
        volatile uint32_t *dst = (volatile uint32_t *)LSYM_ADDR(_vtor_itcm);
        g_vtor_want = (uint32_t)LSYM_ADDR(_vtor_itcm);
        /* ★ 判据必须能失败: 目标区装不下就点灯停机, 而不是"静默只拷一部分"
         *   (截断的向量表 = 部分中断跑飞, 比整体不启动更难查)。TICK 脚上面已初始化。 */
        if (n4 == 0u || n4 > cap4) {
            g_boot_status = -100;      /* 记因: 向量表 > ITCM 副本区 */
            g_stage = 0xFFu;
            blink_error(100);           /* 不返回 */
        }
        /* ★★★ EXP-E (2026-09-12): 先用**内存全宽写 (64 位)** 把整片向量表区"写实",
         *   再执行后面的 32 位逐项拷贝。这是 ST 官方给的解法, 也是本轮要验证的机制:
         *
         *   机制 (来源: DS13313 + AN5342 + ST 社区, 三条互证):
         *     · ITCM 是 **64 位宽**接口, 且**每 64 位字带 8 位 ECC** (SEC-DED), ECC **不可关闭**;
         *     · 当**写宽 < 内存宽度**时, 存储器控制器改走 **RD / MODIFY / WR** ;
         *     · 那个 **RD 会读到"从未写过"的 ITCM** ⇒ **触发 ECC 错误**;
         *     · AN5342 的要求原文: "使用 ECC 时**必须初始化代码访问的所有存储器**",
         *       且初始化应**按内存宽度写** (原文: "WR with the memory width to avoid RD/MODIFY/WR")。
         *   ⇒ 向量表拷贝是 32 位逐项写 ⇒ 恰好踩中这条。本段先把整片 `.itcm_vectors`
         *     用 64 位写填 0, 后面的 32 位拷贝就踩在**已初始化**的字上, RD 不再报错。 */
        {
            volatile uint64_t *iv = (volatile uint64_t *)LSYM_ADDR(_vtor_itcm);
            uint32_t n8 = (uint32_t)((LSYM_ADDR(_evtor_itcm) - LSYM_ADDR(_vtor_itcm)) / 8u);
            for (uint32_t i = 0; i < n8; i++) iv[i] = 0ull;
            __asm__ volatile("dsb" ::: "memory");
        }
        for (uint32_t i = 0; i < n4; i++) dst[i] = src[i];
        __asm__ volatile("dsb" ::: "memory");
        *((volatile uint32_t *)0xE000ED08UL) = (uint32_t)LSYM_ADDR(_vtor_itcm);
        __asm__ volatile("dsb; isb" ::: "memory");
        g_vtor = *((volatile uint32_t *)0xE000ED08UL);   /* 观测面: 供外部读回核对 */
    }
#else
    /* 对照构建: 什么都不搬, 只把 VTOR 的**实际值**读出来当观测面
     * (默认 = 0x08000000)。这样 A/B 两份固件用同一个判据读数 —— 不是靠"我记得改前是什么"。 */
    g_vtor_want = 0u;
    g_vtor      = *((volatile uint32_t *)0xE000ED08UL);
#endif
#if PA9_MODE == 0
    pin_out_init(UARTT_PORT, UARTT_BIT);
#endif
    g_isr_itcm = ISR_ITCM;      /* ★ 在代码里写一次, 否则会被 --gc-sections 回收 */
    g_stage = 1;

    /* ① 时钟 */
    int err = clock_init();
    g_boot_status = err;
    g_stage = 2;
    if (err != CLK_OK) blink_error(err);

    g_clock_hclk = clock_get_hclk_hz();
    g_stage = 3;

    /* ② 落位自检 (宣称=实现: "表在 DTCM" 必须可验证) */
    g_shm_addr         = (uint32_t)(uintptr_t)g_shm;
    g_shm_ok           = (uint32_t)shm_layout_ok();
    g_scan_itcm_addr   = (uint32_t)(uintptr_t)&engine_scan_itcm;
    g_scan_flash_addr  = (uint32_t)(uintptr_t)&engine_scan_flash;
    g_stage = 4;

    /* ③ 表装载: 冷启动清零(单一入口) → 铺栈哨兵 → **先试持久化恢复** → 回退 profile
     *    ★ 哨兵必须铺在"第一次大量用栈"之前 —— 否则铺的时候已经踩过一遍了
     *    ★★ W2.4: 顺序很关键。persist_load 必须在 engine_fill_tables **之前**
     *       (有持久化配置时不该白填一遍 profile 表), 且必须在 ENGINE_RUN=1
     *       **之前** (恢复要写 ACTIVE 表, 此刻无 ISR 扫描 = 无撕裂风险)。 */
    cold_start_reset();
    shm_guard_paint();
#if MB_DEFAULT_USE_UART
    /* W4: 通信域物理口 (USART2 PA2/PA3) —— **只调一次**, 不随冷启动重复。
     * 与 mb_config 的分工见 modbus.h。 */
    mb_uart_enable();
#endif
    g_persist_loads++;
    int restored = persist_load(g_shm);

    /* ★ BOOT_PROFILE 的行为在 W2.4 之后分两种, 必须说清楚 (否则"上电为什么不是
     *   我编的那个 profile"会成为一个排障陷阱):
     *     · 有有效持久化配置 → **恢复它**, 引擎保持 STOP (安全语义, 与 S3 一致),
     *       profile 不回填; 工具的判据是 g_persist_loaded_n > 0。
     *     · 无有效配置       → 回退 BOOT_PROFILE 填表 + 引擎 RUN (bench 默认行为,
     *       保持阶段 1/2/3 的所有既有判据不变)。
     *   ★ 这意味着"烧了 -DBOOT_PROFILE=1 却读回 profile 0 的表"是**正常**的 ——
     *     因为 flash 里有上一轮 persist 的数据。要拿回 bench 默认行为, 先擦扇区 6/7
     *     (tools/h723_persist.py --wipe) 或发一次空程序 deploy。 */
    if (restored > 0) {
        g_persist_loaded_n = (uint32_t)restored;
        PersistInfo_t _pi; persist_probe(&_pi);
        g_persist_loaded_sec = (_pi.seq_a >= _pi.seq_b) ? PERSIST_SECTOR_A : PERSIST_SECTOR_B;
        g_persist_ab_valid = _pi.ab_valid;
        g_persist_seq_a = _pi.seq_a; g_persist_seq_b = _pi.seq_b;
        g_persist_crc_a = _pi.crc_a; g_persist_crc_b = _pi.crc_b;
        g_table_profile = 0xFFu;    /* 哨兵值: "不是 profile, 是恢复的配置" */
    } else {
        engine_fill_tables(g_shm, BOOT_PROFILE);
        g_table_profile = BOOT_PROFILE;
        g_persist_loaded_sec = 0xFFFFFFFFu;
    }
    g_table_ck      = engine_table_checksum(g_shm);
    g_bucket_ck     = engine_bucket_checksum(g_shm);
    g_bucket_zero_slots = engine_bucket_dead_slots(g_shm);
    g_active_routes = engine_active_routes(g_shm);
    g_guard_ok      = (uint32_t)shm_guard_ok();
    g_engine_sel    = BOOT_SEL;
    g_scan_mode     = BOOT_SCAN_MODE;
    g_n_routes      = MAX_ROUTES;
    g_engine_gate   = BOOT_GATE;
    /* ★ W1: ENGINE_RUN 的上电值 —— 但 W2.4 起必须区分两种情况:
     *     · 恢复的持久化配置 → **保持 STOP** (安全语义: 执行器绝不无人监督上电即动,
     *       与 S3 persist.h 头注释一致)。PC 需显式 0x11 START。
     *     · 无持久化配置 (bench) → RUN, 保持阶段 1/2/3 既有判据不变。 */
    SHM_U8(g_shm, OFF_CTRL_ENGINE_RUN) = (restored > 0) ? 0u : 1u;
    __asm__ volatile("dsb" ::: "memory");
    g_stage = 5;

    /* ④ DWT 标定 */
    dwt_enable();
    calibrate();
    /* ★★★ 计时统计的**冷启动初始化** (2026-09-11 实测缺陷修复):
     *   这批量 (`g_isr_n` / `g_isr_cyc_*` / `g_per_cyc_*` / `g_per_prev`) 住在 **DTCM**,
     *   而 DTCM **跨复位不丢** ⇒ 不在这里清, 它们就是**上一轮启动的残留**, 会与本轮的
     *   数字混在一起。实测症状 (`0x38` 直接读, 没发任何命令):
     *     pmin=39992 (正常) 但 **pmax=0xFD68B1BF (4.25e9, 垃圾)**, emin=0
     *   —— 机制很具体: `dwt_enable()` 把 **CYCCNT 清零**, 而上一轮留下的 `g_per_prev`
     *   是个大数, 于是本轮**第一个周期样本** = `0 - g_per_prev` = 一个巨值, 被 pmax
     *   永久记住。**pmin 正常而 pmax 垃圾**这个组合就是它的指纹。
     *   ★ 为什么 g_per_prev 也要清 (而 `stats_reset()` 刻意保留它):
     *     在**一轮之内**保留它是对的 (RESET 后第一个样本仍有效); 但在**跨轮**处它必须
     *     归零 —— 因为 CYCCNT 刚被清零, 保留的旧值属于"上一个纪元", 相减没有意义。
     *   ⇒ 冷启动 = 两个钟一起归零 (CYCCNT 与 g_per_prev 同起点)。
     *   ★ 顺带的好处: `samples`/`g_tick_count` 之外的统计也从每轮 0 起, 与"S3 判据
     *     `samples` 必须小于复位前"的口径一致 (此前靠 CMD_RESET 清, 上电路径没清)。 */
    stats_reset();
    g_per_prev = 0;
    g_stage = 6;

    /* ⑤ 100μs 拍 */
    tick_timer_init();
    /* ★★ 拍活体自检 (2026-09-12): 等 3 个 tick, 不动 ⇒ 取向量失败, 当场点灯。
     *   这防的不是某一个 bug, 而是**一切"取向量失败"类故障**: 表位置错、表内容坏、
     *   对齐不足被硬件掩码……它们的症状全是"上电就卡在 Default_Handler, 且卡的
     *   位置与真因毫无关联"(2026-09-12 的向量表对齐事故花了一个通宵, 而这个自检
     *   本可以在 10 分钟内报警)。
     *   ★ 位置: VTOR 已在 main 开头设置, NVIC 使能随 tick_timer_init ⇒ 拍应该
     *     正在跑, 这里只确认它真的在跳 ISR。 */
    {
        uint32_t t0 = g_tick_count;
        uint32_t guard = 0;
        while (((g_tick_count - t0) < 3u) && (++guard < 1000000u)) { }
        if ((g_tick_count - t0) < 3u) {
            g_boot_status = -101;      /* 记因: 拍中断没有真的进来 */
            blink_error(101);           /* 不返回 */
        }
    }
    g_stage = 8;

    /* ⑥ 协议层 (阶段 3.1): USART1 + 帧解析
     *    ★ 放在拍之后: 横幅/自检要能观察到 g_tick_count 在走 (证明拍没被串口拖死) */
    fp_init(&s_parser);
    uart1_init(UART_PCLK2, UART_BAUD);
    g_uart_brr = uart1_brr();
    g_stage = 10;
    /* ★ 组帧自检: 必须在**任何一帧发出去之前**跑 —— 若 CRC 覆盖长度写错,
     *   后面发的每一帧都会被对端丢掉, 而固件这边看起来一切正常。 */
    g_frame_selftest = frame_build_selftest();
#if BOOT_BANNER
    proto_banner();
#endif
#if UART_SELFTEST
    proto_selftest();          /* 内部会置 1(通过) / 2(失败) */
#else
    /* ★ 显式写一次: 只有静态初值、代码里无人读也无人写的全局会被 --gc-sections
     *   整段回收 (实测: 第一版 g_selftest_state 从符号表消失了 —— 与阶段 2 的
     *   g_isr_itcm 同款陷阱, 属"本项目已知族谱"里的一个)。 */
    g_selftest_state = 3;      /* 未启用 */
#endif
    /* 阶段 3.2: deploy 自检 (用合成载荷驱动**同一个** h_deploy 代码路径) */
#if DEPLOY_SELFTEST
    deploy_selftest();
#else
    g_dst_done = 3;            /* 未启用 (同样必须显式写一次, 否则会被回收) */
#endif
    g_stage = 11;

    /* ⑦ W5 外设域: ADC1(16bit)+AI / DI / HIL
     *   ★ 时机: 必须在 clock_init() 之后 —— ADC 的 adc_ker_ck 取自 HSE/per_ck。 */
    g_stage = 21; adc_init();
    g_stage = 22; ai_init(g_shm);
    g_stage = 23; di_init(g_shm);
    g_stage = 24; hil_init(g_shm);
    /* ★★ 把 HIL 的物理输出登记为"安全态必须覆盖的面" (审计 #1)。
     *   必须在 hil_init **之后** —— 登记的是一个会写 TIM3_CCR1 的回调,
     *   TIM3 时钟/引脚未配置时调用它只是往未使能的外设写, 语义上不该发生。
     *   ★ 顺序纪律: **先 init 硬件, 再登记安全态** —— 反过来会出现
     *     "已经被登记、但硬件还没配"的窗口。 */
#if HIL_SAFE
    g_stage = 25; eng_register_output_surface(hil_outputs_safe);
#else
    /* A/B 对照档 (HIL_SAFE=0) **改前行为**: 故意**不登记** ⇒ 注册表为空
     * ⇒ eng_outputs_safe() 清不到任何物理面 ⇒ 复现"STOP 后 PWM 保持最后占空比"。
     * 交付构建永远走上面那支。 */
    g_stage = 25;
#endif
    /* ⑦b DO 输出面 (P3-A, 2026-09-12): init + 安全态登记。
     * ★ 登记后 `eng_outputs_safe` 停机时会调 `do_outputs_safe()` ⇒ GPIOE 的管辖位
     *   **第一次被真正清零** (之前它只有计数、没有动作 —— 定案②的欠账在此收清)。 */
    g_stage = 26; do_init(g_shm);
    do_latch_init();                    /* ★ P3-B: 影子 + MDMA 定时锁存链 (须在 do_init 后) */
    rtc_init(g_shm);                     /* ★ RTC: LSE 32.768kHz 时基 + 事件时间戳 */
    bb_init(g_shm);                      /* ★ 黑匣子: MDMA ch1 每拍 256B 快照 → AXI 环形缓冲 */
    /* ★ SD 卡 (2026-09-12): 初始化 + 把黑匣子缓冲写到卡上。
     *   放在启动期末尾 (允许几百 ms); 失败不阻塞启动 (内部有超时保护)。 */
    g_stage = 27;
    if (sd_init() == 0) { sd_dump_blackbox(); }
#if HIL_SAFE
    eng_register_output_surface(do_outputs_safe);
#endif
    g_out_surfaces = eng_output_surface_count();
    g_w5_ready = 1;
    g_stage = 12;

    /* ⑧ 统一锚定全部观测变量 (防 --gc-sections 回收; 见 obs_anchor 注释) */
    obs_anchor();

    for (;;) {
        /* 协议轮询: 排空串口 → 解析 → 分发 (命令执行在主循环, 不在中断里) */
        proto_poll();
#if BANNER_PERIOD_MS > 0
        {
            static uint32_t last = 0;
            if ((g_tick_count - last) >= (BANNER_PERIOD_MS * 10u)) {   /* 100μs/拍 */
                last = g_tick_count;
                proto_banner();
            }
        }
#endif
        /* L1 I-cache 使能 (实验用, 单向) */
        if (g_icache_req && !g_icache_on) {
            g_icache_req = 0;
            scb_enable_icache();
            g_icache_on = 1;
        }
        /* ★ W2.4: 免串口落盘请求 (pyocd 直写 g_persist_req)。
         *   与 0x43 走**同一条** persist_save 路径, 所以 PERSISTENT 语义门也一样生效:
         *   引擎 RUN 时返回 1 (跳过) → dirty 保持 1, 稍后可重试。
         *   ★ 必须在 proto_poll 之后: 若同一轮里既有串口命令又有本标志, 命令优先
         *     (PC 的显式请求比调试器的暗写更有权威)。 */
        if (g_persist_req) {
            g_persist_req = 0;
            g_persist_req_cnt++;
            int rc = persist_save(g_shm);
            if (rc == 1)      g_persist_skip_run++;
            else if (rc == 0) g_persist_saves++;
            else              g_persist_nak++;
        }
        /* ★★★ 空闲窗口自动落盘 (S3 persist_task 语义) —— T15/T26 修复
         *   ── 完整的三次失败记录在 g_persist_req_cnt 上方, 别原样重试第四次 ──
         *   实测结论: 功能正确 (dirty 会清), 但每次落盘有一段失聪窗口
         *   (sector erase stall flash 取指, 而命令服务路径在 flash)。
         *   前三次都是**固件自己按时间猜窗口** ⇒ 猜错就打崩别的用例 (21/30 → 5/30)。
         *   本次改成**由上位机问出来** + **裁决在这里**:
         *     · 登记 (h_persist_w2): 收到**纯查询型** 0x43 且 dirty==1 → g_persist_auto=1
         *       —— 那就是上位机正在等这个结果的窗口 (S3 wait_flush 每 20ms 轮询)。
         *     · 裁决 (本处): RUN ⇒ 放弃并计数; 否则落盘。
         *   ★ 裁决刻意不放在登记处 (第一版放那里 ⇒ "因 RUN 放弃"的计数恒为 0, 判据变空)。
         *   ★ 位置必须在这里: proto_poll 已返回 ⇒ 那条 ACK 已完整移出 (uart1_write 等 TC),
         *     失聪窗口落在 ACK 之后, 不会污染 PC 侧看到的第一帧。
         *   ★ 引擎保护有两层: 这里先查一次 (为了分类计数), persist_save 内部再查一次
         *     (PERSISTENT 硬门, 与 S3 同款) —— 不依赖调用方守规矩。 */
        if (g_persist_auto) {
            g_persist_auto = 0;
            if (!g_persist_dirty || SHM_U8(g_shm, OFF_CTRL_ENGINE_RUN)) {
                g_persist_auto_gate++;      /* RUN 态被问到 → 放弃 (T26 的 still_pending 靠它) */
            } else {
                g_persist_auto_runs++;
                int rc = persist_save(g_shm);
                if (rc == 1)      g_persist_skip_run++;
                else if (rc == 0) g_persist_saves++;
                else              g_persist_nak++;
            }
        }

        /* ★ W3: 免串口**协议帧**请求 (pyocd 直写暂存区 + g_cmd_req)。
         *   走**真实的 proto_dispatch** (与串口收到的帧完全同一条路径) —— 见
         *   g_cmd_req 的说明。这样命令分发/校验器/ACK-NAK/观测面计数都被走到,
         *   而不是"绕过协议直接调函数"。
         *   ★ 帧格式与串口**一致**: 暂存区里放的是**裸载荷** (不含 FRAME_SYNC/CRC),
         *     因为那几个字节是**链路层**的职责, 与命令语义无关 (校验器的判据是
         *     载荷内容, 不是 CRC)。长度由 g_cmd_req_len 给出。 */
        if (g_cmd_req) {
            g_cmd_req = 0;
            uint32_t rl = g_cmd_req_len;
            if (rl > DEPLOY_REQ_MAX) rl = DEPLOY_REQ_MAX;
            if (rl >= 1u) {
                const uint8_t *pl = (const uint8_t *)(g_shm + OFF_CMD_REQ);
                g_cmd_req_cnt++;
                g_cmd_req_last = pl[0];
                proto_dispatch(pl[0], pl + 1u, rl - 1u);
            }
        }
        /* 重填表: 先关扫描门 (防 ISR 扫到半张表), 填完恢复 */
        if (g_reinit) {
            g_reinit = 0;
            uint32_t sv = g_engine_gate;
            g_engine_gate = 0;
            engine_fill_tables(g_shm, (int)g_table_profile);
            g_table_ck      = engine_table_checksum(g_shm);
            g_bucket_ck     = engine_bucket_checksum(g_shm);
            g_bucket_zero_slots = engine_bucket_dead_slots(g_shm);
            g_active_routes = engine_active_routes(g_shm);
            g_engine_gate = sv;
            g_reinit_done++;
        }
        /* ══════════ 审计发现 C 的配套: SHM 计时镜像 + 条数旁证 ══════════
         * ★ 这两件都刻意放在**主循环**而不是 ISR:
         *   · TIMING 镜像: C 全局才是权威(ISR 每拍更新, 零额外代价)。往 SHM 搬
         *     只是为了让 PC 能用一条 0x22 READ_BURST 一次取走整个计时视图。
         *     放 ISR 里做等于给热路径加 7 次 SHM 写 —— 为一个"被轮询才需要"的
         *     视图付每拍的代价, 不划算 (与 h_engine_status 的"按需打包"同哲学)。
         *   · g_active_routes 重扫: 它是 SHM N_ROUTES 的**独立旁证**(真去扫表的
         *     flags 数出来), 所以**不能**直接复制 SHM 的值 —— 那就失去校验意义。
         *     周期性重扫 ⇒ deploy 热切表之后它会自己追上, 不需要在 deploy 里
         *     猜"ISR 什么时候切完"(那个时机由 ISR 决定, 上层猜不准)。
         * ★ 顺序: 先同步镜像, 再巡检哨兵 (哨兵若被踩, 说明表区已被破坏, 此时
         *   同步过去的也可能已经是脏数据 —— 但先写后检能让"脏数据"本身成为线索)。 */
        SHM_U32(g_shm, OFF_TIMING_SAMPLES)     = g_isr_n;
        SHM_U32(g_shm, OFF_TIMING_PERIOD_MIN)  = pn_or_0(g_per_cyc_min);
        SHM_U32(g_shm, OFF_TIMING_PERIOD_MAX)  = g_per_cyc_max;
        SHM_U32(g_shm, OFF_TIMING_EXEC_MIN)    = pn_or_0(g_isr_cyc_min);
        SHM_U32(g_shm, OFF_TIMING_EXEC_MAX)    = g_isr_cyc_max;
        SHM_U32(g_shm, OFF_TIMING_LAST_PERIOD) = g_per_cyc_last;
        SHM_U32(g_shm, OFF_TIMING_LAST_EXEC)   = g_isr_cyc_last;
        /* ★ 0x3850 与范本**同址** —— 它不在上面 0x18..0x33 这一块里 (那里已排满,
         *   0x34 起是保留的 GPIO_MASK), 是照 S3 的独立位置放的。 */
        SHM_U32(g_shm, OFF_TIMING_OVERRUN)     = g_isr_overrun;
        g_active_routes = engine_active_routes(g_shm);

        /* 通信域读区镜像: wire[0..63] 工程量 → MB_HOLD, **每 10ms 刷一次**。
         * ★ 为什么不是每拍: 它是一块 64×u16 的搬运 + 64 次浮点乘, 每拍做等于给
         *   热路径白加成本; 而外部主站读 40001-40064 的周期远长于 100μs。
         * ★ 为什么放主循环不放 ISR: 同上 —— 它不参与拍内时序 (§S3 也是放 ISR,
         *   但它那边是 core1 独立核, 本平台单核必须更省)。 */
        if ((g_tick_count % 100u) == 0u) mb_refresh_hold(g_shm);

        /* ★ W5: macro 循环执行 —— 由**主循环**驱动, 不在 ISR 里。
         *   macro 是 ms 级慢动作 (S3 用 10ms FreeRTOS 任务); 塞进 100μs 硬拍会
         *   直接吃掉拍预算。macro_tick 内部按 loop_ms 自节流, 未到间隔即返回，
         *   所以每轮调用只花几条指令。 */
        if (macro_tick(g_shm, g_tick_count)) g_macro_ticks++;

        /* ★ W5: AI / DI / HIL 周期任务 —— **交付档下已搬进拍内** (P1/P2, 2026-09-12)。
         * ★ IO_IN_ISR=1 (交付): 不再在此驱动, 三者由 ISR 的**输入段/输出段**按拍执行
         *   —— 这就是"把主循环上的、ISR 引擎外的 I/O 搬进引擎内"。
         * ★ IO_IN_ISR=0 (**A/B 对照档 = 改前行为**): 三者照旧在主循环跑, 供同一套测量
         *   方法打出"改前"的那一份数据。没有这一档, "搬进拍内更确定"就只能算相关性,
         *   不能算结论 (本项目"结构性改动必须配可失败对照"的既有纪律)。 */
#if !IO_IN_ISR
        /* 对照档 (= 改前行为): AI / DI / HIL 三个周期域全部由主循环按 10ms 驱动 */
        ai_tick(g_shm, g_tick_count);
        di_tick(g_shm, g_tick_count);
        hil_tick(g_shm, g_tick_count);
#else
        /* ★ 交付档: 三个域**都已进 ISR 拍内** (输入段 di_poll/adc_poll + 输出段 hil_out_poll),
         *   所以主循环**不再驱动任何一个** —— 一个输出面只应有一个驱动者。
         *   ★ 这里之前是不对的: `hil_tick` 曾两档都跑, 于是它和 ISR 里的 `hil_out_poll`
         *     **重复写 TIM3_CCR1**。两者读的是同一份 WIRE[20]、算出的 duty 相同, 所以
         *     **恰好无害** —— 但那是"碰巧一致", 不是设计。既然输出臂已经进了拍内,
         *     就该把主循环这条路径**整个撤掉**, 而不是靠"反正算出来一样"来兜。
         *     (本项目铁律: 一个量只能有一个权威来源/驱动者。) */
#endif

        /* 栈哨兵周期巡检 (廉价: 32 个字, 主循环有 100μs 一次的机会) */
        g_guard_ok = (uint32_t)shm_guard_ok();
        g_stage = 9;
        __asm__ volatile("wfi");
    }
}
