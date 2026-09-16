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
#include "faultlog.h"   /* 统一故障台账: 主循环/ISR/协议层的异常都留案底 */
#include "manifest.h"   /* 诊断资源目录: 0x64 让板子自报"哪里出问题读什么" */
#include "i2c_sm.h"     /* ★ G6-1: 拍内 I2C 事务状态机 (契约 §3.6 四条约束 / §3.7 实施) */
#include "i2c_bb.h"     /* ★ G6-2: 总线独占门 i2c_bus_owner()/I2C_OWNER_SM */
#include "timebase.h"   /* ★ 生产时基 = TIM5 (不再押在调试单元 DWT 上) —— 见该头文件的说明 */

/* ★★ 总线独占门已**移到资源处**（`src/i2c_bb.c` 的 `i2c_bus_acquire/release`，声明在 `i2c_bb.h`）。
 *   早先在这里定义一个 `g_i2c_blk_active` 标志、由状态机去读 —— 那是"**守卫放错层**":
 *   只在"我以为的那个调用点"有效, 多一个调用者就静默穿透。本项目的同族教训不止一次
 *   （`do_latch_init` 无条件启动 / `0x43` 那条链）。⇒ **谁拥有引脚, 谁守门。** */

/* ★ G6-2 诊断占用（`0x39 op=22`）的到期 tick：0 = 未占用。**有界**，见 ISR 尾部的自动释放。 */
static volatile uint32_t g_i2c_hold_until = 0u;
#include "wdt.h"        /* 独立看门狗: 喂狗点=拍 ISR (契约见 wdt.h 文件头) */
#include "lsym.h"
#include "adc.h"
#include "di.h"
#include "hil.h"
#include "do.h"
#include "i2c_bb.h"
#include "as5600.h"
#include "step.h"
#include "rtc.h"
#include "blackbox.h"
#include "sd.h"
#include "prog_store.h"   /* S5: DCL 程序持久化 (SD A/B 双副本 + 事务式上传) */

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
/* ★ 时基活性 (2026-09-13): 1 = 主循环检测到 DWT_CYCCNT 不推进 (多半是调试器停的)。
 *   用途: ① 让"时间量全是 0"这类静默污染**自己浮出来** (见 FAULT_TIMEBASE);
 *         ② 当 ISR 的"零成本"防御闸门 —— 时基死时 d==0 无意义, 不该计数/记账。 */
OBS uint32_t g_timebase_dead  = 0;
/* ★ 时基已停摆的**时长** (拍)。为什么要它: 检测改成"只记一笔台账"之后, "坏了多久"
 *   不再能由台账 total 推出来 ⇒ 用这个单调计数器代替 (既保住信息又不污染台账)。
 *   ★ 检测与置位都在**拍 ISR** (与 SCAN_DIV0 的门同源) —— 见 ISR 入口那段长注释。 */
OBS uint32_t g_timebase_dead_ticks = 0;
/* ★ 看门狗与"主循环是否在推进" (2026-09-13)
 *   g_loop_hb    : 主循环心跳 (每轮 +1)。★ 它**不**参与喂狗 ——
 *                  看门狗只保证"CPU+定时器+拍ISR 活着"(理由见 wdt.h 的喂狗契约):
 *                  实测 sector erase 会让主循环阻塞 ~816ms, 把主循环拉进门 ⇒ 每次刷盘都误复位。
 *                  ⇒ 主循环的死活改由**记录 + 上位机判** (FAULT_LOOP_STALL + mgmt.py --health)。
 *   g_wdt_ms     : 实际生效的看门狗超时 (由 wdt.h 算出), 供外部核对"配置到底是多少"。
 *   g_wdt_armed  : wdt_start() 的返回值 (0=已启动)。 */
OBS uint32_t g_loop_hb        = 0;
/* ★★ "主循环**已进入**"标记 (2026-09-13 修, 实测教训)。
 *   为什么不能拿 `g_loop_hb == 0` 当这个闸门 (第一版就是这么写的):
 *     注入 3s 阻塞发生在主循环**第一轮** ⇒ 那时 g_loop_hb 本来就是 0 ⇒ 闸门把
 *     "**刚进入就死了**" 永久当成 "**还没开始**" ⇒ 停滞判据**永不触发**。
 *     实测: `未声明段最大间隔=34806 拍(3.5s) ≥ 阈值` 而 `停滞事件=0`。
 *   ⇒ 闸门必须靠**显式的进入标记**, 而不是靠一个数值的巧合。
 *   ★ 语义: 0 = 还停在启动段 (SD 初始化那段可能阻塞很久, 不许判);
 *     1 = 主循环已开始跑 ⇒ 此后任何 ≥阈值 的不推进都必须判。 */
OBS uint32_t g_loop_entered   = 0;
OBS uint32_t g_wdt_ms         = 0;
OBS uint32_t g_wdt_armed      = 0xFFu;
/* ★ 故障注入钩子 (SD_CFG[13]=1 触发): 在拍 ISR 里**故意死循环**。
 *   用途: 证明"看门狗真的会复位" —— 这是**能失败判据**的对照组来源:
 *     · `-DDCL_WDT=0` 构建 + 注入 ⇒ 板子永久卡死 (串口全无应答)
 *     · `-DDCL_WDT=1` 构建 + 注入 ⇒ 200ms 内复位, 且 `mgmt.py --boot` 说得出原因
 *   没有前一半, "看门狗有效"就只是一句话。 */
OBS uint32_t g_hang_isr       = 0;
/* ★ 看门狗启动的"每一步读回" (见 wdt.h 的 WdtDiag_t): 返回码 + RCC_CSR + IWDG PR/RLR/SR。
 *   为什么要它: 实测第一次 `wdt_start()` 返回 -2 (PR/RLR 没写进去), 而当时手里只有
 *   "我写过 0x5555" —— 查不出为什么。**"写过了就算"必须配读回核对**(项目铁律)。 */
WdtDiag_t g_wdt_diag;
/* ★★ 关闸 (喂狗门) —— 定义与理由见 wdt.h 文件头 ② / RM0468 §50.3.6。
 *   `g_wdt_kr_busy`: 1 = wdt_start() 正在编程 PR/RLR, 此刻**任何**对 IWDG_KR 的写
 *   (含喂狗用的 0xAAAA) 都会"breaks the sequence"、把 PR/RLR 重新保护起来。
 *   为什么用 OBS(=volatile+used) 而不是普通全局: 它在 **ISR 与主线程间共享**,
 *   volatile 保证"置 1"对 ISR 可见地**早于**解锁写 (不被优化掉/不重排)。
 *   `g_wdt_kr_blocked`: 被闸门拦下的喂狗次数 —— 让"闸门有没有真拦住东西"可读回,
 *   而不是一句"我加了闸"。 */
OBS uint32_t g_wdt_kr_busy    = 0;
OBS uint32_t g_wdt_kr_blocked = 0;
OBS uint32_t g_opt_sr = 0;   /* FLASH_OPTSR_CUR (bit4=IWDG1_SW: 1=软件看门狗/0=硬件看门狗) */

/* ══════════ 复位取证记录 (BOOT_AXI) 的地址与归因锚点 ══════════
 * ★ 基址必须与 manifest.h 的 `MF_ENTRY("BOOT_AXI", 0x24000500u, ...)` 一致。
 *   在这里起一个名字, 是为了**消掉地址的第二份副本** —— 审计 P2 指出的"字段语义有两份副本"
 *   已经害过一次 (工具写死 PR=4/RLR=99, 换档即假报警), 地址同理。 */
#define BOOT_REC_ADDR        0x24000500u
/* ★★ 挂死归因锚点 (2026-09-13, 回应审计 P1②)。
 *   为什么必须有它: 复位后读到的 `RSR=IWDG1RSTF` **无法区分**两种完全不同的世界 ——
 *     (a) ISR 按设计走进死循环 ⇒ 没人喂狗 ⇒ 看门狗复位        ← 我们想证明的
 *     (b) 注入引发了别的异常 (HardFault → Default_Handler 死循环) ⇒ 同样没人喂狗 ⇒ 同样复位
 *   两者读数**完全同形**, 所以"注入 → 复位"这条正向判据**本身不足以定罪**。
 *   锚点写在 AXI(NOLOAD, 跨复位不丢) ⇒ 复位后读它就能证明"我们确实走到了那个死循环"。 */
#define BOOT_REC_W_HANG      32u          /* [32] **本轮**是否挂死 (ISR 写) */
#define BOOT_REC_W_HANG_PREV 33u          /* [33] **上一轮**是否挂死 (取证段保存) */
#define HANG_MAGIC           0x474E4148u  /* "HANG" (小端: 低字节起 H,A,N,G) */
#define BOOT_REC_W_LOOPRST   34u          /* [34] 本轮"因主循环停滞而复位"的次数 (ISR 复位前写) */
#define BOOT_REC_W_GAPMAX    35u          /* [35] 本轮主循环两轮最大间隔 (拍) —— 实测证据 */
#define BOOT_REC_W_LOOPRST_P 36u          /* [36] 上一轮的 [34] (取证段搬过来) */
#define BOOT_REC_W_GAPMAX_P  37u          /* [37] 上一轮的 [35] (取证段搬过来) */
#define BOOT_REC_W_DEFH      38u          /* [38] "进过 Default_Handler" 魔数 (由默认处理器锚点写) */
#define BOOT_REC_W_DEFH_PC   39u          /* [39] 进默认处理器时**出错指令的 PC** ⇒ 直接指到元凶 */

/* ★★ ISR 段检查点 (2026-09-13, 为"ISR 卡在哪一段"而加)
 *   背景: LA 宽度探针已证明**擦 flash 时 ISR 卡死 210.6ms**, 但静态排查已排除 5 条假设
 *   (直接调用/故障处理器/尾调用/读 rodata/运行期扫描选择) ⇒ 必须知道**卡在哪一段**。
 *   ★ 为什么不用调试器现场抓 PC: pyocd 连接要数秒, 而窗口只有 ~210ms ⇒ 抓不到。
 *   ⇒ 同一套"加可读面"的办法: ISR 每过一个段就把 `(段号<<28 | 拍号)` 写进 **AXI**
 *     (不被启动清零 ⇒ 跨复位不丢) ⇒ 复位后读它 = **最后走完的段** ⇒ 卡点在它之后。
 *   ★ 成本: 每段一次 store (APB/AXI 各走各的总线), 7 段/tick ≈ 7 个周期 / 40000 ⇒ 可忽略。 */
#define BOOT_REC_W_CKPT      40u          /* [40] ISR 段检查点 (每拍覆盖, = 当前这轮) */
#define BOOT_REC_W_CKPT_P    41u          /* [41] **上一轮**的检查点 (取证段在 ISR 启动前搬过来)
                                           *   ★ 为什么必须有 prev: 检查点**每拍都写**, 复位后新一轮
                                           *     会立刻覆盖 [40] ⇒ 直接读 [40] 读到的永远是"当前这轮",
                                           *     对"上一轮卡在哪"毫无用处 (第一版就是这么被骗的)。 */
/* ★★ 为什么放 BOOT_REC[40] 而**不是**另开一个 AXI 地址: 协议只允许读 **SHM** 区,
 *   绝对地址区只有"启动记录"这条既有的读取通道 (BOOT_AXI 就是这么读的)。
 *   第一版我另开了 0x24000600, 结果工具报"读被拒"—— 判据放错地方等于没有判据。 */
/* ★ dsb 是必须的 (第一版漏了): 复位**不会冲刷 AXI 写缓冲**, 没有 dsb 时"最后一次 store"
 *   可能根本没落到内存 ⇒ 读回来的检查点是**上一次能落地的值** ⇒ 仪器自己骗人
 *   (症状: 明明 ISR 冻在入口, 读出来却是"上一拍走完了"). 诊断档加 dsb 可接受。 */
#define ISR_CKPT(id)  do { \
        *(volatile uint32_t *)(BOOT_REC_ADDR + BOOT_REC_W_CKPT * 4u) = \
            (((uint32_t)(id) << 28) | (g_tick_count & 0x0FFFFFFFu)); \
        __asm__ volatile("dsb 0xF" ::: "memory"); \
    } while (0)

/* ★★ 默认处理器锚点 (2026-09-13, 一次实测缺陷的产物) —— 见启动文件里 Default_Handler 的注释。
 *   目的: 把"**是谁跳进了 Default_Handler**"变成**跨复位可读**的证据。
 *   为什么必须有它: 本缺陷躲过两轮排查, 就是因为一次故障只表现为"板子重启了",
 *   什么都没留下 (故障台账在 SHM, 复位就清零; 而 vg_stage 之类的 .bss 更是启动即清)。
 *   ⇒ 证据必须写进 **AXI(不被启动清零)** 才活得下来。
 *   ★ 必须 ISR_PLACE(ITCM): 它要在**flash 忙时**也能跑 —— 否则连这几行记录都执行不到,
 *     那就又回到"什么都没留下"的局面 (这正是本次缺陷的形态)。
 *   ★ 不许碰 flash: 只用栈上取数 + AXI 写 (AXI 是 RAM, 与 flash 控制器无关)。
 *   ★ 不喂狗: 保留既有语义 —— 卡在默认处理器 ⇒ 看门狗复位;
 *     负向对照 (`-DDCL_WDT=0`) 仍应表现为"永久卡死"。 */
ISR_PLACE void dcl_default_handler_anchor(uint32_t exc_return)
{
    uint32_t sp;
    /* EXC_RETURN bit2: 0 = 用的是 MSP, 1 = PSP */
    if (exc_return & 0x4u) __asm__ volatile("mrs %0, psp" : "=r"(sp));
    else                   __asm__ volatile("mrs %0, msp" : "=r"(sp));
    /* Cortex-M 标准异常帧: 偏移 0x18 处是出错时的 PC */
    uint32_t pc = *(volatile uint32_t *)(uintptr_t)(sp + 0x18u);
    volatile uint32_t *r = (volatile uint32_t *)BOOT_REC_ADDR;
    r[BOOT_REC_W_DEFH_PC] = pc;                          /* 先写 PC, 再写魔数 (顺序: 魔数=有效标记) */
    r[BOOT_REC_W_DEFH]    = 0x44454648u;                 /* "DEFH" */
    __asm__ volatile("dsb" ::: "memory");
}

/* ══════════ 主循环活性: 阈值 / 已知阻塞窗口 / 自愈 (2026-09-13, 回应审计 P1①) ══════════
 * 审计指出: 喂狗点在拍 ISR ⇒ **主循环死了既不复位、也读不到** (协议口全在主循环里) ——
 * 这是本系统**唯一不能自愈的失效模式**。补齐它要三件东西, 每件的理由都写在下面:
 *
 *  ① **阈值与窗口都必须按实测定, 不许按记忆/手册定**:
 *     实测最长**合法**阻塞 = **persist 落盘 ~1.55s**
 *     (2026-09-11 `tools/h723_tick_erase.py`: deploy → STOP → 连续问 0x01 ⇒ 板子哑 ~1.55s 后恢复,
 *      dirty 已清 ⇒ 功能对、代价是这个失聪窗口。★ 注意别把 T26 的 **sector erase ~816ms**
 *      当成总量 —— 那是擦除本身, 落盘的端到端阻塞更大。)
 *     ⇒ 阈值 `LOOP_STALL_TICKS` (1.2s) **小于**这个合法阻塞 ⇒ **不给 persist 声明窗口,
 *       每次刷盘都会误判成"主循环死了"并复位** —— 一个在正常工况下误报的保护, 比没有更坏。
 *     ⇒ 所以窗口 `PERSIST_BLOCK_TICKS` 取 2.0s (> 阈值), 并带截止时间 (见 ②)。
 *  ② **已知阻塞窗口(带截止时间)**: 光把阈值调大挡不住"将来出现 > 阈值的合法阻塞";
 *     正确形状是**声明窗口**。★ 必须是**截止时间**而非布尔开关: 若开关由主循环维护,
 *     主循环**死在窗口内** ⇒ 开关永远挂着 ⇒ 判据被永久抑制 (又一个空判据);
 *     截止时间到点即自动恢复判据 ✓。
 *  ③ **触发动作**: 记录 → `eng_outputs_safe()` → 软复位。
 *     ★ 宣称边界(不许夸大): 这是"**停止驱动 + 清已注册物理面**", **不是**"真安全电平"
 *       —— 真安全电平还缺硬件侧外部下拉 (见 wdt.h 的边界说明)。
 *     ★ 语义决策(审计要求显式说出来): 对 PLC, 「复位(停止驱动)」优于
 *       「失联但保持输出」—— 后者会让操作工以为机器仍受控, 是更危险的失效模式。 */
#ifndef DCL_LOOP_RESET
#define DCL_LOOP_RESET  1               /* 1 = 交付 (停滞⇒安全态+复位) / 0 = 能失败的对照构建 */
#endif
#define LOOP_STALL_TICKS      12000u    /* 停滞阈值 (拍) = 1.2s, 依据见 ① */
/* ★★★ 2026-09-16 (S5 实测事故): **程序存储的 SD 操作也是一个"声明式阻塞窗口"。**
 *   起因: `COMMIT` 落盘(写 1 块 + 读回 1 块) 若 SD 侧超时, 累计 > LOOP_STALL_TICKS
 *   ⇒ 被判"主循环死了" ⇒ `eng_outputs_safe()` + 复位 ⇒ 串口上表现为"COMMIT 返回假应答后失联"。
 *   ★ 与 persist 落盘**同一条纪律**: 已知会阻塞的动作, 由调用者**显式声明窗口**,
 *     而不是让停滞判据去猜。★ begin/end 成对; 块内死掉则 end 不执行、到点自动失效 ⇒ 判据恢复。
 *   ★ 取值: 单块写 ≈0.5 ms、15 块 ≈8 ms、读回同量级 ⇒ **2 s 是极宽的界**,
 *     留足余量又能在真死时 2 s 内恢复判据(而不是永远挂着)。 */
#define PROG_BLOCK_TICKS      20000u    /* 程序存储窗口 = 2s */
#define PERSIST_BLOCK_TICKS   80000u    /* persist 落盘窗口 = **8s, 必须 ≥ flash.c 的擦除超时预算**
                                         *   (FL_ERASE_TIMEOUT_CYC = 8s) —— 两者是同一件事的两种单位,
                                         *   改一个必须改另一个, 否则 3s 的擦除会在 2.5s 处被停滞自愈复位。
                                         *   ★ 实测依据 (2026-09-13): 真实擦除会**触发看门狗复位**
                                         *     (擦除期间喂狗停摆), 已在 flash.c 的 fl_wait_qw 里用
                                         *     "有界喂狗"修掉; 这里的窗口是**第二道**保险:
                                         *     即使喂狗失效, 停滞判据也不该在合法擦除期间动手。 */
#define SD_POLL_BLOCK_TICKS    8000u    /* sd_log_poll 例行刷盘: 实测单次间隔最大 0.48s ⇒ 0.8s 窗口 */
#define SD_INIT_BLOCK_TICKS   30000u    /* sd_reopen_log (整卡重初始化, 含识别重试) 的声明窗口 */

/* ── 双心跳 (PLC 级实时可观测: 不依赖主循环) ──
 *   ISR 心跳 = "CPU + 定时器 + 拍 ISR 活着"; 主循环心跳 = "主循环在推进"。
 *   两者一对比就能区分**整机死**与**主循环死** —— 这两件事的处置完全不同。
 *   ★ 为什么用 GPIO 而不往 UART 插字节: 主循环正在组帧/发帧时 ISR 插字节会**劈开
 *     响应帧** (对端解析器错位) —— 那是拿"可观测"换"通信正确性"。GPIO 完全不碰数据流,
 *     且天然符合铁律 0 (观测不改变被测对象); 想看波形接 LA 即可 (`saleae-la-verify`)。
 *   ★ 脚位: **PB0 = ISR 心跳 / PB1 = 主循环心跳**。
 *     ★★ 为什么**不能**用 PE0/PE1 (2026-09-13 纠正, 我第一版就选错了):
 *       `do.h` 定案 **GPIOE 整个端口 = DO 数字量输出面** (`DO_GPIO_PORT 4`):
 *       `do_init()` 把 **PE0..15 全部**配成推挽输出, 而 `do_poll()` **在 ISR 里每拍**
 *       把 `ACTUATOR[0..15]` 写到 GPIOE(BSRR 原子写) ⇒ 心跳要么被每拍覆盖, 要么反过来
 *       翻转 DO 通道 0/1。而 `docs/CORE-ENGINE.md` 早写了"macro **不许**碰 PE"。
 *       ★ 更阴的是: **"心跳计数在涨"这条软件侧验证照样通过** —— 又一个"看起来在工作"。
 *       ⇒ 所以下面加了**编译期防撞断言**, 让"选到已占用端口"这件事**编译不过**。
 *     ★ 端口占用总表 (选脚前必须对照; 已按源码逐条核过):
 *       PA(0): AI=PA0/PA1/PA4, HIL 反馈=PA5, HIL PWM=PA6, 协议口=PA9/PA10, PA13/14=SWD
 *       PB(1): **无组件占用** ← 心跳放这里
 *       PC(2): DI=PC0..3, SDMMC1=PC8..12
 *       PD(3): 485 口=PD5/PD6 (line_probe 也在 PD6), SDMMC1=PD2
 *       PE(4): **DO 输出面 16 路** (见上)
 *     若你的排针上取不到 PB0/PB1, **只改下面两个宏**即可 (其余代码与脚位无关),
 *     但**必须先查上面这张表**(防撞断言只挡 DO 那一个已知冲突)。 */
