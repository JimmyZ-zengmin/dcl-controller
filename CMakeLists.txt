cmake_minimum_required(VERSION 3.20)
project(dcl_h723 C ASM)

# ---------------------------------------------------------------------------
# DCL 引擎 H723 平台
#
# 目标产物: dcl_h723.elf / .bin / .hex, 可直接用 pyocd 烧录:
#   pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex
#
# 配置旋钮一律用 `bash build.sh -DDCL_XXX=n` 传 (build.sh 每次显式传全部默认值,
# 不会受 CMake cache 残留影响 —— 那曾导致"把自检版当交付固件"的事故)。
# 当前主频/分频的**唯一权威来源**是 src/clock.h 的 CLK_* 宏。
# ---------------------------------------------------------------------------

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# ★ 独立的"最小固件"开关: 1 = 只编 src/min_uart.c (与 DCL 引擎/中断/协议全无关)。
#   排障用: 当"串口不通"时, 用它把自研代码整体排除, 先证明 USART 通路本身可用。
set(DCL_MIN_UART "0" CACHE STRING "1 = 只编最小 UART 固件 (src/min_uart.c), 与引擎无关")
# ★★ 第二个最小固件 (2026-09-13): 把"485 收不到"这件事从自研代码里彻底摘出来。
#   与 DCL_MIN_UART 的区别: 那一档只测 USART1 (观察窗), 这一档测的是**被测口**
#   —— USART2 @ PD5/PD6, 并且额外做一次"整口逐脚活动扫描", 用来回答
#   "模块的 TXD 那根线到底插在 GPIOD 的哪个脚上" (485 排障里最费轮次的问题)。
#   ★ 必须与 DCL_MIN_UART 互斥: 两个文件都定义 main()。
set(DCL_MIN_UART2 "0" CACHE STRING "1 = 只编最小 485/USART2 接收固件 (src/min_uart485.c)")
set(DCL_MIN_STRESS "0" CACHE STRING "最小固件: 1 = TX 不停发 (压力模式, 便于判 TX 通路)")

# ---- ★ 响应延迟优化 (2026-09-13): 两个 A/B 开关, 见 src/engine.h 的推导 ----
#   MB_BUILD_BUDGET: 组装每拍字节预算 (纯 CPU 部分)。4 = **改前行为** (与 RX 预算同用 4),
#     32 = 交付; 133B 响应的组装从 33 拍压到 5 拍 (3.3ms → 0.5ms), WCET 代价 ≈4µs/拍。
#   MB_FAST_FRAME: 1 = 按长度早判帧 (长度到 + CRC 通过即判帧), 0 = **改前行为** (只等静默)。
#   ★ 为什么要留 0 档 (本项目铁律): "改完只跑交付版看到变快"不构成证据 ——
#     必须用**同一套判据**在 0 档上量出"慢"(能失败的对照), 改进才算被证明。
set(DCL_MB_BUILD_BUDGET "32" CACHE STRING "组装每拍字节预算 (4 = 改前行为对照)")
set(DCL_MB_FAST_FRAME "1" CACHE STRING "按长度早判帧 (0 = 改前行为对照)")

