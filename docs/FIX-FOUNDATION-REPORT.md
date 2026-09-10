# H723 基础问题修复报告（审计 A1/A2/A3/A4/A5 + H1~H11）

日期: 2026-09-10 晚 · 项目: `9.10 H723newest` · 平台: STM32H723ZGT6 @400MHz

**触发**: 用户指令 ——「先停吧，问题太多。基础不牢固，我们在现在的上层就麻烦，先去解决基础问题」。
**审计来源**: `C:\Users\min\WorkBuddy\项目审查官\` 的 `H723-STAGE2-AUDIT.md`（17:38）+
`H723-STAGE3-AUDIT.md`（20:38）。这两份审计覆盖阶段 2 与阶段 3 的 4 个提交
（`4d82413` / `09c05ef` / `a067fd3` / `0879fd9`）。

**一句话结论**: 审计抓到的 1 个 P1（A1）确认成立并已修复 **实机复验**；
修复过程中**自己又发现 1 个更严重的 P1**（组帧 CRC 覆盖少一个字节 —— 会让协议层
在接线与中断都修好之后**依然永远收不到一帧**）；A3 修复后**引出成本基线整体位移
~10%**，据此重测了全部 19 个原语成本。所有修复都带可复跑的实机证据。

---

## 0. 优先级总表

| # | 级别 | 问题 | 状态 | 实机证据 |
|---|---|---|---|---|
| **A1** | **P1** | `NVIC_ISER = (1u << 37)` 位移溢出 → USART1 中断从未使能 | ✅ 已修 | `ISER[1]=0x20`、`g_uart_irq_en=1`（新增判据）|
| **★N1** | **P1** | **审计未覆盖**：组帧 CRC 覆盖 `2u+n`（应 `3u+n`）→ 载荷最后一字节不受保护 → 对端**每一帧都判 CRCBAD** | ✅ 已修 | 帧尾 `44 E7` → **`C9 C9`**（与独立实现逐位一致）；`g_frame_selftest=1` |
| **A3** | P2 | wire2 判据缺 `flags` 分支 → 静默读 `wire[0]` 当第二输入 | ✅ 已修 | `wire[5]` **17.0 → 10.0**；阳性对照（置 WIRE2 标志）仍得 17.0 |
| **A4** | P2 | 实现了热重载却未声明 `DCL_CAP_HOTRELOAD` | ✅ 已修 | 实发帧 `cap=0x0033`（原 `0x0021`）|
| **A5** | P2 | CMake 缓存污染 → "交付固件"其实是自检版 | ✅ 已修 | `build.sh` 每次显式传全部默认值 + 打印生效开关 |
| **H3/A7** | P2 | `OFF_MB_SET` `0x4AC0` vs S3 `0x4AA0`，"逐字节对齐"声明不成立 | ✅ 已修 | 改回 `0x4AA0`；断言链改为 `==` 精确相接 |
| **H4** | P1 | linker 符号地址比较被 GCC 折叠（已修但未固化）| ✅ 已固化 | `shm_layout_ok` **2 条指令 → 34 条**；`g_shm_ok=1` |
| **H2/A8** | P3 | `N_SEQ` 注释称"0x38-0x3F 空闲"与事实冲突 | ✅ 已修 | 注释与 0x3A-0x3F 占用对齐 |
| **H5** | P3 | 统计口径报 min（最乐观值），与 WCET 项目方向相反 | ✅ 已修 | 新增首样本观测量；工具在 `min==首样本` 时打 `?` |
| **H6** | P3 | 一文件内并存 550/520/450/400/465 五个频率 | ✅ 已修 | 收敛为"CLK_* 宏 = 唯一权威" |
| **H7** | P3 | `CLK_TIMXCLK_HZ = CLK_HCLK_HZ` 只在 APB=/2 成立 | ✅ 已修 | 改 `2×PCLK` + 两条断言 |
| **H8** | P3 | 两个测量工具的假阳性/循环论证判据 | ✅ 已修 | `--cpu` 改从 `clock.h` 解析；stage1 需 `--la-us` |
| **H9** | P3 | `sweep_freq.sh` 原地改源码不恢复 | ✅ 已修 | `trap` 自动还原 + 重建 |
| **H10** | P3 | `AINLINE` 丢 `static` | ✅ 已修 | 加回 `static` |
| **H11** | P3 | `state_offset = i % MAX_STATES` 会生成 0（无槽哨兵）| ✅ 已修 | 改 `(i % (MAX_STATES-1)) + 1` |
| **A6** | P3 | `0x38` 字段语义偏离 S3（`gate` vs `run`、ISR 时长 vs 引擎时长）| ⏳ 未修 | 需与 S3 逐字段确认后改（见 §6）|
| **A2** | 环境 | CH340 的 RXD 未接 PA9 | ⏳ 等接线 | 与 A1/N1 叠加，见 §5 |
| **A9** | P3 | `g_tick_count` uint32 @100μs → 4.97 天溢出 | ⏳ 未修 | 见 §6 |

**根因治理（比任何单条修复都重要）**: `-Werror` + 构建后零警告闸门。
A1 之所以能上线，是因为**同一次构建里还有 24 条无害警告**（newlib 桩的
unused-parameter 等）把那条 `-Wshift-count-overflow` 埋掉了 —— 而我的构建命令还用
`grep -E "error|ITCM:|..."` 主动过滤掉了警告。**噪声不清零，真警告就一定会被漏掉。**

---

## 1. A1（P1）NVIC 位移溢出 —— 协议层"功能上不可用"的真因

### 1.1 代码与后果

```c
/* regs.h（旧）*/  #define NVIC_ISER  REG32(0xE000E100UL)   /* 只管 IRQ 0..31 */
/* uart.c:78（旧）*/ NVIC_ISER = (1u << IRQ_USART1);        /* IRQ_USART1=37 → UB */
```

`1u << 37` 对 32 位量是**未定义行为**。GCC 报了 `-Wshift-count-overflow`，被淹没。

后果：即便接线完全正确，上位机发来的**每个字节都不触发中断** → 环形缓冲永远为空 →
`proto_poll` 永远读到空 → **所有命令 TIMEOUT**。而 TIM2 = IRQ 28（<32）一切正常，
所以症状只剩"串口安静地坏掉"。

### 1.2 修法：让"IRQ≥32"在编译层面不可能写错

```c
#define NVIC_REG(base, irq)  REG32((base) + 4UL * ((uint32_t)(irq) >> 5u))
#define NVIC_ISER_W(irq)     NVIC_REG(0xE000E100UL, irq)
#define NVIC_BIT(irq)        (1u << ((uint32_t)(irq) & 31u))
static inline void nvic_enable_irq(uint32_t irq) { NVIC_ISER_W(irq) = NVIC_BIT(irq); }
```

**关键改动**: 头文件里**不再提供** `NVIC_ISER` 这个"裸寄存器"名字 —— 只提供按 IRQ
号索引的宏与函数。原版坏就坏在宏名（单数）掩盖了"这是寄存器数组"的事实。
另加两条 `_Static_assert(IRQ_x < 64)` 护栏。

### 1.3 ★ 新增"中断使能"观测量（让 A1 这类问题第一次读回就暴露）

```c
uint32_t uart1_irq_enabled(void) { return nvic_is_enabled(IRQ_USART1); }  /* 读 ISER 位本身 */
OBS uint32_t g_uart_irq_en = 0;   /* 主循环每轮刷新 */
```

为什么必须加：A1 期间**所有"配置类"检查全绿**（CR1/BRR/GPIO/AFRH/RCC 都对），
只有"中断使能"这一项没人查。现在它是**可以被外部读走的量**，而不是"写过了就算"。

### 1.4 实机复验（halt 后读，符合项目纪律"外设寄存器必须 halt 后读"）

```
ISER[0] = 0x10000000   ← bit28 = TIM2 (IRQ28) 仍使能 ✓
ISER[1] = 0x00000020   ← bit5  = IRQ37%32 → USART1 使能 ✓   (修复前 = 0x00000000)
g_uart_irq_en = 1
USART1_CR1 = 0x0000002D (UE|TE|RE|RXNEIE)
```

---

## 2. ★N1（P1，审计未覆盖）组帧 CRC 覆盖少一个字节

### 2.1 这是修复 A1 时"顺藤摸瓜"发现的

给 A1 做验证时，我把目光移到"真的能收到帧吗"。做法：读固件里那个静态发送缓冲
`s_txbuf`（最后发出的一帧还躺在里面），再用**独立实现的 CRC** 校验。

```
s_txbuf = C1 00 04 00 | 00 02 33 00 | 44 E7
                            ↑ fw=0x0200  ↑ cap=0x0033