#define HB_GPIO_PORT          1u        /* GPIO_BASE(1) = GPIOB (软件侧唯一无占用的端口) */
#define HB_ISR_PIN            0u        /* PB0 */
#define HB_LOOP_PIN           1u        /* PB1 */
#define HB_DIV_TICKS          500u       /* 500 拍 = 50ms 翻转 ⇒ 10Hz 方波 (LA 低采样率也看清) */

/* ★★ 编译期防撞: 心跳端口**不得**落在 DO 输出面上。
 *   这一条是把我自己踩的坑 (选了 PE0/PE1) 变成**机制** —— 选错就编译不过,
 *   而不是等到"脚上没波形"才发现 (那时软件计数还是好好的)。 */
_Static_assert(HB_GPIO_PORT != DO_GPIO_PORT,
               "heartbeat port collides with the DO output plane (GPIOE)");

/* ── 主循环活性 / 阻塞窗口 / 心跳 的状态 ── */
OBS uint32_t g_block_until    = 0;   /* 已知阻塞窗口的截止 tick (0 或已过期 = 无窗口) */
OBS uint32_t g_block_win_max  = 0;   /* 本轮声明过的最大窗口 (拍) —— 调阈值时的实测依据 */
OBS uint32_t g_block_used     = 0;   /* 本轮迭代里**用过**声明窗口 (主循环每轮开头清) */
OBS uint32_t g_loop_gap_max   = 0;   /* ★实测: 主循环两轮之间的最大间隔 (拍) */
OBS uint32_t g_loop_gap_at    = 0;   /* 该最大值出现时的 tick (定位"当时在干什么") */
OBS uint32_t g_loop_gap_decl  = 0;   /* 该最大值所在那段**是否已声明**过 (1 = 声明过) */
OBS uint32_t g_loop_gap_max_undecl = 0; /* ★★ **未声明**段里的最大间隔 (拍) —— 判据用这个 */
OBS uint32_t g_loop_reset_cnt = 0;   /* 因主循环停滞而复位过几次 (跨复位单调: 从 AXI 接续) */
OBS uint32_t g_hb_isr_tog     = 0;   /* ISR 心跳翻转次数 (软件侧证据; 脚上波形靠 LA) */
OBS uint32_t g_hb_loop_tog    = 0;   /* 主循环心跳翻转次数 */

/** 开一个"已知会阻塞"的窗口。@param ticks 该段的**已知最坏耗时**。@return 供 end 恢复的前值。
 *  ★★ 必须与 `block_window_end()` **成对使用** (2026-09-13 实测教训):
 *     第一版只有 `open(ticks)`(把截止时间设成 now+ticks), 结果 **每轮都调它的短阻塞段
 *     (如 sd_log_poll) 会把窗口**永远续期** ⇒ `block_window_active()` 恒为 1 ⇒
 *     停滞判据被**永久抑制** —— 又是一个"看起来在保护、实际什么都没测"的空判据。
 *     实测证据: 注入 3.5s 阻塞后 `停滞事件=0 / 此刻在窗口内=1`。
 *  ★ 而**截止时间仍然保留**: 块内死掉 ⇒ end 永远不执行 ⇒ 到点自动失效 ⇒ 判据恢复。 */
static inline uint32_t block_window_begin(uint32_t ticks)
{
    uint32_t prev  = g_block_until;              /* end 时恢复 (嵌套安全) */
    uint32_t until = g_tick_count + ticks;
    g_block_until = until;
    g_block_used  = 1u;                          /* 供间隔测量区分"声明过的阻塞" */
    if (ticks > g_block_win_max) g_block_win_max = ticks;
    return prev;
}

/** 关窗 (把 begin 返回的前值恢复回去)。 */
static inline void block_window_end(uint32_t prev)
{
    g_block_until = prev;
}

/** 窗口是否生效。★ 带截止时间 —— 到点**自动失效**, 所以"主循环死在窗口内"也会被
 *  后续判据抓到 (布尔开关做不到这件事, 见上面 ② 的说明)。
 *  ★ `always_inline`: 它被**拍 ISR** 调用, 而 ISR 在 ITCM、本函数在 flash —
 *    本项目踩过"ISR 跨区直接 BL 触发 veneer 故障"的坑 (见 ISR 上方那段长注释),
 *    内联掉它就没有跨区调用这回事了。 */
static inline __attribute__((always_inline)) uint32_t block_window_active(void)
{
    return ((int32_t)(g_block_until - g_tick_count) > 0) ? 1u : 0u;
}

/** 心跳翻转: 一条 BSRR 写, 非阻塞。★ `always_inline` 同 block_window_active (ISR 调用)。
 *  BSRR 低 16 位 = 置位 / 高 16 位 = 清位 ⇒ 按当前 ODR 决定翻哪边。
 *  ★ 只用于诊断 (单个写者, 不需要原子语义)。 */
static inline __attribute__((always_inline)) void hb_toggle(uint32_t pin)
{
    if (GPIO_ODR(HB_GPIO_PORT) & (1u << pin)) GPIO_BSRR(HB_GPIO_PORT) = (1u << (pin + 16u));
    else                                      GPIO_BSRR(HB_GPIO_PORT) = (1u << pin);
}

/** 直接置高 (BSRR 低 16 位) —— 用于 ISR 入口/出口探针。 */
static inline __attribute__((always_inline)) void hb_set(uint32_t pin)
{
    GPIO_BSRR(HB_GPIO_PORT) = (1u << pin);
}

/** 直接拉低 (BSRR 高 16 位)。 */
static inline __attribute__((always_inline)) void hb_clr(uint32_t pin)
{
    GPIO_BSRR(HB_GPIO_PORT) = (1u << (pin + 16u));
}

/** 心跳脚初始化: PB0/PB1 设为推挽输出 (低速足够, 少一点边沿噪声)。
 *  ★ 必须在拍中断开始**之前**调用 —— 否则前几次 hb_toggle() 写的是还没配成输出的寄存器
 *    (结果不是错, 是"没效果", 属静默失败族)。 */
static void hb_pins_init(void)
{
    RCC_AHB4ENR |= RCC_AHB4ENR_GPIOBEN;      /* 权威: stm32h723xx.h GPIOBEN_Pos = 1 */
    __asm__ volatile("dsb" ::: "memory");
    /* MODER: 每脚 2 位, 01 = 通用输出 ⇒ 先清两位再置低位 */
    GPIO_MODER(HB_GPIO_PORT) &= ~((3u << (HB_ISR_PIN * 2u)) | (3u << (HB_LOOP_PIN * 2u)));
    GPIO_MODER(HB_GPIO_PORT) |=  ((1u << (HB_ISR_PIN * 2u)) | (1u << (HB_LOOP_PIN * 2u)));
    /* OSPEEDR: 心跳是诊断信号, 低速足够 (也少一点边沿噪声) */
    GPIO_OSPEEDR(HB_GPIO_PORT) &= ~((3u << (HB_ISR_PIN * 2u)) | (3u << (HB_LOOP_PIN * 2u)));
    GPIO_PUPDR(HB_GPIO_PORT) &= ~((3u << (HB_ISR_PIN * 2u)) | (3u << (HB_LOOP_PIN * 2u)));  /* 无上下拉 */
    /* 起点: 两脚都拉低 (LA 上一眼看出"从低电平开始跳") */
    GPIO_BSRR(HB_GPIO_PORT) = (1u << (HB_ISR_PIN + 16u)) | (1u << (HB_LOOP_PIN + 16u));
    __asm__ volatile("dsb" ::: "memory");
}
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

/* ══════════ ★ 引脚码型诊断 (DCL 抖动三方对照实验用) ══════════
 * 目的: 让板子在**拍 ISR 内**往 PE0..PE6 输出一个递增码型(0..127 循环),
 *       同时记录"写 GPIO 那一刻"的 DWT_CYCCNT。
 *
 * 为什么必须在 ISR 里写 (而不是复用主循环的 do_poll):
 *   本实验要测的是"**引脚变化的时刻**"的确定性。若输出由主循环驱动,
 *   变化时刻就由主循环调度决定 —— 测到的是调度抖动, 不是拍的确定性。
 *
 * ★ 与 do.c 的管辖冲突: do.c 每轮按 ACTUATOR[] 写 PE0..PE15。
 *   诊断模式开启时**必须让 do 面让出 PE0..PE6** (把 GPIO_MASK 置 0),
 *   否则两边互相覆盖, 引脚上是"谁后写谁赢"的竞态, 测出来毫无意义。
 *
 * ★ 观测口径: g_ppat_min/max 是**相邻两次"写 BSRR 之后读 DWT"的差值**,
 *   量的是"软件写引脚的时刻"间隔 —— 与 LA 测到的**引脚真实边沿**是两回事,
 *   两者之差 = 从寄存器写入到引脚翻转的延迟 (本次实验想首次量化的量)。 */
OBS uint32_t g_ppat_on   = 0;              /* 1 = 诊断码型模式开 */
OBS uint32_t g_ppat_wr_n = 0;              /* 本模式开启后写的次数 */
OBS uint32_t g_ppat_min  = 0xFFFFFFFFu;    /* 相邻写时刻间隔 min (cyc) */
OBS uint32_t g_ppat_max  = 0;              /* max */
OBS uint32_t g_ppat_prev = 0;              /* 上一拍写时刻 (DWT) */
OBS uint32_t g_ppat_last = 0;              /* 最近一次写时刻 */
OBS uint32_t g_ppat_val  = 0;              /* 最近写出的码型 (0..127) */
/* ★★★ 毛刺/异常间隔分档统计 (2026-09-14, 持久测试用)。
 * 为什么不能只报 min/max: 一个极端样本就能把极差拉成几万 ns, 而**看不见它有多少个**。
 * 分档同时回答"多严重"与"多频繁"两个问题 —— 这正是 LA 侧那次教训
 * (0.117% 的毛刺把极差从 166ns 拉成 50µs) 的固件版修法。
 * 正常拍长 = 40000 cyc; 下面以 ±250ns(=100cyc) 为正常带。 */
OBS uint32_t g_ppat_b_short = 0;   /* d < 39000   (异常短, > 1 拍少 1000cyc) */
OBS uint32_t g_ppat_b_low   = 0;   /* 39000..39899 */
OBS uint32_t g_ppat_b_ok    = 0;   /* 39900..40100 (正常带 ±100cyc = ±250ns) */
OBS uint32_t g_ppat_b_high  = 0;   /* 40101..41000 */
OBS uint32_t g_ppat_b_long  = 0;   /* > 41000 */
OBS uint32_t g_ppat_first   = 0;   /* 首次写时刻 (算总时长) */
/* ★★★ ISR 入口让路延时 (2026-09-14, 用户提出"在输出时刻左右留时间")。
 * 动机: MDMA 锁存链 (TIM2_UP → DMA2_S0 → MDMA → ODR, 七级) 的触发点**恰好是拍边界**,
 *       而 ISR 也在拍边界立刻启动 ⇒ **两者抢同一段总线时间** ⇒ 实测 MDMA 路径
 *       σ ≈ 61 ns, 而 CPU 直写只有 ≈ 21 ns。
 * 做法: 在 ISR 最前面空转若干周期, 把"CPU 抢总线"的窗口往后推, 给 MDMA 让路。
 * ★ 这是**诊断旋钮**, 不是交付配置: 它牺牲 ISR 时间预算换输出稳定性,
 *   用来判定"抖动到底是不是争抢引起的"。扫出最佳值后, 正确做法是改触发源
 *   (`DMAMUX1_C8` 换成 `TIM2_CC4`, 把锁存点移出 ISR 窗口), 而不是长期空转。
 * ★ 单位 = CPU 周期 (400MHz ⇒ 1 cyc = 2.5 ns)。0 = 关闭(交付行为)。 */
OBS uint32_t g_isr_delay_cyc = 0;
/* ★ 黑匣子开关 (2026-09-14, 干涉对照用): 1 = 关闭 bb_kick。
 *   目的是做"去掉一个 AXI 写手"的单变量对照 —— 见 ARCH-TIMELINE 的双时间表分析。 */
OBS uint32_t g_bb_off = 0;
/* ★★ 为什么"写入是否生效"必须由固件自证、而不是用调试器读:
 * pyocd 在 `connect_mode=halt` 下读 AHB4 外设寄存器会返回无意义常数
 * (本项目实测: GPIOA/E 读回 0xABFFFFFF / 0xFFFFFFFF, RCC_AHB4ENR 读回 0),
 * 而加 `-c reset` 又会复位板子破坏运行态。
 * ⇒ 自证放在 `0x39 op=2` 应答尾部: 被问时**当场读** ODR/MODER/RCC,
 *   不进 ISR ⇒ 不给被测对象加成本 (铁律 0: 观测不得改变被测对象)。 */
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
/* ★★ 2026-09-13: 加 ISR_PLACE(ITCM) 并**去掉 inline** —— 闸门证明它"从 ISR 可达却在
 *   FLASH"(0x080002E0)。ISR_PLACE 含 noinline, 与 inline 互斥 (见 modbus.c 的既有教训),
 *   故必须先去掉 inline。放 ITCM 后它比在 flash 更快, 不存在性能回退。 */
