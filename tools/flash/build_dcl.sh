#!/bin/bash
# dcl-controller firmware builder (Win git-bash)
# Call: bash D:/STM/work/dcl-controller/tools/flash/build_dcl.sh
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

ST="/c/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924/tools/bin"
GCC="$ST/arm-none-eabi-gcc.exe"
SIZE="$ST/arm-none-eabi-size.exe"
BINCPY="$ST/arm-none-eabi-objcopy.exe"

HERE="/d/STM/work/dcl-controller/firmware/h723-core0"
cd "$HERE"

MCU="-mcpu=cortex-m7 -mfpu=fpv5-d16 -mfloat-abi=hard -mthumb"
CFLAGS="$MCU -std=gnu11 -g3 -O2 -ffunction-sections -fdata-sections -Wall --specs=nano.specs -DSTM32 -DSTM32H723ZGTx -DDEBUG"

echo "=== [1/3] CC main.c ==="
"$GCC" $CFLAGS -c Src/main.c -o bld/Src/main.o

echo "=== [2/3] LD core0_h723.elf ==="
"$GCC" $MCU -T STM32H723ZGTX_FLASH.ld --specs=nano.specs -Wl,--gc-sections -Wl,-Map=bld/core0_h723.map bld/Src/main.o bld/Startup/startup.o -o bld/core0_h723.elf 2>&1 | grep -v "redeclaration of memory"

echo "=== [3/3] SIZE ==="
"$SIZE" bld/core0_h723.elf

echo
ls -la bld/core0_h723.elf
echo "=== Done: bld/core0_h723.elf ==="