独立算 CRC(00 04 00 00 02 33 00) = 0xC9C9   ← 帧里却是 44 E7
```

### 2.2 根因

```c
/* src/main.c（旧）*/
uint16_t crc = crc16_ccitt(s_txbuf + 1, 2u + n);   /* ← 少一字节 */
```

`s_txbuf + 1` 是 `[sts]`，长度 `2+n` 只能覆盖到 `payload[n-2]` ——
**载荷最后一个字节（本例是 cap 的高字节）不在 CRC 保护范围内**。

对照 S3 原文（`esp32-core0/main/main.c:62-78`）：

```c
cb[0] = sts; cb[1] = out[2]; cb[2] = out[3];
if (n) memcpy(cb + 3, p, n);
uint16_t crc = crc16_ccitt(cb, 3 + n);            /* ← 正确: 3+n */
```

### 2.3 后果与"为什么一直没抓到"

**后果**：PC 侧按 `frame[1:-2]`（7 字节）校验 → **每一帧都被判成 `CRCBAD`** →
即使 A1 与接线都修好，协议层**依然永远收不到一帧合法响应**。
换句话说：这是叠在 A1 后面的**第二道完全独立的"协议不可用"**。

**为什么 3 轮验证都没发现**：
1. 无人真正收到过帧 —— 接线（A2）与 NVIC（A1）两重故障先把它挡住了；
2. **阶段 3.1 报告里那个"手算好的横幅帧 `C1 00 04 00 00 02 21 00 D8 AC`"是错的** ——
   它是按"CRC 覆盖 7 字节"这个**错误假设**手算的，而且当时 LA 抓取失败，
   **从未在线上验证过**。这正是本项目第一铁律要防的"宣称 > 实现"，
   本次一并纠正。

### 2.4 修法 + 可失败判据

把"覆盖长度"收敛成一个常量，并配一个用**独立实现算出的期望值**做的自检：

```c
#define FRAME_CRC_COVER(n)  (3u + (n))            /* [sts][len_lo][len_hi][payload] */