static ISR_PLACE void stats_reset(void)
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
    /* ★★ ISR 宽度探针 (2026-09-13, 回应审计的强判据建议) —— 出口在函数末尾。
     *   **入口置高 / 出口拉低** ⇒ 正常是 ~µs 级窄脉冲 (每拍一个);
     *   而"ISR 卡在 flash 取指"时会**拉成一条长高电平, 直到看门狗复位**。
     *   ⇒ 判据从"心跳在不在跳"(**存在性**判据 —— 10Hz 停 200ms 只丢 2 个沿,
     *     长采集里看起来"仍在跳", 会得出**反向结论**)变成
     *     "**最大高电平宽度**"(可量化): 正常 <10µs, 卡死 **≈200ms = DCL_WDT_MS**
     *     ⇒ 两个独立量互证, 一次判死。
     *   ★ 放在**第一句**: 探针要覆盖整个 ISR —— 若入口之后的取指就卡住, 也必须看得到。 */
    ISR_CKPT(1);      /* ① 入口 —— ★ 放在 hb_set **之前**: 探针置高说明"已进入",
                       *   但若连这一句都没写成, 就说明冻在**进入后的第一条总线访问**上。 */
    hb_set(HB_ISR_PIN);
    /* ★ 诊断细分 (2026-09-13, 为"擦 flash 时 ISR 卡在哪"):
     *   ⑨ 证明 **GPIOB(AHB4)** 写成功 —— 与下面的 **TIM2(APB1)** 访问做对照,
     *   用来区分"所有总线都停"与"只有 APB 停下来"。读法: 复位后看 BOOT_REC[41] 的段号。 */
    ISR_CKPT(9);

    /* ★★★ 生产时基 (2026-09-16): **不再用 DWT_CYCCNT** —— 它是调试单元, 调试器会话收尾会
     *   主动清 `DEMCR.TRCENA` 把它关掉 (pyOCD #1540 / SEGGER KB 明文的**设计行为**)。
     *   改用 TIM5 自由运行 32 位计数器（5ns @200MHz）。DWT 降级为**第二条独立路径**:
     *   扫描段会同时读它并在那里比对（`g_dwt_dead_n` / `g_tb_dead_n`），
     *   于是"DWT 死了"是一个**可计数、可读走**的量。本处只读时基。
     *   ★ 必要性不止"量准": `flash.c` 拿 DWT 当**超时判据**, DWT 一冻 ⇒ 超时永不触发
     *     ⇒ 有界喂狗退化成无限喂狗 ⇒ **卡死且看门狗失效**。见 docs/ASSESS-toolchain-2026-09-16.md */
    uint32_t t0 = tb_cyc();

    /* ★★ 时基活性 —— **检测必须与"使用"同源** (2026-09-13 修, 回应审计的口径不一致):
     *   原先 `g_timebase_dead` 只由**主循环**("连续 100 轮 CYCCNT 不推进")置位, 而
     *   `SCAN_DIV0` 的门在**拍 ISR** 里读它。于是出现: 由 pyocd 会话触发的启动,
     *   其启动段 (DWT 被停, 而主循环**还没进入**) ISR 每拍记一笔 SCAN_DIV0,
     *   主循环一笔 TIMEBASE 也没记 —— 实测 **1.67s 内 16681 笔 SCAN_DIV0 / 0 笔 TIMEBASE**。
     *   ⇒ 台账被调试器污染, 审计时会被当成固件缺陷 (而它其实是外部条件)。
     *   ⇒ 把检测**下沉到取数的地方** (ISR): 拍周期 = 40000 周期, 活的 CYCCNT 两拍之间
     *     必然差 ~40000 ⇒ **连续 4 拍完全相等**只可能是被停了。
     *   ★ 为什么是"连续 4 拍"而不是"跨度大": 它只看"有没有动"(400µs 量级), 与
     *     "擦 flash 造成几十 ms 跨度"那种**已知合理窗口**无关 ⇒ 不产生噪声。
     *   ★ 只记**一笔** + 用 `g_timebase_dead_ticks` 记"坏了多久": 原先"每 100 轮记一笔"
     *     的写法会把一次 1.7s 的停摆写成上万条 —— 那正是要消掉的污染。 */
    {
        static uint32_t s_tb_prev = 0u, s_tb_same = 0u;
        if (t0 == s_tb_prev) {
            if (++s_tb_same >= 4u) {
                if (g_timebase_dead) {
                    g_timebase_dead_ticks++;      /* 仍在坏: 只累时长, 不刷台账 */
                } else {
                    g_timebase_dead = 1u;
                    g_timebase_dead_ticks = 0u;
                    /* 上下文 = (CYCCNT 值, 来源码 1 = ISR 检测) —— 便于与外部条件归因 */
                    fault_record(g_shm, FAULT_TIMEBASE, g_tick_count, t0, 1u);
                }
            }
        } else {
            s_tb_same = 0u;
            g_timebase_dead = 0u;                 /* 活了 ⇒ 撤闸门 (不再累时长) */
        }
        s_tb_prev = t0;
    }

        /* ★ 诊断细分: ⑩ 到了 TIM2 访问之前。
         *   TIM2 是 **APB1** 外设 —— 若卡在 ⑩→⑪ 之间, 卡点就是"**擦 flash 期间
         *   APB 外设访问无法完成**"(原始报告 §9.4 的归因), 而不是"代码取指 stall"
         *   (那个已在本次修复中解决: 见 hil_out_apply 等 → ITCM)。 */
        ISR_CKPT(10);
        if (TIM_SR(TIM2_BASE) & TIM_SR_UIF) {
            TIM_SR(TIM2_BASE) = ~TIM_SR_UIF;
            g_stage = 7;
            /* ★ 诊断细分: ⑪ = **TIM2 的读+写都过了** ⇒ 卡点在喂狗或其后。 */
            ISR_CKPT(11);

#if DCL_WDT
            /* ★★ 喂狗 (2026-09-13) —— 位置**有意放在中断入口最早处**:
             *   这一行执行得到 ⇒ **CPU + TIM2 + 拍中断三者都活着**。
             *   契约与理由(为什么不把主循环拉进门)见 src/wdt.h 的文件头。
             *   ★ 必须早于任何可能 return 的分支 —— 否则那条路径会跳过喂狗。
             *
             * ★★★ 必须过"关闸"这一道 (2026-09-13 第二次修正, 依据 RM0468 §50.3.6):
             *   喂狗 = 写 `IWDG_KR = 0xAAAA`, 而手册逐字写明 **写 KR 的其它值 (点名
             *   0xAAAA 这个重载操作) 会打断 PR/RLR 的解锁序列、把寄存器重新保护起来**。
             *   本 ISR **每 100µs 跑一次**, 而 `wdt_start()` 的"解锁 → 写 PR/RLR →
             *   等更新落"窗口**实测 ≈10.1ms** (PR=4; = 5 个预分频步长, 见 wdt.h) ⇒
             *   期间会有 ~101 次喂狗撞进来 —— 若某一次落在"解锁与写 RLR"之间, 则 RLR
             *   的写会被**重新保护而静默丢弃** (只在读回时暴露为 -3)。
             *   ⇒ 这道闸是"**配置期间不得被异步写者干扰**"这条一般纪律的实例:
             *     凡带解锁序列/写保护的寄存器, 其编程期都必须有明确的互斥。
             *   ★★ 但要说清楚: **它未被证明是当时 -2 的成因** (不关闸的对照构建同样成功,
             *     见 wdt.h)。保留它是因为这个竞态真实存在 —— 把小概率变成"由构造保证"。
             *   ★ 用 `DCL_WDT_FEED_GATE=0` 可回到**改前行为**(不关闸) —— 对照构建。 */
#if WDT_FEED_GATE
            if (g_wdt_kr_busy) {
                g_wdt_kr_blocked++;              /* ★ 证据: ISR 确实想写, 被闸门拦下 */
            } else
#endif
            {
                /* ★ 诊断细分 (2026-09-13): 把**喂狗**夹住。
                 *   ⑫→⑬ 之间只有一条 `wdt_feed()` (写 `IWDG_KR=0xAAAA`),
                 *   ⇒ 若上一轮停在 ⑫, 卡点就是 **IWDG 寄存器访问本身**。
                 *   注意 IWDG 与 TIM2 **不在同一个域**: TIM2 在 D2/APB1(实测能访问),
                 *   IWDG 在 D3/SRD —— 这条区分是本次定位的关键。 */
                ISR_CKPT(12);
                wdt_feed();
                ISR_CKPT(13);
                SHM_U32(g_shm, OFF_WDT_STAT + 20u)++;   /* ★ 喂狗计数 (单调) —— 外部可见的"在喂"证据 */
            }
#endif  /* DCL_WDT */

            /* ★★ 故障注入 (对照组用): 注入后本拍不再返回 ⇒ 之后没有喂狗 ⇒ 看门狗到期复位。
             *   ★★★ 这一段**必须在 `#if DCL_WDT` 之外** (2026-09-13 修, 回应审计 P1②):
             *     原实现把它包在 `#if DCL_WDT` 里 ⇒ **`-DDCL_WDT=0` 档里注入被编译掉了**,
             *     而主循环的钩子 (`SD_CFG[13]`) 仍在 ⇒ 注入后板子**照常活着**。
             *     后果不是"对照做不出来", 而是**结论相反**: 会看到"没有看门狗也没事"。
             *     而当时的注释还宣称"WDT=0 构建下它会永久卡死" —— **宣称 ≠ 实现**,
             *     正是本项目最忌的一类。⇒ 注入路径必须与喂狗**解耦**, 两档构建都有。
             *   ★ 位置仍在喂狗之后 ⇒ WDT=1 档的语义不变 (先喂一拍, 再死)。 */
            if (g_hang_isr) {
                /* ★★ 先写归因锚点再死 (见上方 BOOT_REC_W_HANG 的长注释):
                 *   否则"设计中的挂死"与"注入引发的 HardFault → Default_Handler"
                 *   在复位后读数**完全同形**, 正向判据不足以定罪。 */
                *(volatile uint32_t *)(BOOT_REC_ADDR + BOOT_REC_W_HANG * 4u) = HANG_MAGIC;
                for (;;) { __asm__ volatile("nop"); }
            }

            /* ★ 双心跳之 **ISR 侧** —— 已从"每 500 拍翻转(10Hz 方波)"改成
             *   **"入口置高 / 出口拉低"的宽度探针** (2026-09-13, 回应审计):
             *   10Hz 那种形状只能回答"心跳还在不在"(存在性判据), 而本缺陷恰恰是
             *   "ISR 卡 200ms 后复位再恢复" —— 长采集里只丢 2 个沿, 看不出来。
             *   ⇒ 探针脉冲的**宽度**可量化, 且卡死时 ≈200ms 正好等于看门狗超时。
             *   ★ `g_hb_isr_tog` 语义随之变准: 现在数的是**走完的 ISR 次数**
             *     (卡住时不增长) ⇒ 协议侧也能判"ISR 是否还在完成"。 */

            /* ★★ 主循环停滞检测 (2026-09-13) —— **判据必须放在这里**:
             *   主循环自己无法报告自己停了 (第一版写在主循环里 ⇒ 恒不成立的空判据)。
             *   本 ISR 每拍看一次 g_loop_hb: 若连续 ≥阈值 没推进 ⇒ 判停滞。
             *   ★ 阈值 = `LOOP_STALL_TICKS` (1.2s), 依据见文件上方 ① —— 阈值由**实测**
             *     的最长合法阻塞定, 且**固件自报**给工具 (工具不许写死, 那是假报警源)。
             *   ★ 已知阻塞窗口(带截止时间)抵扣见文件上方 ② —— 主循环死在窗口内也能被抓到。 */
            {
                static uint32_t s_hb_seen = 0u, s_hb_stall = 0u;
                if (g_loop_hb != s_hb_seen) {
                    s_hb_seen = g_loop_hb; s_hb_stall = 0u;
                } else if (!g_loop_entered) {
                    /* ★★ 闸门: 主循环**还没进入**时不许判停滞。
                     *   上电时 SD 卡不在位会让 `sd_init → sd_identify → sd_cmd` 阻塞很久,
                     *   最早那版用"g_loop_hb==0"当闸门, 拿到"stall_cnt=15"全是启动期误报。
                     *   ★ 但**不能用 g_loop_hb==0 当闸门** —— 那个数值在"刚进入就卡死"时
                     *     同样是 0, 会把"已经死了"读成"还没开始"(实测 3.5s 阻塞零触发)。
                     *   ⇒ 用显式标记 `g_loop_entered`。 */
                    s_hb_stall = 0u;
                } else if (++s_hb_stall >= LOOP_STALL_TICKS) {
                    /* ★★ 已知阻塞窗口内**不判**。注意这里**故意不清** s_hb_stall:
                     *   窗口一旦过期而它仍 ≥ 阈值, 下一拍立刻判 ⇒ "主循环死在长阻塞段里"
                     *   同样会被抓到 (这正是不用布尔开关的原因)。 */
                    if (!block_window_active()) {
                        s_hb_stall = 0u;
                        fault_record(g_shm, FAULT_LOOP_STALL, g_tick_count, g_loop_hb, g_stage);
                        SHM_U32(g_shm, OFF_WDT_STAT + 16u)++;   /* 停滞事件计数 (外部可见) */
#if DCL_LOOP_RESET
                        /* ★★★ PLC 级处置: **不等它自己好** —— 先进安全态, 再复位。
                         *   语义与宣称边界见文件上方 ③ (只说"停止驱动", 不说"真安全电平")。 */
                        eng_outputs_safe();
                        /* 归因证据先写进 AXI(NOLOAD, 跨复位不丢) —— 否则复位后现场全没了,
                         * 只剩"又启动了一次" (这与 HANG 锚点同一个理由)。 */
                        *(volatile uint32_t *)(BOOT_REC_ADDR + BOOT_REC_W_GAPMAX * 4u)
                            = g_loop_gap_max;
                        *(volatile uint32_t *)(BOOT_REC_ADDR + BOOT_REC_W_LOOPRST * 4u)
                            = g_loop_reset_cnt + 1u;
                        __asm__ volatile("dsb" ::: "memory");
                        /* Cortex-M 软复位: 必须先写 VECTKEY=0x5FA, 否则整次写被忽略。 */
                        SCB_AIRCR = SCB_AIRCR_VECTKEY | SCB_AIRCR_SYSRESETREQ;
                        __asm__ volatile("dsb" ::: "memory");
                        /* SYSRESETREQ 是**异步**的 ⇒ 自旋等它生效 (不能假设后面不执行)。 */
                        for (;;) { __asm__ volatile("nop"); }
#endif
                    }
                }
            }

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

        /* ---- ★ 引脚码型诊断 (三方对照实验) ----
         * ★★★ 正确姿势 (2026-09-14 纠正): **写 SHADOW_GPIO, 让 MDMA 照常搬运**。
         *
         * 第一版我直接写 `GPIO_BSRR(4u)`, 结果是错的 —— 那等于把
         * **"计算与输出解耦"这条核心设计原则关掉了**:
         *   DCL 的灵魂就是 `ISR 算完只把结果写进影子缓冲(无 timing 要求)`
         *   → `硬件定时器触发 MDMA` → `DMA 在拍边界把影子缓冲锁存进 GPIOE_ODR`。
         *   绕过它直接写 BSRR, 测到的就是"被拆掉的架构", 数据没有意义。
         *
         * ⇒ 现在只写影子 + 记下"写影子的时刻"。**引脚的真实输出时刻由硬件锁存决定**,
         *   它比软件写时刻更稳 —— 这正是本实验要用"固件 vs LA"两路去量出来的东西。 */
        if (g_ppat_on) {
            uint32_t pat  = g_tick_count & 0x7Fu;              /* 0..127 循环 */
            /* 只写影子缓冲 —— MDMA 会在下一个拍边界把它锁存到 GPIOE_ODR。
             * ★ 同时写 SEQ, 便于事后核对"哪个电平对应哪一拍"(do.c 的既有诊断约定)。 */
            *(volatile uint32_t *)(g_shm + OFF_DO_SHADOW)     = pat;
            *(volatile uint32_t *)(g_shm + OFF_DO_SHADOW_SEQ) = g_tick_count;
            uint32_t tp = DWT_CYCCNT;                          /* ★ 写影子之后 (软件侧) */
            if (g_ppat_prev != 0u) {
                uint32_t d = tp - g_ppat_prev;
                if (d < g_ppat_min) g_ppat_min = d;
                if (d > g_ppat_max) g_ppat_max = d;
                /* ★★ 分档 (持久测试用): 正常拍长 40000 cyc。
                 * ★ 2026-09-14 收紧: 原来的正常带 ±100cyc(±250ns) 太粗 ——
                 *   它能报出"极差 175ns", 却**看不出典型值有多集中**。
                 *   极差 ≠ 典型抖动: 62 万个样本里只要有 2~3 个偏离, 极差就成了那个数。
                 *   现收紧到 ±10cyc(±25ns), 并保留 ±40cyc(±100ns) 档, 用来区分
                 *   "**典型抖动**"与"**偶发极值**"这两件完全不同的事。 */
                if (d < 39960u)       g_ppat_b_short++;   /* < -100ns */
                else if (d < 39990u)  g_ppat_b_low++;     /* -100ns .. -25ns */
                else if (d <= 40010u) g_ppat_b_ok++;      /* ★ ±25ns 正常带 */
                else if (d <= 40040u) g_ppat_b_high++;    /* +25ns .. +100ns */
                else                  g_ppat_b_long++;    /* > +100ns */
            } else {
                g_ppat_first = tp;
            }
            g_ppat_prev = tp;
            g_ppat_last = tp;
            g_ppat_val  = pat;
            g_ppat_wr_n++;
        }

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
            uint32_t ta = tb_cyc();          /* ★ 生产时基（TIM5）：不受调试器影响 */
            uint32_t wa = DWT_CYCCNT;        /* 对照路径（可能被调试器冻住）*/
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
            uint32_t tz = tb_cyc();
            uint32_t wz = DWT_CYCCNT;

            /* ★★ 两条路径**互相监看** —— "时基在走"从此是一个**可读走的量**, 不再靠约定:
             *   时基动/DWT 不动 ⇒ DWT 被调试器关了（`g_dwt_dead_n`）
             *   时基不动/DWT 动 ⇒ 我们的 TIM5 出问题（`g_tb_dead_n`）
             * 成本 ≈ 两次减法 + 两次比较, 每拍一次。★ 判据必须能失败: 跑一次 `pyocd reset`
             * 就能让 `g_dwt_dead_n` 从 0 涨起来（而 1 档的统计**照常工作**）。 */
            {
                uint32_t d_tb  = tz - ta;
                uint32_t d_dwt = wz - wa;
                if (d_tb  == 0u && d_dwt != 0u) { g_tb_dead_n++; }
                if (d_dwt == 0u && d_tb  != 0u) { g_dwt_dead_n++; }
                g_tb_cyc_last = d_tb;
            }

            g_eng_sel_used = sel;
            g_eng_n_used   = g_n_routes;
            g_eng_ck       = ck;
            g_eng_routes_last = nrun;
            g_eng_routes_total += nrun;
            g_eng_ticks++;

            uint32_t d = tz - ta;
            g_eng_cyc_last = d;
            if (!g_eng_cyc_first) g_eng_cyc_first = d;     /* ★ 首样本留痕 (H5) */
            if (d < g_eng_cyc_min) g_eng_cyc_min = d;
            if (d > g_eng_cyc_max) g_eng_cyc_max = d;
            g_eng_cyc_sum += d;
            g_eng_n++;
            if (d == 0u) {
                /* ★ 闸门: 时基已死时 d==0 **不携带任何信息** (DWT 停 → t0==t1)
                 *   ⇒ 既不计数也不记台账。否则会出现实测到的"185034 拍里 184043 拍
                 *     都在报 0 周期"—— 既是噪声淹没真信号, 又是**每拍在 ISR 里写台账**
                 *     的性能隐患。时基故障本身由 FAULT_TIMEBASE 单独报 (主循环检测)。 */
                if (!g_timebase_dead) {
                    g_eng_div0++;      /* 防御: "零成本"一定是测量坏了 */
                    /* ★ 台账 (ISR 内, 只在异常时付代价): 上下文 = (测得周期, 样本序号) */
                    fault_record(g_shm, FAULT_SCAN_DIV0, g_tick_count, d, g_eng_n);
                }
            }
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
        ISR_CKPT(3);  /* ③ 时基检测/喂狗之后 */
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
        ISR_CKPT(4);  /* ④ mb_tick 之后 */
        hil_out_poll(g_shm, g_tick_count);
        ISR_CKPT(5);  /* ⑤ **hil_out_poll 之后** (上一轮实测卡在 ④→⑤ 之间 ⇒ 细分) */
        do_poll(g_shm, g_tick_count);
        ISR_CKPT(6);  /* ⑥ do_poll 之后 */        /* ★ P3-A: DO 输出面 ACTUATOR → GPIOE (BSRR 原子写) */

        /* ★★★ 输出后让路 (2026-09-14, 诊断旋钮; 用户提出"把输出后的那个任务推后 1~2µs"):
         *   本拍该写的都写完了 (输出面已进影子/ODR), 而 **MDMA 锁存链还在同一个拍边界上
         *   搬运** (TIM2_UP → DMA2_S0 → MDMA → ODR, 七级串联)。ISR 若立刻继续跑后面的段
         *   (黑匣子快照/统计/协议收尾), 就会与 MDMA **争抢总线** ——
         *   实测: MDMA 路径 σ ≈ 61 ns, 而 CPU 直写只有 ≈ 21 ns。
         *   ⇒ 在这里空转 N 个周期, 让 MDMA 先搬完再继续。
         *
         *   ★ 与"入口让路"的关键区别: 入口让路会把**整个** ISR 推后(连输入采样一起),
         *     破坏"输入→计算→输出"的相位关系; 这里只推**输出之后**的部分 ⇒
         *     前半段时序完全不变, 只有"输出后到 ISR 结束"这段被让开。
         *   ★ 0 = 关闭 (交付行为); 由 `0x39 op=3` 运行期设定, 便于扫参数找最佳值。 */
        if (g_isr_delay_cyc != 0u) {
            /* ★★★ 必须用**纯 NOP 循环**, 不能用 `while ((DWT_CYCCNT - d0) < n) {}`！
             *   实测教训 (2026-09-14): 第一版用 DWT 忙等 ⇒ 延时档的抖动**反而变大**
             *   (PE0 58.8→64.9 ns, PA8 25.3→30.1 ns)。
             *   原因: **`DWT_CYCCNT` 是 CoreSight 组件, 读它要过总线** ——
             *   忙等循环每几纳秒就做一次总线访问 ⇒ "让路"变成了"**更加争抢**"。
             *   ⇒ 让路必须**完全不碰总线**: NOP 在 ITCM 里取指+执行, 不产生数据访问。
             *   ★ 单位换算: 每轮 ≈ 3 cyc (NOP + 循环开销), 故 `g_isr_delay_cyc` 现在
             *     按"轮数"解释; 2.5ns/cyc × 3 ≈ 7.5ns/轮。 */
            uint32_t n = g_isr_delay_cyc;
            while (n-- != 0u) { __asm__ volatile("nop"); }
        }
#endif

        /* ★★★ 2026-09-16: 这里的尾读**必须与头部的 t0 同源** —— 头部已改成 `tb_cyc()`(TIM5)。
         *   第一版漏了这一处 ⇒ `di = DWT_CYCCNT - TIM5_CNT` **把两个不同计数器相减**,
         *   实测表现: `emax = 3.7e9`(垃圾) 且 `ov` **每拍都判超预算**（15050/15050）。
         *   ★ 这正是"半个对齐比不对齐更坏"的又一次: 改了一半、看起来能编译、数值全是垃圾。
         *   ★ 而且 `ov` 不是纯观测 —— 它喂给动态预算门, 所以 DWT 一死会让引擎**自认为每拍超支**。 */
        uint32_t t1 = tb_cyc();
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
        /* ★ 预算按**时间**表达（80 µs），不写死频率 —— 见 timebase.h 的 EXEC_BUDGET_TB。
         *   这条断言是"两档表达同一个物理预算"的**机器判据**（0 档 32000 cyc / 1 档 16000 tick）。 */
        _Static_assert(TB_US(80u) == EXEC_BUDGET_CYCLES || (TB_HZ != CLK_SYSCLK_HZ),
                       "TB 档的预算必须与 EXEC_BUDGET_CYCLES 表达同一个物理量 (80µs)");
        if (di > EXEC_BUDGET_TB) {
            g_isr_overrun++;
            /* ★ 台账: 上下文 = (实测周期, 预算上限) —— 超了多少一眼可见 */
            fault_record(g_shm, FAULT_ISR_OVER, g_tick_count, di, EXEC_BUDGET_TB);
        }
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
        rtc_latch();                 /* ★ 刷新 RTC 镜像 (自带降频) ⇒ 黑匣子才记得到挂钟 */
        /* ★★★ 黑匣子快照 (表 A 的 #10) —— **本拍第二个 AXI 写手**。
         *   干涉分析见 docs/ARCH-TIMELINE-CPU-MDMA.md:
         *   它与 MDMA 锁存链 (表 B) 都从拍边界起步、都要访问 AXI,
         *   实测 MDMA 路径 σ ≈ 61 ns 而 CPU 直写仅 ≈ 21 ns。
         *   ⇒ 这个开关用来做"**关掉黑匣子**"的对照: 若关掉后 σ 明显下降,
         *     说明 AXI 争抢是主因之一。默认开 (= 交付行为)。 */
        if (g_bb_off == 0u) {
            bb_kick(g_tick_count);   /* ★ 黑匣子: 拍尾快照 → AXI 环形缓冲 (MDMA 后台搬运) */
        }

        /* ★★★ G6-1: I2C 事务状态机 —— **每拍推进一个相位**（契约 §3.6 约束①）。
         *   放在 ISR **最尾部**: 它不属于数据链（不参与采样→计算→输出），
         *   放这里才能保证它**不扰动扫描段的时序**（本项目反复强调的"别把实时量挂错位置"）。
         *   ★ 开销有界: 一个相位最多一个字节(9 位) ≈ 9000 cyc @400kHz ≈ 预算(32000)的 28%;
         *     空闲时它是 `s_phase == PH_IDLE` 的早退 ⇒ 常数级。
         *   ★ 就绪门: 事务**跨拍**（一次 2 字节读 = 8 拍 ≈ 800µs）⇒ 使用者必须等
         *     `i2c_sm_status() == OK` 才能取结果, **不能假设"发请求即得值"**。 */
        i2c_sm_tick();

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

    /* ★★ ISR 出口: 拉低探针 + 记"走完一次" —— **必须放在单出口处**。
     *   本 ISR 无提前 return (已核) ⇒ 这一个出口就够; 若将来加了提前 return,
     *   必须同时补 hb_clr(), 否则探针会**永久拉高**、把判据变成恒真的假报警。
     *   ★ 这也让 `g_hb_isr_tog` 成为"**完成次数**": ISR 卡住时它停止增长。 */
    ISR_CKPT(7);      /* ⑦ 出口 (走完 ⇒ 说明本拍完整) */
    hb_clr(HB_ISR_PIN);
    g_hb_isr_tog++;
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

/* 拒绝原因码 —— 让"为什么被拒"可被外部读走, 而不是只有一串 NAK 文本。
 * 文本对人类友好, 码对**脚本判据**友好 (脚本比对字符串太脆, 改一个字就失效)。
 * ★ 2026-09-13 从 W1 段上移到 nak() 之前: nak() 现在要把拒因码记进故障台账,
 *   而台账登记点在 nak() 内 —— 宏必须先可见 (原位置在 nak() 之后, 会编译不过)。 */
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

static void nak(const char *m)
{
    uint32_t n = 0;
    while (m && m[n]) n++;
    g_nak_count++;
    /* ★ 台账: 协议层"被拒"必须留案底 (否则只有 g_nak_count 一个总数, 不知道**为什么**被拒)。
     *   上下文带 g_nak_last = 具体拒因码 (NAKRH_*), 排障时一眼可辨。
     *   ★ 为什么挂在 nak() 里而不是各拒绝点: 本函数是**唯一的拒绝出口** ——
     *     一处登记覆盖全部 (若将来新增拒绝点忘了登记, 是"少记"不是"记错",
     *     且 PC 侧有"NAK 数 == 台账 NAK 数"这条交叉判据兜底)。 */
    fault_record(g_shm, (g_nak_last == NAKRH_BUDGET) ? FAULT_DEPLOY_REJ : FAULT_PROTO_NAK,
                 g_tick_count, (uint32_t)g_nak_last, n);
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
/* ★★★ 2026-09-15 (S5): 把 h_deploy 的全部静态校验**抽成一个函数**。
 *   动机: 契约 §7 第 5 道闸要求"**装载持久化程序时, 重跑与上传时同一套静态校验**"。
 *   如果那条路径另写一份校验, 就是本项目的老族 —— "同一个语义两处存放, 只改一处就静默失效"。
 *   ⇒ 唯一的校验实现就是这一个函数, 上传路径(h_deploy)与装载路径都调它。
 *   ★ 本函数**不碰任何计数器** —— 计数由调用者按自己的语义记 (deploy_nak / load_reject)。
 *   @param budget_out 非空时输出算出的每拍成本 (仅在返回 NULL 时有意义)
 *   @retval NULL = 通过; 否则 = 拒绝串 (与旧 h_deploy 逐字一致, 老上位机不受影响) */
static const char *prog_validate(const uint8_t *p, uint32_t n, uint32_t *budget_out)
{
    if (n < 6) return "short";
    uint16_t nr = get16(p), np = get16(p + 2), ns = get16(p + 4);
    if (nr > MAX_ROUTES || np > MAX_PARAMS || ns > MAX_STATES) return "counts exceed max";
    uint32_t need = 6u + ((uint32_t)nr + np + ns) * 16u;
    if (n < need) return "payload short";
    const uint8_t *d = p + 6;
    const uint8_t *pd = d + (size_t)nr * 16u;

    /* ① 参数有限性: NaN/Inf 会经 DIRECT/SCALE/积分直接传播成 NaN 输出。
     *    只查每字的高 8 位全 1 (即指数 0xFF) —— 不用浮点比较, 也不依赖 FPU 状态。 */
    for (uint16_t i = 0; i < (uint16_t)(np * 4u); i++) {
        if (((get32(pd + (size_t)i * 4u) >> 23) & 0xFFu) == 0xFFu) return "param not finite";
    }

    /* ② 逐条校验 + dst 唯一写者 (两条路由写同一个 wire = 结果取决于表序, 非确定性) */
    uint64_t dst_seen[2] = { 0, 0 };
    for (uint16_t i = 0; i < nr; i++) {
        RouteEntry_t r;
        memcpy(&r, d + (size_t)i * 16u, 16u);
        if (!(r.flags & ROUTE_FLAG_ACTIVE)) continue;
        const char *err = engine_route_validate(&r);
        if (err) return err;
        if (r.op == OP_LPF) {                       /* v0.2: LPF 参数是时间常数 τ 秒, 必须 > 0 */
            uint32_t tb = get32(pd + (size_t)r.param_idx * 16u);
            if ((tb & 0x7FFFFFFFu) == 0u) return "lpf tau must be >0";
        }
        uint64_t bit = 1ULL << (r.dst_channel & 63u);
        if (dst_seen[r.dst_channel >> 6] & bit) return "dst conflict";
        dst_seen[r.dst_channel >> 6] |= bit;
    }

    /* ★★ 跨档速率检查 (S3 语义, 由 S3 回归套件 T18 发现 H723 **漏了这一步**):
     *   慢消费者读快生产者 = **欠采样** → 混叠/发散。判据: 生产者 div_idx < 消费者 div_idx 即拒。
     *   ★ 这是**程序级**检查 —— 单看一条路由看不出来, 必须先知道"那个 wire 的生产者是谁"。 */
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
            if (pdiv >= 0 && pdiv < cdiv) return "rate mismatch";
        }
    }

    /* ③ 预算门: 条数限制 ≠ 成本限制 —— 128 条 PID 是 128 条, 成本却是 DIRECT 的 2.6 倍。
     *    Σ ceil((op_cost+src_cost)/div倍率) 必须 ≤ EXEC_DEPLOY_BUDGET,
     *    否则放行的程序会把拍吃掉 (阶段 2 审计真的踩到过一次 102.9% 超载)。 */
    uint32_t budget = engine_prog_budget(d, nr);
    if (budget_out) *budget_out = budget;
    if (budget > EXEC_DEPLOY_BUDGET) return "exec budget exceeded";
    return NULL;
}