# ---- ★ 独立看门狗 (2026-09-13): 喂狗契约与理由见 src/wdt.h 文件头 ----
#   DCL_WDT=1 (交付): IWDG1 超时 200ms, **喂狗点 = 拍 ISR** (不把主循环拉进门)。
#   DCL_WDT=0: **改前行为**, 不看门狗。
#   ★ 为什么必须有 0 档 (本项目铁律: 结构性改动必配"能失败的对照"):
#     配合运行时注入钩子 `SD_CFG[13]` (让拍 ISR 死循环) 打两份:
#       WDT=0 ⇒ 板子**永久卡死** (串口全无应答)      ← 判据证明得出"坏"
#       WDT=1 ⇒ **200ms 内复位** 且 `mgmt.py --boot` 说得出原因 ← 修复才算被证明
#     只跑 1 档看到"能复位"不构成证据 —— 你无法排除"根本没注入进去"。
#   DCL_WDT_MS: 超时档。200ms 的依据 = 已知最长合法阻塞 ~816ms 只压主循环、
#     而**擦除期间拍中断不丢** (T26 的 A/B: 对照 8153 拍缺口 / 交付 0 拍) ⇒ 拍能喂狗。
set(DCL_WDT     "1"   CACHE STRING "独立看门狗 (1 交付 / 0 改前行为对照)")
set(DCL_WDT_MS  "200" CACHE STRING "看门狗超时 ms (LSI 32kHz, PR=4 ⇒ 2ms/计数)")
#   DCL_WDT_FEED_GATE=1 (交付): 拍 ISR 喂狗前先过"关闸"—— wdt_start() 编程 PR/RLR 期间
#     不许写 IWDG_KR。依据 RM0468 §50.3.6: 写 KR 的其它值(点名重载键 0xAAAA)**会打断
#     PR/RLR 的解锁序列**。而喂狗就是写 0xAAAA、且每 100µs 一次 ⇒ 不关闸就会用自己的
#     喂狗把初始化打断, 现象是 `wdt_start()` 返回 -2 (PVU/RVU 永不清零)。
#   =0: **改前行为**对照 —— 用同一套测量方法打一份"坏"的读数出来。
set(DCL_WDT_FEED_GATE "1" CACHE STRING "喂狗门 (1 交付 / 0 改前行为: 不关闸)")
#   等 PR/RLR 更新落的轮询预算 (次数)。400MHz 下次循环 ≈10 周期 ⇒ 4e6 ≈ 100ms。
#   实测 4e5(≈10ms) 两次开机都"刚好用完" ⇒ 换成 4e6 做 A/B, 区分"真耗时"与"跑满预算"。
#   关闸期间 IWDG 按默认档在数 (~512ms) ⇒ 100ms 仍在安全区。
set(DCL_WDT_SYNC_CYC "40000000" CACHE STRING "等更新落的周期死线 (4e7 = 100ms @400MHz)")
#   DCL_WDT_START_FIRST=1 (交付): **先写 0xCCCC 启动, 再配 PR/RLR**。
#     ★ 这一条是 `wdt_start()` 返回 -2 的**已证实根因** (单变量对照: 只改这一处 ⇒ -2→0)。
#   =0: 改前行为对照 (先配后启动) —— 必须有, 否则"修好了"只是一句话。
set(DCL_WDT_START_FIRST "1" CACHE STRING "先启动后配置 (1 交付 / 0 改前行为对照)")
#   DCL_WDT_PR: 预分频档 0..7。存在的理由 = 验证"更新耗时 ≈ 5 个预分频步长"这条推断:
#     PR=4 ⇒ 2ms/步 ⇒ ~10ms (已实测); PR=6 ⇒ 8ms/步 ⇒ 应变成 ~40ms。
set(DCL_WDT_PR "4" CACHE STRING "IWDG 预分频档 0..7")
#   ★★★ DCL_WDT_PERSIST_WINDOW (2026-09-13, 解锁"掉电保持"的那一项):
#     擦 flash 期间拍 ISR 的喂狗(写 IWDG_KR)**无法完成** ⇒ ISR 卡住 ⇒ 喂狗停
#     ⇒ 200ms 后 IWDG 复位 ⇒ "保存"变"重启", 配置从未落盘(见 docs/audit/H723-PERSIST-WDT-DEFECT.md)。
#     ⇒ 落盘时**临时**把超时放大到本值, 擦完在 persist_save 的单一出口恢复。
#     取值必须 ≥ 擦除超时预算 (flash.c 的 FL_ERASE_TIMEOUT_CYC = 8s): 正常擦除实测仅 0.97s,
#     多余的部分是给**失败路径**留的。★ 这三个量是同一件事的三种单位, 改一个要改另外两个:
#       PERSIST_BLOCK_TICKS(main.c) · FL_ERASE_TIMEOUT_CYC(flash.c) · 本值。
#   =0: **改前行为**对照 (不开窗) ⇒ 落盘应当**复位**, 用同一套判据打出坏的那一半。
set(DCL_WDT_PERSIST_WINDOW "8000" CACHE STRING "落盘窗口 ms (0 = 改前行为对照: 不开窗, 会复位)")
#   DCL_LOOP_RESET: 主循环失活自愈 (2026-09-13, 回应审计 P1①)。
#     1 (交付): 拍 ISR 判停滞 → 记录 → eng_outputs_safe() → 软复位。
#       依据: 主循环死时协议口全断 ⇒ "失联但保持输出"比"复位(停止驱动)"更危险。
#     0 (对照): 只记录 FAULT_LOOP_STALL, 不复位 ⇒ 用来证明"不会自愈"那一半。
#   ★ 阈值/窗口的定义与实测依据见 main.c 的"主循环活性"段 (阈值不许拍)。
set(DCL_LOOP_RESET "1" CACHE STRING "主循环失活自愈 (1 交付 / 0 只记录不复位)")
#   DCL_PERSIST_SAVE: 掉电保持"保存" (2026-09-13 **已降级为默认 0**)。
#     为什么默认 0: 擦/写内部 flash 与 200ms 看门狗不共存 ⇒ "保存"= 重启机器。
#     置 1 只用于**诊断/将来修复后的回归** (见 docs/audit/H723-PERSIST-WDT-DEFECT.md)。
set(DCL_PERSIST_SAVE "0" CACHE STRING "掉电保持保存 (0=已降级不可用 / 1=打开, 需先修 ISR 卡死)")

