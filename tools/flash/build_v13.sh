#!/bin/bash
# 快速编译 V1.3 单文件 main.c (已知可工作)
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

ST="/c/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924/tools/bin"
GCC="$ST/arm-none-eabi-gcc.exe"
SIZE="$ST/arm-none-eabi-size.exe"

HERE="/d/STM/work/dcl-controller/firmware/h723-core0"
cd "$HERE"

MCU="-mcpu=cortex-m7 -mfpu=fpv5-d16 -mfloat-abi=hard -mthumb"
CFLAGS="$MCU -std=gnu11 -g3 -O2 -ffunction-sections -fdata-sections -Wall --specs=nano.specs -DSTM32 -DSTM32H723ZGTx -DDEBUG"

echo "=== 编译 V1.3 main.c ==="
"$GCC" $CFLAGS -c Src/main.c -o bld/Src/main.o 2>&1 | grep -v "redeclaration of memory"

echo "=== 链接 ==="
"$GCC" $MCU -T STM32H723ZGTX_FLASH.ld --specs=nano.specs -Wl,--gc-sections -Wl,-Map=bld/core0_h723.map \
  bld/Src/main.o bld/Startup/startup.o -o bld/core0_h723.elf 2>&1 | grep -v "redeclaration of memory"

echo "=== SIZE ==="
"$SIZE" bld/core0_h723.elf
ls -la bld/core0_h723.elf
