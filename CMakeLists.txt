cmake_minimum_required(VERSION 3.20)
project(dcl_h723 C ASM)

# ---------------------------------------------------------------------------
# DCL 引擎 H723 平台 (阶段 0: 工具链 + 时钟基线)
#
# 目标产物: dcl_h723.elf / .bin / .hex, 可直接用 pyocd 烧录:
#   pyocd flash -t stm32h723xx build/dcl_h723.hex
#
# 阶段 0 的验收 = "PLL 锁到 550MHz, MCO 引出来能被逻辑分析仪量到 550MHz"
#   见 MIGRATE-H723.md §3.5 / §3.6
# ---------------------------------------------------------------------------

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

set(TARGET dcl_h723)

# ---- bench 启动默认配置 (外部仪器测非默认配置时必须编进固件, 见 main.c 注释) ----
set(DCL_BOOT_PROFILE "0" CACHE STRING "启动默认 table_profile (0 全DIRECT / 1 混合 / 2 全PID)")
set(DCL_BOOT_GATE    "0" CACHE STRING "启动默认 engine_gate (0 骨架 / 1 开扫描)")
set(DCL_BOOT_SEL     "1" CACHE STRING "启动默认 engine_sel  (0 走FLASH / 1 走ITCM)")

# Cortex-M7 + 双精度 FPU + 硬浮点 (与老项目 build.bat 一致)
set(MCU_FLAGS -mcpu=cortex-m7 -mfpu=fpv5-d16 -mfloat-abi=hard -mthumb)

add_compile_options(
    ${MCU_FLAGS}
    -ffunction-sections -fdata-sections     # 配合 --gc-sections 去死代码
    -Wall -Wextra -Wshadow
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
)

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

set(SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/src/main.c
    ${CMAKE_CURRENT_SOURCE_DIR}/src/engine.c
    ${CMAKE_CURRENT_SOURCE_DIR}/src/clock.c
    ${CMAKE_CURRENT_SOURCE_DIR}/src/syscalls.c
    ${CMAKE_CURRENT_SOURCE_DIR}/src/sysmem.c
    ${STARTUP}
)

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