if(DCL_MIN_UART)
    set(TARGET min_uart)
elseif(DCL_MIN_UART2)
    set(TARGET min_uart485)
else()
    set(TARGET dcl_h723)
endif()

# ---- bench 启动默认配置 (外部仪器测非默认配置时必须编进固件, 见 main.c 注释) ----
set(DCL_BOOT_PROFILE "0" CACHE STRING "启动默认 table_profile (0 全DIRECT / 1 混合 / 2 全PID)")
set(DCL_BOOT_GATE    "1" CACHE STRING "启动默认 engine_gate (0 骨架 / 1 开扫描; W1 起默认 1)")
set(DCL_BOOT_SEL     "1" CACHE STRING "启动默认 engine_sel  (0 走FLASH / 1 走ITCM)")
set(SCAN_FLASH_PAD   "0" CACHE STRING "engine_scan_flash 之前的填充字节数 (落位敏感实验)")
set(DCL_BOOT_SCAN_MODE "0" CACHE STRING "启动默认 scan_mode (0 全表扫 / 1 分档调度)")
# ★ 向量表落位 (T26 配套修复, 2026-09-11): 1 = 运行期搬进 ITCM 并设 VTOR (交付默认);
#   0 = 留在 FLASH (改前行为) —— **只为 A/B 对照存在**: 用它证明"擦除期间丢拍"这个
#   判据真的能失败 (同一套测量方法在 0 上必须量到缺口), 否则判据是空的。
set(DCL_VTOR_ITCM    "1" CACHE STRING "向量表搬进 ITCM+设VTOR (1) / 留在FLASH 对照 (0)")

# ---- 阶段 3.1 协议层开关 ----
set(DCL_PA9_MODE       "1"      CACHE STRING "PA9 用途 (0 GPIO方波 / 1 USART1_TX)")
# ---- 通信域 (Modbus) 物理口引脚 (2026-09-13) ----
#   1 = **PD5(TX)/PD6(RX)** —— 交付默认。USART2 的备用脚, 用来切开
#       "PA2/PA3 本身有问题"(引脚损伤 / 板上丝印认错 / 被别的东西占用)这个一直没排除的可能。
#   0 = PA2(TX)/PA3(RX) 原方案 —— 对照档: 同一套判据在两档上各跑一遍才有意义。
#   ★ 两档都是 **同一路 USART2 的 AF7**, 所以波特率/协议/状态机一个字都不改。
set(DCL_MB_UART_ALT "1" CACHE STRING "Modbus 物理口: 1=PD5/PD6 备用脚 / 0=PA2/PA3 原方案")
set(DCL_BOOT_BANNER    "1"      CACHE STRING "上电发一帧版本响应 (LA 免 PC 验协议)")
set(DCL_BANNER_PERIOD  "0"      CACHE STRING "横幅重播周期 ms (0=只发一次; >0 仅供 LA 抓取)")
set(DCL_UART_SELFTEST  "0"      CACHE STRING "上电回环自检 (1 需 PA9↔PA10 短接)")
set(DCL_UART_BAUD      "115200" CACHE STRING "协议波特率")
set(DCL_DEPLOY_SELFTEST "0"     CACHE STRING "上电跑 deploy 自检 (9 例)")

