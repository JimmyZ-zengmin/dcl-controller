#!/bin/bash
# v3_l0 Layer 0 firmware builder (Win git-bash)
# Call: bash D:/STM/work/v3_l0/build.sh
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

ST="/c/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924/tools/bin"
GCC="$ST/arm-none-eabi-gcc.exe"
SIZE="$ST/arm-none-eabi-size.exe"
OBJCPY="$ST/arm-none-eabi-objdump.exe"
BINCPY="$ST/arm-none-eabi-objcopy.exe"

HERE="/d/STM/work/v3_l0/firmware"
cd "$HERE"

MCU="-mcpu=cortex-m7 -mfpu=fpv5-d16 -mfloat-abi=hard -mthumb"
CFLAGS="$MCU -std=gnu11 -g3 -O2 -ffunction-sections -fdata-sections -Wall --specs=nano.specs -DSTM32 -DSTM32H723ZGTx -DDEBUG -IInc"

echo "=== [1/4] AS startup_stm32h723zgtx.s ==="
"$GCC" $MCU -x assembler-with-cpp -c Startup/startup_stm32h723zgtx.s -o build/Startup/startup.o

echo "=== [2/4] CC main.c ==="
"$GCC" $CFLAGS -c Src/main.c -o build/Src/main.o

echo "=== [3/4] LD v3_l0.elf ==="
"$GCC" $MCU -T STM32H723ZGTX_FLASH.ld --specs=nano.specs -Wl,--gc-sections -Wl,-Map=build/v3_l0.map build/Src/main.o build/Startup/startup.o -o build/v3_l0.elf 2>&1 | grep -v "redeclaration of memory"

echo "=== [4/4] BIN ==="
"$BINCPY" -O binary build/v3_l0.elf build/v3_l0.bin

echo "=== SIZE ==="
"$SIZE" build/v3_l0.elf
echo
ls -la build/v3_l0.bin build/v3_l0.elf

echo "=== VECTOR TABLE check ==="
OBJCPY_WIN=$(cygpath -w "$BINCPY" 2>/dev/null)
ELF_WIN=$(cygpath -w "$HERE/build/v3_l0.elf" 2>/dev/null)
cmd //c "${OBJCPY_WIN} -s -j .isr_vector ${ELF_WIN} 2>&1" | head -8

echo
echo "=== Done: build/v3_l0.bin ==="
