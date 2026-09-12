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
set(DCL_MIN_STRESS "0" CACHE STRING "最小固件: 1 = TX 不停发 (压力模式, 便于判 TX 通路)")

if(DCL_MIN_UART)
    set(TARGET min_uart)
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
    -DBOOT_BANNER=${DCL_BOOT_BANNER}
    -DBANNER_PERIOD_MS=${DCL_BANNER_PERIOD}
    -DUART_SELFTEST=${DCL_UART_SELFTEST}
    -DUART_BAUD=${DCL_UART_BAUD}u
    -DDEPLOY_SELFTEST=${DCL_DEPLOY_SELFTEST}
    -DMIN_TX_STRESS=${DCL_MIN_STRESS}
    -DHIL_SAFE=${DCL_HIL_SAFE}
    -DIO_IN_ISR=${DCL_IO_IN_ISR}
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