# ---- 审计 #1 的 A/B 对照开关: 停机安全态是否覆盖 HIL 物理输出 ----
#   1 = 交付行为: HIL 的 PWM 登记为"输出面", STOP/RESET 归零, 且输出臂受 ENGINE_RUN 门控
#   0 = **改前行为**: 不登记、不门控 ⇒ STOP 后 PWM 保持最后占空比 (这正是被修掉的缺陷)
#   ★ 为什么必须有 0 档 (本项目纪律, 与 DCL_VTOR_ITCM 同一条):
#     "改完只跑交付版看到 PASS" 不构成证据 —— 无法排除"判据本身量不出问题"。
#     必须用**同一套测量方法**在 0 档上量到 FAIL(占空比不归零), 判据才算成立。
#     (历史教训: 向量表那次就是靠 0 档量出 8153 拍缺口, 才证明判据没瞎。)
set(DCL_HIL_SAFE "1" CACHE STRING "停机安全态覆盖 HIL 输出 (1 交付 / 0 改前行为对照)")

# ---- ★ P1/P2 拍内 I/O 搬迁的 A/B 对照开关 (2026-09-12) ----
#   1 = 交付: DI / AI / HIL 由 **ISR 按拍**驱动 (输入段 + 输出段, 见 main.c 的 ISR 注释)
#   0 = **改前行为**: 三者仍由主循环按 10ms 自节流驱动 (ai_tick / di_tick / hil_tick)
#   ★ 为什么必须有 0 档 (与 DCL_HIL_SAFE / DCL_VTOR_ITCM 同一条纪律):
#     "搬进拍内"改变的是**驱动位置**, 影响的是**确定性** —— 而确定性恰恰是最容易被
#     "改完跑一下看起来正常"糊过去的性质。必须用**同一套测量方法**在两档上打出两份
#     数字 (isr_cyc 增量 / DI 采样间隔 / PWM 跟随延迟), 差异才算证据。
#   ★ 附带价值 (可失败判据): IO_IN_ISR=1 而 ADC 非阻塞化失效时, 拍周期会**当场崩掉**
#     (ADC 单次转换 259µs > 拍长 100µs) —— 这本身就是"非阻塞化是必需, 不是优化"的实证。
set(DCL_IO_IN_ISR "1" CACHE STRING "I/O 驱动位置 (1 ISR 拍内 / 0 主循环对照)")

# ---- ★★ DO 输出路径 A/B 对照开关 (2026-09-16 **补接线**) ----
#   0 = **交付档**: CPU 一次 32 位 `GPIO_BSRR` 直写（那次写就是写引脚）
#   1 = **对照档**: 影子 + MDMA 定时锁存（那次写只写影子，引脚由 MDMA 在拍上溢锁存）
#   ★★ 为什么是"补接线": `src/do.h` 的注释**早就宣称**"可被 CMake `-DDCL_DO_LATCH=0`
#     覆盖", 但 CMakeLists **从来没有** `set(... CACHE ...)` 也没有传 `-D`
#     ⇒ 那是个**假开关**: 你用 `-D` 传它，CMake 连缓存项都不建，构建脚本那节
#     "生效开关(读自 CMakeCache)"里**根本不会出现 DCL_DO_LATCH** ⇒ 你以为切了, 其实没切。
#     （本项目第 2 次栽在"声称可覆盖但没接线": 第 1 次是 ISR 调用树闸门。）
#   ★ 判据/量化: 0 档引脚抖动 ≈3.6 ns, 1 档 ≈54~60 ns（LA 实测 PE0 σ≈60 ns）。
#     但**必须有 1 档**才能跑 A/B —— 没有对照档 = 这个差异无法自证。
#   ★★ 切到 0 档的**必要补丁**在 src/do.c `do_latch_init()`: 0 档必须**完全不启动**
#     MDMA 锁存链, 否则它每 100 µs 把影子(0 档没人写它 ⇒ 恒 0)锁进 GPIOE_ODR 低字节
#     ⇒ **PE0~PE7 被静默清零, DO 0..7 永不可用**（观测面看不出来, 寄存器读回全对）。
set(DCL_DO_LATCH "0" CACHE STRING "DO 输出路径 (0 CPU直写=交付 / 1 影子+MDMA锁存=对照)")