static uint32_t frame_build_selftest(void)        /* 上电时跑, 结果给 g_frame_selftest */
{
    static const uint8_t pl[4] = {0x00, 0x02, 0x33, 0x00};
    ... build_frame_into(f, STS_ACK, pl, 4) ...
    return (f[8] == 0xC9u && f[9] == 0xC9u) ? 1u : 0u;   /* 0xC9C9 由 PC 侧独立算出 */
}
```

**实机证据**：

| | 帧尾 | `g_frame_selftest` |
|---|---|---|
| 修复前 | `44 E7`（覆盖 6 字节）| 0 |
| **修复后** | **`C9 C9`**（覆盖 7 字节，与独立实现逐位一致）| **1** |

---

## 3. A3（P2）第二输入判据 —— 静默读错值

### 3.1 差异

```c
/* S3 最终形态 (M1→F2→N-A→OA1 四轮才修好) */
bool w2_ok = ((r->flags & ROUTE_FLAG_WIRE2) || r->wire2_idx) && (r->wire2_idx < MAX_WIRES);
/* H723（旧）*/
float wb = (r->wire2_idx < MAX_WIRES) ? wm[r->wire2_idx] : 0.0f;   /* 缺前半 */
```

`wire2_idx == 0` 既是"没接第二输入"的默认值，**又是合法索引 wire[0]** —— 只查范围
无法区分二者。

### 3.2 修法：判据收敛成一个函数，ISR 与 deploy 校验共用

```c
static inline int wire2_valid(uint8_t flags, uint16_t wire2_idx)
{ return ((flags & ROUTE_FLAG_WIRE2) || wire2_idx) && (wire2_idx < MAX_WIRES); }

