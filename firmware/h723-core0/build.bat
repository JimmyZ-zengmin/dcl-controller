@echo off
setlocal

set "ST=C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin"
set "GCC=%ST%\arm-none-eabi-gcc.exe"
set "MCU=-mcpu=cortex-m7 -mfpu=fpv5-d16 -mfloat-abi=hard -mthumb"
set "CFLAGS=%MCU% -std=gnu11 -g3 -O2 -ffunction-sections -fdata-sections -Wall --specs=nano.specs -DSTM32 -DSTM32H723ZGTx -DDEBUG -IInc"

if not exist build\Startup mkdir build\Startup
if not exist build\Src mkdir build\Src

echo [1/3] Compiling startup...
"%GCC%" %MCU% -x assembler-with-cpp -c Startup\startup_stm32h723zgtx.s -o build\Startup\startup.o
if errorlevel 1 goto :fail

echo [2/3] Compiling main.c...
"%GCC%" %CFLAGS% -c Src\main.c -o build\Src\main.o
if errorlevel 1 goto :fail

echo [3/3] Linking...
"%GCC%" %MCU% -T STM32H723ZGTX_FLASH.ld --specs=nano.specs -Wl,--gc-sections build\Src\main.o build\Startup\startup.o -o build\core0_h723.elf
if errorlevel 1 goto :fail

echo.
echo BUILD OK: build\core0_h723.elf
goto :eof

:fail
echo.
echo BUILD FAILED
exit /b 1