# ---- ★★★ DO 掩码"归属"开关 (2026-09-17) —— 步进保留位是否可被上位机掩码剔除 ----
#   1 = **交付档**: 生效掩码 = host | DO_STEP_RESERVED_MASK(0x0F00) ⇒ **PE8..PE11 永不被丢**
#   0 = **对照档**: 生效掩码 = host（改前行为）⇒ 上位机换掩码就能让 PE9 停在非受控电平
#   ★ 起因（一个被单个计数器掩盖了真因的问题）: `g_step_ena_mismatch_n` 涨到 **327616 后冻结**,
#     当时被解读为"有别的代码在驱 PE9"; 实测真因是 `h723_do_hold_test.py` 把掩码换成 0x00FF、
#     `h723_w1.py` 注入 0x7FC00000（&0xFFFF = 0）⇒ `do_poll` 不再驱 PE9 ⇒ 引脚冻结在旧电平。
#   ★ 判据（能失败）: `tools/h723_do_mask_owner_test.py` —— 对照档上 mismatch_n **必须涨**,
#     交付档上 **必须为 0** 且 `do_mask_step_drop_n` 涨 1（同一套测量方法打两份, 缺一不成证据）。
set(DCL_DO_MASK_UNION "1" CACHE STRING "DO 掩码保留位 (1 保留位不可剔=交付 / 0 掩码说了算=对照)")

# ---- ★★★ 斜坡推进 dt 修复开关 (2026-09-18) ----
#   1 = 交付(按真实 dt 算 + 余数累加器) / 0 = 对照(改前: dt_ms 向上取整到 >=1ms)
#   病灶: step_tick 由主循环每 ~0.37ms 调一次, 而 dt_ms 被算成 1ms => 每次多爬 2.7 倍
#        => 声明 12000 Hz/s 实测 ~32 kHz/s (四量互证, 见 MEMORY §5.31)
set(DCL_STEP_RAMP_FIX "1" CACHE STRING "斜坡 dt 修复 (1 按真实 dt=交付 / 0 改前行为=对照)")

# ---- ★★★ 步进 ENA 极性 + 停止保持力矩 (2026-09-17, PLAN-device-config-v1 P0) ----
#   起因: 现场"电机响但轴不动" = `g_step_ena_pol` 的**隐藏默认值**与实机接线相反,
#         把「使能 / 失能 / 上电安全态」**三处同时弄反** (H723-MOTION-QUALITY-AUDIT §16)。
#   ★ 工业做法(雷赛 DMC2210"板卡跳线设置" / ADLINK 限位 NO/NC 板上 DIP / PCI-8132 `8132.cfg`):
#     **极性是接线属性, 必须显式给出** ⇒ 这里把它从"藏在 .c 里的字面量"变成**出厂声明**;
#     未声明就**不许使能**(fail-closed), 而不是"猜一个值"。
#   DCL_STEP_ENA_POL  三态:
#     -1 = **未配置** ⇒ fail-closed: 拒绝 ena(1) + 上电**物理失能**
#      0 = 光耦导通(MCU 拉低) = 使能
#      1 = 光耦不导通(MCU 高)   = 使能   ← ★ **本台实机**(光耦正端接 3.3V, 实测)
#   DCL_STEP_STOP_HOLD 停止/上电时是否**保持力矩**(通电):
#      1 = 保持 (= 今天的行为; **垂直轴必须** —— 去使能 = 掉力滑车)
#      0 = 去使能 (省热; 仅水平轴且已评估时用)
#   ★★ 两者**正交**: 极性只决定"怎么通电", 保不保力矩是按轴决定的**安全策略**。
#   ★ 判据(都能失败): 未声明 ⇒ ena(1) 必拒且 PE9 物理失能; 两种极性下上电态都要满足 stop_hold;
#     "指令 vs PE9 实读"不一致 ⇒ g_step_ena_mismatch_n 必涨（见 src/step.c 的 step_tick）。
set(DCL_STEP_ENA_POL "1" CACHE STRING "步进 ENA 极性 (-1=未配置/fail-closed, 0=拉低使能, 1=拉高使能)")
set(DCL_STEP_STOP_HOLD "1" CACHE STRING "停止/上电是否保持力矩 (1=通电保持, 0=去使能)")