/* ★★★ 2026-09-16: **"把一份程序装载并生效"的序列只能有一份。**
 *   起因(实测): 我的开机自动装载是**手抄** `h_deploy` 的收尾, 结果**漏掉了
 *   `SHM_U8(OFF_CTRL_RELOAD) = 1`** ⇒ staging 写好了, 但 ISR 永远不把它换到 ACTIVE
 *   ⇒ `0x38` 的 `n_routes` 一直是 0, 而 `deploy_seq` 已经是 1（"装而不生效"）。
 *   ⇒ 抽成这一个函数：`h_deploy`（上位机部署）与开机装载都调它。
 *   ★ 与 `prog_validate` 完全同一条纪律 —— 本项目的老族
 *     "**同一个语义两处存放 ⇒ 只改一处就静默失效**", 我这次又踩了一次。
 * @retval 实际装进去的路由条数（= engine_stage_program 的返回值） */
static uint16_t eng_apply_program(const uint8_t *payload, uint16_t nr, uint16_t np, uint16_t ns)
{
    uint16_t nw = engine_stage_program(g_shm, payload, nr, np, ns);
    g_deploy_routes = nw;
    /* 新程序 = 新语义: 旧的 force 点位可能指向新程序里根本不存在的 wire。
     * 留着它会在下一拍把无关 wire 钉住, 而 PC 完全看不到(MASK 位还在但程序换了)。 */
    eng_force_clear(g_shm);
    g_force_clears++;
    g_deploy_seq++;
    SHM_U16(g_shm, OFF_CTRL_DEPLOY_SEQ) = (uint16_t)g_deploy_seq;
    g_deploy_set_tick = g_tick_count;
    __asm__ volatile("dsb" ::: "memory");
    SHM_U8(g_shm, OFF_CTRL_RELOAD) = 1;      /* ★ 单字节写 = 原子。**少了这句就是"装而不生效"** */
    __asm__ volatile("dsb" ::: "memory");
    return nw;
}