static inline int op_needs_wire2(uint8_t op)
{ return (op==OP_AND||op==OP_OR||op==OP_ARITH||op==OP_SR||op==OP_CNT); }
```

- ISR 走 `wire2_valid()`；
- `engine_route_validate()` 的"需要第二输入"集合从 `{AND,OR}` 扩到
  `{AND,OR,ARITH,SR,CNT}` —— **S3 自己也漏了这三类**（同样消费 `wb`：
  ARITH 的右操作数 / SR 的复位 / CNT 的减计数与复位输入），值得回写 S3 清单；
- `engine_fill_tables()` 对双输入原语**同时置** `ROUTE_FLAG_WIRE2`（填表侧也要说清
  "我确实接了第二输入"，否则与 ISR 判据打架）。

### 3.3 实机证据（同一路 route，只改 flags 的一位）

```
route[0]: src=CONST(param[0]=10.0) dst=wire[5] op=ARITH wire2_idx=0
wire[0] = 7.0                                    ← 诱饵
① flags=0x01（仅 ACTIVE, 无 WIRE2）→ wire[5] = 10.0000   期望 10.0 ✓  (旧版 = 17.0)
② flags=0x03（ACTIVE|WIRE2）        → wire[5] = 17.0000   期望 17.0 ✓
```

② 是**阳性对照**：证明判据不是退化成"恒返回 0"（那会是另一个方向的假判据）。

---

## 4. ★ A3 修复引出的成本基线位移（~10%）—— 必须解释，不能手挥

修复 A3 后回归显示 ITCM 全表从 7225 掉到 6476。这是 WCET 相关数字，**先做对照实验**
再下结论：只把 A3 那一行还原、其余修复全部保留，重新构建烧录测量：

| 构建 | B1 FLASH·全DIRECT | B2 ITCM·全DIRECT | 扫描体尺寸 |
|---|---|---|---|
| 旧判据（其余修复都在）| 29483 | **7270** | — |
| **A3 修复版** | 27727 | **6476** | 0x860 (+8B) |

⇒ **变快确实由 A3 判据引起**，且方向是"代码略大但更快"。

**合理机制（未做周期级归因，标注为假设）**：新判据在常见路径（无 WIRE2 标志且
`wire2_idx==0`）给出常量 `wb = 0.0f`，于是编译器可以省掉**一次 DTCM 加载**，并把
`_finite_f(wb)` 折叠成常量真。旧写法的三目运算通常被编译成"先无条件加载再看情况选"，
所以每条约多付一次加载与其延迟。

**据此重测全部 19 个原语成本表**（`build/op_cost_after_fix.json`，19/19 校验和可信）：

```
DIRECT 56 → 50      PID 145 → 140     全表：56 70 74 68 91 140 62 70 64 82 83 78 75 68 61 71 72 65 79
```

`k_op_cost_itcm[]` 与 `OP_COST_MAX_MEASURED`（145→140）已同步更新。
**教训**: 成本模型是"代码的函数"，代码一动就得重测；否则预算模型算的是上一版代码。

---

## 5. A5 / 根因治理：让"零警告"成为硬闸门

### 5.1 A5（CMake 缓存）已修

`build.sh` 现在**每次显式传全部开关默认值**，用户参数追加在后（CMake 以最后一次
`-D` 为准）⇒ 缓存永远被覆盖。并且构建后**打印从 CMakeCache 读回的生效开关**，
而不是打印"我以为传了什么"。

### 5.2 ★★ 零警告闸门（这才是让 A1 上线的根因）

```
CMakeLists:  -Werror                        # 警告即编译失败
build.sh:    构建后扫完整日志, warning: 数 > 0 即 exit 1 并全量打印
```

配套把噪声清干净（原 25 条 → 0 条）：

| 原噪声 | 处理 |
|---|---|
| `syscalls.c` 20 条 `unused-parameter` | 只对该文件关掉**这一种**警告，写明理由（newlib 桩签名固定）|
| `clock.c` `ppre_div` 未使用 | 删掉（它让人以为"APB 分频被反推过"，其实没有）|
| `main.c` `proto_selftest`/`deploy_selftest` 未使用 | 显式 `__attribute__((unused))`（条件是编译期开关）|
| `uart.c:78` **位移溢出（真警告）** | 见 §1 |

### 5.3 顺带：`_Static_assert` 的消息必须 ASCII

做负例验证时发现 GCC 7.3.1 会把断言消息里的**非 ASCII 字节打成八进制转义**
（`"SHM: MB_SET \37777777745\37777777677..."`）—— 完全不可读，等于断言的价值打对折。
已把 32 条断言消息全部改为 ASCII 英文。现在负例输出可读：

```
static assertion failed: "SHM MB_SET must abut MB tail"
static assertion failed: "EXEC hole size changed from 0x2B0 - did you resize a neighbour?"
```

---

## 6. H 系列修复要点

| 项 | 修法 | 验证 |
|---|---|---|
| **H3/A7** | `OFF_MB_SET` 改回 `0x4AA0`；空洞显式命名（`OFF_RSVD_DSL_DOMAIN` / `OFF_RSVD_EXEC_DOMAIN` / `OFF_MB_TAIL`）并把尺寸钉成常量断言；相邻区断言 **`<=` 改 `==`**（精确相接，静默空隙会被拒绝）| 故意把 `OFF_MB_SET` 改成 `0x4AC0` → **构建失败**（rc=1）并给出可读原因 |
| **H4** | 新增 `src/lsym.h` 的 `LSYM_ADDR()` 宏（空内联汇编强制物化符号地址），用于所有 linker 符号比较 | `shm_layout_ok` 由 **2 条指令 → 34 条**真比较；权威比对：`nm` 的 `_shm_start=g_shm=0x200033e0`、`_shm_end=0x2000b3e0`(+0x8000)，固件自报同值 |
| **H5** | 新增 `g_eng_cyc_first`/`g_isr_cyc_first`（首样本留痕）；工具在 `min == 首样本` 时打 `?`，并明确"权威口径 = max (WCET)" | 回归表里 B1/C2/G4 正确带 `?` |
| **H6** | `clock.h` 头/`clock.c`/`regs.h` 的过期频率全部改为当前值或标注为历史；声明"**CLK_* 宏是唯一权威**" | `grep 275MHz\|137.5 src/` 只剩手册引用与护栏 |
| **H7** | `CLK_TIMXCLK_HZ` 改 `2u * CLK_PCLK_HZ` + 断言 `== HCLK` + 断言 `APB_DIV ∈ {2,4}` | 编译期生效 |
| **H8** | `la_tick_freq.py` 的 `--cpu` 默认改为**从 `src/clock.h` 解析**（解析器实测输出 `400.0 MHz ← CLK_CPU_HZ`），并打印"假定值来源"；显式指定且与源码不符时**拒绝**给出"时钟树正确"结论。`h723_stage1_read.py` 的"权威标定"改为必须传 `--la-us`（否则跳过并说明这是循环论证）| 解析器实跑输出正确 |
| **H9** | `sweep_freq.sh` 加备份 + `trap ... EXIT INT TERM`，退出时还原 `clock.h` **并重建** | — |
| **H10** | `AINLINE` 加回 `static` | 编译通过 |
| **H11** | `state_offset` 改 `(i % (MAX_STATES-1)) + 1`（避开 0 哨兵）；工具预测器同步 | 全量回归表校验和仍逐位吻合 |

---

## 7. 回归与最终状态

```
零警告构建          : ✓ (25 → 0; -Werror 生效)
A1  ISER[1]         : 0x00000020 ✓      g_uart_irq_en = 1 ✓
N1  实发帧          : C1 00 04 00 00 02 33 00 C9 C9   (独立 CRC = 0xC9C9 ✓)
A4  cap             : 0x0033 ✓ (HOTRELOAD | WIRE2_FLAG | MULTICYCLE | VERINFO)
H4  g_shm_ok        : 1 ✓  (0x200033e0 == _shm_start; _shm_end = +0x8000)
A3  wire2 对照      : 10.0 / 17.0 依 flags 区分 ✓
全量 A/B 回归       : 22 ✓ / 0 ✗
成本表              : 19/19 重测可信, 已同步进固件
```

---

## 8. 未修 / 待办（诚实清单）

| 项 | 为什么没做 |
|---|---|
| **A2 接线** | 物理动作在用户侧。**但现在已知：即使接好，A1 与 N1 两道闸也会各拦一次** —— 两根线接好后应能直接过 6/6 |
| **A6 `0x38` 字段语义** | 需要先与 S3 逐字段核对 `run` vs `gate`、引擎扫描时长 vs ISR 总时长，再决定"改字段"还是"改声明"。**不能凭印象改**（这正是 S3 审计里 SCB bit16/17 那两次误判的教训）|
| **A9 `g_tick_count` 溢出** | 4.97 天回绕会在 `g_per_prev` 差值上产生一个假极值。属"长时间运行"问题，需要定"回绕时钳位 or 换 u64" |
| **PA9 波形外部复核** | LA 已由用户断开；A1/N1 修复后需重新接上做一次字节级复核 |
| **ITCM 自身的落位敏感** | 本轮观测到扫描体 +8 字节即带来 10% 成本变化，机制未做周期级归因。建议照 `REF-flash-placement.md` 的方法对 ITCM 也做一次落位扫描 |

---

## 9. 方法论收获（写进 skill）

1. **噪声不清零，真警告一定会被漏掉**。`-Werror` + 构建后零警告闸门是**基础设施**，
   不是洁癖。A1 的代价是一整轮方向错误的排障。
2. **"配置全对"不等于"功能可用"**。A1 期间 CR1/BRR/GPIO/AFRH/RCC 全绿、LA 还能看到
   波形 —— 缺的是"中断使能"这一项**没人检查**。凡是"写过了就算"的状态，
   都要补一个**能被外部读走的量**（`g_uart_irq_en`）。
3. **对端视角才是判据**。N1 能藏这么久，是因为我们一直在"固件内部"验证；
   把 `s_txbuf` 读出来用**独立实现**校验 CRC，5 分钟就露了。
4. **成本数字必须做对照实验才归因**。A3 修复导致 10% 位移，用"只还原那一行"的
   受控对照确认了因果，而不是凭直觉说"大概是代码变了吧"。
5. **断言消息要 ASCII**，否则失败信息本身不可读。