# ---- ★★★ 生产时基 A/B 对照开关 (2026-09-16) ----
#   1 = **交付**: 生产时基 = **TIM5**（普通 32 位定时器, 200MHz ⇒ 5ns）—— **不受调试器影响**
#   0 = **改前行为对照**: 生产时基 = **DWT_CYCCNT**（400MHz ⇒ 2.5ns）—— 调试器会话收尾会清
#       `DEMCR.TRCENA` 把它关掉, 而 `flash.c` 拿它当**超时判据** ⇒ DWT 一死, 超时永不触发
#       ⇒ "有界喂狗"退化成**无限喂狗** ⇒ **卡死且看门狗失效**。
#   依据（三个独立来源）: pyOCD issue #1540 / SEGGER KB(J-Link 同款) / ARMv7-M ARM("Debug state 时暂停")。
#   评估全文: docs/ASSESS-toolchain-2026-09-16.md
#   ★ 判据（能失败的）: 跑一次 `pyocd reset`（把 DWT 弄死）之后, 0 档的 `h723_jitter` 必须红,
#     而 1 档必须仍然绿 —— 这就是这个开关存在的全部意义。
set(DCL_TIMEBASE "1" CACHE STRING "生产时基 (1=TIM5 抗调试器=交付 / 0=DWT 改前行为对照)")

# Cortex-M7 + 双精度 FPU + 硬浮点 (与老项目 build.bat 一致)
set(MCU_FLAGS -mcpu=cortex-m7 -mfpu=fpv5-d16 -mfloat-abi=hard -mthumb)

add_compile_options(
    ${MCU_FLAGS}
    -ffunction-sections -fdata-sections     # 配合 --gc-sections 去死代码
    -Wall -Wextra -Wshadow
    # ★★ **-Werror**: 警告即失败。
    #   起因是一次真事故: `uart.c` 里 `NVIC_ISER = (1u << 37)` 位移溢出, GCC 老老实实
    #   报了 `-Wshift-count-overflow`, 但同一次构建里还有 24 条无害警告
    #   (newlib 桩的 unused-parameter 等), 这条真警告被淹没, 没人看见 ——
    #   结果是 USART1 中断从未使能、协议层整轮不可用, 而所有"配置类"检查全绿。
    #   所以本项目不再接受"无害警告": 噪声清零, 剩下的每一条都必须处置。
    -Werror
    -O2 -g3
    --specs=nano.specs                      # 精简 newlib
    -DSTM32H723xx
    # ★ 必须关 IPA-ICF: 引擎扫描体有两份**逐指令相同**的实例 (FLASH / ITCM),
    #   GCC 的 IPA-ICF 会把"内容相同的函数"折叠成一个 → 只剩一份 → A/B 实验作废。
    #   (实测: 打开时 nm 里只剩 engine_scan_flash 或只剩 itcm 之一)
    -fno-ipa-icf
    -DBOOT_PROFILE=${DCL_BOOT_PROFILE}
    -DBOOT_GATE=${DCL_BOOT_GATE}
    -DBOOT_SEL=${DCL_BOOT_SEL}
    -DSCAN_FLASH_PAD=${SCAN_FLASH_PAD}
    -DBOOT_SCAN_MODE=${DCL_BOOT_SCAN_MODE}
    -DVTOR_ITCM=${DCL_VTOR_ITCM}
    -DPA9_MODE=${DCL_PA9_MODE}
    -DMB_UART_ALT=${DCL_MB_UART_ALT}
    -DMB_BUILD_BUDGET=${DCL_MB_BUILD_BUDGET}
    -DMB_FAST_FRAME=${DCL_MB_FAST_FRAME}
    -DDCL_WDT=${DCL_WDT}
    -DWDT_TIMEOUT_MS=${DCL_WDT_MS}u
    -DWDT_FEED_GATE=${DCL_WDT_FEED_GATE}
    -DWDT_SYNC_BUDGET_CYC=${DCL_WDT_SYNC_CYC}u
    -DWDT_START_FIRST=${DCL_WDT_START_FIRST}
    -DWDT_PR_VALUE=${DCL_WDT_PR}u
    -DWDT_PERSIST_WINDOW_MS=${DCL_WDT_PERSIST_WINDOW}u
    -DDCL_LOOP_RESET=${DCL_LOOP_RESET}
    -DDCL_PERSIST_SAVE=${DCL_PERSIST_SAVE}
    -DBOOT_BANNER=${DCL_BOOT_BANNER}
    -DBANNER_PERIOD_MS=${DCL_BANNER_PERIOD}
    -DUART_SELFTEST=${DCL_UART_SELFTEST}
    -DUART_BAUD=${DCL_UART_BAUD}u
    -DDEPLOY_SELFTEST=${DCL_DEPLOY_SELFTEST}
    -DMIN_TX_STRESS=${DCL_MIN_STRESS}
    -DHIL_SAFE=${DCL_HIL_SAFE}
    -DIO_IN_ISR=${DCL_IO_IN_ISR}
    -DDCL_DO_LATCH=${DCL_DO_LATCH}
    -DDCL_DO_MASK_UNION=${DCL_DO_MASK_UNION}
    -DDCL_STEP_RAMP_FIX=${DCL_STEP_RAMP_FIX}
    -DDCL_TIMEBASE=${DCL_TIMEBASE}
    -DDCL_STEP_ENA_POL=${DCL_STEP_ENA_POL}
    -DDCL_STEP_STOP_HOLD=${DCL_STEP_STOP_HOLD}
)

