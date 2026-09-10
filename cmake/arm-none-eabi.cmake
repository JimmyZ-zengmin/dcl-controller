# arm-none-eabi.cmake — CMake 交叉编译工具链文件
# 工具链来源: 本机已有 STM32CubeIDE 自带的 GNU Tools for STM32 (GCC 7.3.1)
# 路径取自 D:/STM/work/dcl-controller/firmware/h723-core0/build.bat (老项目使用记录)
# 若要换新版工具链, 只改 TOOLCHAIN_BIN 一行即可。

set(CMAKE_SYSTEM_NAME      Generic)
set(CMAKE_SYSTEM_PROCESSOR arm)

# 交叉编译不做可执行文件试跑 (否则 CMake 会尝试运行 arm 程序而失败)
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)

set(TOOLCHAIN_BIN "C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924/tools/bin")

set(CMAKE_C_COMPILER   "${TOOLCHAIN_BIN}/arm-none-eabi-gcc.exe")
set(CMAKE_ASM_COMPILER "${TOOLCHAIN_BIN}/arm-none-eabi-gcc.exe")
set(CMAKE_CXX_COMPILER "${TOOLCHAIN_BIN}/arm-none-eabi-g++.exe")
set(CMAKE_OBJCOPY      "${TOOLCHAIN_BIN}/arm-none-eabi-objcopy.exe" CACHE FILEPATH "")
set(CMAKE_SIZE         "${TOOLCHAIN_BIN}/arm-none-eabi-size.exe"    CACHE FILEPATH "")

set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