static void h_deploy(const uint8_t *p, uint32_t n)
{
    uint32_t budget = 0u;
    const char *verr = prog_validate(p, n, &budget);
    if (verr) { g_deploy_nak++; nak(verr); return; }
    g_deploy_budget = budget;
    const uint8_t *d = p + 6;
    uint16_t nr = get16(p), np = get16(p + 2), ns = get16(p + 4);

    /* ④ 装载 STAGING (不碰 ACTIVE) → 置 RELOAD 让 ISR 在下一拍原子切换
     * ★ 序列在 eng_apply_program 里, 与**开机自动装载**共用同一份 (见该函数注释) */
    (void)eng_apply_program(d, nr, np, ns);

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

/* 0x63 MB_DIAG — 把通信域诊断区整块吐出来 (128B)。
 * ★★ 为什么必须走协议口、不能用 pyocd 读: 实测 **pyocd 每次连接都会复位本板**
 *   (AXI 里的启动计数器: 6 次读取 → 启动次数 1..6, 一一对应), 而 SHM 诊断区在
 *   DTCM 上、上电会被清零 ⇒ **用 pyocd 读它 = 读一个刚被自己复位清零的值**:
 *   观测动作本身把证据毁了, 而且**看不出来**(读数看着很正常)。
 *   这正是"非侵入式交互"要解决的那类问题 —— 能走协议就别走调试器。 */
static void h_mb_diag(void)
{
    ack((const uint8_t *)SHM_PTR(g_shm, OFF_MB_DIAG), OFF_MB_DIAG_SZ);
}

/* 0x64 SYS_MANIFEST [page:u8 可选] → ACK [total:u8][n:u8][条目…]
 *
 * ★★ 这是"管理面"的入口 (2026-09-13, 见 src/manifest.h 的长注释):
 *   板子把自己的**诊断资源目录**说出来 —— 名字 / 地址 / 长度 / 解释方式。
 *   目的有三:
 *     ① **消灭"PC 端硬编码地址"**: 今天两次踩到 (硬编码 SHM 基址读出台账假故障;
 *        手抄各诊断区偏移)。按名字读 ⇒ 布局怎么挪都不会错。
 *     ② **回答"哪里出问题读什么"**: 诊断知识从人的记忆搬进代码。
 *     ③ **看门狗的前置**: 复位归因需要的 BOOT_AXI 在 AXI, 靠只读窗 + 本目录即可
 *        走常规通信读到, 不需要调试器。
 *
 * ★ 分页而非一次全回: 目录会长大; 每页 4 条 = 82 字节, 帧小、栈占用固定。
 *   第一字节回**总条目数**, PC 据此知道翻几页。
 * ★ 非阻断保证: 只读 + 固定长度 + 不写 SD / 不停引擎 / 不等待任何外设。 */
#define MF_PER_PAGE 4u
static void h_manifest(const uint8_t *p, uint32_t n)
{
    static uint8_t r[2u + MF_PER_PAGE * 20u];
    uint32_t page  = (n >= 1u) ? (uint32_t)p[0] : 0u;
    uint32_t first = page * MF_PER_PAGE;
    uint32_t cnt   = (first < MANIFEST_N) ? (MANIFEST_N - first) : 0u;
    uint32_t shm   = g_shm_addr;
    if (cnt > MF_PER_PAGE) cnt = MF_PER_PAGE;
    r[0] = (uint8_t)MANIFEST_N;
    r[1] = (uint8_t)cnt;
    for (uint32_t i = 0u; i < cnt; i++) {
        const ManifestEnt_t *e = &g_manifest[first + i];
        uint8_t *o = r + 2u + i * 20u;
        for (uint32_t k = 0u; k < 12u; k++) o[k] = (uint8_t)e->name[k];
        put32(o + 12u, (e->flags & MF_F_SHM) ? (shm + e->addr) : e->addr);
        o[16] = (uint8_t)(e->words & 0xFFu);
        o[17] = (uint8_t)(e->words >> 8);
        o[18] = e->kind;
        o[19] = e->flags;
    }
    ack(r, 2u + cnt * 20u);
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
/* 0x39 PIN_PATTERN — 引脚/器件**诊断命令族**（逐条见 docs/ARCH-H723.md §5.2.1）
 * 载荷 [op:u8] + op 专属字段；应答 0 / 40 / 64 / 96 B 不等。
 * ★★ **交付固件实际只有 8 个 op**: 0=关 · 1=开+清统计 · 2=引脚快照(64B) ·
 *   3=ISR 让路延时 · 4=黑匣子开关 · 17=AS5600 上电诊断(64B) ·
 *   18=AS5600 运行态(40B) · 19=步进+静态探针+寄存器透视(96B)。
 *   曾经存在过的 op=5..13 **只活在实验补丁**(docs/exp-2026-09-14-mdma-trigger)，
 *   op=12/14/15/16 全仓查无 ⇒ **"支持 op=1..19"是错的叙述**。
 * ★★★ **定位声明: 临时脚手架, 不是架构的一部分。**
 *   依据 docs/ASSESS-exp-standardization-2026-09-15.md 与 docs/PLAN-dcl-standardization.md:
 *     · 诊断量**未进 SHM、无 obs_anchor() 登记、不占能力位**;
 *     · **③层程序不得依赖它**; `op=19` 保留为兼容壳(步进控制另有正式入口)。
 *   ⇒ 读它的只能是**外部诊断工具**, 不能是程序或控制回路。
 * ★ 口径提醒(op=2): min/max 是**相邻两次"写 BSRR 之后读 DWT"的差值** —— 量的是
 *   "软件写引脚的时刻"间隔；与 LA 测到的**引脚真实边沿**是两回事, 两者之差
 *   = 从寄存器写入到引脚翻转的延迟。 */
static void h_pin_pattern(const uint8_t *p, uint32_t n)
{
    uint32_t op = (n >= 1u) ? p[0] : 0u;
    if (op == 1u) {
        g_ppat_min  = 0xFFFFFFFFu; g_ppat_max = 0u; g_ppat_prev = 0u;
        g_ppat_wr_n = 0u; g_ppat_val = 0u; g_ppat_on = 1u;
        g_ppat_b_short = 0u; g_ppat_b_low = 0u; g_ppat_b_ok = 0u;
        g_ppat_b_high = 0u; g_ppat_b_long = 0u; g_ppat_first = 0u;
        /* ★★★ 2026-09-14 纠正 (原则性错误, 用户指出): 第一版这里**把 MDMA ch0 停了**,
         *   理由是"它每拍把 ODR 覆盖成 0"。**那个处置是错的** ——
         *   `SHADOW → GPIOE_ODR` 这条硬件锁存链路 (定时器触发 + MDMA 搬运)
         *   **正是本项目的核心设计**: 它把"计算"与"输出"解耦, 引脚输出时刻由硬件
         *   决定而不是 CPU。**把它停掉 = 先把被测对象拆掉再测它, 数据没有意义。**
         *
         *   真正的问题只是"**影子缓冲是空的**":
         *     `GPIO_MASK=0` 时 `do_poll` 早退 ⇒ 从不写 shadow ⇒ MDMA 每拍搬 0
         *     ⇒ 引脚上是 0, 与本诊断写不进 BSRR 无关。
         *   ⇒ **正确修法: 让 shadow 有值**(本诊断自己写 shadow, 见 ISR 段),
         *      **而不是停 MDMA。**
         *
         *   所以这里改为**确保 MDMA 使能** (EN=1), 保证测的是**交付配置**。 */
        *(volatile uint32_t *)0x5200004Cu = 1u;   /* MDMA_CH0_CCR: EN=1 (恢复影子锁存) */
        __asm__ volatile("dsb; isb" ::: "memory");
        ack(NULL, 0u);
        return;
    }
    if (op == 0u) { g_ppat_on = 0u; ack(NULL, 0u); return; }
    if (op == 3u) {
        /* ★ 运行期设定 "ISR 入口让路延时": [op:u8][cyc:u32 LE]
         *   用途: 扫参数找"给 MDMA 让路"的最佳窗口, 判定抖动是否来自总线争抢。
         *   ★ 交付档不设它 (= 0)。 */
        g_isr_delay_cyc = (n >= 5u)
            ? (uint32_t)(p[1] | ((uint32_t)p[2] << 8) | ((uint32_t)p[3] << 16) | ((uint32_t)p[4] << 24))
            : 0u;
        ack(NULL, 0u);
        return;
    }
    if (op == 4u) {
        /* ★ 黑匣子开关: [op:u8][on:u8]  (1 = 关闭 bb_kick, 0 = 开启)
         *   用途: 去掉 ISR 内**第二个 AXI 写手**, 做干涉对照。 */
        g_bb_off = (n >= 2u && p[1] != 0u) ? 1u : 0u;
        ack(NULL, 0u);
        return;
    }
    if (op == 2u) {
        uint8_t r[64];
        put32(r +  0, g_ppat_on);
        put32(r +  4, g_ppat_wr_n);
        put32(r +  8, (g_ppat_min == 0xFFFFFFFFu) ? 0u : g_ppat_min);
        put32(r + 12, g_ppat_max);
        put32(r + 16, g_ppat_last);
        put32(r + 20, g_ppat_val);
        /* ★★ 自证三连 (不动 ISR, 只在被问时当场读 —— 不干扰被测对象):
         *   ① GPIOE_ODR 低 8 位: 若在 0..127 之间变化 ⇒ **写入真的进了寄存器**
         *   ② GPIOE_MODER 低 16 位: 应 = 0x5555 (每脚 01 = 推挽输出)
         *   ③ RCC_AHB4ENR: bit4 (GPIOEEN) 应为 1 */
        put32(r + 24, GPIO_ODR(DO_GPIO_PORT) & 0xFFu);
        put32(r + 28, GPIO_MODER(DO_GPIO_PORT) & 0xFFFFu);
        put32(r + 32, RCC_AHB4ENR);
        /* ★★★ 自写自读 (关键判据): 写一个**已知非零值**到 BSRR, 立刻把 ODR 读回来。
         *   若读回 0 ⇒ 这个口的写入**根本没生效** (时钟/地址/模式之外的第三种原因)。
         *   若读回非 0 (哪怕 != 0x7F, 因为 ISR 可能插进来改) ⇒ 写入通路是通的。
         *   ★ 为什么这个测法能无条件成立: ISR 写的值也在 0..127, 不为 0 (除非恰好 pat==0),
         *     所以"读到 0"只可能来自"写不进去"。 */
        GPIO_BSRR(DO_GPIO_PORT) = 0x0000007Fu;      /* 置位 PE0..PE6 */
        put32(r + 36, GPIO_ODR(DO_GPIO_PORT) & 0xFFu);   /* 立刻读回 */
        /* ★ 持久测试: 分档统计 (正常拍长 40000 cyc) */
        put32(r + 40, g_ppat_b_short);   /* <39000 */
        put32(r + 44, g_ppat_b_low);     /* 39000..39899 */
        put32(r + 48, g_ppat_b_ok);      /* 39900..40100 正常带 */
        put32(r + 52, g_ppat_b_high);    /* 40101..41000 */
        put32(r + 56, g_ppat_b_long);    /* >41000 */
        put32(r + 60, g_ppat_first);     /* 首次写时刻 (仅参考) */
        ack(r, 64u);
        return;
    }
    if (op == 17u) {
        /* ★ AS5600 上电诊断: 扫 4 组候选引脚对 (初次接线定位用) — 2026-09-15
         *   64 字节 = 16 word: [4×ACK][4×RAW][自检][拉低读回][释放读回][0,0]
         *   组序: 0=(PB10 SCL,PB11 SDA) 1=(接反) 2=(PB6,PB7) 3=(PB8,PB9)
         * ★ 判读: 自检必须通过(拉低读到 0), 否则 START 条件不成立, 谈 ACK 没意义。 */
        uint32_t a4[4] = {0}, rw[4] = {0}, st2 = 0u;
        as5600_scan(a4, NULL, rw, &st2);      /* ★ 别叫 ack —— 会遮蔽协议应答函数 ack() */
        uint8_t r[64];
        for (uint32_t k = 0u; k < 4u; k++) put32(r + k * 4u, a4[k]);
        for (uint32_t k = 0u; k < 4u; k++) put32(r + 16 + k * 4u, rw[k]);
        put32(r + 32, st2);
        put32(r + 36, g_as_scan_lo);
        put32(r + 40, g_as_scan_hi);
        put32(r + 44, 0u);
        put32(r + 48, 0u);
        put32(r + 52, 0u);
        put32(r + 56, 0u);
        put32(r + 60, 0u);
        ack(r, 64u);
        return;
    }
    if (op == 18u) {
        /* ★ AS5600 运行态 (2026-09-15): 40 字节 = 10 word
         *   [0]raw(0..4095) [1]deg×1000 [2]STATUS [3]MD磁铁OK [4]成功次数 [5]失败次数
         *   [6]最后错误码 [7]I2C事务数 [8]I2C成功 [9]I2C NAK次数
         * ★ 判读: ok_n 在涨且 err_n 不涨 ⇒ 稳定; deg×1000 转轴时应跟着变。 */
        uint8_t r[40];
        put32(r +  0, g_as_raw_v);
        put32(r +  4, g_as_deg_x1000);
        put32(r +  8, g_as_status);
        put32(r + 12, g_as_mag_ok);
        put32(r + 16, g_as_ok_n);
        put32(r + 20, g_as_err_n);
        put32(r + 24, g_as_last_err);
        put32(r + 28, g_i2c_tx_n);
        put32(r + 32, g_i2c_ok_n);
        put32(r + 36, g_i2c_nak_n);
        ack(r, 40u);
        return;
    }
    if (op == 19u) {
        /* ★★ 步进控制 (2026-09-15): [op][sub][arg:u32 LE]
         *   ★★★ sub=0 = **只查询(无动作)** —— 这个改动是被坑出来的:
         *     最初 sub=0 是"停止", 于是任何"只想读状态"的工具 (裸发一个 op=19) **都在悄悄停脉冲**。
         *     症状: 刚起脉冲, 下一次查询就变成"已停" —— 看起来像"限时逻辑坏了/电机不转",
         *     实际是**查询自己把脉冲关了**。★ 教训: **"读取"必须是零副作用的**, 否则它就是陷阱。
         *   sub=1 arg=频率Hz(0=停)  sub=2 arg=方向0/1  sub=3 arg=使能0/1
         *   sub=4 arg=限时毫秒(0=不限)  sub=5 arg=ENA极性  sub=6 = 停止+失能(安全态)
         * ★ 判据: 返回的是**实际**频率(由 ARR 反算), 不是请求值。
         * ★ 安全: 脉冲只由 CC1E 决定; 有了限时, 到点自动 step_stop_safe()。 */
        uint32_t sub = (n >= 2u) ? p[1] : 0u;
        uint32_t arg = (uint32_t)((n >= 6u)
            ? (uint32_t)(p[2] | ((uint32_t)p[3] << 8) | ((uint32_t)p[4] << 16) | ((uint32_t)p[5] << 24))
            : 0u);
        switch (sub) {
        case 0u: break;                            /* ★ 只查询, 零副作用 */
        case 1u: step_set_rate(arg); break;
        case 2u: step_set_dir(arg); break;
        case 3u: step_set_ena(arg); break;
        case 4u: step_set_deadline_ms(arg); break;
        case 5u: step_set_ena_pol(arg); break;     /* ENA 极性: 0=拉低使能 1=拉高使能 */
        case 6u: step_stop_safe(); break;          /* ★ 停止+失能 (安全态) */
        /* ★★★ 2026-09-15 新增 sub=7/8/9/10: **PA6 静态电平探针** —— 为"万用表判接线"而设。
         *   动机: 500Hz 方波在万用表 DC 档上读的是**平均值**(50% 占空 ⇒ 约 1.65V), 于是
         *   "接通"与"断路"被平均值糊成一团; 而且"脉冲还在不在跑"本身也是个变量
         *   (带限时, 到点自动停)。⇒ 把 PA6 从 TIM3 上摘下来, 输出一个**确定的静态电平**:
         *       PA6 = 0V   ⇒ 接通的 PUL− 会被光耦 LED 钳到 ~1.0~1.5V; 断路的 PUL− 停在 ~4.5~5V
         *       PA6 = 3.3V ⇒ 接通的 PUL− 抬到 ~2~4V;                断路的 PUL− 纹丝不动
         *   ⇒ 两个读数**一起看**, 一次判死: 线通不通 / 端子插错没 / 3.3V 高电平关不关得掉光耦。
         *   ★ 零风险: 进入前先关 CC1E(保证无脉冲), 只切 MODER/AFRL/OTYPER, 不碰别的资源。 */
        case 7u:                                   /* PA6 → GPIO 输出 **静态低 (0V)** */
        case 8u: {                                 /* PA6 → GPIO 输出 **静态高 (3.3V)** */
            step_set_rate(0u);                     /* 先停脉冲 (CC1E=0) */
            uint32_t b2 = 6u;                      /* PA6 */
            GPIO_MODER(0)  = (GPIO_MODER(0) & ~(3u << (b2 * 2u))) | (1u << (b2 * 2u));  /* 01=输出 */
            GPIO_PUPDR(0) &= ~(3u << (b2 * 2u));   /* ★ 无内部上下拉 —— 否则"高"可能是被拉出来的 */
            GPIO_OTYPER(0) &= ~(1u << b2);         /* 推挽 */
            if (sub == 8u) { GPIO_ODR(0) |=  (1u << b2); }
            else           { GPIO_ODR(0) &= ~(1u << b2); }
            break;
        }
        case 9u: {                                 /* PA6 → 还原 TIM3_CH1 (AF2, 无脉冲) */
            step_set_rate(0u);
            uint32_t b2 = 6u;
            GPIO_OTYPER(0) &= ~(1u << b2);         /* 还原推挽 */
            GPIO_MODER(0)  = (GPIO_MODER(0) & ~(3u << (b2 * 2u))) | (2u << (b2 * 2u));  /* 10=AF */
            GPIO_AFRL(0)   = (GPIO_AFRL(0)  & ~(0xFu << (b2 * 4u))) | (2u << (b2 * 4u)); /* AF2=TIM3 */
            break;
        }
        case 10u: {                                /* PA6 输出类型: arg=1 ⇒ **开漏** (候选修复) */
            uint32_t b2 = 6u;
            if (arg) { GPIO_OTYPER(0) |=  (1u << b2); }
            else     { GPIO_OTYPER(0) &= ~(1u << b2); }
            break;
        }
        default: break;
        }
        /* ★★★ 修 (2026-09-15): 原为 `uint8_t r[40]` 而本块写了 **68 字节** (r+0..r+67)
         *   ⇒ **栈上越界 28 字节**。它不报错、不崩, 只会把调用者的局部量/保存寄存器写坏 ——
         *   正是"跑着跑着行为不对、却查不出谁写坏的"那一类。
         *   ★ 最阴的地方: 读回来的 17 个字段**全是合理值** ⇒ 越界在观测面上**完全看不见**。
         *   判据只能来自"数一数最多写到哪个偏移"这种静态检查, 而非"读数看着对不对"。 */
        uint8_t r[112];
        put32(r +  0, g_step_rate_hz);
        put32(r +  4, g_step_dir);
        put32(r +  8, g_step_ena);
        put32(r + 12, g_step_deadline_tick);
        put32(r + 16, TIM_CCER(TIM3_BASE_ADDR));   /* 看 CC1E: 1=有脉冲 0=停 */
        put32(r + 20, g_step_arr);
        put32(r + 24, g_step_ccr1);
        put32(r + 28, SHM_U32(g_shm, OFF_CTRL_GPIO_MASK));
        put32(r + 32, g_as_raw_v);
        put32(r + 36, g_step_stop_n);
        /* ★ 上电安全态的直接证据: **引脚的实际电平**。r+40 的低半字节 = PE0..PE15。
         *   PE8..PE11 应为 1 (共阳接法下 = 光耦不导通 = ENA 失效 / 继电器不吸合)。 */
        put32(r + 40, GPIO_ODR(DO_GPIO_PORT));
        put32(r + 44, GPIO_MODER(DO_GPIO_PORT));
        put32(r + 48, g_step_ena_pol);      /* ENA 极性 (0=拉低使能) */
        /* ★ 诊断: PA6(TIM3_CH1) 到底有没有在动。
         *   判据三件套: TIM3_CNT 在变(计数器在跑) + PA6 MODER=10(AF) + AFRL=2(TIM3)。
         *   ★★★ 2026-09-15 更正: 上面那句"三者都对 ⇒ 引脚必然在翻转"**是错的**,
         *     而且是本项目"配置全对 ≠ 功能可用"的第 N 次复现 ——
         *     `MODER=AF` + `AFRL=2` + `CNT` 在跑 + `CC1E=1` **四件套全绿, 引脚仍然可能
         *     一个电平都不输出**(AF 号不对 / 通道没真正接到脚上 ⇒ 该脚一直是**高阻**)。
         *     ★ 唯一能证伪它的量是 **`GPIOx_IDR`**: IDR 读的是**引脚上的真实电平**,
         *       与"谁在驱动它"无关。把 IDR 读回来, 再对比 ODR:
         *         · ODR 说 1 / IDR 读 0  ⇒ 被外部拉低
         *         · ODR 说 0 / IDR 读 1  ⇒ **不是我们在驱动**(高阻 + 外部上拉) ← 关键判据
         *     ⇒ 见 r+76/r+80。 */
        put32(r + 52, TIM_CNT(TIM3_BASE_ADDR));
        put32(r + 56, GPIO_MODER(0));       /* GPIOA: PA6 = bit[13:12] */
        put32(r + 60, GPIO_AFRL(0));        /* PA6 在 AFRL: bit[27:24] */
        put32(r + 64, g_step_dt_max);       /* ★ step_tick 见过的最大 dt */
        put32(r + 68, GPIO_ODR(0));         /* ★ PA6 静态探针: **实际输出电平** (bit6) */
        put32(r + 72, GPIO_OTYPER(0));      /* ★ PA6 输出类型: bit6=1 开漏 / 0 推挽 */
        /* ★★★ 引脚**真实**电平 (IDR 与谁在驱动无关) —— 判"高阻 vs 被驱动"的唯一硬判据。
         *   比对 ODR: ODR=0 而 IDR=1 ⇒ **不是 MCU 在驱动这个脚**(高阻 + 外部上拉)。 */
        put32(r + 76, GPIO_IDR(0));             /* GPIOA: PA6 = bit6 */
        put32(r + 80, GPIO_IDR(DO_GPIO_PORT));  /* GPIOE: PE8..PE11 = bit8..11 */
        /* ★★★ 实时寄存器透视 (2026-09-15, 审计用)。动机:
         *   上面 r+20/r+24 报的是 **缓存** `g_step_arr`/`g_step_ccr1`(step_set_rate 自己记的),
         *   而**任何别处**改 TIM3 的运行期寄存器都不会反映到缓存里 ——
         *   典型如 `hil_outputs_safe()` 把 `TIM_CCR1` 清零 (它**没有** g_step_owns_tim3 门)。
         *   那样脉冲会变成**恒定低**(光耦持续导通), 而缓存读回依然显示"50% 占空比"。
         *   ⇒ 必须同时给出**实时**寄存器值, 否则"脉冲看着在出、轴不动"这类现象无从区分。
         *   ★ 同族教训: "同一个量两处存放"(缓存 vs 硬件) ⇒ 只改一处就静默失效。 */
        put32(r + 84, TIM_CCMR1(TIM3_BASE_ADDR));  /* OC1M/OC1PE/CC1S: 通道模式是否还是 PWM1 */
        put32(r + 88, TIM_CCR1(TIM3_BASE_ADDR));   /* ★ **实时** CCR1 (与 r+24 的缓存对比) */
        put32(r + 92, TIM_ARR(TIM3_BASE_ADDR));    /* ★ **实时** ARR  (与 r+20 的缓存对比) */
        ack(r, 96u);
        return;
    }
    if (op == 22u) {
        /* ★★★ G6-2 判据面: **人工占住总线**, 好让"占用期再申请必须被拒"这条判据能被跑到。
         * 为什么需要它: 真实的重叠窗口只有 ~250µs(阻塞)/~800µs(状态机), 从串口根本抓不到 ⇒
         *   没有这个诊断口, 那条判据就**永远无法执行**（"判据不能失败 = 判据不存在"）。
         * 载荷 [op][sub][arg]: sub=0 占住(arg=拍数, 默认 500=50ms, 上限 2000) · 1 释放 · 2 只查询
         * ★ 安全: 占用**有界** —— ISR 尾部按 tick 到期自动释放, 忘了解也不会把总线永久占死。
         * 应答 32B:
         *   +0 acquire 结果(1=拿到 0=被占) +4 当前 owner(0=NONE 1=BLOCKING 2=SM)
         *   +8 被门拦下的次数(**占用期再申请 ⇒ 这个数必须涨**)
         *   +12 状态机被门拒的次数 +16 状态机 status +20 状态机 phase
         *   +24 阻塞路径 tx 计数 +28 阻塞路径 nak 计数（占用期间它们不应增长）
         *   +32 引用计数（**正常空闲必须 == 0** —— 给"引用计数泄漏"准备的判据）*/
        uint32_t sub = (n >= 2u) ? p[1] : 0u;
        uint32_t arg = (uint32_t)((n >= 6u)
            ? (uint32_t)(p[2] | ((uint32_t)p[3] << 8) | ((uint32_t)p[4] << 16) | ((uint32_t)p[5] << 24))
            : 500u);
        uint8_t rx[36];
        uint32_t got = 1u;
        if (sub == 0u) {
            if (arg == 0u) { arg = 500u; }
            if (arg > 2000u) { arg = 2000u; }        /* ≤200ms: 有界 */
            got = i2c_bus_acquire(I2C_OWNER_BLOCKING);
            g_i2c_hold_until = got ? (g_tick_count + arg) : 0u;
        } else if (sub == 1u) {
            i2c_bus_release(I2C_OWNER_BLOCKING);
            g_i2c_hold_until = 0u;
        }
        put32(rx +  0, got);
        put32(rx +  4, (uint32_t)i2c_bus_owner());
        put32(rx +  8, g_i2c_bus_busy_n);
        put32(rx + 12, g_i2c_sm_gate_n);
        put32(rx + 16, i2c_sm_status());
        put32(rx + 20, i2c_sm_phase());
        put32(rx + 24, g_i2c_tx_n);
        put32(rx + 28, g_i2c_nak_n);
        put32(rx + 32, (uint32_t)i2c_bus_refs());
        ack(rx, 36u);
        return;
    }
    if (op == 21u) {
        /* ★★★ 时基健康度（2026-09-16）—— 交付固件此前**没有**"时基在走"的可读量:
         *   `0x39 op=7`(重新校时)只活在实验补丁里 ⇒ 时基死了在协议面上**无法自证**
         *   ⇒ 死时基会让所有统计**看起来完美稳定**（本项目铁律第 1 条要防的空判据）。
         * 应答 40B（每项都能失败）:
         *   +0 时基档 (1=TIM5 交付 / 0=DWT 改前对照)   +4 时基频率 Hz
         *   +8 **自检探针 Δ**（=0 即时基是死的）        +12 时基不动而 DWT 在动 的次数
         *   +16 **DWT 不动而时基在动 的次数**（调试器停的——跑一次 pyocd 就该涨）
         *   +20 最近每拍时基增量                       +24 pmin(哨兵→0)  +28 pmax
         *   +32 ISR 执行最大周期                       +36 超预算次数
         * ★ 判据: 若 DWT 被杀, `+16` 应从 0 涨起来, 而 `+24/+28` **照常正常** —— 这就是"换了
         *   时基之后调试器再也弄不坏我们"的正证据。 */
        uint8_t rx[40];
        put32(rx +  0, (uint32_t)DCL_TIMEBASE);
        put32(rx +  4, (uint32_t)TB_HZ);
        /* ★ 探针必须有**间距**：连读两次同一寄存器恒为 0 ⇒ 那会是一条**空判据**
         *   （本项目最常见的假判据形态）。这里夹一段 nop 循环，Δ=0 才真的意味着"时基死了"。 */
        {
            uint32_t a = tb_cyc();
            for (volatile uint32_t i = 0u; i < 400u; i++) { __asm__ volatile("nop"); }
            put32(rx + 8, tb_cyc() - a);
        }
        put32(rx + 12, g_tb_dead_n);
        put32(rx + 16, g_dwt_dead_n);
        put32(rx + 20, g_tb_cyc_last);
        put32(rx + 24, (g_per_cyc_min == 0xFFFFFFFFu) ? 0u : g_per_cyc_min);
        put32(rx + 28, g_per_cyc_max);
        put32(rx + 32, g_isr_cyc_max);
        put32(rx + 36, g_isr_overrun);
        ack(rx, 40u);
        return;
    }
    if (op == 20u) {
        /* ★★★ G6-1 诊断面: I2C 事务状态机（契约 §3.6 四条约束 / §3.7 实施与验收）
         * ★ 为什么先放**诊断族**而不开新协议命令: 契约 §3.7 定案 ——
         *   **在状态机被证明之前, 不得把它挂上契约面**（否则等于把未验证的东西写进契约）。
         *   本族已声明"临时脚手架, 不是架构的一部分" ⇒ 放这里合规。
         * 载荷 [op][sub][arg]:
         *   sub=0 发起一次读 (AS5600 0x36 / reg 0x0C / 2B = RAW ANGLE)
         *   sub=1 只读回状态与结果（**等就绪门**: 2 字节读 = 8 拍后再读才有值）
         *   sub=2 ping 0x36（只验 ACK, 不读数据）
         * 应答 48B（全部是"能失败"的量）:
         *   +0 status +4 phase +8 phase_cnt +12 tick_cnt
         *   +16 req_n +20 ok_n +24 nak_n +28 stuck_n +32 gate_n +36 ticks_n
         *   +40 result_len +44 data(4B 打包) */
        uint32_t sub = (n >= 2u) ? p[1] : 0u;
        uint8_t rx[48];        /* ★ 名字必须与本函数内 op=19 的 `r[112]` 区分开 ——
                                *   否则 ackbuf 静态判据**按名字合并**两个缓冲, 报假阳性
                                *   （已实测: 它把"48 字节缓冲写到 96"报了出来, 那是误报）。
                                *   判据本身也已加固: 同名缓冲现在会被判为"不可判定"。 */
        if (sub == 0u) {
            (void)i2c_sm_request(0x36u, I2C_SM_OP_READ, 0x0Cu, NULL, 2u);
        } else if (sub == 2u) {
            (void)i2c_sm_request(0x36u, I2C_SM_OP_PING, 0u, NULL, 0u);
        }
        put32(rx +  0, i2c_sm_status());
        put32(rx +  4, i2c_sm_phase());
        put32(rx +  8, i2c_sm_phase_cnt());
        put32(rx + 12, i2c_sm_tick_cnt());
        put32(rx + 16, g_i2c_sm_req_n);
        put32(rx + 20, g_i2c_sm_ok_n);
        put32(rx + 24, g_i2c_sm_nak_n);
        put32(rx + 28, g_i2c_sm_stuck_n);
        put32(rx + 32, g_i2c_sm_gate_n);
        put32(rx + 36, g_i2c_sm_ticks_n);
        uint8_t d[4] = { 0u, 0u, 0u, 0u };
        uint32_t got = i2c_sm_result(d, 4u);
        put32(rx + 40, got);
        put32(rx + 44, (uint32_t)d[0] | ((uint32_t)d[1] << 8)
                     | ((uint32_t)d[2] << 16) | ((uint32_t)d[3] << 24));
        ack(rx, 48u);
        return;
    }
    nak("bad pin pattern op");
}

/* ══════════ S5 (2026-09-15): DCL 程序持久化 —— 事务式上传 ══════════
 * 契约: docs/REF-program-contract.md §4.2 / §7。传输层成帧细节见 transport.h 的 0x45–0x49。
 *
 * 为什么事务式: 0x10 载荷满配 = 6150 = FRAME_PAYLOAD_MAX ⇒ 零余量 (契约 GAP-2)。
 * 五道闸在这里怎么落:
 *   闸1 帧 CRC16        → 传输层 (已有)
 *   闸2 清单 CRC32+len  → h_prog_commit: 收齐长度 + 对**收到的字节**算 CRC32
 *   闸3 写完回读比对    → prog_store_save 内部
 *   闸4 A/B 双副本+seq  → prog_store_save 内部
 *   闸5 同一套静态校验  → h_prog_commit 与开机装载**都调 `prog_validate`** (同一个函数, 不是两份)
 *
 * ★ 上传事务与落盘**共用同一个载荷缓冲** (`prog_store_buf()`), 不在 DTCM 里摆两份 7.5KB。
 */
static uint8_t  s_txn_busy = 0u;
static uint32_t s_txn_total = 0u;
static uint32_t s_txn_got = 0u;
static uint32_t s_txn_crc = 0u;
static ProgManifest_t s_txn_mf;

OBS uint32_t g_prog_txn_begin  = 0;   /* 收到 BEGIN 的次数 */
OBS uint32_t g_prog_txn_commit = 0;   /* 成功落盘的次数 */
OBS uint32_t g_prog_txn_abort  = 0;   /* 中途弃掉的次数 (长度不符/CRC 错/校验拒/写失败) */
OBS uint32_t g_prog_reject_why = 0;   /* 最近一次拒绝的原因码 (见 prog_store.h 的 PROG_RC_*) */
OBS uint32_t g_prog_boot_rc    = 0xFFFFFFFFu;  /* 开机装载返回码 (PROG_RC_*) */
OBS uint32_t g_prog_boot_loaded = 0;  /* 1 = 开机确实装载了一份程序到 STAGING */

static void put16le(uint8_t *b, uint16_t v)
{
    b[0] = (uint8_t)(v & 0xFFu);
    b[1] = (uint8_t)((v >> 8) & 0xFFu);
}

static void h_prog_begin(const uint8_t *p, uint32_t n)
{
    if (n < 16u) { nak("need manifest"); return; }
    s_txn_total = get32(p);
    s_txn_crc   = get32(p + 4);
    s_txn_mf.prog_id  = get32(p + 8);
    s_txn_mf.prog_ver = get16(p + 12);
    s_txn_mf.min_fw   = get16(p + 14);
    s_txn_mf.req_caps = (n >= 20u) ? get32(p + 16) : 0u;

    if (s_txn_total < 6u || s_txn_total > prog_store_buf_sz()) { nak("bad total"); return; }
    /* ★ R3: 版本门与能力门必须在**上传期**判 —— 契约 §4.5「任何不满足必须在上传期失败,
     *   禁止运行期静默给假值」。这是 SRC_HMI 已经立下的好范式, 这里推广。 */
    if (s_txn_mf.min_fw != 0u && (uint32_t)s_txn_mf.min_fw > (uint32_t)DCL_FW_VERSION_H723) {
        g_prog_reject_why = PROG_RC_MINF; nak("min_fw too high"); return;
    }
    if ((s_txn_mf.req_caps & ~(uint32_t)DCL_CAP_H723_IMPL) != 0u) {
        g_prog_reject_why = PROG_RC_CAPS; nak("cap missing"); return;
    }
    s_txn_got = 0u; s_txn_busy = 1u;
    g_prog_txn_begin++;
    uint8_t r[4]; put32(r, s_txn_total);
    ack(r, 4);
}

static void h_prog_data(const uint8_t *p, uint32_t n)
{
    if (!s_txn_busy) { nak("no txn"); return; }
    if (n < 5u) { nak("need offset+data"); return; }
    uint32_t off = get32(p);
    uint32_t len = n - 4u;
    /* ★ 必须顺序、不跳不重: 允许跳会让"洞"因为后面的 CRC 也算不出来而变成静默损坏 */
    if (off != s_txn_got) { g_prog_txn_abort++; s_txn_busy = 0u; nak("bad offset"); return; }
    if (s_txn_got + len > s_txn_total) {
        g_prog_txn_abort++; s_txn_busy = 0u;
        g_prog_reject_why = PROG_RC_LEN; nak("overflow"); return;
    }
    memcpy(prog_store_buf() + s_txn_got, p + 4, len);
    s_txn_got += len;
    uint8_t r[4]; put32(r, s_txn_got);
    ack(r, 4);
}

static void h_prog_commit(void)
{
    uint8_t r[8];
    uint32_t budget = 0u;
    if (!s_txn_busy) { nak("no txn"); return; }
    if (s_txn_got != s_txn_total) {
        s_txn_busy = 0u; g_prog_txn_abort++; g_prog_reject_why = PROG_RC_LEN;
        nak("incomplete"); return;
    }
    /* ★闸2: 对**收到的字节**算 CRC32 (不是信客户端报的) */
    if (dcl_crc32(prog_store_buf(), s_txn_total) != s_txn_crc) {
        s_txn_busy = 0u; g_prog_txn_abort++; g_prog_reject_why = PROG_RC_CRC;
        nak("crc mismatch"); return;
    }
    /* ★闸5 (落盘前): 存进去的东西必须是"能跑的" —— 调的是上传路径同一个 prog_validate */
    {   const char *err = prog_validate(prog_store_buf(), s_txn_total, &budget);
        if (err) { s_txn_busy = 0u; g_prog_txn_abort++;
                   g_prog_reject_why = PROG_RC_VALIDATE; nak(err); return; } }
    /* 落盘 (内部含闸3 回读比对 + 闸4 A/B+seq, 头最后写)
     * ★★ 必须开**阻塞窗口**: 这里是"写 1 块 + 读回 1 块", SD 侧一旦超时就会超过
     *    1.2 s 的停滞阈值 ⇒ 被判主循环死了 ⇒ 复位(实测事故)。窗口让停滞判据在这段时间不判。 */
    {   int rc;
        uint32_t _w = block_window_begin(PROG_BLOCK_TICKS);
        rc = prog_store_save(prog_store_buf(), s_txn_total, &s_txn_mf);
        block_window_end(_w);
        s_txn_busy = 0u;
        if (rc != PROG_RC_OK) { g_prog_txn_abort++; g_prog_reject_why = (uint32_t)rc;
                                nak("store failed"); return; }
        put32(r, (uint32_t)rc); put32(r + 4, budget);
        g_prog_txn_commit++;
        ack(r, 8); }
}

static void h_prog_status(void)
{
    ProgStoreInfo_t o;
    uint8_t r[112];       /* ★ 2026-09-16: 64 → 96 (尾部追加开机装载结果, 见下方注释) */
    prog_store_probe(&o);
    put32(r +  0, o.part_ok);
    put32(r +  4, o.ab_valid);
    put32(r +  8, o.seq_a);
    put32(r + 12, o.seq_b);
    put32(r + 16, o.crc_a);
    put32(r + 20, o.crc_b);
    put32(r + 24, o.len_a);
    put32(r + 28, o.len_b);
    put32(r + 32, o.active);
    put32(r + 36, o.ok_n);
    put32(r + 40, o.fail_n);
    put32(r + 44, o.reject_n);
    put32(r + 48, o.last_rc);
    put32(r + 52, g_prog_txn_begin);
    put32(r + 56, g_prog_txn_commit);
    put32(r + 60, g_prog_txn_abort);
    /* ★★★ 2026-09-16 追加 (一直缺的那块可观测性): **开机自动装载的结果必须能被外部读走**。
     *   规格: 契约要求"装载失败/结果必须可被外部读走 —— 否则装载失败在观测面上不可见"。
     *   之前只能靠 SWD 读 DTCM 里的 `g_prog_boot_*`/`g_deploy_routes`, 而 SWD 会停核(踩过)。
     *   ⇒ 尾巴追加 4 字, 长度 64 → 80。 */
    put32(r + 64, g_prog_boot_rc);       /* 开机装载返回码 (PROG_RC_*) */
    put32(r + 68, g_prog_boot_loaded);   /* 1 = 开机确实把一份程序装进了 STAGING */
    put32(r + 72, g_deploy_routes);      /* engine_stage_program 回报的 ACTIVE 条数 */
    put32(r + 76, g_deploy_seq);         /* 部署序号 */
    /* ★★★ 2026-09-16: **"我们从卡上到底读回了什么"必须能被看见。**
     *   起因: `boot_rc=0`(成功) `boot_loaded=1`(装了) 而 `deploy_routes=0` —— 三者看似矛盾。
     *   矛盾的唯一去处就是**载荷内容**: 若 `payload[0..1]`(nr) 是 0, 那 stage 数出 0 条就完全自洽。
     *   ⇒ 尾巴再追加载荷头 16 字节 (4 字), 长度 80 → 96。
     *   ★ 判据: 读回来的 `pl[0..1]` 必须等于上传时声明的 nr。 */
    {   const uint8_t *pl = prog_store_buf();
        put32(r + 80, (uint32_t)(pl[0] | ((uint16_t)pl[1] << 8) | ((uint32_t)pl[2] << 16) | ((uint32_t)pl[3] << 24)));
        put32(r + 84, (uint32_t)(pl[4] | ((uint16_t)pl[5] << 8) | ((uint32_t)pl[6] << 16) | ((uint32_t)pl[7] << 24)));
        put32(r + 88, (uint32_t)(pl[8] | ((uint16_t)pl[9] << 8) | ((uint32_t)pl[10] << 16) | ((uint32_t)pl[11] << 24)));
        put32(r + 92, (uint32_t)(pl[12] | ((uint16_t)pl[13] << 8) | ((uint32_t)pl[14] << 16) | ((uint32_t)pl[15] << 24)));
    }
    ack(r, 96);
}

static void h_prog_erase(void)
{
    uint8_t r[4];
    int rc;
    uint32_t _w = block_window_begin(PROG_BLOCK_TICKS);   /* ★ SD 双块写: 见 PROG_BLOCK_TICKS */
    rc = prog_store_erase();
    block_window_end(_w);
    put32(r, (uint32_t)rc);
    if (rc == PROG_RC_OK) { g_prog_reject_why = PROG_RC_OK; ack(r, 4); }
    else { nak("erase failed"); }
}

/* 0x4A DEVICE_DESC —— 我们的 "ESI 等价物" (契约 §3.5)。
 * 目的: 让上位机在**编译期/上传期**就知道本机有什么能力、有什么具名设备,
 *       而不是跑起来才发现。★ 这是把"未实现的能力必须在上传期失败"做成机器可判的一步。 */
static void h_device_desc(void)
{
    uint8_t r[64];
    uint32_t k = 0u;
    uint16_t caps = (uint16_t)(DCL_CAP_H723_IMPL & 0xFFFFu);
    put16le(r + k, (uint16_t)DCL_FW_VERSION_H723); k += 2u;   /* fw_ver */
    put16le(r + k, caps); k += 2u;                            /* cap_lo */
    put16le(r + k, 0u); k += 2u;                              /* cap_hi (为 u32 扩展预留) */
    put16le(r + k, 6u); k += 2u;                              /* 具名设备条数 */
    /* 具名设备表: [device_type:u16][product_code:u16][revision:u16] × N
     * ★ 这是"本机有什么"的**声明**; 槽号/op 号见各设备自己的头文件。
     *   revision 变了就表示行为变了 —— 与 CiA 301 的 Identity Object(0x1018) 同一语义。 */
    static const uint16_t k_dev[6][3] = {
        { 1u, 1u, 1u },   /* DI   4ch            */
        { 2u, 1u, 1u },   /* AI   3ch            */
        { 3u, 1u, 1u },   /* DO  16ch (A 档下 0..7 不可用, 见 GAP) */
        { 4u, 1u, 1u },   /* PWM  HIL / TIM3_CH1 */
        { 5u, 1u, 2u },   /* AS5600 磁编码器 rev2 (SCL/SDA 更正后) */
        { 6u, 1u, 2u },   /* TB6600 步进     rev2 (光耦正端改 3.3V) */
    };
    for (uint32_t i = 0; i < 6u; i++) {
        put16le(r + k, k_dev[i][0]); k += 2u;
        put16le(r + k, k_dev[i][1]); k += 2u;
        put16le(r + k, k_dev[i][2]); k += 2u;
    }
    ack(r, k);
}

/* ★★★ 0x38 应答契约长度 —— **一个常量同时定"缓冲大小"与"发送长度"**。
 * 2026-09-16 发现真缺陷: 原来缓冲区写死 `uint8_t r[40]`, 而 `ack(r, 51)`
 *   ⇒ **越界 11 字节**, 写坏调用者的栈。它是本项目"**一个语义两处存放 ⇒ 静默失效**"
 *   族的再现（把 `0x38` 从 39 扩到 51 时，只改了写的偏移，没同步改缓冲声明）。
 * ★ 这类缺陷**运行期完全看不出来**（不报错、不崩，读回的字段全是合理值）
 *   ⇒ 只能靠**静态**判据: `tools/h723_ackbuf_check.py`（已纳入构建闸门）。
 * ⇒ 变更契约长度时**只改这一个数**，不要在两处各写一遍字面量。 */
#define ENG_STATUS_LEN 51u

static void h_engine_status(void)
{
    uint8_t r[ENG_STATUS_LEN];
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
    /* ══════════ ★★★ 2026-09-16 追加: **接收环的健康度** (39 → 51 字节) ══════════
     * 动机: 接收环 `RX_RING_SZ = 512`，而协议允许 6150 字节的载荷。**帧大小本身不受环限制**
     *   （实测 1036 字节的 `0x23` 也能整帧收到并写对 —— 因为主循环**边收边排空**，环只是延迟缓冲）。
     *   **真正的风险是"主循环阻塞期间到达的字节"**：阻塞超过 ~44 ms（= 512 B ÷ 11.5 KB/s）
     *   环就写满 ⇒ 按设计**丢弃并计数**（`s_drop_cnt`）⇒ **帧被静默截断**。
     * ★ 而 `g_uart_ore/g_uart_drop` 原来**只刷进全局量、不进任何应答面** ⇒
     *   **"帧被截断"在协议面上完全不可见**（只能 SWD 读 DTCM，而 SWD 会停核）。
     *   ⇒ 违反本项目自己的纪律"新观测变量必须能被外部读走"。
     * ★ 判据（能失败的）：**这两个计数在正常收发下必须恒为 0**。
     *   非 0 ⇒ 有帧被截断 ⇒ 该次"NAK/无应答"**不是固件的判断，而是数据没到全**。
     *   ⇒ 它把"帧损坏"与"固件拒绝"这两件在读数上一样的事**分开**（同 r[38] 的理由）。 */
    put32(r + 39, g_uart_ore);    /* 硬件溢出次数 (ORE) —— 非 0 = 排空太慢 */
    put32(r + 43, g_uart_drop);   /* 软件满丢弃次数 —— 非 0 = 环写满 */
    put32(r + 47, g_frame_bad);   /* 坏帧数 (CRC/长度不符; 含"数据没到全"造成的坏帧) */
    ack(r, ENG_STATUS_LEN);   /* 长度与缓冲区同源 —— 见 ENG_STATUS_LEN 的说明 */
}

/* ══════════ W1: 运行控制 + SHM 读写 (0x11/0x12/0x13 + 0x20-0x23) ══════════
 * 这 7 条命令的意义: 把引擎的配置态从**编译期旋钮**搬到**运行期协议**。
 * 在此之前"改一条路由"要重新编译+烧录; 之后 PC 一条帧就能读写。
 *
 * ★ 与 S3 的关系: 语义逐字搬, 地址表必须重写 (见 engine.h 守卫段说明)。
 * ★ 四个安全守卫必须搬全 (S3 main.c:83-140), 少一个就是一个可利用的洞:
 *     valid_addr / valid_range / write_allowed(NaN 防护) / outputs_safe
 */

/* 0x20 READ [addr:u32] → ACK [val:u32] */
static void h_read_w1(const uint8_t *p, uint32_t n)
{
    if (n < 4) { g_shm_rd_nak++; g_nak_last = NAKRH_SHORT; nak("need addr"); return; }
    uint32_t a = get32(p);
    if (!eng_valid_raddr(a)) { g_shm_rd_nak++; g_nak_last = NAKRH_ADDR; nak("bad addr"); return; }
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
    if (!eng_valid_rrange(a, (uint32_t)c * 4u)) {
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
#if !DCL_PERSIST_SAVE
        /* ★★ 显式拒绝 (2026-09-13 降级): 与其谎报成功, 不如明确说"本平台不提供"。
         *   旧行为: 真的去擦 flash ⇒ 拍 ISR 卡死 210ms ⇒ 看门狗复位 ⇒ **"保存"= "重启"**,
         *   且配置从未落盘。⇒ 现在直接 NAK, 板子不再被自己弄重启。 */
        g_persist_nak++;
        g_persist_last_err = 0xD15Au;
        nak("persist: save disabled on this platform (flash erase vs WDT; see docs/audit/H723-PERSIST-WDT-DEFECT.md)");
        return;
#endif
        g_persist_saves++;
        /* ★ 声明窗口: persist 落盘端到端**实测 ~1.55s** —— 已知长阻塞, 且**大于阈值**,
         *   所以不给它开窗就会被误判成"主循环死了"并复位 (每次刷盘误复位)。
         *   ★ begin/end 成对 ⇒ 块一结束窗口立即关 (不能只 open: 见 begin 的注释)。 */
        {   uint32_t _w = block_window_begin(PERSIST_BLOCK_TICKS);
            save_rc = persist_save(g_shm);
            block_window_end(_w); }
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
        case CMD_PIN_PATTERN:   h_pin_pattern(p, n); break;
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

        /* ★ S5: DCL 程序持久化 (事务式上传 → SD A/B 双副本) */
        case CMD_PROG_BEGIN:    h_prog_begin(p, n); break;
        case CMD_PROG_DATA:     h_prog_data(p, n); break;
        case CMD_PROG_COMMIT:   h_prog_commit(); break;
        case CMD_PROG_STATUS:   h_prog_status(); break;
        case CMD_PROG_ERASE:    h_prog_erase(); break;
        case CMD_DEVICE_DESC:   h_device_desc(); break;
        /* ---- W4: 通信域 (Modbus RTU 从站) ---- */
        case CMD_MB_INJECT:     h_mb_inject(p, n); break;
        case CMD_MB_RESP:       h_mb_resp(); break;
        case CMD_MB_CFG:        h_mb_cfg(p, n); break;
        case 0x63:              h_mb_diag(); break;   /* 通信域诊断区整块读回 */
        case 0x64:              h_manifest(p, n); break; /* ★ 诊断资源目录 (管理面入口) */
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
    sink ^= g_timebase_dead;              sink ^= g_opt_sr;              /* ★ 时基活性标志 (防 --gc-sections 回收) */
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

    /* ══════════ ★★★ 复位取证 (2026-09-13 重写) ══════════
     * 目的: 回答"**上一轮为什么停 / 停在哪 / 之前有多少故障**" —— 看门狗的价值全在这。
     *
     * ★★ 顺序是硬要求: 本段必须在 `g_stage = 1` **之前**。
     *    g_stage 住在 DTCM, 而 **DTCM 跨复位不丢**(系统复位不清 SRAM) ⇒
     *    此刻读到的还是**上一轮最后写的值** = "它卡在哪一步"。一旦先执行 g_stage=1,
     *    这个现场就被自己覆盖了。同理, 故障台账在 SHM(DTCM)里也还活着 ——
     *    但它马上会被 cold_start_reset() 的整段 memset 清掉, 所以**必须在这里抄走摘要**。
     *
     * ★★ 位定义抄权威 (ST stm32h723xx.h), **不是注释里那套**:
     *      16=RMVF(清除位) 17=CPURSTF 19=D1RSTF 20=D2RSTF 21=BORRSTF
     *      22=PINRSTF 23=PORRSTF 24=SFTRSTF
     *    ⚠️ 旧代码写的是 `|= (1u << 24)` —— 那清的是 **SFTRSTF**, 而 RMVF 在 **bit 16**
     *      ⇒ **复位标志从首次上电起从未被清过**, 实测读出 0x01FA0000 且 7 个域复位位同置。
     *    ⇒ 修法: **纯写 RMVF 位, 不做读改写**(避免把只读标志位写回的隐患)。
     *
     * ★ AXI(NOLOAD) 上电=随机 ⇒ 用 [1]"RCLK" 首次标记 + [29] 校验和把"上电垃圾"与
     *   "真现场"分开。0x24000500 起 32 字。 */
    {   volatile uint32_t *rc = (volatile uint32_t *)BOOT_REC_ADDR;

        /* ★ 先把上一轮的 ISR 段检查点搬到 [41] —— 必须在**拍 ISR 开始覆盖 [40] 之前**做。
         *   本段在启动早期执行, 而拍 ISR 从阶段 ⑤ 才开跑, 所以这里来得及。 */
        rc[BOOT_REC_W_CKPT_P] = rc[BOOT_REC_W_CKPT];
        const FaultLedger_t *lg = fault_ledger_r(g_shm);   /* 此刻 SHM 还没被清 */
        /* ★★ "上一轮卡在哪一步"的正确来源 (2026-09-13 修正 —— 此前它是个**空字段**):
         *   旧写法读 `g_stage` 并论证"DTCM 跨复位不丢" —— 论证**漏了一环**:
         *   g_stage 住在 `.bss`, 而**标准启动代码在 main() 之前就把 .bss 清零了**
         *   ⇒ 本段读到的永远是 0。实测: 拍 ISR 被注入挂死 (上一轮明明已跑到 stage=9),
         *     `--boot` 仍报 "停在 g_stage=0"。**一个恒为 0 的现场等于没有现场。**
         *   ⇒ 改从 AXI 的**活体镜像** [30]/[31] 取 —— 主循环每轮写当前值, 而 AXI 是
         *     (NOLOAD)、启动不清、跨复位不丢 ⇒ 复位后读到的就是上一轮的最后现场。
         *     顺带它还能当"**没复位时**此刻跑到哪"的活体视图 (见 BOOT_AXI 的 [30]/[31])。 */
        uint32_t rec_valid  = (rc[1] == 0x52434C4Bu) ? 1u : 0u;
        uint32_t prev_stage = rec_valid ? rc[30] : 0u;
        uint32_t prev_tick  = rec_valid ? rc[31] : 0u;
        /* ★ "上一轮是否走到了**设计的挂死点**" —— 归因锚点 (回应审计 P1②)。
         *   记录无效时当 0: AXI 上电=随机, 不能把随机值当现场 (与 [4]/[5] 同口径)。 */
        uint32_t prev_hang  = rec_valid ? rc[BOOT_REC_W_HANG] : 0u;
        /* ★ 主循环活性归因 (跨复位): 上一轮"因停滞复位过几次" + "最大间隔是多少" ——
         *   复位后 .bss 全清, 不从这里接续就永远只剩"又启动了一次"。 */
        uint32_t prev_looprst = rec_valid ? rc[BOOT_REC_W_LOOPRST] : 0u;
        uint32_t prev_gapmax  = rec_valid ? rc[BOOT_REC_W_GAPMAX]  : 0u;
        uint32_t rsr = RCC_RSR;
        if (!rec_valid) { rc[1] = 0x52434C4Bu; rc[0] = 0u; }
        rc[0]++;
        rc[2] = rsr;                       /* 本轮启动的复位原因 */
        rc[3] = RCC_BDCR;
        rc[4] = prev_stage;                /* ★ 上一轮卡在哪一步 */
        rc[5] = prev_tick;
        rc[6] = lg->total;                 /* ★ 上一轮的故障摘要 (台账马上要被 memset) */
        rc[7] = lg->f_code;  rc[8] = lg->f_tick; rc[9] = lg->f_c0;
        rc[10] = lg->l_code; rc[11] = lg->l_tick;
        for (uint32_t i = 0u; i < 16u; i++) rc[12u + i] = lg->cats[i];
        /* ★ 归因锚点: [33] = 上一轮是否走到设计的挂死点, 然后**清掉本轮标记** [32]。
         *   ★ 不清的后果: 一次挂死会把 "HANG" **永久**留在 AXI 里, 之后每一次干净启动
         *     都会被读成"上一轮挂死了" —— 又一个只会撒谎的字段。
         *   ★ [32]/[33] **不进校验和**([4..28]): [32] 是挂死那一刻(启动之后)才写的,
         *     进校验和会把记录判成无效。信任口径同 [4]/[5]: 先看 rec_valid(rc[1]=="RCLK")。 */
        rc[BOOT_REC_W_HANG_PREV] = prev_hang;
        rc[BOOT_REC_W_HANG]      = 0u;
        rc[BOOT_REC_W_LOOPRST_P] = prev_looprst;
        rc[BOOT_REC_W_GAPMAX_P]  = prev_gapmax;
        rc[BOOT_REC_W_LOOPRST]   = 0u;      /* 本轮从 0 起算 (由 ISR 复位前写 1..n) */
        rc[BOOT_REC_W_GAPMAX]    = 0u;
        /* ★ 复位次数跨复位**单调**: 从上一轮接续 —— 否则每次复位都从 0 开始,
         *   "它已经因主循环停滞复位过 5 次"这句话就永远说不出来。 */
        g_loop_reset_cnt = prev_looprst;
        rc[28] = 0x50524556u;              /* "PREV" 段有效标记 */
        {   uint32_t s = 0u;
            for (uint32_t i = 4u; i <= 28u; i++) s += rc[i];
            rc[29] = s;                    /* 校验和 ⇒ 上电随机值必然对不上 */
        }
        RCC_RSR = RCC_RSR_RMVF;            /* ★ 纯写 bit16: 清标志, 下次只见下次的 */
        g_stage = 1;
    }

    /* ① 时钟 */
    int err = clock_init();
    g_boot_status = err;
    g_stage = 2;
    if (err != CLK_OK) blink_error(err);

    g_clock_hclk = clock_get_hclk_hz();
    g_stage = 3;

    /* ★★ 复位后"尽早进安全电平" (2026-09-13, 看门狗配套) ——
     *   事实: 输出脚(DO=GPIOE)的引脚配置在 **do_init (阶段 26)** 才做;
     *   在那之前引脚是**复位默认 = 输入/高阻**。⇒ 若此刻看门狗/掉电把板子复位,
     *   从"停止驱动"到"do_init 把它配成输出"之间有一个几十毫秒的高阻窗口,
     *   执行器侧没有外部下拉时会随漏电/干扰漂移。
     *   修法(纯软件): 一拿到时钟就把 DO 口**显式配成推挽输出并输出低** ——
     *   把高阻窗口从"到阶段 26"缩到"到阶段 3"。
     *   ★ 边界(必须写清, 否则又是"宣称>实现"): 这给出的是**软件定义的安全电平**,
     *     它**不能**覆盖"复位瞬间到本段代码执行"那一小段(μs~ms 级)以及**掉电**场景 ——
     *     那两种只有**硬件外部下拉**(或失效安全驱动器)能保证。 */
    RCC_AHB4ENR |= (1u << DO_GPIO_PORT);        /* 先开该口时钟 (否则写入被丢弃) */
    for (uint32_t p = 0u; p < 16u; p++) {
        GPIO_BSRR(DO_GPIO_PORT) = (1u << (16u + p));   /* 先置低电平 (写 BR) */
    }
    GPIO_MODER(DO_GPIO_PORT) = 0x55555555u;     /* 全部推挽输出 (低) */
    __asm__ volatile("dsb" ::: "memory");

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
    /* ★★★ 生产时基（TIM5）—— 必须紧跟 dwt_enable() 之后:
     *   `dwt_enable()` 现在只负责"打开对照路径(DWT)"; **生产统计全部走 TIM5**。
     *   ★ 顺带解惑: 上述"时钟不连续保护"是因为 `dwt_enable()` 清 CYCCNT;
     *     换成 TIM5 之后 TIM5 没人清 ⇒ 该保护不会再触发（保留它无害, 且对 0 档仍必要）。 */
    tb_init();
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

    /* ⑤ 双心跳脚 (PE0/PE1) —— **必须在拍中断开始前配好**, 否则拍 ISR 的翻转没有效果。 */
    hb_pins_init();

    /* ⑤ 100μs 拍 */
    tick_timer_init();
#if DCL_WDT
    /* ★★ 启动独立看门狗 (2026-09-13) —— 位置: **紧接拍定时器之后**。
     *   理由: 喂狗点在拍 ISR 里, 所以"武装"必须**晚于**拍中断开始跑 ——
     *   否则从武装到第一次喂狗之间没人喂, 200ms 后自己复位一次。
     *   从本行到第一拍喂狗只差 ≤100µs ✓。
     *   ★ 若拍中断因"取向量失败"根本没起来 (项目真实踩过的整机卡死族):
     *     看门狗会在 200ms 后复位 ⇒ AXI 记录里会看到**启动次数单调涨 + 上一轮 stage 停在 6**
     *     ⇒ "上电就卡住"这件事从"通宵排查"变成**一眼可读**。这也是它值得武装在此的原因。 */
    g_wdt_ms    = wdt_timeout_ms();
    g_wdt_armed = (uint32_t)wdt_start();
    /* ★ 启动失败必须留案底 (否则表现只是"看门狗没救场", 查不出为什么)。
     *   context 带 (返回码, RCC_CSR) —— 本平台已知的坑: 若 LSI 没起, PR/RLR 同步永远
     *   完不成 ⇒ 写被拒 ⇒ 返回 -2, 而"我写过寄存器了"这句话毫无用处。 */
    if (g_wdt_armed != 0u) {
        fault_record(g_shm, FAULT_WDT_INIT_FAIL, g_tick_count,
                     g_wdt_armed, g_wdt_diag.csr);
    }
    /* ★★ IWDG 配不进去的**头号嫌疑**: H7 的 option byte `FLASH_OPTSR.IWDG1_SW` (bit4)
     *   决定 IWDG1 是"硬件看门狗"(上电自动启动、配置由 option byte 定、**软件写不动**)
     *   还是"软件看门狗"。若为硬件模式, 我们的 -2 就是它 —— 且这**不是缺陷而是特性**
     *   (硬件模式的看门狗软件关不掉, 对 PLC 反而更好)。
     *   ⇒ 读一次 FLASH_OPTSR_CUR 定案 (权威偏移: ST 头 FLASH_TypeDef offset 0x1C) ——
     *     本平台 pyocd 读不了 flash 寄存器区, 所以必须由固件自读。 */
    g_opt_sr = REG32(0x5200201Cu);
#endif
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
 as5600_init();                  /* AS5600 磁编码器: 绑定 PB10/PB11 */
    i2c_sm_init();                  /* ★ G6-1: 拍内 I2C 事务状态机（同一对引脚; 与阻塞路径共用 i2c_bb 的总线门）*/
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
    /* ★★ 2026-09-15 步进脉冲源: **必须在 do_init() 之后** ——
     *   它要写 OFF_CTRL_GPIO_MASK(接通 DO 面) 并预置 ACTUATOR[8..11]。
     *   ★ 踩过: 第一次把本调用插在 hil_init 之后, 而那次替换**没匹配上、静默没生效**
     *     ⇒ GPIO_MASK 一直是 0 ⇒ do_poll 早退 ⇒ PE8~PE11 停在 0(光耦导通!)
     *     ⇒ 如果那时接了 24V, 电机会上使能。**安全态验收** 才把它抓出来。 */
    step_init(g_shm);
    rtc_init(g_shm);                     /* ★ RTC: LSE 32.768kHz 时基 + 事件时间戳 */
    bb_init(g_shm);                      /* ★ 黑匣子: MDMA ch1 每拍 256B 快照 → AXI 环形缓冲 */
    /* ★ SD 卡 (2026-09-12): 初始化 + 把黑匣子缓冲写到卡上。
     *   放在启动期末尾 (允许几百 ms); 失败不阻塞启动 (内部有超时保护)。 */
    g_stage = 27;
    if (sd_init() == 0) {
        /* 一次性吞吐/回读自检 (调试器预写 SD_CFG[3]=1 才跑; 平时不占用启动时间) */
        if (sd_cfg_take(3u) != 0u) {
            uint32_t t0 = g_tick_count;          /* 拍计数计时 (100us/拍), 不依赖 DWT */
            sd_dump_write();                     /* 只测写 (256 块) */
            sd_set_perf(g_tick_count - t0, 0u, 0u);
        }
        /* ★ 打开日志: 之后由**主循环**每轮 sd_log_poll() 把新产出的快照
         *   (RAM 环) 成批冻结并追加落盘, 卡满则回卷覆盖最旧。 */
        (void)sd_log_open();
        /* ★★★ S5: 开机装载持久化程序。
         *   ★ 顺序不能反: 分区边界是在 `sd_log_open` → `sd_part_layout` 里算出来的,
         *     所以本段必须在它之后。
         *   ★ 这里**只装载不启动**: 装载 = stage + RELOAD(下一拍原子切到 ACTIVE)。
         *     是否上电就 RUN 是**策略问题**(上电即动机械), 留给上位机显式 0x11 决定。
         *   ★ 闸5 在这里生效: `prog_store_boot_load` 会调 `prog_validate`
         *     —— 与上传路径**同一个函数**, 不是第二份实现。不过就**不装载**,
         *     保持旧程序运行, 并把原因留在 g_prog_boot_rc / g_prog_reject_str。 */
        {
            int prc;
            uint32_t _w = block_window_begin(PROG_BLOCK_TICKS);  /* ★ SD 读: 见 PROG_BLOCK_TICKS */
            prc = prog_store_boot_load(prog_store_buf(), prog_store_buf_sz(), prog_validate);
            block_window_end(_w);
            g_prog_boot_rc = (uint32_t)prc;
            g_prog_reject_why = (uint32_t)prc;
            if (prc == PROG_RC_OK) {
                const uint8_t *pl = prog_store_buf();
                uint16_t nr = (uint16_t)(pl[0] | ((uint16_t)pl[1] << 8));
                uint16_t np = (uint16_t)(pl[2] | ((uint16_t)pl[3] << 8));
                uint16_t ns = (uint16_t)(pl[4] | ((uint16_t)pl[5] << 8));
                uint16_t nw = eng_apply_program(pl + 6u, nr, np, ns);
                (void)nw;
                g_prog_boot_loaded = 1u;
                __asm__ volatile("dsb" ::: "memory");
            }
        }
    }
#if HIL_SAFE
    eng_register_output_surface(do_outputs_safe);
#endif
    g_out_surfaces = eng_output_surface_count();
    g_w5_ready = 1;
    g_stage = 12;

    /* ⑧ 统一锚定全部观测变量 (防 --gc-sections 回收; 见 obs_anchor 注释) */
    obs_anchor();

    for (;;) {
        /* ★★ 主循环进入标记 —— **必须放在本轮最前面** (在任何可能阻塞的调用之前),
         *   否则"第一轮就卡住"依然会被当成"还没进入"。见 g_loop_entered 的注释。 */
        g_loop_entered = 1u;

        step_tick(g_tick_count);        /* 限时截止 (只做一次比较, 极短) */

        /* ★ AS5600: 每 10ms 读一次。单次 ≈250µs ⇒ 占主循环 ~2.5%, 远低于停滞阈值。
         * ★★ 必须用**边沿触发**, 不能用 `g_tick_count % 100 == 0`:
         *   主循环一拍内会转很多圈, 那种写法在"命中的那一拍"里会**反复调用**,
         *   实测采样率变成 ~190/s(期望 100/s) —— 即"模运算当调度器"的经典坑。
         *   `(now - last) >= N` 的无符号减法天然处理回绕, 且保证"每 N 拍恰好一次"。 */
        {
            static uint32_t s_as_next = 0u;
            if ((int32_t)(g_tick_count - s_as_next) >= 0) {
                s_as_next = g_tick_count + 100u;
                /* ★ G6-2: 延迟放门（状态机在 ISR 里收尾, 放门必须由主循环做 —— 见 i2c_sm.h）*/
                i2c_sm_service();
                /* ★★ G6-2 诊断占用的**有界**释放（`0x39 op=22 sub=0`）——
                 *   占用到 tick 到期就自动放门, 不依赖"上位机记得来释放"。
                 *   ★ 为什么放在**主循环**而不是 ISR 尾部: `i2c_bus_release()` 在 flash 里,
                 *     放进 ISR 就违反 ISR 调用树不变量（擦 flash 期间取指被 stall ⇒ 喂狗停）。
                 *     —— 这是**闸门当场拦下来**的（`gate_isr_itcm.py` 点了 `i2c_bus_release@0x08008BDC`）。
                 *     而 ≤200ms 的诊断占用本来也不需要拍级精度, 主循环(~1.3ms 一圈)足够。 */
                if (g_i2c_hold_until != 0u && (int32_t)(g_tick_count - g_i2c_hold_until) >= 0) {
                    g_i2c_hold_until = 0u;
                    i2c_bus_release(I2C_OWNER_BLOCKING);
                }
                /* ★★ G6-2: 状态机持有总线时**连调用都不发起** —— 门本身已经能拒绝
                 *   （`i2c_bb_*` 内部 acquire），这里只是省掉一次必然失败的 250µs 往返,
                 *   并避免污染 AS5600 的错误计数（那是判据, err 必须保持 0）。 */
                if (i2c_bus_owner() != I2C_OWNER_SM) {
                    as5600_poll(g_shm);
                }
            }
        }

        /* ★ 主循环间隔**实测** (回应审计 P1① 的"阈值得先测"): 阈值不能拍。
         *   为什么在主循环里量就够: 它只需上报**已经完成**的间隔; 而"永久停滞"由拍 ISR 判
         *   (主循环自己报不了自己停了 —— 本项目的老教训)。 */
        {
            static uint32_t s_prev_tick = 0u;
            /* ★★ 记账顺序有讲究 (2026-09-13 实测抓到的**差一格**缺陷):
             *   本段在"轮的开头"跑, 它量的是**上一轮循环体**的耗时 ⇒ 描述"那一轮有没有
             *   声明过窗口"的标记也必须是"上一轮循环体攒下来的"。第一版把
             *   `s_prev_decl = g_block_used` 写在本段**之后**(当轮开头), 于是读到的是
             *   **上上轮**的状态 ⇒ 声明过的阻塞被算成"未声明" ⇒ 判据误报"阈值无余量"。
             *   ⇒ 正确姿势: 本段先读 `g_block_used`(自上次清零以来 = 上一轮循环体),
             *     读完再清零, 交给下一轮。 */
            if (s_prev_tick) {
                uint32_t g    = g_tick_count - s_prev_tick;
                uint32_t decl = g_block_used;        /* ★ 上一轮循环体是否声明过窗口 */
                if (g > g_loop_gap_max) {
                    g_loop_gap_max = g; g_loop_gap_at = g_tick_count;
                    g_loop_gap_decl = decl;
                    /* ★ 同时镜像进 AXI(NOLOAD, 跨复位不丢) —— 只在创新高时写 (极少),
                     *   于是"死前卡了多久"这个数**跨复位也留得住**。 */
                    *(volatile uint32_t *)(BOOT_REC_ADDR + BOOT_REC_W_GAPMAX * 4u) = g;
                }
                /* ★★ **未声明**段里的最大间隔才是判据的输入:
                 *   把"声明过的合法长阻塞 (如 persist 1.55s)"和"没声明的可疑阻塞"
                 *   混成一个数, 会让阈值余量判据在**正常刷盘**时误报 —— 两个语义不同的量
                 *   必须分开 (这是本项目反复踩过的一类)。 */
                if (!decl && g > g_loop_gap_max_undecl) g_loop_gap_max_undecl = g;
            }
            s_prev_tick = g_tick_count;
            g_block_used = 0u;                       /* 清零: 交给下一轮判"上一轮体" */
        }
        /* ★ 双心跳之**主循环侧** —— 与 ISR 心跳一对比, 就能区分"整机死"与"主循环死"。
         *   (主循环死时协议口也断 ⇒ 这是唯一还能从外部实时看见的通道。) */
        {
            static uint32_t s_hb_l = 0u;
            if (++s_hb_l >= HB_DIV_TICKS) { s_hb_l = 0u; hb_toggle(HB_LOOP_PIN); g_hb_loop_tog++; }
        }

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
            uint32_t _w = block_window_begin(PERSIST_BLOCK_TICKS);   /* 见文件上方 ② */
            int rc = persist_save(g_shm);
            block_window_end(_w);
            if (rc == 1)      g_persist_skip_run++;
            else if (rc == 0) g_persist_saves++;
            else              g_persist_nak++;
        }
        /* ★★★ 每拍连续落盘 (2026-09-12): 把 RAM 环里新产出的快照成批落盘。
         *   放在 proto_poll 之后 ⇒ 协议优先被服务; 单批 64 条(=6.4ms 产量) 约 3ms 写完。
         *   卡满则回卷覆盖最旧 (专用裸介质, 见 sd.c 的日志段注释)。 */
        {   /* ★ 卡顿归因: 量"两次落盘之间的间隔"与"落盘内部耗时" */
            static uint32_t s_last = 0, s_gapmax = 0, s_inmax = 0, s_slow = 0;
            uint32_t t0 = g_tick_count;
            if (s_last != 0u) {
                uint32_t g = t0 - s_last;
                if (g > s_gapmax) s_gapmax = g;
                if (g > 200u) s_slow++;              /* >20ms 记一次慢轮询 */
            }
            /* ★ 声明窗口 (begin/end 成对): 例行刷盘是**已知会阻塞**的段。
             *   ★ 成对是必须的 —— 只 open 不 close 会把窗口永久续期 (见 begin 的注释)。 */
            {   uint32_t _w = block_window_begin(SD_POLL_BLOCK_TICKS);
                sd_log_poll();
                block_window_end(_w); }
            {   uint32_t d = g_tick_count - t0;
                if (d > s_inmax) s_inmax = d; }
            s_last = t0;
            sd_log_diag_gap(s_gapmax, s_inmax, s_slow);
        }
        /* ★★ 上位机触发的"重新开日志" (卡插晚了 / 换卡): 调试器预写 SD_CFG[8]=1
         *   + 魔数 [15]=0xF00DBEEF。★ 必须外部触发, 别让固件自己定时猜窗口。 */
        /* ★ 声明窗口: SD 重新初始化可能几秒 (识别重试) —— 远大于停滞阈值,
         *   所以这里必须声明, 否则一次卡插晚了的重初始化会触发误复位。 */
        if (sd_cfg_take(8u) != 0u) { uint32_t _w = block_window_begin(SD_INIT_BLOCK_TICKS); (void)sd_reopen_log(); block_window_end(_w); }
        /* 排障: GPIOD 口线检 (SD_CFG[10]=1 + 魔数) —— 回答"对方那根线插在哪个脚上" */
        if (sd_cfg_take(10u) != 0u) mb_line_test(g_shm);
        /* 排障: 在 PD6 上量波形 (SD_CFG[11]=1 + 魔数) */
        if (sd_cfg_take(11u) != 0u) mb_line_probe(g_shm);
        /* ★ 把故障台账全景刷进 SD 日志头 (SD_CFG[12]=1 + 魔数)。
         *   显式触发而非固件定时刷 —— 刷一次要写一次 LBA0, 什么时候值得只有上位机知道。 */
        if (sd_cfg_take(12u) != 0u) { uint32_t _w = block_window_begin(SD_POLL_BLOCK_TICKS); (void)sd_flt_snapshot(); block_window_end(_w); }
        /* ★ 故障注入: 让拍 ISR 死循环 (SD_CFG[13]=1 + 魔数)。
         *   用来给看门狗做**能失败的对照** —— 注入后正常工作必需复位 (见 wdt.h)。 */
        if (sd_cfg_take(13u) != 0u) { g_hang_isr = 1u; }
        /* ★★ 主循环停滞注入 (SD_CFG[14], 加魔数) —— 给"主循环失活⇒安全态+复位"打**能失败的
         *   对照**。与 SD_CFG[13] 是配对的两半:
         *     [13] 证明"**拍 ISR** 死了 → 看门狗救场"   (复位源 = IWDG1)
         *     [14] 证明"**主循环**死了 → 也救得了"        (复位源 = 软件复位, 且进安全态)
         *   ★ 做成**有界**阻塞而不是永久挂死: 对照档 (DCL_LOOP_RESET=0) 会自己恢复,
         *     不需要调试器去救 —— 对照实验因此可反复跑, 且"存活"本身就是证据。
         *   ★ 单位取千拍 (100ms): 手写 30 = 3s 比 30000 更不容易数错零。
         *   ★★ 编码 (2026-09-13): 低 16 位 = 千拍; **bit16 = 这段阻塞声明窗口**;
         *     **bit17 = 不关窗** (模拟"**死在已声明窗口内**", 需同时置 bit16)。
         *     于是同一支钩子能打三组对照:
         *       0x1E      = 未声明 3s 阻塞        ⇒ 判据应触发 (验证"判据能失败")
         *       0x1001E   = **已声明**且正常结束    ⇒ 判据应**被抵扣**(不触发)
         *       0x3001E   = 已声明(只声明 1/3 时长)+**不关窗** ⇒ 窗口到点失效后**仍应触发**
         *                    ← 这一组专门验证"截止时间"这条安全网: 若只靠 begin/end,
         *                      死在窗口内就永远不会恢复判据 (见 block_window_begin 注释)。 */
        {   uint32_t d = sd_cfg_take(14u);
            if (d != 0u) {
                uint32_t decl = (d & 0x10000u) ? 1u : 0u;
                uint32_t noend= (d & 0x20000u) ? 1u : 0u;
                uint32_t ticks = (d & 0xFFFFu) * 1000u;
                if (decl && noend) {
                    /* 死在窗口内: 只声明 1/3 时长 (窗口先过期), 然后**不关窗**
                     * ⇒ 判据应在窗口到期后恢复并触发。 */
                    (void)block_window_begin(ticks / 3u);
                    uint32_t t_end = g_tick_count + ticks;
                    while ((int32_t)(g_tick_count - t_end) < 0) { __asm__ volatile("nop"); }
                    /* ★ 故意**不调用** block_window_end —— 模拟"主循环死在块内" */
                } else if (decl) {
                    /* 声明得比实际长一点 (真实用法也是这样: 声明的是**上界**) */
                    uint32_t _w = block_window_begin(ticks + 2000u);
                    uint32_t t_end = g_tick_count + ticks;
                    while ((int32_t)(g_tick_count - t_end) < 0) { __asm__ volatile("nop"); }
                    block_window_end(_w);
                } else {
                    uint32_t t_end = g_tick_count + ticks;
                    while ((int32_t)(g_tick_count - t_end) < 0) { __asm__ volatile("nop"); }
                }
            }
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
                uint32_t _w = block_window_begin(PERSIST_BLOCK_TICKS);   /* 见文件上方 ② */
                int rc = persist_save(g_shm);
                block_window_end(_w);
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

        /* ★★ 时基活性自检 (2026-09-13) —— 让"时间基座被停掉"自己浮出来。
         *   背景: 台账上线第一次运行就抓到 `SCAN_DIV0` 以 184043/185034 的频率在报,
         *   上下文 c0 恒 0 ⇒ 引擎扫描测得**恰好 0 周期** ⇒ `DWT_CYCCNT` 根本没在走
         *   (调试器会话会静默停它 —— 项目"铁律 0"记录过的既知事故)。
         *   后果极隐蔽: `eng_cyc_*` / `isr_cyc_*` / `pmin/pmax` **全部静默变垃圾**,
         *   而现象只是"数字是 0", 不会报任何错。
         *   ⇒ 判据: 主循环每轮 WFI 睡 ~100µs, 活着的 CYCCNT 必然推进数千周期;
         *     连续多轮 delta==0 ⇒ 只能是被停了。
         *   ★ 只判"完全不推进" (delta==0): 擦 flash 造成的几十 ms 跨度是**已知合理**窗口,
         *     判它只会制造噪声 (噪声淹没真警告 = 本项目最恨的失效模式)。
         *   ★ 连续 100 轮才报 (≈10ms), 避免刚复位/DWT 刚使能时的边界误报;
         *     之后每 100 轮记一笔当"仍在坏"的心跳 —— 台账 total 会随之增长,
         *     既能看出"坏了"也能看出"坏了多久"。 */
        /* ★★ 时基活性检测**已移入拍 ISR** (2026-09-13, 回应审计"两个检测器口径不一致"):
         *   原来在这里判"连续 100 轮 CYCCNT 不推进", 而 `SCAN_DIV0` 的门在 ISR 里读它 ——
         *   两个检测器**位置不同、条件不同**, 于是启动段(pyocd 会话后 DWT 被停而主循环
         *   还没进)出现 "16681 笔 SCAN_DIV0 / 0 笔 TIMEBASE"。
         *   实测口径: 主循环判据要求"连续 100 **轮**", 而启动段主循环根本没跑 ⇒ 永不触发。
         *   ⇒ 现在只有**一处**真值源 (ISR), 本处不再重复实现 (两份副本 = 迟早再次分叉)。
         *   仍在此处发布 `g_timebase_dead_ticks` 供外部读走 (见下方的 WDT_STAT 发布)。 */

        /* ★ 主循环心跳 (2026-09-13) —— 只有 +1 这一个动作。
         *   ★ 检测**不在这里**: 主循环自己跟自己比是**恒成立的空判据**
         *     (第一版就这么写过)。停滞后主循环根本不执行 ⇒ 它没法报告自己停了。
         *   ⇒ 判据放在**拍 ISR** 里观察本计数器 (见 ISR 那段), 这样"瞬时阻塞"与
         *     "永久卡死"两种都能记下来。喂狗**不**依赖它 (理由见 wdt.h)。 */
        g_loop_hb++;

        /* ★ 发布看门狗/主循环状态到 SHM (供 0x22 按名字读; 见 manifest.h 的 WDT_STAT)。
         *   为什么每轮都发: "武装了就完事"属静默失败族 —— 喂狗计数与超时档必须**可读回**。 */
        SHM_U32(g_shm, OFF_WDT_STAT +  0u) = g_wdt_armed;
        SHM_U32(g_shm, OFF_WDT_STAT +  4u) = g_wdt_ms;
        SHM_U32(g_shm, OFF_WDT_STAT +  8u) = g_loop_hb;
        SHM_U32(g_shm, OFF_WDT_STAT + 12u) = g_hang_isr;
        SHM_U32(g_shm, OFF_WDT_STAT + 24u) = 12000u;      /* 停滞阈值 (拍) */
        SHM_U32(g_shm, OFF_WDT_STAT + 28u) = g_stage;     /* 当前阶段 (实时) */
        /* ★ 启动的读回 (rc/RCC_CSR/IWDG PR/RLR/SR) —— 让"到底写进去了没"可被外部核对,
         *   不必再靠 pyocd 读外设区 (本平台 pyocd 读外设不可靠, 实测读数自相矛盾)。 */
        SHM_U32(g_shm, OFF_WDT_STAT + 32u) = g_wdt_diag.rc;
        SHM_U32(g_shm, OFF_WDT_STAT + 36u) = g_wdt_diag.csr;
        SHM_U32(g_shm, OFF_WDT_STAT + 40u) = g_wdt_diag.pr;
        SHM_U32(g_shm, OFF_WDT_STAT + 44u) = g_wdt_diag.rlr;
        SHM_U32(g_shm, OFF_WDT_STAT + 48u) = g_wdt_diag.sr;
        SHM_U32(g_shm, OFF_WDT_STAT + 52u) = g_opt_sr;      /* FLASH_OPTSR_CUR */
        SHM_U32(g_shm, OFF_WDT_STAT + 56u) = g_wdt_diag.sr0;
        SHM_U32(g_shm, OFF_WDT_STAT + 60u) = g_wdt_diag.pr0;
        /* ★ 分步快照的第二截 (2026-09-13 第二次修正后新增) —— 顺序/关闸这两项改动
         *   必须留下"这次真的不一样"的可读证据, 否则就是"改完看到 rc=0 就说修好了"。 */
        SHM_U32(g_shm, OFF_WDT_STAT + 64u) = g_wdt_diag.rlr0;
        SHM_U32(g_shm, OFF_WDT_STAT + 68u) = g_wdt_diag.sr1;      /* 解锁后 */
        SHM_U32(g_shm, OFF_WDT_STAT + 72u) = g_wdt_diag.sr2;      /* 写完 PR/RLR 后 */
        SHM_U32(g_shm, OFF_WDT_STAT + 76u) = g_wdt_diag.sr_start; /* 启动(KR=0xCCCC)后 */
        SHM_U32(g_shm, OFF_WDT_STAT + 80u) = g_wdt_diag.wait_cyc; /* 等标志落的 CPU 周期 */
        SHM_U32(g_shm, OFF_WDT_STAT + 84u) = g_wdt_diag.kr_busy_snap;  /* 开闸前闸门状态(应=1) */
        SHM_U32(g_shm, OFF_WDT_STAT + 88u) = g_wdt_diag.kr_blocked;    /* ISR 被拦下的喂狗次数 */
        /* ★ [23] 同步是否**成功** (1 = 真落 / 0 = 跑满预算啥也没等到)。
         *   ★ 极性有意做成"1 = 好": 原写法 1 = 跑满, 读的人容易当成功 (审计 P3)。 */
        SHM_U32(g_shm, OFF_WDT_STAT + 92u) = g_wdt_diag.sync_ok;
        /* ★ [24][25] **固件自己声明的目标值** (WDT_PR_VALUE / WDT_RLR_VALUE)。
         *   为什么要报出来: 工具原先把"期望 PR=4 / RLR=99"**硬编码**在自己身上 ——
         *   换成 PR=6 档时立刻变成假报警 ("PR=6 ≠ 我写的 4")。
         *   判据必须是"**意图 vs 读回**", 而意图只能由固件给 (它就是编译产物的一部分)。 */
        SHM_U32(g_shm, OFF_WDT_STAT + 96u) = WDT_PR_VALUE;
        SHM_U32(g_shm, OFF_WDT_STAT + 100u) = WDT_RLR_VALUE;
        /* ★ 时基活性的外部可见面 (检测在拍 ISR, 见 ISR 入口那段):
         *   [26] 1 = 此刻 DWT 计时无效 / [27] 本轮累计停摆了多少拍。
         *   "坏了多久"必须能从外部读走 —— 否则审计只有一笔 TIMEBASE, 不知道持续多久。 */
        SHM_U32(g_shm, OFF_WDT_STAT + 104u) = g_timebase_dead;
        SHM_U32(g_shm, OFF_WDT_STAT + 108u) = g_timebase_dead_ticks;
        /* ★ 主循环活性 / 阻塞窗口 / 心跳 / 自愈 的外部可见面 (审计 P1① 的整改面)。
         *   ★ 阈值 `[33]` 与"本档是否启用自愈" `[32]` 都**由固件自报** ——
         *     工具不许把这些写死在自己身上 (那是 PR=4/RLR=99 那类假报警的源头)。 */
        SHM_U32(g_shm, OFF_WDT_STAT + 112u) = g_block_win_max;          /* [28] */
        SHM_U32(g_shm, OFF_WDT_STAT + 116u) = g_loop_gap_max;           /* [29] 实测最大间隔(拍) */
        SHM_U32(g_shm, OFF_WDT_STAT + 120u) = g_loop_gap_at;            /* [30] 何时发生 */
        SHM_U32(g_shm, OFF_WDT_STAT + 124u) = g_loop_reset_cnt;         /* [31] 停滞复位次数 */
        SHM_U32(g_shm, OFF_WDT_STAT + 128u) = (uint32_t)DCL_LOOP_RESET; /* [32] 本档是否启用 */
        SHM_U32(g_shm, OFF_WDT_STAT + 132u) = LOOP_STALL_TICKS;         /* [33] 阈值(自报) */
        SHM_U32(g_shm, OFF_WDT_STAT + 136u) = block_window_active();    /* [34] 此刻在窗口内? */
        SHM_U32(g_shm, OFF_WDT_STAT + 140u) = g_hb_isr_tog;             /* [35] ISR 心跳次数 */
        SHM_U32(g_shm, OFF_WDT_STAT + 144u) = g_hb_loop_tog;            /* [36] 主循环心跳次数 */
        /* ★ [37] **未声明**段的最大间隔 / [38] 最大间隔那段是否已声明过 ——
         *   判据用 [37]: 把"合法声明过的长阻塞"混进来会让阈值余量判据误报。 */
        SHM_U32(g_shm, OFF_WDT_STAT + 148u) = g_loop_gap_max_undecl;    /* [37] */
        SHM_U32(g_shm, OFF_WDT_STAT + 152u) = g_loop_gap_decl;          /* [38] */
        /* ★ [39] 主循环是否**已进入** —— 让外部能区分"还停在启动段"与"主循环死了"
         *   (这两件事的处置完全不同: 前者是正常的 ~33s 启动, 后者要自愈)。 */
        SHM_U32(g_shm, OFF_WDT_STAT + 156u) = g_loop_entered;           /* [39] */
        /* ★ 心跳的"自证"面 (2026-09-13, 纠正 PE0/PE1 那次的直接产物):
         *   [40] 当前 ODR 快照 —— 两次读值不同就说明**寄存器真的在翻** (LA 之外的第一道证据);
         *   [41..43] **固件自报**端口/两脚编号 —— 工具不许写死脚位 (否则换脚即假报, 同
         *            PR=4/RLR=99 那类教训); 人也能据此直接把 LA 夹到对的两脚上。 */
        SHM_U32(g_shm, OFF_WDT_STAT + 160u) = GPIO_ODR(HB_GPIO_PORT);   /* [40] */
        SHM_U32(g_shm, OFF_WDT_STAT + 164u) = HB_GPIO_PORT;             /* [41] */
        SHM_U32(g_shm, OFF_WDT_STAT + 168u) = HB_ISR_PIN;               /* [42] */
        SHM_U32(g_shm, OFF_WDT_STAT + 172u) = HB_LOOP_PIN;              /* [43] */
        /* ★★ [44..45] 扫描选择面 (2026-09-13): 它决定**拍 ISR 调哪一份扫描** ——
         *   `sel ? engine_scan_itcm : engine_scan_flash`, 而 **flash 版落在 FLASH**:
         *   擦除期间走到它就 stall ⇒ ISR 卡死 ⇒ 看门狗复位 (本缺陷的机制链)。
         *   ⇒ "运行期到底用哪一份"必须能回读, 否则又是一处只能猜的状态。
         *   [45] 直接报**将被调用的那个函数的地址** ⇒ 拿它和 nm 的符号地址一比即知。 */
        SHM_U32(g_shm, OFF_WDT_STAT + 176u) = g_engine_sel;             /* [44] 0=FLASH版 1=ITCM版 */
        SHM_U32(g_shm, OFF_WDT_STAT + 180u) =
            (uint32_t)(uintptr_t)(g_engine_sel ? engine_scan_itcm : engine_scan_flash); /* [45] */
        SHM_U32(g_shm, OFF_WDT_STAT + 184u) = (uint32_t)(uintptr_t)engine_scan_flash;   /* [46] 对照: flash 版地址 */
        SHM_U32(g_shm, OFF_WDT_STAT + 188u) = (uint32_t)(uintptr_t)engine_scan_itcm;    /* [47] 对照: itcm 版地址 */

        /* ★★ 掉电保持诊断 (2026-09-13, 起因是一次真实的误判):
         *   关键是 [5] `erase_ok` —— **"这次擦了没"必须可读回**。原先这两个计数器只被
         *   obs_anchor 锚定, 外部完全看不见 ⇒ 我把一次被**跳过**的落盘(21ms)读成了
         *   "落盘很快", 得出相反结论。没有判据就会得出相反结论 (本项目铁律)。
         *   ★ 能失败的判据: 代码**每次都先擦后写** ⇒ `erase_ok` 应恒等于 `writes`;
         *     若 writes 涨而 erase_ok 不涨 ⇒ 有路径绕过了擦除 (那就是缺陷)。 */
        SHM_U32(g_shm, OFF_PERSIST_STAT +  0u) = g_persist_cmds;      /* [0] 0x43 调用次数 */
        SHM_U32(g_shm, OFF_PERSIST_STAT +  4u) = g_persist_saves;     /* [1] 受理的 save 请求 */
        SHM_U32(g_shm, OFF_PERSIST_STAT +  8u) = g_persist_skip_run;  /* [2] 因引擎 RUN 跳过 */
        SHM_U32(g_shm, OFF_PERSIST_STAT + 12u) = g_persist_nak;       /* [3] 拒绝次数 */
        SHM_U32(g_shm, OFF_PERSIST_STAT + 16u) = g_persist_writes;    /* [4] 真正写完的次数 */
        SHM_U32(g_shm, OFF_PERSIST_STAT + 20u) = g_persist_erase_ok;  /* [5] ★ 擦成功次数 */
        SHM_U32(g_shm, OFF_PERSIST_STAT + 24u) = g_persist_erase_fail;/* [6] 擦失败次数 */
        SHM_U32(g_shm, OFF_PERSIST_STAT + 28u) = (g_persist_dirty ? 1u : 0u)
                                               | ((uint32_t)g_persist_target << 8); /* [7] */

        /* ★★ AXI 活体镜像 (2026-09-13) —— 把当前 stage / 拍号写进复位取证记录 [30]/[31]。
         *   为什么必须"活着写": `g_stage`/`g_tick_count` 住在 .bss, 而**启动代码的清零
         *   先于取证段执行** ⇒ 复位后从它们读不到"上一轮停在哪"(旧实现就是这么读到恒 0 的)。
         *   而 AXI 是 (NOLOAD)、启动不清、跨复位不丢 ⇒ 这里的写入就是留给下一轮取证的现场。
         *   顺带得到"没复位时此刻跑到哪"的活体视图 —— 同一个字段两个用途。
         *   代价: 每轮 2 次 AXI 写 (HCLK=CPU/2, 每次约 2 周期), 相对 4 万周期/拍可忽略。 */
        {   volatile uint32_t *bk = (volatile uint32_t *)BOOT_REC_ADDR;
            bk[30] = g_stage; bk[31] = g_tick_count; }

        /* 栈哨兵周期巡检 (廉价: 32 个字, 主循环有 100μs 一次的机会) */
        g_guard_ok = (uint32_t)shm_guard_ok();
        if (g_guard_ok != 1u) {
            /* ★ 台账: 护栏被踩 = SHM 与栈边界失守, 属"结构性"异常。
             *   ★ 去重: 一旦坏了会**每次巡检都判坏** (100µs 一次) ⇒ 不设闸门会
             *     把台账瞬间刷爆。这里只在"由好变坏"的那一拍记一次 (沿触发, 不是电平触发)。 */
            static uint32_t s_guard_seen_bad = 0u;
            if (!s_guard_seen_bad) {
                s_guard_seen_bad = 1u;
                fault_record(g_shm, FAULT_SHM_GUARD, g_tick_count, g_guard_ok,
                             g_guard_bad_off);
            }
        }
        g_stage = 9;
        __asm__ volatile("wfi");
    }
}