# syscalls.c 是 newlib 桩: 函数签名由 newlib 规定, 参数天然用不上。
# 单独关掉该文件的 unused-parameter —— 它曾贡献 20 条无害警告, 是"真警告被淹没"
# 的主要噪声源 (见上面 -Werror 的说明)。**只关这一种, 且只关这一个文件。**
set_source_files_properties(${CMAKE_CURRENT_SOURCE_DIR}/src/syscalls.c
    PROPERTIES COMPILE_OPTIONS "-Wno-unused-parameter")

add_link_options(
    ${MCU_FLAGS}
    -T${CMAKE_CURRENT_SOURCE_DIR}/ld/STM32H723ZG_FLASH.ld
    --specs=nano.specs
    -Wl,--gc-sections
    -Wl,-Map=${CMAKE_BINARY_DIR}/${TARGET}.map
    -Wl,--print-memory-usage
)

# startup 用 .s (小写) → 显式按"带预处理汇编"编译, 与 ST 模板一致
set(STARTUP ${CMAKE_CURRENT_SOURCE_DIR}/startup/startup_stm32h723zgtx.s)
set_source_files_properties(${STARTUP} PROPERTIES
    LANGUAGE ASM
    COMPILE_OPTIONS "-x;assembler-with-cpp")

if(DCL_MIN_UART)
    # 最小固件: 只要一个 C 文件 + 启动文件 (不用 newlib, 不用 syscalls/sysmem)
    set(SOURCES
        ${CMAKE_CURRENT_SOURCE_DIR}/src/min_uart.c
        ${STARTUP}
    )
elseif(DCL_MIN_UART2)
    # 最小 485 固件: 同上, 但测的是 USART2 @ PD5/PD6 + 整口扫描
    set(SOURCES
        ${CMAKE_CURRENT_SOURCE_DIR}/src/min_uart485.c
        ${STARTUP}
    )
else()
    set(SOURCES
        ${CMAKE_CURRENT_SOURCE_DIR}/src/main.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/engine.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/clock.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/flash.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/persist.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/modbus.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/macro.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/adc.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/di.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/do.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/rtc.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/blackbox.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/sd.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/hil.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/i2c_bb.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/i2c_sm.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/dev_bind.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/timebase.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/as5600.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/step.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/prog_store.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/transport.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/uart.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/syscalls.c
        ${CMAKE_CURRENT_SOURCE_DIR}/src/sysmem.c
        ${STARTUP}
    )
endif()

add_executable(${TARGET} ${SOURCES})

# 头文件目录
target_include_directories(${TARGET} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)

# ---- 产物: .bin / .hex + 体积报告 ----
# 用工具链文件里显式设置的路径 (工具链 bin 不在 PATH 上, find_program 找不到)
if(NOT CMAKE_OBJCOPY OR NOT CMAKE_SIZE)
    message(FATAL_ERROR "工具链文件未设置 CMAKE_OBJCOPY / CMAKE_SIZE")
endif()

add_custom_command(TARGET ${TARGET} POST_BUILD
    COMMAND "${CMAKE_OBJCOPY}" -O binary $<TARGET_FILE:${TARGET}> ${CMAKE_BINARY_DIR}/${TARGET}.bin
    COMMAND "${CMAKE_OBJCOPY}" -O ihex   $<TARGET_FILE:${TARGET}> ${CMAKE_BINARY_DIR}/${TARGET}.hex
    COMMAND "${CMAKE_SIZE}" $<TARGET_FILE:${TARGET}>
    COMMENT "生成 .bin / .hex 并报告体积"
)
